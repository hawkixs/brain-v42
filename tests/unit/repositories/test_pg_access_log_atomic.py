"""Unit tests for PgAccessLogRepo — atomic in-session aggregate API.

Verifies the new aggregate_in_session(session) contract that allows the
caller (DecayFlusher) to own the transaction boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.repositories.pg_access_log import PgAccessLogRepo

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_session(
    *,
    max_id: int | None = 100,
    agg_rows: list[dict] | None = None,
) -> AsyncMock:
    """Return a mock AsyncSession configured for aggregate_in_session calls."""
    session = AsyncMock()

    # 1st execute: advisory lock (returns nothing useful)
    lock_result = MagicMock()

    # 2nd execute: SELECT MAX(id) → max_id
    max_id_result = MagicMock()
    max_id_result.scalar_one_or_none.return_value = max_id

    if max_id is None:
        # No rows → only 2 executes expected (lock + max_id)
        session.execute = AsyncMock(side_effect=[lock_result, max_id_result])
        return session

    # 3rd execute: aggregate GROUP BY → rows
    if agg_rows is None:
        agg_rows = []
    agg_result = MagicMock()
    agg_result.mappings.return_value.all.return_value = agg_rows

    # 4th execute: DELETE WHERE id <= max_id
    del_result = MagicMock()

    session.execute = AsyncMock(side_effect=[lock_result, max_id_result, agg_result, del_result])
    return session


# ---------------------------------------------------------------------------
# Tests for aggregate_in_session (new atomic API)
# ---------------------------------------------------------------------------


class TestAggregateInSession:
    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_table_empty(self) -> None:
        """aggregate_in_session returns {} when access_log is empty (max_id=None)."""
        session = _make_session(max_id=None)
        repo = PgAccessLogRepo(session_factory=MagicMock())

        result = await repo.aggregate_in_session(session)

        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_aggregated_stats(self) -> None:
        """aggregate_in_session returns the correct stats dict."""
        entity_id_1 = uuid4()
        entity_id_2 = uuid4()
        now = datetime.now(tz=UTC)

        agg_rows = [
            {
                "entity_type": "decision",
                "entity_id": entity_id_1,
                "actor": "red-lab",
                "max_accessed": now,
                "cnt": 5,
            },
            {
                "entity_type": "learning",
                "entity_id": entity_id_2,
                "actor": "dream-codex-reorg",
                "max_accessed": now,
                "cnt": 2,
            },
        ]
        session = _make_session(max_id=42, agg_rows=agg_rows)
        repo = PgAccessLogRepo(session_factory=MagicMock())

        result = await repo.aggregate_in_session(session)

        assert len(result) == 2
        # `max_accessed_human` — migration 044. Recency now has its human
        # counterpart, as the counter has had since 041: without it, the decay
        # formula's 0.3-weight term stayed driven by machine reads. `None` when
        # no human actor appears in the batch, and that is distinct from "never
        # read" — the flusher then keeps the existing value.
        assert result[("decision", entity_id_1)] == {
            "max_accessed": now,
            "max_accessed_human": now,
            "count": 5,
            "count_human": 5,
        }
        assert result[("learning", entity_id_2)] == {
            "max_accessed": now,
            "max_accessed_human": None,
            "count": 2,
            "count_human": 0,
        }

    @pytest.mark.asyncio
    async def test_acquires_advisory_lock_first(self) -> None:
        """aggregate_in_session must call pg_advisory_xact_lock as the FIRST execute.

        This serializes concurrent flushers so they cannot double-count the
        same access_log rows.  The lock must be released automatically at txn end.
        """
        entity_id = uuid4()
        now = datetime.now(tz=UTC)
        agg_rows = [
            {
                "entity_type": "decision",
                "entity_id": entity_id,
                "actor": "red-lab",
                "max_accessed": now,
                "cnt": 1,
            }
        ]
        session = _make_session(max_id=10, agg_rows=agg_rows)
        repo = PgAccessLogRepo(session_factory=MagicMock())

        await repo.aggregate_in_session(session)

        # First execute call must compile to a pg_advisory_xact_lock invocation
        first_call = session.execute.call_args_list[0]
        stmt = first_call[0][0]
        compiled = str(stmt.compile())
        assert "pg_advisory_xact_lock" in compiled, (
            f"First execute must acquire advisory lock, got: {compiled}"
        )

    @pytest.mark.asyncio
    async def test_does_not_commit(self) -> None:
        """aggregate_in_session must NOT call session.commit() — caller owns txn."""
        session = _make_session(max_id=None)
        repo = PgAccessLogRepo(session_factory=MagicMock())

        await repo.aggregate_in_session(session)

        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_only_snapshotted_rows(self) -> None:
        """aggregate_in_session only deletes rows with id <= snapshot max_id."""
        entity_id = uuid4()
        now = datetime.now(tz=UTC)
        max_id = 77

        agg_rows = [
            {
                "entity_type": "snippet",
                "entity_id": entity_id,
                "actor": "red-lab",
                "max_accessed": now,
                "cnt": 3,
            }
        ]
        session = _make_session(max_id=max_id, agg_rows=agg_rows)
        repo = PgAccessLogRepo(session_factory=MagicMock())

        await repo.aggregate_in_session(session)

        # 4th call = DELETE
        delete_call = session.execute.call_args_list[3]
        delete_stmt = delete_call[0][0]
        compiled = str(delete_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "WHERE" in compiled, f"DELETE must have WHERE clause, got: {compiled}"
        assert str(max_id) in compiled, (
            f"DELETE WHERE must reference max_id={max_id}, got: {compiled}"
        )

    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_no_agg_rows(self) -> None:
        """Returns {} (and skips DELETE) when max_id exists but aggregate is empty."""
        session = _make_session(max_id=5, agg_rows=[])
        repo = PgAccessLogRepo(session_factory=MagicMock())

        result = await repo.aggregate_in_session(session)

        # No rows → empty result AND no delete should be issued
        assert result == {}
        # Only 3 executes: lock + max_id + aggregate (no delete)
        assert session.execute.call_count == 3


class TestAdvisoryLockConstant:
    """The advisory lock key must be a documented, stable constant."""

    def test_advisory_lock_key_exported(self) -> None:
        """DECAY_FLUSH_ADVISORY_LOCK constant must be exported from the module."""
        from brain_v42.repositories.pg_access_log import DECAY_FLUSH_ADVISORY_LOCK

        assert isinstance(DECAY_FLUSH_ADVISORY_LOCK, int)
        assert DECAY_FLUSH_ADVISORY_LOCK > 0
