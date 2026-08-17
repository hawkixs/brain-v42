"""CockpitCollector — assembles the /api/cockpit payload.

Wraps MetricsCollector for in-memory state (rates, percentiles, recent-log)
and delegates DB-side fields to memory_stats + search_log queries. 1-second
in-memory cache keeps p95 response time under 100ms under polling load.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import time
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.config import get_settings
from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.collector import MetricsCollector, _get_rss_bytes
from brain_v42.metrics.memory_stats import collect_memory_stats

logger = structlog.get_logger(__name__)

_CACHE_TTL_S = 1.0


class CockpitCollector:
    """Assembles the cockpit payload from the in-memory collector + DB queries."""

    def __init__(
        self,
        collector: MetricsCollector,
        session_factory: async_sessionmaker[AsyncSession],
        codex_registry: ClientActivityRegistry | None = None,
    ) -> None:
        self._collector = collector
        self._session_factory = session_factory
        self._codex_registry = codex_registry
        self._started_at = time.time()
        self._cache: dict[str, Any] | None = None
        self._cache_ts: float = 0.0
        self._cache_lock = asyncio.Lock()

    async def snapshot(self) -> dict[str, Any]:
        """Return the full cockpit payload (cached 1s)."""
        now = time.monotonic()
        if self._cache is not None and now - self._cache_ts < _CACHE_TTL_S:
            return self._cache
        async with self._cache_lock:
            if self._cache is not None and time.monotonic() - self._cache_ts < _CACHE_TTL_S:
                return self._cache
            self._cache = await self._build()
            self._cache_ts = time.monotonic()
            return self._cache

    async def _build(self) -> dict[str, Any]:
        try:
            version = importlib.metadata.version("brain_v42")
        except importlib.metadata.PackageNotFoundError:
            version = "dev"

        tool_pct = self._collector.tool_percentiles(window_s=300.0)
        rates = self._collector.compute_rates(window_s=60.0)
        retrieval_pct = self._collector.retrieval_percentiles(window_s=86400.0)

        memory = await collect_memory_stats(self._session_factory)
        retrieval = await self._retrieval_stats(retrieval_pct)
        tools_block = self._tools_block()

        # cache_hit_ratio: no data source exists in this process — the embedding
        # service does not expose a cache-hit counter and the DB layer has no
        # cache layer instrumented.  Returning None is honest ("not measured");
        # a fake 0.0 would look like a measured 0% hit-rate.
        cache_hit_ratio = None
        rss_bytes = _get_rss_bytes()

        # endpoint: read from settings instead of hardcoding "stdio".
        # Post-HTTP-cutover the transport is "http"; hardcoding "stdio" would
        # show the wrong value in red-monitor for all HTTP deployments.
        try:
            _settings = get_settings()
            _endpoint: str = _settings.brain_mcp_transport
        except Exception:
            _endpoint = "unknown"

        buckets = self._collector.latency_buckets()
        by_model_raw = self._collector.cost_by_model()
        # cost.today = None when no record_cost caller exists.
        # An empty by_model dict means no cost data was ever recorded
        # (record_cost has zero callers in the current MCP request lifecycle).
        # Showing 0.0 would be indistinguishable from a real zero-spend session
        # and causes flat-zero rows in metrics_timeseries — the 'zéros plats'
        # the spec forbids.  None signals 'not measured / no data source'.
        # When record_cost is eventually wired, by_model becomes non-empty and
        # today resumes as a rounded float.
        has_cost_data = bool(by_model_raw)
        total_cost = self._collector.cost_total() if has_cost_data else 0.0
        by_model: list[dict[str, Any]] = [
            {
                "m": name,
                "v": round(v, 4),
                "pct": round(v / total_cost, 2) if total_cost else 0.0,
            }
            for name, v in sorted(by_model_raw.items(), key=lambda kv: -kv[1])
        ]
        rps_hist = await self._history("rps", limit=48)
        p95_hist = await self._history("p95", limit=48)
        err_hist = await self._history("err_rate", limit=48)
        cost_hist = await self._history("cost", limit=24)
        codex_activity = (
            self._codex_registry.snapshot()
            if self._codex_registry is not None
            else {"active_convs": 0, "ctx_tokens": 0, "activeConvs": [], "clients": []}
        )

        return {
            "version": version,
            "pid": os.getpid(),
            "uptime_s": int(time.time() - self._started_at),
            "endpoint": _endpoint,
            "metrics": {
                "rps": rates["rps"],
                "p50": tool_pct["p50"],
                "p95": tool_pct["p95"],
                "p99": tool_pct["p99"],
                "err_rate": rates["err_rate"],
                "cache_hit": cache_hit_ratio,  # None = no data source; not 0.0
                "active_convs": codex_activity["active_convs"],
                "ctx_tokens": codex_activity["ctx_tokens"],
                "memory_mb": rss_bytes // (1024 * 1024),
            },
            "activeConvs": codex_activity["activeConvs"],
            # Additive on purpose: one row per client, filled by whichever source
            # exists. The shipped red-monitor panel still reads activeConvs and
            # switches over later, so nothing is removed or moved here.
            "clients": codex_activity["clients"],
            "tools": tools_block,
            "skills": [],
            "memory": memory,
            "retrieval": retrieval,
            "latencyBuckets": buckets,
            "cost": {
                # None when no record_cost caller exists — honest 'no data source'.
                # Becomes a float once record_cost is wired in the MCP lifecycle.
                "today": round(total_cost, 2) if has_cost_data else None,
                "yesterday": 0.0,  # deferred: 7-day rollup
                "week": 0.0,
                "month": 0.0,
                "byModel": by_model,
            },
            "handoff": [],
            "rpsHistory": rps_hist,
            "p95History": p95_hist,
            "errHistory": err_hist,
            "costHistory": cost_hist,
            "recent": self._collector.get_recent_log(),
        }

    async def _history(self, metric: str, limit: int) -> list[float]:
        """Fetch last N bucketed points for a metric, oldest → newest."""
        try:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT value FROM metrics_timeseries "
                            "WHERE metric = :metric "
                            "AND bucket_ts > NOW() - INTERVAL '24 hours' "
                            "ORDER BY bucket_ts ASC "
                            "LIMIT :limit"
                        ),
                        {"metric": metric, "limit": limit},
                    )
                ).fetchall()
                return [float(r[0]) for r in rows]
        except Exception:
            logger.warning("metrics.cockpit.history_failed", metric=metric, exc_info=True)
            return []

    def _tools_block(self) -> list[dict[str, Any]]:
        # Aggregate per-tool across all agents (output shape unchanged — Task 2.1
        # will expose per-agent breakdown; _tool_stats is now [agent][tool]).
        aggregated: dict[str, dict[str, Any]] = {}
        for agent_stats in self._collector._tool_stats.values():
            for name, stats in agent_stats.items():
                if name.startswith("_"):
                    continue
                if name not in aggregated:
                    aggregated[name] = {"calls": 0, "errors": 0, "total_latency": 0.0}
                aggregated[name]["calls"] += stats["calls"]
                aggregated[name]["errors"] += stats["errors"]
                aggregated[name]["total_latency"] += stats["total_latency"]
        tools: list[dict[str, Any]] = []
        for name, agg in aggregated.items():
            calls = agg["calls"]
            avg = round(agg["total_latency"] / calls, 1) if calls else 0.0
            tools.append(
                {
                    "name": name,
                    "calls24h": calls,
                    "err": agg["errors"],
                    "p95": avg,
                    "lastErr": None,
                }
            )
        return tools

    def _cache_hit_ratio(self) -> None:
        """No data source exists for cache-hit ratio — always returns None.

        The embedding service does not expose a cache-hit counter; the DB layer
        has no cache instrumentation.  Returning None is honest and prevents a
        misleading 0.0 in the cockpit.  Method kept for back-compat so existing
        callers that check the return value see a clear intent.
        """
        return None

    async def _retrieval_stats(self, pct: dict[str, float]) -> dict[str, Any]:
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT COUNT(*), "
                            "COUNT(*) FILTER (WHERE result_count = 0) "
                            "FROM search_log "
                            "WHERE created_at > NOW() - INTERVAL '24 hours'"
                        )
                    )
                ).one()
                total, zero = int(row[0] or 0), int(row[1] or 0)
        except Exception:
            logger.warning("metrics.cockpit.retrieval_stats.failed", exc_info=True)
            total, zero = 0, 0

        rer_total = self._collector._reranker_stats["total_calls"]
        rer_ratio = rer_total / total if total else 0.0

        return {
            "p50": pct["p50"],
            "p95": pct["p95"],
            "hitRate": round(1 - (zero / total), 2) if total else 0.0,
            "rerankUsed": round(rer_ratio, 2),
            "queries24h": total,
        }
