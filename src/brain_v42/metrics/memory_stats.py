"""Memory-stats DB helper for the /api/cockpit sidecar endpoint.

Derives `memory.*` fields from PostgreSQL system catalogs + consolidation_log.
Fully degraded: any SQL failure returns nulls (never raises).
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.metrics.slow_block_cache import BlockHealth

logger = structlog.get_logger(__name__)

# Episodic = short-term event log (decisions/learnings).
# Semantic = long-term compacted knowledge (snippets/runbooks/adrs + chunks).
_EPISODIC_TABLES = ("decisions", "learnings")
_SEMANTIC_TABLES = ("snippets", "runbooks", "adrs", "indexed_plans")

_NULL_STATS: dict[str, Any] = {
    "episodes": None,
    "episodic_mb": None,
    "semantic_chunks": None,
    "semantic_mb": None,
    "lastCompaction": None,
    "freedLastCompaction": None,
    "vectorIndex": None,
}

_HNSW_PARAM_RE = re.compile(
    r"m\s*=\s*['\"]?(\d+)['\"]?.*?ef_construction\s*=\s*['\"]?(\d+)['\"]?",
    re.IGNORECASE,
)


async def collect_memory_stats(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    health: BlockHealth | None = None,
) -> dict[str, Any]:
    """Return the `memory` block of the cockpit payload.

    Failures keep every key with null values, so unavailable counts cannot
    look empty. An optional tracker lets the handler retain the last success.
    """
    # Table names are hardcoded module constants — inline safely into the SQL
    # to avoid SQLAlchemy bind-expansion surprises on tuple params.
    ep_inlist = ", ".join(f"'{t}'" for t in _EPISODIC_TABLES)
    se_inlist = ", ".join(f"'{t}'" for t in _SEMANTIC_TABLES)
    try:
        async with session_factory() as session:
            episodic_row = (
                await session.execute(
                    text(
                        "SELECT "  # nosec B608 # node spans 56-58; its sole interpolation is {t} from _EPISODIC_TABLES (line 20), a module constant of string literals (audited 2026-08-16)
                        "(SELECT COALESCE(SUM(n), 0) FROM (VALUES "
                        + ", ".join(f"((SELECT COUNT(*) FROM {t}))" for t in _EPISODIC_TABLES)  # nosec B608 # {t} iterates _EPISODIC_TABLES (line 20), a module-level tuple of string literals; collect_memory_stats has no name parameter (audited 2026-08-16)
                        + ") AS v(n)), "  # nosec B608 # {ep_inlist} is built line 49 by quoting the _EPISODIC_TABLES literals, no caller input (audited 2026-08-16)
                        "(SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0) "
                        f" FROM pg_class c WHERE c.relname IN ({ep_inlist}))"  # noqa: S608
                    )
                )
            ).one()
            semantic_row = (
                await session.execute(
                    text(
                        "SELECT "  # nosec B608 # node spans 68-70; its sole interpolation is {t} from _SEMANTIC_TABLES (line 21), a module constant of string literals (audited 2026-08-16)
                        "(SELECT COALESCE(SUM(n), 0) FROM (VALUES "
                        + ", ".join(f"((SELECT COUNT(*) FROM {t}))" for t in _SEMANTIC_TABLES)  # nosec B608 # {t} iterates _SEMANTIC_TABLES (line 21), a module-level tuple of string literals; collect_memory_stats has no name parameter (audited 2026-08-16)
                        + ") AS v(n)), "  # nosec B608 # {se_inlist} is built line 50 by quoting the _SEMANTIC_TABLES literals, no caller input (audited 2026-08-16)
                        "(SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0) "
                        f" FROM pg_class c WHERE c.relname IN ({se_inlist}))"  # noqa: S608
                    )
                )
            ).one()
            compaction_row = (
                await session.execute(
                    text(
                        "SELECT MAX(created_at), "
                        "COUNT(*) FILTER ("
                        "  WHERE created_at >= ("
                        "    SELECT MAX(created_at) - INTERVAL '5 minutes' FROM consolidation_log"
                        "  )"
                        ") "
                        "FROM consolidation_log"
                    )
                )
            ).one()
            index_def = (
                await session.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE tablename = 'decisions' AND indexdef ILIKE '%hnsw%' "
                        "LIMIT 1"
                    )
                )
            ).scalar()
        last_compaction_ts = compaction_row[0]
        last_compaction_str: str | None = (
            last_compaction_ts.strftime("%H:%M:%S") if last_compaction_ts is not None else None
        )

        vector_index_str: str | None = None
        if index_def:
            match = _HNSW_PARAM_RE.search(index_def)
            if match:
                vector_index_str = f"hnsw (m={match.group(1)}, ef={match.group(2)})"

        result = {
            "episodes": int(episodic_row[0] or 0),
            "episodic_mb": int((episodic_row[1] or 0) // (1024 * 1024)),
            "semantic_chunks": int(semantic_row[0] or 0),
            "semantic_mb": int((semantic_row[1] or 0) // (1024 * 1024)),
            "lastCompaction": last_compaction_str,
            "freedLastCompaction": int(compaction_row[1] or 0),
            "vectorIndex": vector_index_str,
        }
        if health is not None:
            health.record("memory", "ok")
        return result
    except Exception as exc:
        logger.warning("metrics.collect_memory_stats.failed", exc_info=True)
        if health is not None:
            health.record("memory", "degraded", type(exc).__name__)
        return dict(_NULL_STATS)
