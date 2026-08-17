"""Unit tests for DecayFlusher — atomic single-transaction flush.

The key invariant: aggregation (aggregate_in_session) + entity updates +
access_log DELETE must all happen inside ONE transaction.  If the process
crashes between entity-update and access_log-delete, counts are NOT lost
(the transaction rolls back and access_log rows remain intact on next boot).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.services.decay import DecayCalculator
from brain_v42.services.decay_flusher import DecayFlusher

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_session(
    *,
    entity_rows: list[dict] | None = None,
) -> AsyncMock:
    """Build a minimal mock session for _flush tests."""
    session = AsyncMock()
    if entity_rows is not None:
        select_result = MagicMock()
        select_result.mappings.return_value.all.return_value = entity_rows
        session.execute = AsyncMock(return_value=select_result)
    return session


def _make_session_factory(session: AsyncMock) -> MagicMock:
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAtomicFlush:
    """aggregate_in_session must be called with the SAME session used for entity updates."""

    @pytest.mark.asyncio
    async def test_aggregate_in_session_called_with_session(self) -> None:
        """DecayFlusher._flush must call repo.aggregate_in_session(session)
        — not aggregate_and_flush() — so the aggregation + delete happens
        inside the same transaction as the entity updates.
        """
        entity_id = uuid4()
        now = datetime.now(tz=UTC)

        # Mock repo: aggregate_in_session returns one entity to process
        access_log_repo = AsyncMock()
        access_log_repo.aggregate_in_session = AsyncMock(
            return_value={("learning", entity_id): {"max_accessed": now, "count": 1}}
        )
        access_log_repo.purge_old = AsyncMock()
        access_log_repo.row_count = AsyncMock(return_value=0)

        session = _make_session(
            entity_rows=[
                {
                    "id": entity_id,
                    "created_at": now - timedelta(days=5),
                    "access_count": 0,
                    "access_count_human": 0,
                    "freshness_status": "fresh",
                    "last_accessed_at": None,
                    "last_accessed_at_human": None,
                    "validated_at": None,
                }
            ]
        )
        session_factory = _make_session_factory(session)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=DecayCalculator(),
        )
        await flusher._flush()

        # New contract: aggregate_in_session must be called (not aggregate_and_flush)
        access_log_repo.aggregate_in_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aggregate_and_flush_not_called(self) -> None:
        """_flush must NOT call the old aggregate_and_flush() method.

        That method opens its own session + commit, breaking atomicity.
        """
        access_log_repo = AsyncMock()
        access_log_repo.aggregate_in_session = AsyncMock(return_value={})
        access_log_repo.aggregate_and_flush = AsyncMock(return_value={})
        access_log_repo.purge_old = AsyncMock()

        session = _make_session()
        session_factory = _make_session_factory(session)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=DecayCalculator(),
        )
        await flusher._flush()

        access_log_repo.aggregate_and_flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_session_opened_for_aggregate_and_update(self) -> None:
        """Only ONE session must be opened for the aggregate+update phase.

        Old code: session1 = aggregate_and_flush's own session (commits)
                  session2 = entity update session
        New code: one session only (opened by _flush).
        """
        entity_id = uuid4()
        now = datetime.now(tz=UTC)

        access_log_repo = AsyncMock()
        access_log_repo.aggregate_in_session = AsyncMock(
            return_value={("snippet", entity_id): {"max_accessed": now, "count": 2}}
        )
        access_log_repo.purge_old = AsyncMock()

        session = _make_session(
            entity_rows=[
                {
                    "id": entity_id,
                    "created_at": now - timedelta(days=1),
                    "access_count": 0,
                    "access_count_human": 0,
                    "freshness_status": "fresh",
                    "last_accessed_at": None,
                    "last_accessed_at_human": None,
                }
            ]
        )
        session_factory = _make_session_factory(session)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=DecayCalculator(),
        )
        await flusher._flush()

        # session_factory called once for aggregate+update block
        # (purge_old has its own session separately — that's fine)
        flush_calls = session_factory.call_count
        # At most 2 calls: one for aggregate+update, one for _count_freshness_totals (if collector)
        # Without collector, must be exactly 1.
        assert flush_calls == 1, f"Expected 1 session open for aggregate+update, got {flush_calls}"

    @pytest.mark.asyncio
    async def test_session_commit_called_after_entity_updates(self) -> None:
        """The session must be committed once after both aggregation and entity updates."""
        entity_id = uuid4()
        now = datetime.now(tz=UTC)

        access_log_repo = AsyncMock()
        access_log_repo.aggregate_in_session = AsyncMock(
            return_value={("decision", entity_id): {"max_accessed": now, "count": 1}}
        )
        access_log_repo.purge_old = AsyncMock()

        session = _make_session(
            entity_rows=[
                {
                    "id": entity_id,
                    "created_at": now - timedelta(days=10),
                    "access_count": 0,
                    "access_count_human": 0,
                    "freshness_status": "fresh",
                    "last_accessed_at": None,
                    "last_accessed_at_human": None,
                }
            ]
        )
        session_factory = _make_session_factory(session)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=DecayCalculator(),
        )
        await flusher._flush()

        # Exactly one commit in the aggregate+update session
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_noop_when_aggregate_returns_empty(self) -> None:
        """Empty aggregate → no entity updates, session opened but immediately closed."""
        access_log_repo = AsyncMock()
        access_log_repo.aggregate_in_session = AsyncMock(return_value={})
        access_log_repo.purge_old = AsyncMock()

        session = _make_session()
        session_factory = _make_session_factory(session)

        flusher = DecayFlusher(
            session_factory=session_factory,
            access_log_repo=access_log_repo,
            decay_calculator=DecayCalculator(),
        )
        await flusher._flush()

        # aggregate_in_session was called
        access_log_repo.aggregate_in_session.assert_awaited_once()
        # No entity execute calls
        session.execute.assert_not_called()
        # Purge still happens
        access_log_repo.purge_old.assert_awaited_once_with(30)
