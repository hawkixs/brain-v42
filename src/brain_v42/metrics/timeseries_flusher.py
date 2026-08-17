"""TimeseriesFlusher — writes bucketed rps/p95/err_rate/cost to metrics_timeseries.

30-minute buckets for rps / p95 / err_rate, 1-hour buckets for cost.
Retention: 7 days (48 × 30min + 24 × 1h ≈ 500 rows total).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.metrics.collector import MetricsCollector

logger = structlog.get_logger(__name__)

_30MIN = 1800
_1H = 3600


def _bucket_floor(ts: datetime, bucket_s: int) -> datetime:
    total = int(ts.timestamp())
    floored = (total // bucket_s) * bucket_s
    return datetime.fromtimestamp(floored, tz=UTC)


class TimeseriesFlusher:
    """Periodic flusher for bucketed cockpit histories."""

    def __init__(
        self,
        collector: MetricsCollector,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._collector = collector
        self._session_factory = session_factory
        self._task: asyncio.Task[None] | None = None
        self._last_cost_bucket: datetime | None = None
        self._last_retention: datetime | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        logger.info("timeseries_flusher.started")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("timeseries_flusher.stopped")

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(_30MIN)
                await self._flush_30min_metrics()
                now = datetime.now(UTC)
                if self._last_cost_bucket is None or (now - self._last_cost_bucket) >= timedelta(
                    seconds=_1H
                ):
                    await self._flush_cost_bucket()
                    self._last_cost_bucket = now
                if self._last_retention is None or (now - self._last_retention) >= timedelta(
                    days=1
                ):
                    await self._retention_sweep()
                    self._last_retention = now
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("timeseries_flusher.error")

    async def _flush_30min_metrics(self) -> None:
        rates = self._collector.compute_rates(window_s=1800.0)
        pct = self._collector.tool_percentiles(window_s=1800.0)
        bucket = _bucket_floor(datetime.now(UTC), _30MIN)
        async with self._session_factory() as session:
            for metric, value in (
                ("rps", rates["rps"]),
                ("p95", pct["p95"]),
                ("err_rate", rates["err_rate"]),
            ):
                await session.execute(
                    text(
                        "INSERT INTO metrics_timeseries (bucket_ts, metric, value) "
                        "VALUES (:bucket_ts, :metric, :value) "
                        "ON CONFLICT (bucket_ts, metric) DO UPDATE SET value = EXCLUDED.value"
                    ),
                    {"bucket_ts": bucket, "metric": metric, "value": float(value)},
                )
            await session.commit()

    async def _flush_cost_bucket(self) -> None:
        # Skip the cost bucket write when no record_cost caller has ever fired.
        # An empty by_model dict means no cost data was recorded; persisting
        # flat-zero cost rows would produce the 'zéros plats' the spec forbids.
        if not self._collector.cost_by_model():
            logger.debug("timeseries_flusher.cost_skipped", reason="no_data_source")
            return
        cost = self._collector.cost_total()
        bucket = _bucket_floor(datetime.now(UTC), _1H)
        async with self._session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO metrics_timeseries (bucket_ts, metric, value) "
                    "VALUES (:bucket_ts, :metric, :value) "
                    "ON CONFLICT (bucket_ts, metric) DO UPDATE SET value = EXCLUDED.value"
                ),
                {"bucket_ts": bucket, "metric": "cost", "value": float(cost)},
            )
            await session.commit()

    async def _retention_sweep(self) -> None:
        async with self._session_factory() as session:
            await session.execute(
                text("DELETE FROM metrics_timeseries WHERE bucket_ts < NOW() - INTERVAL '7 days'")
            )
            await session.commit()
