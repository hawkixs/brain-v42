"""Contracts for the bounded, secret-free legacy Neo4j snapshot."""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.legacy_graph_snapshot import LegacyGraphSnapshotReader


def _async_result(rows: list[dict[str, Any]]) -> Any:
    result = MagicMock()

    async def iterate():
        for row in rows:
            yield row

    result.__aiter__ = lambda _self: iterate()
    return result


def _driver_with_rows(node_rows: list[dict[str, Any]], relation_rows: list[dict[str, Any]]):
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock()
    session = AsyncMock()
    transaction = AsyncMock()
    transaction.run = AsyncMock(
        side_effect=[_async_result(node_rows), _async_result(relation_rows)]
    )

    async def execute_read(callback):
        return await callback(transaction)

    session.execute_read = AsyncMock(side_effect=execute_read)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    driver.session.return_value = context
    return driver, session, transaction


@pytest.mark.asyncio
async def test_reader_is_strictly_bounded_and_sanitizes_relation_payload() -> None:
    driver, session, transaction = _driver_with_rows(
        [
            {
                "identity": "00000000-0000-0000-0000-000000000001",
                "labels": ["Decision"],
                "label": "D" * 300,
                "project_key": "brain-v42",
            },
            {
                "identity": "brain-v42",
                "labels": ["Project"],
                "label": "Brain v42",
                "project_key": "brain-v42",
            },
        ],
        [
            {
                "source_identity": "00000000-0000-0000-0000-000000000001",
                "source_labels": ["Decision"],
                "target_identity": "brain-v42",
                "target_labels": ["Project"],
                "type": "BELONGS_TO",
                "similarity": 0.75,
                "score": math.inf,
                "model": "m" * 300,
                "secret": "must-not-cross-the-boundary",
            },
            {
                "source_identity": "brain-v42",
                "source_labels": ["Project"],
                "target_identity": "00000000-0000-0000-0000-000000000001",
                "target_labels": ["Decision"],
                "type": "CONTAINS",
            },
        ],
    )
    reader = LegacyGraphSnapshotReader(driver, timeout=0.2)

    snapshot = await reader.read(max_nodes=1, max_relations=1)

    assert snapshot.status == "ok"
    assert snapshot.truncated_nodes is True
    assert snapshot.truncated_relations is True
    assert len(snapshot.nodes) == len(snapshot.relations) == 1
    assert len(snapshot.nodes[0].display_label or "") == 180
    assert snapshot.relations[0].properties == {
        "similarity": 0.75,
        "model": "m" * 128,
    }

    session.execute_read.assert_awaited_once()
    session.run.assert_not_awaited()
    node_query, node_params = transaction.run.await_args_list[0].args
    relation_query, relation_params = transaction.run.await_args_list[1].args
    transaction_callback = session.execute_read.await_args.args[0]
    assert transaction_callback.timeout == 0.2
    assert isinstance(node_query, str)
    assert isinstance(relation_query, str)
    assert node_params == {"limit": 2}
    assert relation_params == {"limit": 2}
    assert "properties(relation)" not in relation_query
    assert "relation.secret" not in relation_query
    assert "type(relation) IN" not in relation_query
    assert "WHERE identity IS NOT NULL" not in node_query
    assert "substring(toString(identity), 0, 129)" in node_query
    assert "substring(toString(source_identity), 0, 129)" in relation_query
    assert "substring(toString(coalesce" in node_query
    assert "substring" in relation_query
    assert "ORDER BY" not in node_query
    assert "ORDER BY" not in relation_query


@pytest.mark.asyncio
async def test_reader_skips_invalid_nodes_relations_and_self_loops() -> None:
    driver, _, _ = _driver_with_rows(
        [
            {"identity": "not-a-uuid", "labels": ["Decision"], "label": "bad"},
            {"identity": "unknown", "labels": ["Unsupported"], "label": "bad"},
            {
                "identity": "p" * 51,
                "labels": ["Project"],
                "label": "oversized project key",
                "project_key": "p" * 51,
            },
            {"identity": "infra", "labels": ["Domain"], "label": "Infra"},
        ],
        [
            {
                "source_identity": "infra",
                "source_labels": ["Domain"],
                "target_identity": "infra",
                "target_labels": ["Domain"],
                "type": "RELATED_TO",
            },
            {
                "source_identity": "infra",
                "source_labels": ["Domain"],
                "target_identity": "brain-v42",
                "target_labels": ["Project"],
                "type": "NOT_CANONICAL",
            },
        ],
    )

    snapshot = await LegacyGraphSnapshotReader(driver).read(10, 10)

    assert [node.entity_type for node in snapshot.nodes] == ["domain"]
    assert snapshot.relations == ()
    assert snapshot.skipped_nodes == 3
    assert snapshot.skipped_relations == 2


@pytest.mark.asyncio
async def test_reader_canonicalizes_known_project_aliases_on_nodes_and_relations() -> None:
    entity_id = "00000000-0000-0000-0000-000000000001"
    driver, _, _ = _driver_with_rows(
        [
            {
                "identity": "brain_v42",
                "labels": ["Project"],
                "label": "Brain alias",
                "project_key": "brain_v42",
            },
            {
                "identity": entity_id,
                "labels": ["Decision"],
                "label": "Decision",
                "project_key": "brain",
            },
        ],
        [
            {
                "source_identity": entity_id,
                "source_labels": ["Decision"],
                "target_identity": "brain_v42",
                "target_labels": ["Project"],
                "type": "BELONGS_TO",
            },
            {
                "source_identity": "brain",
                "source_labels": ["Project"],
                "target_identity": entity_id,
                "target_labels": ["Decision"],
                "type": "RELATED_TO",
            },
        ],
    )

    snapshot = await LegacyGraphSnapshotReader(driver).read(10, 10)

    project, decision = snapshot.nodes
    assert (project.entity_key, project.project_key) == ("brain-v42", "brain-v42")
    assert decision.project_key == "brain-v42"
    assert snapshot.relations[0].target_key == "brain-v42"
    related = snapshot.relations[1]
    assert (related.target_type, related.target_key) == ("project", "brain-v42")


@pytest.mark.asyncio
async def test_reader_reports_unavailable_without_leaking_connection_error() -> None:
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock(side_effect=OSError("bolt://user:secret@host"))

    snapshot = await LegacyGraphSnapshotReader(driver, timeout=0.2).read(10, 10)

    assert snapshot.status == "unavailable"
    assert snapshot.nodes == ()
    assert snapshot.relations == ()


@pytest.mark.asyncio
async def test_reader_rejects_non_positive_limits_before_connecting() -> None:
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock()

    with pytest.raises(ValueError, match="positive"):
        await LegacyGraphSnapshotReader(driver).read(0, 10)

    driver.verify_connectivity.assert_not_awaited()
