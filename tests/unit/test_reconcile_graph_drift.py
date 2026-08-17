"""Unit tests for scripts.dream.reconcile_graph_drift."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from scripts.dream import reconcile_graph_drift


class TestDiffSides:
    def test_symmetric_difference(self) -> None:
        a = uuid4()
        b = uuid4()
        c = uuid4()
        pg = {a, b}
        graph = {b, c}
        missing_in_graph, missing_in_pg = reconcile_graph_drift.diff_sides(pg, graph)
        assert missing_in_graph == {a}
        assert missing_in_pg == {c}

    def test_no_drift_when_equal(self) -> None:
        shared = {uuid4(), uuid4()}
        assert reconcile_graph_drift.diff_sides(shared, shared) == (set(), set())

    def test_empty_graph_everything_missing_in_graph(self) -> None:
        pg = {uuid4(), uuid4(), uuid4()}
        missing_in_graph, missing_in_pg = reconcile_graph_drift.diff_sides(pg, set())
        assert missing_in_graph == pg
        assert missing_in_pg == set()

    def test_empty_pg_everything_missing_in_pg(self) -> None:
        graph = {uuid4(), uuid4()}
        missing_in_graph, missing_in_pg = reconcile_graph_drift.diff_sides(set(), graph)
        assert missing_in_graph == set()
        assert missing_in_pg == graph


class TestReconcileType:
    @pytest.fixture
    def graph(self) -> MagicMock:
        svc = MagicMock()
        svc._run_read = AsyncMock(return_value=[])
        svc.upsert_node = AsyncMock(return_value=None)
        svc.delete_node = AsyncMock(return_value=None)
        return svc

    @pytest.fixture
    def session_factory(self) -> MagicMock:
        """Session factory whose ``execute`` returns no rows by default."""
        result = MagicMock()
        result.all = MagicMock(return_value=[])
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=ctx), session, result

    @pytest.mark.asyncio
    async def test_dry_run_reports_drift_without_calling_mutations(
        self, graph: MagicMock, session_factory
    ) -> None:
        factory, _, pg_result = session_factory
        pg_id = uuid4()
        graph_id = uuid4()
        pg_row = MagicMock()
        pg_row.id = str(pg_id)
        pg_row.title = "Test ADR"
        pg_row.project_key = "brain-v42"
        pg_result.all = MagicMock(return_value=[pg_row])
        graph._run_read = AsyncMock(return_value=[{"id": str(graph_id)}])

        summary = await reconcile_graph_drift.reconcile_type(
            "adrs", "ADR", factory, graph, fix=False, limit=100
        )

        assert summary["missing_in_graph"] == 1
        assert summary["missing_in_pg"] == 1
        assert summary["repaired_add"] == 0
        assert summary["repaired_del"] == 0
        graph.upsert_node.assert_not_called()
        graph.delete_node.assert_not_called()

    @pytest.mark.asyncio
    async def test_fix_upserts_missing_in_graph(self, graph: MagicMock, session_factory) -> None:
        factory, _, pg_result = session_factory
        pg_id = uuid4()
        pg_row = MagicMock()
        pg_row.id = str(pg_id)
        pg_row.title = "New ADR"
        pg_row.project_key = "proj"
        pg_result.all = MagicMock(return_value=[pg_row])
        graph._run_read = AsyncMock(return_value=[])

        summary = await reconcile_graph_drift.reconcile_type(
            "adrs", "ADR", factory, graph, fix=True, limit=100
        )

        assert summary["repaired_add"] == 1
        graph.upsert_node.assert_awaited_once()
        call_args = graph.upsert_node.await_args
        assert call_args.args[0] == "ADR"
        assert call_args.args[1] == pg_id
        assert call_args.args[2]["title"] == "New ADR"
        assert call_args.args[2]["project_key"] == "proj"

    @pytest.mark.asyncio
    async def test_fix_deletes_orphans_in_graph(self, graph: MagicMock, session_factory) -> None:
        factory, _, pg_result = session_factory
        orphan_id = uuid4()
        pg_result.all = MagicMock(return_value=[])
        graph._run_read = AsyncMock(return_value=[{"id": str(orphan_id)}])

        summary = await reconcile_graph_drift.reconcile_type(
            "adrs", "ADR", factory, graph, fix=True, limit=100
        )

        assert summary["repaired_del"] == 1
        graph.delete_node.assert_awaited_once_with("ADR", orphan_id)

    @pytest.mark.asyncio
    async def test_fix_respects_limit(self, graph: MagicMock, session_factory) -> None:
        factory, _, pg_result = session_factory
        pg_rows = []
        for _ in range(5):
            row = MagicMock()
            row.id = str(uuid4())
            row.title = "t"
            row.project_key = None
            pg_rows.append(row)
        pg_result.all = MagicMock(return_value=pg_rows)
        graph._run_read = AsyncMock(return_value=[])

        summary = await reconcile_graph_drift.reconcile_type(
            "adrs", "ADR", factory, graph, fix=True, limit=2
        )

        assert summary["missing_in_graph"] == 5
        assert summary["repaired_add"] == 2
        assert graph.upsert_node.await_count == 2

    @pytest.mark.asyncio
    async def test_learning_uses_topic_column(self, graph: MagicMock, session_factory) -> None:
        factory, session, pg_result = session_factory
        pg_result.all = MagicMock(return_value=[])
        graph._run_read = AsyncMock(return_value=[])

        await reconcile_graph_drift.reconcile_type(
            "learnings", "Learning", factory, graph, fix=False, limit=100
        )

        # The SQL query must reference ``topic`` (learnings' title-equivalent),
        # not ``title`` which doesn't exist on that table.
        call_args = session.execute.await_args
        sql_text = str(call_args.args[0])
        assert "topic" in sql_text
        assert "FROM learnings" in sql_text

    @pytest.mark.asyncio
    async def test_skips_invalid_uuid_in_graph_result(
        self, graph: MagicMock, session_factory
    ) -> None:
        factory, _, pg_result = session_factory
        pg_result.all = MagicMock(return_value=[])
        graph._run_read = AsyncMock(return_value=[{"id": "not-a-uuid"}, {"id": str(uuid4())}])

        summary = await reconcile_graph_drift.reconcile_type(
            "adrs", "ADR", factory, graph, fix=False, limit=100
        )
        # Invalid UUID filtered out; only the valid one remains as "missing_in_pg".
        assert summary["graph_count"] == 1
        assert summary["missing_in_pg"] == 1


@pytest.mark.asyncio
async def test_main_refuses_raw_fix_when_canonical_ledger_is_enabled(monkeypatch) -> None:
    engine_factory = MagicMock()
    monkeypatch.setattr(
        reconcile_graph_drift,
        "Settings",
        lambda: SimpleNamespace(graph_ledger_write_enabled=True),
    )
    monkeypatch.setattr(reconcile_graph_drift, "create_async_engine", engine_factory)

    result = await reconcile_graph_drift.main(["--fix"])

    assert result == 2
    engine_factory.assert_not_called()
