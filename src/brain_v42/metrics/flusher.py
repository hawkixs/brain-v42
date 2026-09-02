"""MetricsFlusher — periodic background task for cross-process metrics persistence.

Every MCP process runs a MetricsFlusher that:
- Upserts its in-memory counters into process_metrics every 30s
- Cleans stale process_metrics rows (>1h) on each flush
- Cleans old search_log rows (>30 days) once per hour
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.metrics.collector import MetricsCollector, _get_rss_bytes
from brain_v42.metrics.retention import PROCESS_METRICS_STALE_SQL

logger = structlog.get_logger(__name__)


class MetricsFlusher:
    """Periodically upserts in-memory counters to process_metrics and cleans stale data."""

    def __init__(
        self,
        collector: MetricsCollector,
        session_factory: async_sessionmaker[AsyncSession],
        flush_interval: float = 30.0,
    ) -> None:
        self._collector = collector
        self._session_factory = session_factory
        self._flush_interval = flush_interval
        self._pid = os.getpid()
        self._started_at = time.time()
        self._task: asyncio.Task[None] | None = None
        self._last_cleanup: float = 0.0

    async def start(self) -> None:
        """Start the periodic flusher as an asyncio task."""
        self._task = asyncio.create_task(self._run_loop())
        logger.info("metrics_flusher.started", pid=self._pid)

    async def stop(self) -> None:
        """Stop the flusher's background task (no DB cleanup on shutdown).

        We deliberately do NOT delete our rows here. With the bare PK
        (agent_name), each agent_name owns exactly one row in the DB; a blanket
        ``DELETE WHERE pid`` on shutdown would wipe rows that have already been
        re-claimed by the next server instance, making the cockpit lose an agent
        the instant a server bounces (C2/H7). Idle rows are aged out instead by
        the ``>1h`` cleanup in ``_flush`` (the 60s "active" window already
        excludes them from live counts).
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("metrics_flusher.stopped", pid=self._pid)

    async def _run_loop(self) -> None:
        """Main loop: flush every interval."""
        while True:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("metrics_flusher.error")

    @staticmethod
    def _process_pseudo_tools(entry: dict[str, Any]) -> dict[str, Any]:
        """Build the ``_reranker``/``_graph``/``_cost``/``_buckets`` pseudo-tools
        from a ``_process`` flush entry.

        This preserves the EXACT shape that ``collect_process_metrics`` / cockpit /
        red-monitor already consume; it is the same injection that previously lived
        inline in ``_flush``, now sourced from the ``_process`` entry and written to
        the ``_process`` ROW only (never per real agent — the ×N over-count guard).
        """
        ptools: dict[str, Any] = {}
        rk = entry.get("reranker", {})
        if rk.get("total_calls", 0) > 0:
            ptools["_reranker"] = {
                "calls": rk["total_calls"],
                "errors": rk["total_errors"],
                "recent_errors": rk["recent_errors"],
                "total_latency": rk["total_latency"],
                "total_candidates": rk.get("total_candidates", 0),
            }
        gr = entry.get("graph", {})
        if gr.get("total_queries", 0) > 0:
            ptools["_graph"] = {
                "calls": gr["total_queries"],
                "errors": gr["total_errors"],
                "recent_errors": gr.get("recent_errors", 0),
                "total_latency": gr["total_latency"],
            }
        co = entry.get("cost", {})
        if co.get("total", 0.0) > 0:
            ptools["_cost"] = {
                "total": co["total"],
                "by_model": co["by_model"],
            }
        bk = entry.get("buckets", {})
        if any(bk.values()):
            ptools["_buckets"] = bk
        # Persist decay stats so the sidecar can expose them cross-process and
        # they survive MCP server restarts.  Written unconditionally (even when
        # all zeros) so the sidecar always has a decay block to read back from
        # process_metrics — a missing _decay key would hide real stale/archived
        # counts after a busy decay run.
        decay = entry.get("decay", {})
        ptools["_decay"] = {
            "stale_count": decay.get("stale_count", 0),
            "archived_count": decay.get("archived_count", 0),
            "access_log_size": decay.get("access_log_size", 0),
        }
        return ptools

    async def _flush(self) -> None:
        """Upsert ONE row per agent on the bare PK (agent_name) and clean stale data.

        Iterates ``get_flush_data(dirty_only=True)``: each real-agent entry
        becomes a row carrying its own ``tool_stats`` (embedding empty, rss 0);
        the single ``_process`` entry carries the process-globals (embedding,
        RSS, and the ``_reranker``/``_graph``/``_cost``/``_buckets`` pseudo-tools)
        so they are persisted exactly once, never duplicated ×N per agent (H7a).
        After a successful commit, idle agents are dropped from the dirty set via
        ``mark_flushed`` so they are not re-upserted forever and the ``>1h``
        cleanup can age their rows out.

        ``pid`` is no longer part of the PK (migration 026) but is STILL written
        (and refreshed on conflict) as a plain column: ``collector_db`` derives
        ``active_processes = COUNT(DISTINCT pid)`` from it (a consumer-facing
        back-compat metric). Post-cutover every agent row carries the single HTTP
        server's pid, so ``active_processes`` correctly reports 1.

        DEPLOY GATE: this upsert uses ``ON CONFLICT (agent_name)``. Migration 026
        (bare agent_name PK) MUST be live on every running server before this
        flusher ships.  Mismatches in either direction cause a hard PostgreSQL
        error at write time ("no unique constraint matching given keys for ON
        CONFLICT"):

          - This flusher (ON CONFLICT agent_name) vs DB still at composite PK 025
            → INSERT fails every 30 s per process.
          - Old flusher (ON CONFLICT agent_name, pid) vs DB at bare PK 026
            → INSERT also fails.

        Sequence: run ``alembic upgrade 026`` first, then deploy this binary.
        Rollback: revert this binary first, then ``alembic downgrade -1``.

        snapshot_counters() is called at the START of every flush cycle so that
        rps / err_rate derivations always have fresh counter anchors.  Resolution:
        one snapshot per 30s flush interval.  The timeseries_flusher consumes
        compute_rates(window_s=1800) so two consecutive snapshots 30s apart are
        sufficient to produce a non-zero rps.  Without this call, snapshot_counters
        had zero callers and compute_rates always returned 0.0.
        """
        # Snapshot counters first so rps derivation has a fresh anchor point.
        self._collector.snapshot_counters()

        flush_data = self._collector.get_flush_data(dirty_only=True)
        started_at = datetime.fromtimestamp(self._started_at, tz=UTC)
        rss = _get_rss_bytes()

        # Build (agent_name, tool_json, embedding_json, rss) per row.
        rows: list[dict[str, Any]] = []
        flushed_agent_names: set[str] = set()
        for agent_name, entry in flush_data.items():
            if agent_name == "_process":
                tool_json = self._process_pseudo_tools(entry)
                embedding_json: dict[str, Any] = entry["embedding"]
                row_rss = rss
            else:
                # Real agent: its own tools; embedding/rss are process-global.
                tool_json = entry["tools"]
                embedding_json = {}
                row_rss = 0
                flushed_agent_names.add(agent_name)
            rows.append(
                {
                    "agent_name": agent_name,
                    "pid": self._pid,
                    "started_at": started_at,
                    "tool_stats": json.dumps(tool_json),
                    "embedding_stats": json.dumps(embedding_json),
                    "rss": row_rss,
                }
            )

        async with self._session_factory() as session:
            # N sequential upserts (one per agent) is deliberate: typical agent
            # cardinality per process is low (2-5); switch to executemany if it grows.
            # Use CAST() instead of :: to avoid conflicting with SQLAlchemy's
            # named-parameter syntax (e.g. :param::type is ambiguous).
            for params in rows:
                await session.execute(
                    text("""
                        INSERT INTO process_metrics
                            (agent_name, pid, started_at, updated_at,
                             tool_stats, embedding_stats, memory_rss_bytes)
                        VALUES
                            (:agent_name, :pid, :started_at, NOW(),
                             CAST(:tool_stats AS jsonb), CAST(:embedding_stats AS jsonb), :rss)
                        ON CONFLICT (agent_name) DO UPDATE SET
                            pid = :pid,
                            updated_at = NOW(),
                            tool_stats = CAST(:tool_stats AS jsonb),
                            embedding_stats = CAST(:embedding_stats AS jsonb),
                            memory_rss_bytes = :rss
                    """),
                    params,
                )

            # Age out idle per-agent rows that mark_flushed stopped re-upserting.
            # Same window as the read side — see brain_v42.metrics.retention.
            #
            # The only interpolated fragment is PROCESS_METRICS_STALE_SQL,
            # imported at module top from brain_v42.metrics.retention. It is
            # built there at import time from the literal integer
            # PROCESS_METRICS_RETENTION_SECONDS = 3600; retention.py imports
            # neither os, nor Settings, nor anything external. No call
            # parameter, no environment variable and no payload can therefore
            # reach this string. The invariant is pinned by
            # tests/unit/metrics/test_flusher_stale_sql_is_a_literal_constant.py,
            # which fails if the constant becomes dynamic.
            await session.execute(
                text(f"DELETE FROM process_metrics WHERE {PROCESS_METRICS_STALE_SQL}")  # nosec B608 - fragment = the imported constant PROCESS_METRICS_STALE_SQL (metrics/retention.py), frozen on the literal int 3600, out of reach of any input; exception reviewed on 2026-08-16, to be re-examined before 2026-09-30
            )

            # Clean old search_log (>30 days) — once per hour
            now = time.time()
            if now - self._last_cleanup > 3600:
                await session.execute(
                    text("DELETE FROM search_log WHERE created_at < NOW() - INTERVAL '30 days'")
                )
                self._last_cleanup = now

            await session.commit()

        # Only after a successful commit: drop the flushed real agents from the
        # dirty set so idle agents are not re-upserted next cycle.
        self._collector.mark_flushed(flushed_agent_names)

        logger.debug("metrics_flusher.flushed", pid=self._pid, agents=len(rows))
