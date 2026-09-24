"""Unit tests for the sidecar's `tickets` counters (ticket 0fb857ef).

Contract agreed with red-monitor (monitor-5, 2026-09-23): a top-level `tickets`
key in the /metrics payload, `{generated_at, categories, projects}`. Brain owns
the category semantics -- see repositories/pg_ticket.py::count_grouped_by_project,
which reuses list_grouped's own _ACTIONABLE/_CONFIRMABLE predicates. This module
only pins the SHAPING (categories are fixed brain-owned labels; projects mirror
the aggregate rows in the order they arrive; nothing pending is an empty list,
never an absent key) and the degrade-to-cache-failure contract with
slow_block_cache.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.slow_block_cache import SlowBlockCache


def _make_collector(rows: list[dict]) -> MetricsCollector:
    collector = MetricsCollector.__new__(MetricsCollector)
    collector._session_factory = MagicMock()
    mock_session = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    mock_session.execute = AsyncMock(return_value=result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    collector._session_factory.return_value = mock_session
    return collector


class TestCollectTicketCounts:
    @pytest.mark.asyncio
    async def test_categories_are_fixed_brain_owned_labels(self) -> None:
        collector = _make_collector([])

        result = await collector.collect_ticket_counts()

        assert result["categories"] == [
            {"key": "todo", "label": "à traiter"},
            {"key": "to_confirm", "label": "à confirmer"},
            {"key": "waiting", "label": "en attente"},
        ]

    @pytest.mark.asyncio
    async def test_projects_shaped_from_aggregate_rows_in_given_order(self) -> None:
        rows = [
            {"project": "brain-v42", "todo": 53, "to_confirm": 2, "waiting": 9},
            {"project": "auto-discord", "todo": 21, "to_confirm": 1, "waiting": 16},
        ]
        collector = _make_collector(rows)

        result = await collector.collect_ticket_counts()

        assert result["projects"] == [
            {"project": "brain-v42", "counts": {"todo": 53, "to_confirm": 2, "waiting": 9}},
            {"project": "auto-discord", "counts": {"todo": 21, "to_confirm": 1, "waiting": 16}},
        ]

    @pytest.mark.asyncio
    async def test_nothing_pending_anywhere_is_an_empty_list_not_absent(self) -> None:
        collector = _make_collector([])

        result = await collector.collect_ticket_counts()

        assert result["projects"] == []

    @pytest.mark.asyncio
    async def test_a_db_failure_propagates_instead_of_degrading_internally(self) -> None:
        """SlowBlockCache (slow_block_cache.py) degrades this block to None on
        failure, with its own short error_ttl_seconds retry window --
        collect_ticket_counts must not swallow the exception itself, or the
        cache would treat a transient DB error as a full-TTL success (the
        cache's own docstring names this exact upcoming block).
        """
        collector = MetricsCollector.__new__(MetricsCollector)
        collector._session_factory = MagicMock()
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        collector._session_factory.return_value = mock_session

        with pytest.raises(RuntimeError, match="db down"):
            await collector.collect_ticket_counts()

    @pytest.mark.asyncio
    async def test_transient_sql_failure_recovers_after_the_short_error_ttl(self) -> None:
        """Reviewer's reproduction (PR #201, MAJOR): a failure injected at
        `session.execute` (not a mocked collector that raises) must be retried
        by SlowBlockCache after `error_ttl_seconds`, never held for the full
        `ttl_seconds`. collect_ticket_counts already propagates its own
        exceptions (the established pattern the other three cached blocks now
        follow via CollectorDegraded), so a plain exception is enough here --
        this pins the cache-side half of the contract with the REAL collector
        wired through a REAL SlowBlockCache with an injectable clock."""
        rows = [{"project": "brain-v42", "todo": 1, "to_confirm": 0, "waiting": 0}]
        ok_result = MagicMock()
        ok_result.mappings.return_value.all.return_value = rows

        collector = MetricsCollector.__new__(MetricsCollector)
        collector._session_factory = MagicMock()
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=[Exception("boom"), ok_result])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        collector._session_factory.return_value = mock_session

        time_box = [0.0]
        cache = SlowBlockCache(
            ttl_seconds=30.0,
            error_ttl_seconds=5.0,
            clock=lambda: time_box[0],
            wall_clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        first = await cache.get("tickets", collector.collect_ticket_counts)
        assert first is None
        assert mock_session.execute.call_count == 1

        # Still inside the error TTL: no retry yet.
        time_box[0] = 4.9
        still_degraded = await cache.get("tickets", collector.collect_ticket_counts)
        assert still_degraded is None
        assert mock_session.execute.call_count == 1

        # Past the error TTL (5s, not the 30s success TTL): recomputes and recovers.
        time_box[0] = 5.1
        recovered = await cache.get("tickets", collector.collect_ticket_counts)
        assert recovered is not None
        assert recovered["projects"] == [
            {"project": "brain-v42", "counts": {"todo": 1, "to_confirm": 0, "waiting": 0}}
        ]
        assert mock_session.execute.call_count == 2
