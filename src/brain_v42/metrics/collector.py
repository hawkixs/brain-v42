"""MetricsCollector — in-memory counters + DB stats for the metrics endpoint."""

from __future__ import annotations

import importlib.metadata
import time
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.config import get_settings
from brain_v42.metrics.collector_db import _DbCollectorsMixin
from brain_v42.metrics.collector_dream import _DreamCollectorsMixin
from brain_v42.metrics.collector_nightly import _NightlyCollectorsMixin

logger = structlog.get_logger(__name__)

ERROR_WINDOW = 3600.0


class MetricsCollector(_DbCollectorsMixin, _DreamCollectorsMixin, _NightlyCollectorsMixin):
    """Accumulates metrics counters in memory (asyncio-safe, single-threaded).

    Initialized with engine and session_factory references to query the DB
    for row counts, coverage stats, pool status, and dimension checks.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory
        self._started_at = time.time()
        self._tool_stats: dict[str, dict[str, dict[str, Any]]] = {}
        self._embedding_stats: dict[str, Any] = {
            "total_requests": 0,
            "total_errors": 0,
            # Split counters so red-monitor can distinguish expected GPU
            # contention (lazy supervisor 503) from real outages (TCP layer).
            "gpu_busy_errors": 0,
            "unreachable_errors": 0,
            "total_latency": 0.0,
        }
        self._search_stats: dict[str, Any] = {
            "searches_total": 0,
            "searches_with_zero_results": 0,
            "total_score": 0.0,
        }
        self._reranker_stats: dict[str, Any] = {
            "total_calls": 0,
            "total_errors": 0,
            "total_latency": 0.0,
            "total_candidates": 0,
        }
        self._tool_error_times: dict[str, deque[float]] = {}
        self._embedding_error_times: deque[float] = deque(maxlen=10000)
        self._reranker_error_times: deque[float] = deque(maxlen=10000)
        self._graph_error_times: deque[float] = deque(maxlen=10000)
        self._decay_stats: dict[str, int] = {
            "stale_count": 0,
            "archived_count": 0,
            "access_log_size": 0,
        }
        self._graph_stats: dict[str, Any] = {
            "total_queries": 0,
            "total_errors": 0,
            "total_latency": 0.0,
        }
        # ── rolling-window state (for /api/cockpit) ──
        self._tool_latencies: deque[tuple[float, float]] = deque(maxlen=10000)  # (ts, ms)
        self._search_latencies: deque[tuple[float, float]] = deque(maxlen=10000)
        self._counter_snapshots: deque[tuple[float, int, int]] = deque(
            maxlen=20
        )  # (ts, calls, errors)
        self._recent_log: deque[dict[str, Any]] = deque(maxlen=50)
        # ── latency buckets (6 fixed ranges, Phase 2 cockpit field) ──
        self._latency_buckets: dict[str, int] = {
            "< 100ms": 0,
            "100-300ms": 0,
            "300-600ms": 0,
            "600ms-1s": 0,
            "1-2s": 0,
            "> 2s": 0,
        }
        # ── cost tracking (Phase 2 cockpit field) ──
        self._cost_by_model: dict[str, float] = {}
        # ── dirty-set tracking (Task 2.2, H7b) ──
        # Tracks agents that had a tool call since the last flush.
        # The flusher (Task 3.2) calls mark_flushed() after a successful upsert.
        self._dirty_agents: set[str] = set()
        # ── overflow-warned flag (cardinality cap, M1) ──
        # ONE warn per process lifetime: a per-agent set would itself grow
        # unbounded under a spoofed high-cardinality X-Brain-Agent stream
        # (client-controlled names). The first WARN carries the first
        # offending name + the cap; subsequent overflows are silent (the
        # _overflow bucket still counts them).
        self._overflow_warned: bool = False

    @staticmethod
    def _bucket_for(latency_ms: float) -> str:
        if latency_ms < 100:
            return "< 100ms"
        if latency_ms < 300:
            return "100-300ms"
        if latency_ms < 600:
            return "300-600ms"
        if latency_ms < 1000:
            return "600ms-1s"
        if latency_ms < 2000:
            return "1-2s"
        return "> 2s"

    # Sentinel reserved by get_flush_data for the process-globals row.
    # Any agent name that collides with it (spoofed x-brain-agent header) is
    # remapped to this safe bucket and a WARN is emitted.
    _RESERVED_AGENT = "_process"
    _COLLISION_BUCKET = "_process_collision"

    # Hard cap on distinct real-agent slots.  Client-controlled X-Brain-Agent
    # headers can be spoofed with arbitrary values; without a cap, _tool_stats
    # would grow unbounded.  Agents beyond the cap land in "_overflow" — a
    # single aggregation bucket — and a one-time WARN is emitted.
    _MAX_AGENTS: int = 32
    _OVERFLOW_BUCKET: str = "_overflow"

    def record_tool_call(
        self,
        tool_name: str,
        latency_ms: float,
        error: bool = False,
        agent: str = "unknown",
    ) -> None:
        if agent == self._RESERVED_AGENT:
            logger.warning("metrics.reserved_agent_label", agent=agent)
            agent = self._COLLISION_BUCKET

        # Cardinality cap: count real (non-sentinel) agent slots already taken.
        # Sentinels (_process, _process_collision, _overflow) are exempt from
        # the cap — they are internal buckets, not client-supplied names.
        _sentinels = {self._RESERVED_AGENT, self._COLLISION_BUCKET, self._OVERFLOW_BUCKET}
        if agent not in _sentinels and agent not in self._tool_stats:
            real_count = sum(1 for a in self._tool_stats if a not in _sentinels)
            if real_count >= self._MAX_AGENTS:
                # Emit ONE WARN per process lifetime (not per agent, not per
                # call): both alternatives are unbounded under a spoofed
                # high-cardinality X-Brain-Agent stream — per-call floods the
                # logs, per-agent grows a client-controlled set in memory.
                if not self._overflow_warned:
                    logger.warning(
                        "metrics.agent_cardinality_cap",
                        first_overflow_agent=agent,
                        cap=self._MAX_AGENTS,
                        overflow_bucket=self._OVERFLOW_BUCKET,
                    )
                    self._overflow_warned = True
                agent = self._OVERFLOW_BUCKET

        # Mark dirty AFTER remap so spoofed _process marks _process_collision, never _process.
        self._dirty_agents.add(agent)
        agent_bucket = self._tool_stats.setdefault(agent, {})
        if tool_name not in agent_bucket:
            agent_bucket[tool_name] = {
                "calls": 0,
                "errors": 0,
                "total_latency": 0.0,
            }
        stats = agent_bucket[tool_name]
        stats["calls"] += 1
        stats["total_latency"] += latency_ms
        if error:
            stats["errors"] += 1
            if tool_name not in self._tool_error_times:
                self._tool_error_times[tool_name] = deque(maxlen=10000)
            self._tool_error_times[tool_name].append(time.time())
        self._tool_latencies.append((time.time(), latency_ms))
        self._latency_buckets[self._bucket_for(latency_ms)] += 1

    def record_search_latency(self, latency_ms: float) -> None:
        """Record one search/retrieval latency sample for percentile computation."""
        self._search_latencies.append((time.time(), latency_ms))

    @staticmethod
    def _percentiles(samples: deque[tuple[float, float]], window_s: float) -> dict[str, float]:
        cutoff = time.time() - window_s
        values = sorted(v for ts, v in samples if ts >= cutoff)
        if not values:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        n = len(values)

        def pct(p: float) -> float:
            idx = min(n - 1, int(p * n))
            return round(values[idx], 1)

        return {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)}

    def tool_percentiles(self, window_s: float = 300.0) -> dict[str, float]:
        """Return p50/p95/p99 of tool latencies within the last `window_s` seconds."""
        return self._percentiles(self._tool_latencies, window_s)

    def retrieval_percentiles(self, window_s: float = 86400.0) -> dict[str, float]:
        """Return p50/p95 of retrieval latencies within the window (24h default)."""
        p = self._percentiles(self._search_latencies, window_s)
        return {"p50": p["p50"], "p95": p["p95"]}

    def snapshot_counters(self) -> None:
        """Capture total tool calls + errors at current time for rate derivation."""
        total_calls = sum(t["calls"] for agent in self._tool_stats.values() for t in agent.values())
        total_errors = sum(
            t["errors"] for agent in self._tool_stats.values() for t in agent.values()
        )
        self._counter_snapshots.append((time.time(), total_calls, total_errors))

    def compute_rates(self, window_s: float = 60.0) -> dict[str, float]:
        """Derive rps + err_rate from counter snapshots over the window."""
        if len(self._counter_snapshots) < 2:
            return {"rps": 0.0, "err_rate": 0.0}
        cutoff = time.time() - window_s
        baseline: tuple[float, int, int] | None = None
        for snap in self._counter_snapshots:
            if snap[0] <= cutoff:
                baseline = snap
            else:
                break
        if baseline is None:
            baseline = self._counter_snapshots[0]
        latest = self._counter_snapshots[-1]
        elapsed = latest[0] - baseline[0]
        if elapsed <= 0:
            return {"rps": 0.0, "err_rate": 0.0}
        delta_calls = latest[1] - baseline[1]
        delta_errors = latest[2] - baseline[2]
        return {
            "rps": round(delta_calls / elapsed, 2),
            "err_rate": round(delta_errors / delta_calls, 4) if delta_calls else 0.0,
        }

    def push_recent_log(self, level: str, message: str) -> None:
        """Append an entry to the recent-log ring buffer (max 50)."""
        self._recent_log.append(
            {
                "t": datetime.now().strftime("%H:%M:%S"),
                "lvl": level,
                "msg": message,
            }
        )

    def get_recent_log(self) -> list[dict[str, Any]]:
        """Return a snapshot copy of the recent-log buffer (oldest → newest)."""
        return list(self._recent_log)

    def latency_buckets(self) -> list[dict[str, Any]]:
        """Return fixed 6-bucket latency histogram for the cockpit payload.

        Each bucket carries a `tone` hint (ok/info/warn/crit) matching
        red-monitor's CSS tokens so the Brain tab can color bars by severity.
        """
        tones = {
            "< 100ms": "ok",
            "100-300ms": "info",
            "300-600ms": "warn",
            "600ms-1s": "warn",
            "1-2s": "crit",
            "> 2s": "crit",
        }
        return [
            {"range": r, "count": c, "tone": tones[r]} for r, c in self._latency_buckets.items()
        ]

    def record_cost(self, model: str, cost_usd: float) -> None:
        """Record one billable unit against a model (LLM API call, rerank, dream, …)."""
        self._cost_by_model[model] = self._cost_by_model.get(model, 0.0) + cost_usd

    def cost_by_model(self) -> dict[str, float]:
        return dict(self._cost_by_model)

    def cost_total(self) -> float:
        return sum(self._cost_by_model.values())

    def record_embedding_request(
        self,
        latency_ms: float,
        error: bool = False,
        error_kind: str | None = None,
    ) -> None:
        """Record one embedding request.

        Args:
            latency_ms: Wall-clock latency in milliseconds.
            error: Whether the request ultimately failed.
            error_kind: Optional sub-classification — "gpu_busy" (supervisor
                503, expected contention) or "unreachable" (TCP layer / real
                outage). Other values are accepted but only the two known
                kinds bump the split counters.
        """
        self._embedding_stats["total_requests"] += 1
        self._embedding_stats["total_latency"] += latency_ms
        if error:
            self._embedding_stats["total_errors"] += 1
            self._embedding_error_times.append(time.time())
            if error_kind == "gpu_busy":
                self._embedding_stats["gpu_busy_errors"] += 1
            elif error_kind == "unreachable":
                self._embedding_stats["unreachable_errors"] += 1

    def record_search(self, similarity_score: float, result_count: int) -> None:
        self._search_stats["searches_total"] += 1
        self._search_stats["total_score"] += similarity_score
        if result_count == 0:
            self._search_stats["searches_with_zero_results"] += 1

    def record_reranker_call(
        self, latency_ms: float, candidate_count: int, error: bool = False
    ) -> None:
        self._reranker_stats["total_calls"] += 1
        self._reranker_stats["total_latency"] += latency_ms
        self._reranker_stats["total_candidates"] += candidate_count
        if error:
            self._reranker_stats["total_errors"] += 1
            self._reranker_error_times.append(time.time())

    def record_decay_stats(
        self, stale_count: int, archived_count: int, access_log_size: int
    ) -> None:
        self._decay_stats["stale_count"] = stale_count
        self._decay_stats["archived_count"] = archived_count
        self._decay_stats["access_log_size"] = access_log_size

    def record_graph_query(self, latency_ms: float, error: bool = False) -> None:
        """Record a single Neo4j graph query with its latency."""
        self._graph_stats["total_queries"] += 1
        self._graph_stats["total_latency"] += latency_ms
        if error:
            self._graph_stats["total_errors"] += 1
            self._graph_error_times.append(time.time())

    def get_graph_avg_latency(self) -> float:
        """Return average graph query latency in ms (0.0 if no queries)."""
        # Cast both operands to float so the result is typed (mypy: no-any-return).
        # _graph_stats is dict[str, Any], so accesses default to Any and any
        # arithmetic on them stays Any without an explicit cast.
        total = float(self._graph_stats["total_queries"])
        if not total:
            return 0.0
        return round(float(self._graph_stats["total_latency"]) / total, 1)

    def _count_recent(self, timestamps: deque[float], window: float = ERROR_WINDOW) -> int:
        """Count entries within the last `window` seconds."""
        cutoff = time.time() - window
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        return len(timestamps)

    def get_flush_data(self, dirty_only: bool = False) -> dict[str, Any]:
        """Return per-agent tool stats + a single ``_process`` row for DB persistence.

        Shape (Task 2.1, H7): one entry per REAL agent carrying ONLY a ``tools``
        dict ``{tool: {calls, errors, recent_errors, total_latency}}``, plus a
        single ``"_process"`` entry carrying ONLY the process-global blocks
        (embedding/reranker/graph/cost/buckets/decay). Process-globals appear
        ONLY under ``_process`` — never duplicated per agent (the ×N over-count
        guard, H7a). The flusher (Task 3.2) consumes this and injects the
        ``_reranker``/``_graph``/``_cost``/``_buckets`` pseudo-tools into the
        ``_process`` row — NOT done here.

        Args:
            dirty_only: If True, return ONLY agents in ``_dirty_agents`` plus
                ``"_process"`` unconditionally. The caller (flusher, Task 3.2)
                uses this to skip re-upserting idle agents so the ``>1h`` cleanup
                can age them out. If False (default), return ALL agents + ``_process``
                — unchanged behaviour for existing callers.

        Reading is idempotent — does NOT clear ``_dirty_agents``. Call
        ``mark_flushed()`` after a successful flush to remove processed agents.

        recent_errors attribution: error times are keyed per-tool (not
        per-(agent,tool)), so the per-tool recent_errors count is computed once
        and attached to EVERY agent row that contains that tool. No
        per-(agent,tool) error tracking is introduced in this task.
        """
        # Per-tool recent_errors, computed once (error times are per-tool).
        recent_by_tool: dict[str, int] = {
            name: self._count_recent(times) for name, times in self._tool_error_times.items()
        }

        # Determine which agents to include in the output.
        agents_to_include = self._dirty_agents if dirty_only else self._tool_stats.keys()

        result: dict[str, Any] = {}
        for agent in agents_to_include:
            agent_stats = self._tool_stats.get(agent, {})
            tools: dict[str, Any] = {}
            for name, stats in agent_stats.items():
                tools[name] = {
                    "calls": stats["calls"],
                    "errors": stats["errors"],
                    "recent_errors": recent_by_tool.get(name, 0),
                    "total_latency": stats["total_latency"],
                }
            result[agent] = {"tools": tools}

        result["_process"] = {
            "embedding": {
                "total_requests": self._embedding_stats["total_requests"],
                "total_errors": self._embedding_stats["total_errors"],
                "gpu_busy_errors": self._embedding_stats["gpu_busy_errors"],
                "unreachable_errors": self._embedding_stats["unreachable_errors"],
                "recent_errors": self._count_recent(self._embedding_error_times),
                "total_latency": self._embedding_stats["total_latency"],
            },
            "reranker": {
                "total_calls": self._reranker_stats["total_calls"],
                "total_errors": self._reranker_stats["total_errors"],
                "recent_errors": self._count_recent(self._reranker_error_times),
                "total_candidates": self._reranker_stats["total_candidates"],
                "total_latency": self._reranker_stats["total_latency"],
            },
            "graph": {
                "total_queries": self._graph_stats["total_queries"],
                "total_errors": self._graph_stats["total_errors"],
                "recent_errors": self._count_recent(self._graph_error_times),
                "total_latency": self._graph_stats["total_latency"],
            },
            "cost": {
                "total": self.cost_total(),
                "by_model": self.cost_by_model(),
            },
            "buckets": dict(self._latency_buckets),
            "decay": dict(self._decay_stats),
        }
        return result

    def mark_flushed(self, agents: Iterable[str]) -> None:
        """Remove successfully flushed agents from the dirty set.

        Uses ``difference_update`` rather than ``clear()`` so any agent that
        became dirty DURING the flush cycle (between get_flush_data and this
        call) is not lost — only the acked agents are removed.  The flusher
        (Task 3.2) passes the set of agent keys it successfully upserted.
        """
        self._dirty_agents.difference_update(agents)

    def get_metrics(self) -> dict[str, Any]:
        """Assemble the complete metrics dict (without DB stats — those are async)."""
        settings = get_settings()
        now = time.time()

        try:
            version = importlib.metadata.version("brain_v42")
        except importlib.metadata.PackageNotFoundError:
            version = "dev"

        # Tool stats with avg latency — aggregate across all agents (output shape unchanged;
        # Task 2.1 will expose the per-agent breakdown via get_flush_data).
        aggregated: dict[str, dict[str, Any]] = {}
        for agent_stats in self._tool_stats.values():
            for name, stats in agent_stats.items():
                if name not in aggregated:
                    aggregated[name] = {"calls": 0, "errors": 0, "total_latency": 0.0}
                aggregated[name]["calls"] += stats["calls"]
                aggregated[name]["errors"] += stats["errors"]
                aggregated[name]["total_latency"] += stats["total_latency"]
        tools: dict[str, Any] = {}
        for name, agg in aggregated.items():
            calls = agg["calls"]
            error_times = self._tool_error_times.get(name, deque())
            tools[name] = {
                "calls": calls,
                "errors": agg["errors"],
                "recent_errors": self._count_recent(error_times),
                "avg_latency_ms": round(agg["total_latency"] / calls, 1) if calls else 0.0,
            }

        # Embedding stats with avg latency
        emb_total = self._embedding_stats["total_requests"]
        embedding_service = {
            "status": "unknown",
            "url": settings.embedding_service_url,
            "avg_latency_ms": round(self._embedding_stats["total_latency"] / emb_total, 1)
            if emb_total
            else 0.0,
            "total_requests": emb_total,
            "total_errors": self._embedding_stats["total_errors"],
            "gpu_busy_errors": self._embedding_stats["gpu_busy_errors"],
            "unreachable_errors": self._embedding_stats["unreachable_errors"],
            "recent_errors": self._count_recent(self._embedding_error_times),
        }

        # Search quality
        search_total = self._search_stats["searches_total"]
        search_quality = {
            "avg_score": round(self._search_stats["total_score"] / search_total, 2)
            if search_total
            else 0.0,
            "searches_total": search_total,
            "searches_with_zero_results": self._search_stats["searches_with_zero_results"],
        }

        # Reranker stats
        reranker_total = self._reranker_stats["total_calls"]
        reranker = {
            "total_calls": reranker_total,
            "total_errors": self._reranker_stats["total_errors"],
            "recent_errors": self._count_recent(self._reranker_error_times),
            "total_candidates": self._reranker_stats["total_candidates"],
            "avg_latency_ms": round(self._reranker_stats["total_latency"] / reranker_total, 1)
            if reranker_total
            else 0.0,
        }

        # Graph stats
        graph_total = self._graph_stats["total_queries"]
        graph = {
            "total_queries": graph_total,
            "total_errors": self._graph_stats["total_errors"],
            "recent_errors": self._count_recent(self._graph_error_times),
            "avg_latency_ms": round(self._graph_stats["total_latency"] / graph_total, 1)
            if graph_total
            else 0.0,
        }

        return {
            "collected_at": datetime.now(UTC).isoformat(),
            "started_at": datetime.fromtimestamp(self._started_at, tz=UTC).isoformat(),
            "version": version,
            "model": "Qodo-Embed-1-1.5B",
            "embedding_dim": settings.embedding_dimension,
            "uptime_seconds": int(now - self._started_at),
            "memory_rss_bytes": _get_rss_bytes(),
            "embedding_service": embedding_service,
            "tools": tools,
            "search_quality": search_quality,
            "reranker": reranker,
            "decay": dict(self._decay_stats),
            "graph": graph,
        }

    async def collect_db_stats(self) -> dict[str, Any]:
        """Query PostgreSQL for row counts, coverage, pool stats, dimension checks.

        Returns the 'database' block of the metrics response.
        """
        settings = get_settings()
        pool = self._engine.sync_engine.pool

        pool_size = pool.size()  # type: ignore[attr-defined]
        max_overflow = getattr(pool, "_max_overflow", 0)
        pool_stats = {
            "active": pool.checkedout(),  # type: ignore[attr-defined]
            "idle": pool.checkedin(),  # type: ignore[attr-defined]
            "overflow": max(0, pool.overflow()),  # type: ignore[attr-defined]
            "max": pool_size + max_overflow,
        }

        embedding_tables = {
            "decision": "decisions",
            "learning": "learnings",
            "snippet": "snippets",
            "runbook": "runbooks",
            "adr": "adrs",
        }
        tables_with_embeddings = list(embedding_tables.values())
        tables: dict[str, Any] = {}
        embedding_backlog_by_type: dict[str, dict[str, int | float]] = {}
        worker_last_24h: dict[str, Any] = {
            "attempted": 0,
            "stored": 0,
            "stale": 0,
            "missing": 0,
            "unavailable": 0,
            "timed_out": 0,
            "failed": 0,
            "unavailable_by_kind": {},
        }
        graph_outbox_stats: dict[str, Any] = {
            "available": False,
            "pending": 0,
            "ready": 0,
            "claimed": 0,
            "exhausted": 0,
            "oldest_pending_age_seconds": 0.0,
            "projector": {
                "generation": -1,
                "armed": False,
                "lease_active": False,
                "recovery_active": False,
                "healthy": False,
            },
        }

        try:
            async with self._session_factory() as session:
                for table_name in tables_with_embeddings:
                    row = (
                        await session.execute(
                            text(
                                f"SELECT COUNT(*), "  # noqa: S608  # nosec B608 - fragment = `table_name`, which iterates only `tables_with_embeddings`, the 5 literal values of the local `embedding_tables` dict 36 lines above; `collect_db_stats(self)` takes no parameter, `embedding_dimension` goes through the :dim bind; exception reviewed on 2026-08-16, to be re-examined before 2026-09-30
                                f"COUNT(embedding), "
                                f"COUNT(*) - COUNT(embedding), "
                                f"COALESCE(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - "
                                f"(MIN(created_at) FILTER (WHERE embedding IS NULL)))), 0) "
                                f"FROM {table_name}"
                            )
                        )
                    ).one()
                    total, with_emb, null_emb, oldest_age = row[0], row[1], row[2], row[3]
                    tables[table_name] = {
                        "rows": total,
                        "embedding_coverage": round(with_emb / total, 2) if total else 1.0,
                        "null_embeddings": null_emb,
                    }
                    entity_type = next(
                        key for key, value in embedding_tables.items() if value == table_name
                    )
                    embedding_backlog_by_type[entity_type] = {
                        "count": null_emb,
                        "oldest_age_seconds": round(float(oldest_age or 0), 1),
                    }

                pc_count = (
                    await session.execute(text("SELECT COUNT(*) FROM project_contexts"))
                ).scalar()
                tables["project_contexts"] = {"rows": pc_count}

                db_size = (
                    await session.execute(text("SELECT pg_database_size(current_database())"))
                ).scalar()

                dim = settings.embedding_dimension
                mismatch_total = 0
                for table_name in tables_with_embeddings:
                    row_count = (
                        await session.execute(
                            text(
                                f"SELECT COUNT(*) FROM {table_name} "  # noqa: S608  # nosec B608 - the same `table_name` fragment as above: the loop re-reads `tables_with_embeddings`, derived from the local literal dict `embedding_tables`; the only variable value, `settings.embedding_dimension`, goes through the :dim bind and is not interpolated; exception reviewed on 2026-08-16, to be re-examined before 2026-09-30
                                f"WHERE embedding IS NOT NULL "
                                f"AND vector_dims(embedding) != :dim"
                            ),
                            {"dim": dim},
                        )
                    ).scalar()
                    mismatch_total += row_count or 0

                worker_rows = (
                    await session.execute(
                        text(
                            "SELECT metric, SUM(value) FROM metrics_timeseries "
                            "WHERE metric LIKE 'embedding_backfill.%' "
                            "AND bucket_ts >= NOW() - INTERVAL '24 hours' "
                            "GROUP BY metric"
                        )
                    )
                ).all()
                for metric, value in worker_rows:
                    name = metric.removeprefix("embedding_backfill.")
                    if name.startswith("unavailable."):
                        kind = name.removeprefix("unavailable.")
                        worker_last_24h["unavailable_by_kind"][kind] = int(value)
                    elif name in worker_last_24h and name != "unavailable_by_kind":
                        worker_last_24h[name] = int(value)

                try:
                    outbox_row = (
                        await session.execute(
                            text(
                                "WITH observed AS MATERIALIZED ("
                                "SELECT clock_timestamp() AS now"
                                "), outbox AS ("
                                "SELECT "
                                "COUNT(*) FILTER ("
                                "WHERE delivered_at IS NULL "
                                "AND last_error_code IS DISTINCT FROM 'max_attempts'"
                                ") AS pending, "
                                "COUNT(*) FILTER ("
                                "WHERE delivered_at IS NULL "
                                "AND last_error_code IS DISTINCT FROM 'max_attempts' "
                                "AND available_at <= (SELECT now FROM observed) "
                                "AND (leased_until IS NULL "
                                "OR leased_until <= (SELECT now FROM observed))"
                                ") AS ready, "
                                "COUNT(*) FILTER ("
                                "WHERE delivered_at IS NULL "
                                "AND last_error_code IS DISTINCT FROM 'max_attempts' "
                                "AND lease_owner IS NOT NULL "
                                "AND leased_until > (SELECT now FROM observed)"
                                ") AS claimed, "
                                "COUNT(*) FILTER ("
                                "WHERE delivered_at IS NULL "
                                "AND last_error_code = 'max_attempts'"
                                ") AS exhausted, "
                                "COALESCE(EXTRACT(EPOCH FROM ("
                                "(SELECT now FROM observed) - MIN(created_at) FILTER ("
                                "WHERE delivered_at IS NULL "
                                "AND last_error_code IS DISTINCT FROM 'max_attempts'"
                                ")"
                                ")), 0) AS oldest_pending_age_seconds "
                                "FROM graph_outbox WHERE delivered_at IS NULL"
                                "), projector AS ("
                                "SELECT generation, "
                                "neo4j_armed_generation = generation AS armed, "
                                "owner IS NOT NULL "
                                "AND leased_until > (SELECT now FROM observed) AS lease_active, "
                                "recovery_id IS NOT NULL AS recovery_active "
                                "FROM graph_projection_leases "
                                "WHERE slot = 'neo4j'"
                                ") "
                                "SELECT outbox.pending, outbox.ready, outbox.claimed, "
                                "outbox.exhausted, outbox.oldest_pending_age_seconds, "
                                "projector.generation, projector.armed, "
                                "projector.lease_active, projector.recovery_active "
                                "FROM outbox LEFT JOIN projector ON TRUE"
                            )
                        )
                    ).one()
                except Exception as exc:
                    logger.debug(
                        "metrics.collect_graph_outbox_stats.unavailable",
                        error_type=type(exc).__name__,
                    )
                else:
                    armed = bool(outbox_row[6])
                    lease_active = bool(outbox_row[7])
                    recovery_active = bool(outbox_row[8])
                    graph_outbox_stats = {
                        "available": True,
                        "pending": int(outbox_row[0] or 0),
                        "ready": int(outbox_row[1] or 0),
                        "claimed": int(outbox_row[2] or 0),
                        "exhausted": int(outbox_row[3] or 0),
                        "oldest_pending_age_seconds": round(float(outbox_row[4] or 0), 1),
                        "projector": {
                            "generation": -1 if outbox_row[5] is None else int(outbox_row[5]),
                            "armed": armed,
                            "lease_active": lease_active,
                            "recovery_active": recovery_active,
                            "healthy": armed and lease_active and not recovery_active,
                        },
                    }

        except Exception:
            logger.exception("metrics.collect_db_stats.error")
            return {
                "pool": pool_stats,
                "tables": {},
                "db_size_bytes": 0,
                "dimension_mismatches": 0,
                "embedding_backlog": {
                    "total": 0,
                    "by_entity_type": {},
                    "worker_last_24h": worker_last_24h,
                },
                "graph_outbox": graph_outbox_stats,
            }

        return {
            "pool": pool_stats,
            "tables": tables,
            "db_size_bytes": db_size or 0,
            "dimension_mismatches": mismatch_total,
            "embedding_backlog": {
                "total": sum(item["count"] for item in embedding_backlog_by_type.values()),
                "by_entity_type": embedding_backlog_by_type,
                "worker_last_24h": worker_last_24h,
            },
            "graph_outbox": graph_outbox_stats,
        }


def _get_rss_bytes() -> int:
    """Get current process RSS in bytes via /proc/self/status."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024  # kB -> bytes
    except (FileNotFoundError, ValueError, IndexError):
        pass
    return 0
