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

from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector


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
