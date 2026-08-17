"""Unit tests for PgAccessLogRepo — aggregate + purge."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.repositories.pg_access_log import PgAccessLogRepo


@pytest.fixture
def repo() -> PgAccessLogRepo:
    return PgAccessLogRepo(session_factory=MagicMock())


class TestAggregateAndFlush:
    @pytest.mark.asyncio
    async def test_returns_aggregated_stats(self) -> None:
        """aggregate_and_flush returns dict of entity stats."""
        entity_id_1 = uuid4()
        entity_id_2 = uuid4()
        now = datetime.now(tz=UTC)

        # Mock the session to return aggregated rows
        mock_rows = [
            {
                "entity_type": "decision",
                "entity_id": entity_id_1,
                "max_accessed": now,
                "cnt": 3,
            },
            {
                "entity_type": "learning",
                "entity_id": entity_id_2,
                "max_accessed": now - timedelta(hours=1),
                "cnt": 1,
            },
        ]

        session = AsyncMock()
        # SELECT MAX(id) query
        max_id_result = MagicMock()
        max_id_result.scalar_one_or_none.return_value = 100
        # aggregate query
        agg_result = MagicMock()
        agg_result.mappings.return_value.all.return_value = mock_rows
        # delete query
        del_result = MagicMock()
        session.execute = AsyncMock(side_effect=[max_id_result, agg_result, del_result])

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        repo = PgAccessLogRepo(session_factory=session_factory)
        result = await repo.aggregate_and_flush()

        assert len(result) == 2
        key_1 = ("decision", entity_id_1)
        assert key_1 in result
        assert result[key_1]["max_accessed"] == now
        assert result[key_1]["count"] == 3

    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_no_rows(self) -> None:
        """aggregate_and_flush returns empty dict when no access_log rows."""
        session = AsyncMock()
        # SELECT MAX(id) returns None when table is empty
        max_id_result = MagicMock()
        max_id_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=max_id_result)

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        repo = PgAccessLogRepo(session_factory=session_factory)
        result = await repo.aggregate_and_flush()

        assert result == {}


class TestAggregateSnapshotSafety:
    @pytest.mark.asyncio
    async def test_only_deletes_snapshotted_rows(self) -> None:
        """aggregate_and_flush must only delete rows with id <= max_id at snapshot time."""
        entity_id = uuid4()
        now = datetime.now(tz=UTC)
        max_id = 42

        # Mock results: SELECT MAX(id) -> 42, aggregate query, DELETE
        max_id_result = MagicMock()
        max_id_result.scalar_one_or_none.return_value = max_id

        agg_result = MagicMock()
        agg_result.mappings.return_value.all.return_value = [
            {
                "entity_type": "decision",
                "entity_id": entity_id,
                "max_accessed": now,
                "cnt": 2,
            },
        ]

        del_result = MagicMock()

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[max_id_result, agg_result, del_result])

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        repo = PgAccessLogRepo(session_factory=session_factory)
        result = await repo.aggregate_and_flush()

        assert len(result) == 1

        # The DELETE statement (3rd execute call) must have a WHERE clause
        delete_call = session.execute.call_args_list[2]
        delete_stmt = delete_call[0][0]
        # Compile the statement to check it contains a WHERE clause
        compiled = str(delete_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "WHERE" in compiled, f"DELETE must include WHERE id <= max_id, got: {compiled}"


class TestPurgeOld:
    @pytest.mark.asyncio
    async def test_purge_old_executes_delete(self) -> None:
        """purge_old deletes entries older than N days."""
        session = AsyncMock()
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        repo = PgAccessLogRepo(session_factory=session_factory)
        await repo.purge_old(days=30)

        session.execute.assert_called_once()
        session.commit.assert_called_once()


class TestRowCount:
    @pytest.mark.asyncio
    async def test_row_count_returns_integer(self) -> None:
        """row_count returns the number of rows in access_log."""
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 42
        session.execute = AsyncMock(return_value=count_result)

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        repo = PgAccessLogRepo(session_factory=session_factory)
        result = await repo.row_count()

        assert result == 42
