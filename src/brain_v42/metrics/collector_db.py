"""DB-backed infrastructure metrics collectors for MetricsCollector.

Async read-side collectors over ``search_log``, ``process_metrics`` and the
Neo4j graph inventory. Split out of ``collector.py`` to keep that module
focused on in-memory instrumentation; mixed into ``MetricsCollector`` so the
public API (``collector.collect_*`` / ``collector.record_search_log``) is
unchanged.

These methods depend only on ``self._session_factory`` and never crash the
sidecar — every query degrades to an empty/zero result on error. (Pool / row
counts that also need ``self._engine`` + ``get_settings`` stay in
``collector.py`` as ``collect_db_stats``.)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

from brain_v42.metrics.retention import (
    PROCESS_METRICS_FRESH_SQL,
    PROCESS_METRICS_IS_LIVE_SQL,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

# PG table → Neo4j label mapping for orphan diff (entities with embeddings
# that are expected to have a corresponding lightweight node in the graph).
_PG_LABEL_MAP: dict[str, str] = {
    "decisions": "Decision",
    "learnings": "Learning",
    "snippets": "Snippet",
    "runbooks": "Runbook",
    "adrs": "ADR",
}


class _DbCollectorsMixin:
    """search_log / process_metrics / graph inventory collectors."""

    # Provided by MetricsCollector.__init__ (declared for type-checkers only).
    if TYPE_CHECKING:
        _session_factory: async_sessionmaker[AsyncSession]

    async def record_search_log(
        self,
        tool_name: str,
        project_key: str | None,
        result_count: int,
        top_score: float | None,
        avg_score: float | None,
        latency_ms: float,
    ) -> None:
        """INSERT a row into search_log and update the in-memory latency histogram.

        The in-memory call (``record_search_latency``) is made BEFORE the DB
        write so the percentile ring-buffer is populated even when the DB write
        fails.  ``retrieval_percentiles`` then returns real values instead of
        the structural zeros that result from having zero callers.
        """
        # Update in-memory histogram first — independent of DB availability.
        self.record_search_latency(latency_ms)  # type: ignore[attr-defined]

        try:
            async with self._session_factory() as session:
                await session.execute(
                    text(
                        "INSERT INTO search_log "
                        "(tool_name, project_key, result_count, top_score, avg_score, latency_ms) "
                        "VALUES (:tool, :pk, :cnt, :top, :avg, :lat)"
                    ),
                    {
                        "tool": tool_name,
                        "pk": project_key,
                        "cnt": result_count,
                        "top": top_score,
                        "avg": avg_score,
                        "lat": latency_ms,
                    },
                )
                await session.commit()
        except Exception:
            logger.warning("metrics.record_search_log.failed", tool_name=tool_name, exc_info=True)

    async def collect_search_quality(self) -> dict[str, Any]:
        """Aggregate search quality from search_log (last 24h)."""
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        text("""
                            SELECT
                                COUNT(*),
                                COUNT(*) FILTER (WHERE result_count = 0),
                                AVG(avg_score) FILTER (WHERE avg_score IS NOT NULL)
                            FROM search_log
                            WHERE created_at > NOW() - INTERVAL '24 hours'
                        """)
                    )
                ).one()
                return {
                    "searches_total": row[0],
                    "searches_with_zero_results": row[1],
                    "avg_score": round(row[2], 2) if row[2] is not None else 0.0,
                }
        except Exception:
            logger.warning("metrics.collect_search_quality.failed", exc_info=True)
            return {
                "searches_total": 0,
                "searches_with_zero_results": 0,
                "avg_score": 0.0,
            }

    async def collect_process_metrics(self) -> dict[str, Any]:
        """Aggregate tool/embedding stats from every process still within retention.

        The read window is ``retention.PROCESS_METRICS_FRESH_SQL`` — the exact complement
        of the purge predicate, so a row that survives the purge is always readable.

        Returns:
            active_processes: distinct pid count (back-compat — dashboard already reads this).
            active_agents: distinct real agent_name count (excludes _process).
            total_memory_rss_bytes: RSS from _process row(s) only.
            tools: tool_stats aggregated across ALL rows (real tools + pseudo-tools from
                _process are disjoint, so no ×N).
            embedding: embedding_stats from _process row(s) only (never from real-agent
                rows which carry empty dicts per Task 3.2).
            by_agent: per real-agent breakdown {calls, errors, recent_errors, avg_latency_ms}
                aggregated from that agent's tool_stats; excludes _process.
        """
        try:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT agent_name, pid, started_at, updated_at, "  # nosec B608 - the only 2 fragments are the imported constants PROCESS_METRICS_IS_LIVE_SQL and PROCESS_METRICS_FRESH_SQL (metrics/retention.py), frozen on the literal ints 60 and 3600; `collect_process_metrics(self)` takes no parameter; exception reviewed on 2026-08-16, to be re-examined before 2026-09-30
                            "tool_stats, embedding_stats, memory_rss_bytes, "
                            f"{PROCESS_METRICS_IS_LIVE_SQL} AS is_live "
                            "FROM process_metrics "
                            f"WHERE {PROCESS_METRICS_FRESH_SQL}"
                        )
                    )
                ).all()

            agg_tools: dict[str, dict[str, Any]] = {}
            agg_emb = {
                "total_requests": 0,
                "total_errors": 0,
                "gpu_busy_errors": 0,
                "unreachable_errors": 0,
                "recent_errors": 0,
                "total_latency": 0.0,
            }
            total_rss = 0

            # Per-agent accumulators: {agent_name: {calls, errors, recent_errors, total_latency}}
            agent_agg: dict[str, dict[str, Any]] = {}

            for row in rows:
                agent_name = row[0]
                tool_stats = row[4]  # JSONB → dict
                emb_stats = row[5]
                rss = row[6]

                # Aggregate tools across ALL rows (real tools + pseudo-tools are disjoint)
                for name, stats in tool_stats.items():
                    if name not in agg_tools:
                        agg_tools[name] = {
                            "calls": 0,
                            "errors": 0,
                            "recent_errors": 0,
                            "total_latency": 0.0,
                            "total_candidates": 0,
                        }
                    agg_tools[name]["calls"] += stats.get("calls", 0)
                    agg_tools[name]["errors"] += stats.get("errors", 0)
                    agg_tools[name]["recent_errors"] += stats.get("recent_errors", 0)
                    agg_tools[name]["total_latency"] += stats.get("total_latency", 0.0)
                    agg_tools[name]["total_candidates"] += stats.get("total_candidates", 0)

                if agent_name == "_process":
                    # Process-globals: embedding + RSS from _process only
                    agg_emb["total_requests"] += emb_stats.get("total_requests", 0)
                    agg_emb["total_errors"] += emb_stats.get("total_errors", 0)
                    agg_emb["gpu_busy_errors"] += emb_stats.get("gpu_busy_errors", 0)
                    agg_emb["unreachable_errors"] += emb_stats.get("unreachable_errors", 0)
                    agg_emb["total_latency"] += emb_stats.get("total_latency", 0.0)
                    agg_emb["recent_errors"] += emb_stats.get("recent_errors", 0)
                    total_rss += rss
                else:
                    # Real agent: accumulate per-agent breakdown from its tool_stats
                    if agent_name not in agent_agg:
                        agent_agg[agent_name] = {
                            "calls": 0,
                            "errors": 0,
                            "recent_errors": 0,
                            "total_latency": 0.0,
                        }
                    for stats in tool_stats.values():
                        agent_agg[agent_name]["calls"] += stats.get("calls", 0)
                        agent_agg[agent_name]["errors"] += stats.get("errors", 0)
                        # recent_errors is per-tool (not per-(agent,tool)), so summing across
                        # a shared tool over-estimates per-agent; deliberate — error-times are
                        # not tracked at agent granularity; switch to per-(agent,tool) if needed.
                        agent_agg[agent_name]["recent_errors"] += stats.get("recent_errors", 0)
                        agent_agg[agent_name]["total_latency"] += stats.get("total_latency", 0.0)

            # active_processes: distinct pids among rows STILL being refreshed. The read
            # window spans the whole retention so silent agents stay on the panel, but a
            # pid is only "active" while its row keeps moving — otherwise a process that
            # died an hour ago would still be counted, and server.py gates the whole
            # cross-process override on this being > 0.
            active_processes = len({row[1] for row in rows if row[7]})

            # active_agents: distinct real agents (excludes _process)
            active_agents = len({row[0] for row in rows if row[0] != "_process"})

            # Compute avg latencies for the global tools block
            tools_with_avg: dict[str, Any] = {}
            for name, stats in agg_tools.items():
                calls = stats["calls"]
                entry: dict[str, Any] = {
                    "calls": calls,
                    "errors": stats["errors"],
                    "recent_errors": stats["recent_errors"],
                    "avg_latency_ms": round(stats["total_latency"] / calls, 1) if calls else 0.0,
                }
                if stats.get("total_candidates"):
                    entry["total_candidates"] = stats["total_candidates"]
                tools_with_avg[name] = entry

            # by_agent: per-agent breakdown with avg_latency_ms
            by_agent: dict[str, Any] = {}
            for agent_name, agg in agent_agg.items():
                calls = agg["calls"]
                by_agent[agent_name] = {
                    "calls": calls,
                    "errors": agg["errors"],
                    "recent_errors": agg["recent_errors"],
                    "avg_latency_ms": round(agg["total_latency"] / calls, 2) if calls else 0.0,
                }

            emb_total = agg_emb["total_requests"]
            return {
                "active_processes": active_processes,
                "active_agents": active_agents,
                "total_memory_rss_bytes": total_rss,
                "tools": tools_with_avg,
                "embedding": {
                    "total_requests": emb_total,
                    "total_errors": agg_emb["total_errors"],
                    "gpu_busy_errors": agg_emb["gpu_busy_errors"],
                    "unreachable_errors": agg_emb["unreachable_errors"],
                    "recent_errors": agg_emb["recent_errors"],
                    "avg_latency_ms": round(agg_emb["total_latency"] / emb_total, 1)
                    if emb_total
                    else 0.0,
                },
                "by_agent": by_agent,
            }
        except Exception:
            logger.warning("metrics.collect_process_metrics.failed", exc_info=True)
            return {
                "active_processes": 0,
                "active_agents": 0,
                "total_memory_rss_bytes": 0,
                "tools": {},
                "embedding": {
                    "total_requests": 0,
                    "total_errors": 0,
                    "gpu_busy_errors": 0,
                    "unreachable_errors": 0,
                    "recent_errors": 0,
                    "avg_latency_ms": 0.0,
                },
                "by_agent": {},
            }

    async def collect_graph_inventory(self, graph_svc: Any) -> dict[str, Any]:
        """Inventory of the Neo4j graph + drift estimate vs PG source-of-truth.

        Returns a dict with:
          - ``status``: "ok" | "disabled" | "error"
          - ``nodes_total``: {label: count}
          - ``edges_total``: {rel_type: count}
          - ``orphans_total``: {pg_table: max(0, pg_count - neo4j_count)}

        Orphans are a coarse drift signal: a non-zero value means the PG
        table holds entities that should exist in the graph but don't
        (write-through silently dropped, or AutoLinker crashed mid-run).
        Negative deltas (extra Neo4j nodes) bucket as 0.
        """
        if graph_svc is None:
            return {"status": "disabled"}

        nodes_task = graph_svc.count_nodes_by_label()
        edges_task = graph_svc.count_edges_by_type()
        nodes_result: dict[str, int] | BaseException
        edges_result: dict[str, int] | BaseException
        nodes_result, edges_result = await asyncio.gather(
            nodes_task, edges_task, return_exceptions=True
        )
        nodes = nodes_result if isinstance(nodes_result, dict) else {}
        edges = edges_result if isinstance(edges_result, dict) else {}
        graph_status = (
            "error"
            if isinstance(nodes_result, Exception) and isinstance(edges_result, Exception)
            else "ok"
        )
        if isinstance(nodes_result, Exception):
            logger.warning("metrics.graph_inventory.nodes_failed", exc_info=nodes_result)
        if isinstance(edges_result, Exception):
            logger.warning("metrics.graph_inventory.edges_failed", exc_info=edges_result)

        orphans: dict[str, int] = {}
        try:
            async with self._session_factory() as session:
                for table, label in _PG_LABEL_MAP.items():
                    pg_count = (
                        await session.execute(
                            text(
                                f"SELECT COUNT(*) FROM {table} "  # noqa: S608  # nosec B608 - fragment = `table`, a key of the module-level literal dict `_PG_LABEL_MAP` (line 35); `collect_graph_inventory` receives only a `graph_svc` and no table name; exception reviewed on 2026-08-16, to be re-examined before 2026-09-30
                                f"WHERE embedding IS NOT NULL"
                            )
                        )
                    ).scalar() or 0
                    orphans[table] = max(0, pg_count - nodes.get(label, 0))
        except Exception:
            logger.warning("metrics.graph_inventory.pg_orphans_failed", exc_info=True)
            orphans = {}

        return {
            "status": graph_status,
            "nodes_total": nodes,
            "edges_total": edges,
            "orphans_total": orphans,
        }
