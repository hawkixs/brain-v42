"""Bounded source-reader tests for the Brain graph projection."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.brain_graph_projection import (
    Neo4jGraphSnapshotReader,
    PostgresGraphSnapshotReader,
)


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class FakeSession:
    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        table_name = statement.get_final_froms()[0].name
        return FakeResult(self.rows.get(table_name, []))


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self) -> FakeSession:
        return self.session


def _async_result(rows: list[dict[str, Any]]) -> Any:
    result = MagicMock()

    async def iterate():
        for row in rows:
            yield row

    result.__aiter__ = lambda _self: iterate()
    return result


@pytest.mark.asyncio
async def test_postgres_reader_queries_all_visible_tables_with_slim_columns() -> None:
    session = FakeSession({})
    reader = PostgresGraphSnapshotReader(FakeSessionFactory(session))  # type: ignore[arg-type]

    result = await reader.read(100)

    expected_tables = {
        "project_contexts",
        "decisions",
        "learnings",
        "snippets",
        "runbooks",
        "adrs",
        "features",
        "indexed_plans",
        "gitlab_events",
        "dream_runs",
        "dream_promotions",
        "tickets",
        "brain_sessions",
        "ticket_extraction_proposals",
        "roadmap_curation_proposals",
        "feature_artifacts",
        "consolidation_log",
    }
    assert set(result.tables) == expected_tables
    assert {
        statement.get_final_froms()[0].name for statement in session.statements
    } == expected_tables
    forbidden = {
        "embedding",
        "search_vector",
        "body",
        "content",
        "code",
        "file_path",
        "error_message",
        "rationale",
        "summary",
        "local_path",
    }
    for statement in session.statements:
        assert forbidden.isdisjoint(statement.selected_columns.keys())


@pytest.mark.asyncio
async def test_postgres_reader_uses_limit_plus_one_and_truncates_strictly() -> None:
    session = FakeSession(
        {
            "project_contexts": [
                {"project_key": "a"},
                {"project_key": "b"},
                {"project_key": "c"},
            ]
        }
    )
    reader = PostgresGraphSnapshotReader(FakeSessionFactory(session))  # type: ignore[arg-type]

    result = await reader.read(2)

    assert result.truncated is True
    assert [row["project_key"] for row in result.tables["project_contexts"]] == ["a", "b"]
    assert len(session.statements) == 1
    assert session.statements[0]._limit_clause.value == 3


@pytest.mark.asyncio
async def test_postgres_reader_spends_one_global_budget_across_tables() -> None:
    session = FakeSession(
        {
            "project_contexts": [{"project_key": "brain-v42"}],
            "decisions": [{"id": "a"}, {"id": "b"}],
            "learnings": [{"id": "must-not-be-read"}],
        }
    )
    reader = PostgresGraphSnapshotReader(FakeSessionFactory(session))  # type: ignore[arg-type]

    result = await reader.read(2)

    assert result.truncated is True
    assert result.tables["project_contexts"] == [{"project_key": "brain-v42"}]
    assert result.tables["decisions"] == [{"id": "a"}]
    assert result.tables["learnings"] == []
    assert [statement.get_final_froms()[0].name for statement in session.statements] == [
        "project_contexts",
        "decisions",
    ]


@pytest.mark.asyncio
async def test_neo4j_reader_preserves_labels_direction_and_strict_limits() -> None:
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock()
    session = AsyncMock()
    session.run = AsyncMock(
        side_effect=[
            _async_result(
                [
                    {"identity": "a", "labels": ["Decision"], "label": "A"},
                    {"identity": "b", "labels": ["Learning"], "label": "B"},
                ]
            ),
            _async_result(
                [
                    {
                        "source_identity": "a",
                        "source_labels": ["Decision"],
                        "target_identity": "b",
                        "target_labels": ["Learning"],
                        "type": "RELATED_TO",
                        "weight": 1.0,
                    },
                    {
                        "source_identity": "b",
                        "source_labels": ["Learning"],
                        "target_identity": "a",
                        "target_labels": ["Decision"],
                        "type": "USES",
                        "weight": 1.0,
                    },
                ]
            ),
        ]
    )
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    driver.session.return_value = context
    reader = Neo4jGraphSnapshotReader(driver, timeout=0.2)

    result = await reader.read(max_nodes=1, max_edges=1)

    assert result.status == "ok"
    assert len(result.nodes) == len(result.edges) == 1
    assert result.truncated_nodes is result.truncated_edges is True
    node_query, node_params = session.run.call_args_list[0].args
    edge_query, edge_params = session.run.call_args_list[1].args
    assert "labels(n)" in node_query.text
    assert node_params == {"limit": 2}
    assert "1.0 AS weight" in edge_query.text
    assert "similarity_score" not in edge_query.text
    assert "relation['score']" not in edge_query.text
    assert "source_identity" in edge_query.text and "target_identity" in edge_query.text
    assert edge_params["limit"] == 2
    assert "RELATED_TO" in edge_params["relation_types"]


@pytest.mark.asyncio
async def test_neo4j_reader_reports_unavailable_instead_of_false_empty_success() -> None:
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock(side_effect=OSError("down"))
    reader = Neo4jGraphSnapshotReader(driver, timeout=0.2)

    result = await reader.read(max_nodes=10, max_edges=10)

    assert result.status == "unavailable"
    assert result.nodes == [] and result.edges == []


@pytest.mark.asyncio
async def test_postgres_reader_timeout_covers_the_whole_result_load() -> None:
    class HangingSession(FakeSession):
        async def execute(self, statement: Any) -> FakeResult:
            await asyncio.Event().wait()
            return await super().execute(statement)

    reader = PostgresGraphSnapshotReader(  # type: ignore[arg-type]
        FakeSessionFactory(HangingSession({})),
        timeout=0.01,
    )

    result = await asyncio.wait_for(reader.read(10), timeout=0.1)

    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_neo4j_reader_timeout_covers_result_iteration_not_only_connectivity() -> None:
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock()
    session = AsyncMock()
    blocked_result = MagicMock()

    async def blocked_iteration():
        await asyncio.Event().wait()
        yield {}

    blocked_result.__aiter__ = lambda _self: blocked_iteration()
    session.run = AsyncMock(return_value=blocked_result)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    driver.session.return_value = context
    reader = Neo4jGraphSnapshotReader(driver, timeout=0.01)

    result = await asyncio.wait_for(reader.read(max_nodes=10, max_edges=10), timeout=0.1)

    assert result.status == "unavailable"
