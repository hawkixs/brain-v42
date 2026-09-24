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

``collect_graph_inventory`` -- the block ``SlowBlockCache`` memoizes as
"graph_inventory" -- is the one exception to "degrades and returns": when the
Neo4j counts or the PG orphan scan fail, it still assembles the same degraded
dict it always has, but raises it via ``CollectorDegraded`` instead of
returning it plainly, so the cache retries under the short
``error_ttl_seconds`` rather than the full TTL (slow_block_cache.py).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

from brain_v42.config import get_settings
from brain_v42.metrics.retention import (
    PROCESS_METRICS_FRESH_SQL,
    PROCESS_METRICS_IS_LIVE_SQL,
)
from brain_v42.metrics.slow_block_cache import CollectorDegraded

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

# Pseudo-tools that are DB-WIDE GAUGES, not per-process counters: summing them
# across live processes would multiply a single count by the process count.
# Ticket 04c09575 — `_decay` (stale_count/archived_count/access_log_size) was
# folded through the generic calls/errors/total_latency SUM reducer below,
# so every field read back at its `.get(..., 0)` default and the payload's
# `decay` block was always zero in production. These names are pulled out of
# `tool_stats` BEFORE that generic loop runs, and reduced by "latest row
# wins" (by `updated_at`) instead of by summing.
_GAUGE_PSEUDO_TOOLS: frozenset[str] = frozenset({"_decay"})

# Structural zeros: a missing _decay row (fresh deploy, or the collect_process_metrics
# except-branch) still returns a shaped decay block, per the "zero on a source that
# counts nothing says nothing" convention used elsewhere in this module.
_DECAY_ZERO: dict[str, int] = {"stale_count": 0, "archived_count": 0, "access_log_size": 0}


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
        *,
        fts_fallback: bool = False,
    ) -> None:
        """INSERT a row into search_log and update the in-memory latency histogram.

        The in-memory call (``record_search_latency``) is made BEFORE the DB
        write so the percentile ring-buffer is populated even when the DB write
        fails.  ``retrieval_percentiles`` then returns real values instead of
        the structural zeros that result from having zero callers.

        ``embedding_model`` is read from ``get_settings()`` rather than taken
        as a parameter: the caller has no reason to know which model is
        configured, and threading it through every call site would let it
        drift from the live identity (ticket 4fac067a, decision 1669d429).
        Only the model NAME is read — never ``embedding_api_key``, the
        ``SecretStr`` sitting right next to it in ``Settings``.

        ``fts_fallback`` is set by the caller when NO embedding model served
        the search it is logging (``brain_tools._no_embedding_model_served``):
        an unresolved ``project_group``, answered before any embedding call,
        or a search that ran in ``search_mode == "fts_fallback"`` (the
        embedding service was down
        and ``brain_service.py`` served the search from FTS alone, per
        ``SearchResponse.degraded`` / ``WhatDoIKnowResponse.degraded``). NO
        embedding model produced those results, so the row must say ``NULL``
        rather than whatever model happens to be configured that day — that
        model played no part in serving this particular search.

        The settings read sits INSIDE the ``try`` below, next to the DB write:
        this method is documented to never raise (a metrics failure must never
        break a search), and a broken settings read is exactly the kind of
        failure it must swallow, the same as a DB failure.
        """
        # Update in-memory histogram first — independent of DB/settings availability.
        self.record_search_latency(latency_ms)  # type: ignore[attr-defined]

        try:
            embedding_model = None if fts_fallback else get_settings().embedding_model
            async with self._session_factory() as session:
                await session.execute(
                    text(
                        "INSERT INTO search_log "
                        "(tool_name, project_key, result_count, top_score, avg_score, "
                        "latency_ms, embedding_model) "
                        "VALUES (:tool, :pk, :cnt, :top, :avg, :lat, :model)"
                    ),
                    {
                        "tool": tool_name,
                        "pk": project_key,
                        "cnt": result_count,
                        "top": top_score,
                        "avg": avg_score,
                        "lat": latency_ms,
                        "model": embedding_model,
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
                _process are disjoint, so no ×N). Gauge pseudo-tools (``_GAUGE_PSEUDO_TOOLS``,
                e.g. ``_decay``) never appear here — they are reduced separately, below.
            embedding: embedding_stats from _process row(s) only (never from real-agent
                rows which carry empty dicts per Task 3.2).
            by_agent: per real-agent breakdown {calls, errors, recent_errors, avg_latency_ms}
                aggregated from that agent's tool_stats; excludes _process.
            decay: the LATEST flushed ``_decay`` row's ``{stale_count, archived_count,
                access_log_size}`` (by ``updated_at``), never a sum — these are DB-wide
                gauges, and summing across live processes would multiply them by the
                process count (04c09575). Structural zeros when no row carries one.
            embedding_identity: ``{model, backend, endpoint_host, models_seen}``
                distilled from every LIVE (``is_live``, the 60s window) ``_process``
                row's ``embedding_stats.identity`` (ticket 3a4ed612). One distinct
                live model reports it plainly; more than one reports ``"mixed"``
                (independently per field) plus the full ``models_seen`` list.
                ``None`` when no live row carries an identity at all — server.py's
                cue to fall back to the sidecar's own settings instead.
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
            agg_emb: dict[str, Any] = {
                "total_requests": 0,
                "total_errors": 0,
                "gpu_busy_errors": 0,
                "unreachable_errors": 0,
                "recent_errors": 0,
                "total_latency": 0.0,
                "usage": {
                    "read": {"total_tokens": 0, "reported_requests": 0},
                    "write": {"total_tokens": 0, "reported_requests": 0},
                },
            }
            total_rss = 0

            # Per-agent accumulators: {agent_name: {calls, errors, recent_errors, total_latency}}
            agent_agg: dict[str, dict[str, Any]] = {}

            # Gauge pseudo-tools: latest-row-wins, tracked independently of agg_tools
            # so the generic SUM loop below never sees them (04c09575).
            gauge_latest: dict[str, dict[str, Any]] = {}
            gauge_latest_updated_at: dict[str, Any] = {}

            # Embedding identity (ticket 3a4ed612): distinct {model, backend, host}
            # seen among LIVE _process rows only -- same 60s window active_processes
            # already gates on (row[7]), not the wider 1h retention window every row
            # above is read from. A silent process from 10 minutes ago must not keep
            # reporting a model nobody is serving with any more.
            live_models: set[str] = set()
            live_backends: set[str] = set()
            live_hosts: set[str] = set()

            for row in rows:
                agent_name = row[0]
                updated_at = row[3]
                tool_stats = row[4]  # JSONB → dict
                emb_stats = row[5]
                rss = row[6]
                is_live = row[7]

                # Aggregate tools across ALL rows (real tools + pseudo-tools are disjoint)
                for name, stats in tool_stats.items():
                    if name in _GAUGE_PSEUDO_TOOLS:
                        # Latest-row-wins (by updated_at), split out BEFORE the
                        # generic sum below: _decay's fields (stale_count/
                        # archived_count/access_log_size) have no calls/errors/
                        # total_latency keys, so folding it through the sum
                        # reducer silently zeroed it out (04c09575).
                        last_seen = gauge_latest_updated_at.get(name)
                        if last_seen is None or (
                            updated_at is not None and updated_at >= last_seen
                        ):
                            gauge_latest[name] = stats
                            gauge_latest_updated_at[name] = updated_at
                        continue
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
                    # A malformed usage value costs its own row, not the whole
                    # aggregate: the enclosing fallback would blank tools and agents.
                    usage_by_intent = emb_stats.get("usage")
                    if not isinstance(usage_by_intent, dict):
                        usage_by_intent = {}
                    for intent in ("read", "write"):
                        usage = usage_by_intent.get(intent)
                        if not isinstance(usage, dict):
                            continue
                        agg_emb["usage"][intent]["total_tokens"] += usage.get("total_tokens", 0)
                        agg_emb["usage"][intent]["reported_requests"] += usage.get(
                            "reported_requests", 0
                        )
                    total_rss += rss
                    # A malformed identity value costs its own row, same doctrine
                    # as usage above -- never the whole aggregate.
                    if is_live:
                        identity = emb_stats.get("identity")
                        if isinstance(identity, dict):
                            model = identity.get("model")
                            backend = identity.get("backend")
                            host = identity.get("host")
                            if isinstance(model, str) and model:
                                live_models.add(model)
                            if isinstance(backend, str) and backend:
                                live_backends.add(backend)
                            if isinstance(host, str) and host:
                                live_hosts.add(host)
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

            # None means "no live process reported an identity" — a fresh deploy,
            # or every live row still pre-dates this feature. server.py reads that
            # as its cue to fall back to the sidecar's OWN settings instead.
            embedding_identity: dict[str, Any] | None = None
            if live_models:
                embedding_identity = {
                    "model": next(iter(live_models)) if len(live_models) == 1 else "mixed",
                    "backend": next(iter(live_backends)) if len(live_backends) == 1 else "mixed",
                    "endpoint_host": next(iter(live_hosts)) if len(live_hosts) == 1 else "mixed",
                    "models_seen": sorted(live_models),
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
                    "usage": agg_emb["usage"],
                },
                "by_agent": by_agent,
                # dict(...) always: gauge_latest.get(name, _DECAY_ZERO) would otherwise
                # hand back the shared module-level singleton on a miss, and a caller
                # mutating it (server.py builds the response dict on top of this) would
                # corrupt the structural-zero default for every later scrape.
                "decay": dict(gauge_latest.get("_decay", _DECAY_ZERO)),
                "embedding_identity": embedding_identity,
            }
        except Exception:
            logger.warning("metrics.collect_process_metrics.failed", exc_info=True)
            return {
                "active_processes": 0,
                "active_agents": 0,
                "total_memory_rss_bytes": 0,
                "tools": {},
                "decay": dict(_DECAY_ZERO),
                "embedding_identity": None,
                "embedding": {
                    "total_requests": 0,
                    "total_errors": 0,
                    "gpu_busy_errors": 0,
                    "unreachable_errors": 0,
                    "recent_errors": 0,
                    "avg_latency_ms": 0.0,
                    "usage": {
                        "read": {"total_tokens": 0, "reported_requests": 0},
                        "write": {"total_tokens": 0, "reported_requests": 0},
                    },
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
        nodes_failed = isinstance(nodes_result, Exception)
        edges_failed = isinstance(edges_result, Exception)
        # The published status keeps its meaning (both queries down); the cache
        # signal below reacts to either one failing.
        graph_status = "error" if nodes_failed and edges_failed else "ok"
        if isinstance(nodes_result, Exception):
            logger.warning("metrics.graph_inventory.nodes_failed", exc_info=nodes_result)
        if isinstance(edges_result, Exception):
            logger.warning("metrics.graph_inventory.edges_failed", exc_info=edges_result)

        orphans: dict[str, int] = {}
        orphans_failed = False
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
            orphans_failed = True

        result = {
            "status": graph_status,
            "nodes_total": nodes,
            "edges_total": edges,
            "orphans_total": orphans,
        }
        # Three independent failure points (the Neo4j node count, the Neo4j edge
        # count, the PG orphan scan) share one signal to the cache: any of them
        # degrades the block below "fully fresh", so it gets the short
        # error_ttl_seconds instead of the full TTL (slow_block_cache.py) --
        # even when graph_status stays "ok" because only one side failed.
        if nodes_failed or edges_failed or orphans_failed:
            raise CollectorDegraded(result)
        return result
