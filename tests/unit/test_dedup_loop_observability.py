"""Unit tests for _dedup_loop observability — MAJOR 2 (loop side).

merge_features now returns bool (True = merged, False = skipped).  The
_dedup_loop consumer in brain_v42.metrics.__main__ must:

1. Log 'dedup_loop.merged' ONLY when merge_features returned True — the old
   code logged 'merged' unconditionally, lying about skipped pairs.
2. Log a skip event ('dedup_loop.skipped_missing') when merge_features
   returned False.
3. Track consumed source IDs within a run and skip pairs touching a consumed
   ID WITHOUT opening a session/txn (log 'dedup_loop.skipped_consumed') —
   the old code wasted one FOR UPDATE txn per consumed pair.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.automation.dedup import run_dedup_loop as _dedup_loop

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_candidate_row(name: str, feature_id: uuid.UUID | None = None) -> MagicMock:
    """Mock feature row with .id and .name (name= is reserved in MagicMock)."""
    row = MagicMock()
    row.id = feature_id or uuid.uuid4()
    row.name = name
    return row


def _make_session_factory() -> tuple[MagicMock, AsyncMock]:
    """Session factory mock: factory() -> async ctx manager -> session."""
    session = AsyncMock()
    keys_result = MagicMock()
    keys_result.fetchall.return_value = [("proj1",)]
    session.execute = AsyncMock(return_value=keys_result)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


async def _run_one_iteration(dedup_job: MagicMock, factory: MagicMock) -> MagicMock:
    """Run exactly one _dedup_loop iteration, capturing the module logger.

    asyncio.sleep is patched: first call passes (interval elapsed), second call
    raises CancelledError to terminate the infinite loop after one iteration.
    """
    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("brain_v42.automation.dedup.asyncio.sleep", sleep_mock),
        patch("brain_v42.automation.dedup.logger") as mock_logger,
    ):
        with pytest.raises(asyncio.CancelledError):
            await _dedup_loop(dedup_job, factory, interval=0.0)
    return mock_logger


def _log_events(mock_logger: MagicMock, event: str) -> list:
    return [c for c in mock_logger.info.call_args_list if c[0][0] == event]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestDedupLoopLogging:
    @pytest.mark.asyncio
    async def test_merged_logged_only_when_merge_returned_true(self) -> None:
        """Old behavior logged 'dedup_loop.merged' for every pair, even ones
        merge_features actually skipped.  With merge #2 returning False, the
        loop must log exactly ONE 'merged' and ONE 'skipped_missing'.
        """
        a = _make_candidate_row("A")
        b = _make_candidate_row("B")
        c = _make_candidate_row("C")

        dedup_job = MagicMock()
        dedup_job.find_candidates = AsyncMock(return_value=[(a, b, 0.9), (a, c, 0.85)])
        # First merge real, second skipped (e.g. row vanished under FOR UPDATE)
        dedup_job.merge_features = AsyncMock(side_effect=[True, False])

        factory, _session = _make_session_factory()
        mock_logger = await _run_one_iteration(dedup_job, factory)

        merged = _log_events(mock_logger, "dedup_loop.merged")
        skipped = _log_events(mock_logger, "dedup_loop.skipped_missing")
        assert len(merged) == 1, (
            f"expected exactly 1 'dedup_loop.merged' log, got {len(merged)} — "
            "the loop must not log 'merged' for pairs merge_features skipped"
        )
        assert len(skipped) == 1, (
            f"expected exactly 1 'dedup_loop.skipped_missing' log, got {len(skipped)}"
        )

    @pytest.mark.asyncio
    async def test_consumed_pair_skipped_without_txn(self) -> None:
        """After A absorbs B (merge returns True), a later pair (B, D) touches
        the consumed ID B: the loop must skip it WITHOUT calling merge_features
        and WITHOUT opening a new session, logging 'dedup_loop.skipped_consumed'.
        """
        a = _make_candidate_row("A")
        b = _make_candidate_row("B")
        d = _make_candidate_row("D")

        dedup_job = MagicMock()
        dedup_job.find_candidates = AsyncMock(return_value=[(a, b, 0.9), (b, d, 0.8)])
        dedup_job.merge_features = AsyncMock(return_value=True)

        factory, _session = _make_session_factory()
        mock_logger = await _run_one_iteration(dedup_job, factory)

        # merge_features called exactly once — pair (B, D) never reached it
        assert dedup_job.merge_features.await_count == 1, (
            f"merge_features awaited {dedup_job.merge_features.await_count} times — "
            "pairs touching a consumed ID must be skipped without a merge attempt"
        )
        # Sessions opened: 1 (project keys) + 1 (merge A<-B) = 2.
        # Old code opened a 3rd wasted FOR UPDATE txn for the consumed pair.
        assert factory.call_count == 2, (
            f"session_factory called {factory.call_count} times — expected 2 "
            "(keys query + one merge txn); consumed pairs must not open a txn"
        )
        consumed = _log_events(mock_logger, "dedup_loop.skipped_consumed")
        assert len(consumed) == 1, (
            f"expected exactly 1 'dedup_loop.skipped_consumed' log, got {len(consumed)}"
        )
        merged = _log_events(mock_logger, "dedup_loop.merged")
        assert len(merged) == 1

    @pytest.mark.asyncio
    async def test_all_merged_logs_merged_for_each_pair(self) -> None:
        """Sanity: independent pairs that all merge log 'merged' for each."""
        a = _make_candidate_row("A")
        b = _make_candidate_row("B")
        c = _make_candidate_row("C")
        d = _make_candidate_row("D")

        dedup_job = MagicMock()
        dedup_job.find_candidates = AsyncMock(return_value=[(a, b, 0.9), (c, d, 0.88)])
        dedup_job.merge_features = AsyncMock(return_value=True)

        factory, _session = _make_session_factory()
        mock_logger = await _run_one_iteration(dedup_job, factory)

        assert len(_log_events(mock_logger, "dedup_loop.merged")) == 2
        assert _log_events(mock_logger, "dedup_loop.skipped_missing") == []
        assert _log_events(mock_logger, "dedup_loop.skipped_consumed") == []
