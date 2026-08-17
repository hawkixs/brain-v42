"""Unit tests for MetricsCollector.collect_dream_promotions (T13 of Dream v3 plan)
and MetricsCollector.collect_dream_promoted_health (ADR #4 v2 telemetry).

The sidecar queries `SELECT target_type, count(*) FROM dream_promotions GROUP BY 1`
every flush tick and exports the per-target-type counts as a labelled
Prometheus counter (brain_dream_promotions_total{target_type=...}).

This is a pull-based counter: the sidecar re-reads absolute counts on
each flush; Prometheus computes increments via rate()/increase() on the
scraped series. That's why DB-trigger instrumentation would be overkill —
we already have durable audit rows, querying them is reliable enough.

ADR #4 v2 telemetry (collect_dream_promoted_health): per-target post-promotion
health signals — access_count, days_since_promotion, supersession status —
joined from dream_promotions to live ADR/runbook rows. Surfaced via the
JSON `/dream` endpoint, not Prometheus labels (per-target cardinality risk).
"""

from __future__ import annotations

import datetime as dt
import uuid
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


class TestCollectDreamPromotedHealth:
    """ADR #4 v2 telemetry — per-target post-promotion health signals."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_materialized_promotions(self) -> None:
        """Empty join → empty list."""
        collector = _make_collector_with_promotion_rows([])
        assert await collector.collect_dream_promoted_health() == []

    @pytest.mark.asyncio
    async def test_surfaces_adr_health_signals(self) -> None:
        """ADR row exposes status, supersession, access counts, days deltas."""
        adr_id = uuid.uuid4()
        promoted_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
        last_accessed_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
        rows = [
            (
                "adr",
                adr_id,
                "ADR title",
                "accepted",
                False,
                7,
                last_accessed_at,
                promoted_at,
            )
        ]
        collector = _make_collector_with_promotion_rows(rows)

        result = await collector.collect_dream_promoted_health()

        assert len(result) == 1
        entry = result[0]
        assert entry["target_type"] == "adr"
        assert entry["target_id"] == str(adr_id)
        assert entry["title"] == "ADR title"
        assert entry["status"] == "accepted"
        assert entry["superseded"] is False
        assert entry["access_count"] == 7
        assert 2.9 < entry["days_since_promotion"] < 3.1
        assert entry["days_since_last_access"] is not None
        assert 0.9 < entry["days_since_last_access"] < 1.1
        assert entry["promoted_at"] == promoted_at.isoformat()

    @pytest.mark.asyncio
    async def test_flags_superseded_adr(self) -> None:
        """Superseded ADRs surface superseded=True (quality signal)."""
        rows = [
            (
                "adr",
                uuid.uuid4(),
                "Old ADR",
                "superseded",
                True,
                2,
                None,
                dt.datetime.now(dt.UTC) - dt.timedelta(days=10),
            )
        ]
        collector = _make_collector_with_promotion_rows(rows)

        result = await collector.collect_dream_promoted_health()

        assert result[0]["superseded"] is True
        assert result[0]["status"] == "superseded"

    @pytest.mark.asyncio
    async def test_handles_runbook_row_with_null_status(self) -> None:
        """Runbook rows have no status column — None is preserved."""
        rb_id = uuid.uuid4()
        rows = [
            (
                "runbook",
                rb_id,
                "Recovery RB",
                None,
                False,
                3,
                None,
                dt.datetime.now(dt.UTC) - dt.timedelta(days=5),
            )
        ]
        collector = _make_collector_with_promotion_rows(rows)

        result = await collector.collect_dream_promoted_health()

        assert result[0]["target_type"] == "runbook"
        assert result[0]["status"] is None
        assert result[0]["days_since_last_access"] is None

    @pytest.mark.asyncio
    async def test_handles_db_failure_gracefully(self) -> None:
        """DB errors return [] (sidecar never crashes)."""
        collector = MetricsCollector.__new__(MetricsCollector)
        collector._session_factory = MagicMock()
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=Exception("db down"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        collector._session_factory.return_value = mock_session

        assert await collector.collect_dream_promoted_health() == []
