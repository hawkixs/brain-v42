"""Unit tests for MetricsCollector.collect_dream_promotions (T13 of Dream v3 plan)

The sidecar queries `SELECT target_type, count(*) FROM dream_promotions GROUP BY 1`
every flush tick and exports the per-target-type counts as a labelled
Prometheus counter (brain_dream_promotions_total{target_type=...}).

This is a pull-based counter: the sidecar re-reads absolute counts on
each flush; Prometheus computes increments via rate()/increase() on the
scraped series. That's why DB-trigger instrumentation would be overkill —
we already have durable audit rows, querying them is reliable enough.

"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector


def _make_collector_with_promotion_rows(rows: list) -> MetricsCollector:
    """Build a MetricsCollector with a mocked session returning grouped rows."""
    collector = MetricsCollector.__new__(MetricsCollector)
    collector._session_factory = MagicMock()

    mock_session = AsyncMock()
    result = MagicMock()
    result.all.return_value = rows
    mock_session.execute = AsyncMock(return_value=result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    collector._session_factory.return_value = mock_session
    return collector


class TestCollectDreamPromotions:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_rows(self) -> None:
        """Empty dream_promotions table → empty dict."""
        collector = _make_collector_with_promotion_rows([])
        assert await collector.collect_dream_promotions() == {}

    @pytest.mark.asyncio
    async def test_returns_count_per_target_type(self) -> None:
        """Counts are returned per target_type as a flat dict."""
        rows = [("adr", 3), ("runbook", 1), ("skipped_dedup", 7)]
        collector = _make_collector_with_promotion_rows(rows)

        result = await collector.collect_dream_promotions()

        assert result == {"adr": 3, "runbook": 1, "skipped_dedup": 7}

    @pytest.mark.asyncio
    async def test_handles_db_failure_gracefully(self) -> None:
        """A DB error returns an empty dict (metrics never crash the sidecar)."""
        collector = MetricsCollector.__new__(MetricsCollector)
        collector._session_factory = MagicMock()

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=Exception("db down"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        collector._session_factory.return_value = mock_session

        assert await collector.collect_dream_promotions() == {}
