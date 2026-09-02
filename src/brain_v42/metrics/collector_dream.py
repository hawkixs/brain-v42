"""Dream-mode metrics collectors for MetricsCollector.

Async, DB-backed read-side collectors that query ``dream_runs`` and
``dream_promotions``. Split out of ``collector.py`` to keep that module
focused on in-memory instrumentation; mixed into ``MetricsCollector`` so
the public API (``collector.collect_dream_*``) is unchanged.

These methods depend only on ``self._session_factory`` and never crash the
sidecar — every query is wrapped to degrade to an empty result on error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

# The two families do not multiply the same way. `promote` and `reorg` run
# inside the loop, hence once per project served. `extract`, `roadmap` and
# `sweep` have no project dimension (global queues and rotations): they run once
# and write the sentinel into dream_runs.project_key.
LOOP_PHASES = ("promote", "reorg")
GLOBAL_PHASES = ("extract", "roadmap", "sweep")


def expected_dream_phases(killswitches_path: Path | None = None) -> set[str]:
    """Return explicitly enabled optional phases from the operator drop-in.

    A missing/unreadable drop-in intentionally returns no expectations: metrics
    stay read-only and must not manufacture failures in local development.
    Core phases are not inferred here because preflight skips are not yet
    recorded as rows; optional phases are enabled only by an explicit operator
    setting and therefore their absence is an actionable partial run.
    """
    from brain_v42.metrics.collector_nightly import (  # noqa: PLC0415
        KILLSWITCHES_PATH,
        parse_killswitches,
    )

    path = killswitches_path or KILLSWITCHES_PATH
    try:
        flags = parse_killswitches(path.read_text())
    except OSError:
        return set()
    return {phase for phase in (*LOOP_PHASES, *GLOBAL_PHASES) if flags.get(phase)}


def expected_dream_phase_pairs(killswitches_path: Path | None = None) -> set[tuple[str, str]]:
    """Expected as ``(phase, project_key)`` pairs — empty if the pool is unknown.

    `expected_dream_phases` compares phase NAMES. With several projects that
    mechanism disarms itself: let one project skip `promote` and the others make
    it "observed", so the alarm no longer rings. It does not break loudly, it
    becomes silently useless — the worse of the two, and exactly the hole the
    PROMOTE crashes of 2026-05-02/05-03 slipped through unnoticed for two days.

    An EMPTY set when the drop-in declares no pool. That is not caution: without
    a declared pool the pair is not computable, and the caller must fall back on
    the by-name comparison, that is, on today's behaviour identically.
    """
    from brain_v42.dream_killswitches import parse_project_pool  # noqa: PLC0415
    from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY  # noqa: PLC0415
    from brain_v42.metrics.collector_nightly import (  # noqa: PLC0415
        KILLSWITCHES_PATH,
        parse_killswitches,
    )

    path = killswitches_path or KILLSWITCHES_PATH
    try:
        content = path.read_text()
    except OSError:
        return set()

    pool = parse_project_pool(content)
    if not pool:
        return set()

    flags = parse_killswitches(content)
    pairs = {(phase, project) for phase in LOOP_PHASES if flags.get(phase) for project in pool}
    pairs |= {(phase, GLOBAL_PHASE_PROJECT_KEY) for phase in GLOBAL_PHASES if flags.get(phase)}
    return pairs


class _DreamCollectorsMixin:
    """Dream-run / promotion read-side collectors mixed into MetricsCollector."""

    # Provided by MetricsCollector.__init__ (declared for type-checkers only).
    if TYPE_CHECKING:
        _session_factory: async_sessionmaker[AsyncSession]

    async def collect_dream_metrics(self) -> dict[str, Any]:
        """Query dream_runs for last run detail + recent history.

        Last-run query: DISTINCT ON (phase) ORDER BY phase, id DESC — picks the
        LAST row per phase so a re-run on the same day (extract fail 06:13 then
        done 10:58) never renders the night as "partial" (spec 2026-07-04 §7).
        Each phase entry gains "dry_run": bool — contrat additif, no existing
        keys renamed or removed.

        History query: status derived from a CTE (latest = DISTINCT ON
        (run_date, phase) → day_status = CASE WHEN all done) so re-runs don't
        inflate fail counts; cost_usd and tokens are SUM over ALL rows because
        the money spent on a failed re-run is real.

        When PROMOTE phase ran, also join dream_promotions to expose the
        outcome (dry_run flag, target_type, target_id, reason) so red-monitor
        can tell a rehearsal from a real materialization at a glance.
        """
        try:
            async with self._session_factory() as session:
                # Last row PER PHASE of the last run_date — a re-run on the
                # same day (extract fail 06:13 then done 10:58) must no longer
                # make the night "partial" (spec 2026-07-04 §7).
                phases_rows = (
                    await session.execute(
                        text("""
                            SELECT DISTINCT ON (phase)
                                   phase, model, status, duration_s,
                                   input_tokens, output_tokens,
                                   cache_read_tokens, cache_creation_tokens,
                                   cost_usd, api_calls, tool_calls, run_date,
                                   error_message, phase_dry_run
                            FROM dream_runs
                            WHERE run_date = (SELECT MAX(run_date) FROM dream_runs)
                            ORDER BY phase, id DESC
                        """)
                    )
                ).all()

                if not phases_rows:
                    return {}

                last_date_obj = phases_rows[0][11]
                last_date = str(last_date_obj)
                phases: dict[str, Any] = {}
                total_cost = 0.0
                total_tokens = 0
                total_duration = 0.0
                ok = 0
                fail = 0
                promote_ran = False

                for row in phases_rows:
                    phase_name = row[0]
                    tokens = (row[4] or 0) + (row[5] or 0)
                    cost = row[8] or 0.0
                    dur = row[3] or 0.0
                    phases[phase_name] = {
                        "status": row[2],
                        "model": row[1],
                        "duration_s": round(dur, 1),
                        "cost_usd": round(cost, 4),
                        "tokens": tokens,
                        "api_calls": row[9] or 0,
                        "tool_calls": row[10] or 0,
                        "error_message": row[12],
                        # CLI phases (extract, roadmap): model:null, cost:0 are
                        # legitimate — do not "fix" them.
                        "dry_run": bool(row[13]),
                    }
                    total_cost += cost
                    total_tokens += tokens
                    total_duration += dur
                    if row[2] == "done":
                        ok += 1
                    else:
                        fail += 1
                    if phase_name == "promote":
                        promote_ran = True

                for phase_name in sorted(expected_dream_phases() - set(phases)):
                    phases[phase_name] = {
                        "status": "partial",
                        "model": None,
                        "duration_s": 0.0,
                        "cost_usd": 0.0,
                        "tokens": 0,
                        "api_calls": 0,
                        "tool_calls": 0,
                        "error_message": "expected enabled phase missing from dream_runs",
                        "dry_run": False,
                    }
                    fail += 1

                # History: status over the deduplicated set (last row per
                # (run_date, phase)), costs/tokens over ALL rows — a failed
                # re-run really did cost money.
                history_rows = (
                    await session.execute(
                        text("""
                            WITH latest AS (
                                SELECT DISTINCT ON (run_date, phase)
                                       run_date, status
                                FROM dream_runs
                                ORDER BY run_date, phase, id DESC
                            ),
                            day_status AS (
                                SELECT run_date,
                                       CASE WHEN COUNT(*) FILTER (WHERE status != 'done') = 0
                                            THEN 'success' ELSE 'partial' END AS status
                                FROM latest
                                GROUP BY run_date
                            )
                            SELECT dr.run_date,
                                   SUM(dr.cost_usd) AS cost,
                                   SUM(dr.input_tokens + dr.output_tokens) AS tokens,
                                   ds.status
                            FROM dream_runs dr
                            JOIN day_status ds ON ds.run_date = dr.run_date
                            GROUP BY dr.run_date, ds.status
                            ORDER BY dr.run_date DESC
                            LIMIT 10
                        """)
                    )
                ).all()

                history = [
                    {
                        "date": str(r[0]),
                        "cost_usd": round(float(r[1] or 0), 4),
                        "tokens": int(r[2] or 0),
                        "status": r[3],
                    }
                    for r in history_rows
                ]

                last_run: dict[str, Any] = {
                    "date": last_date,
                    "status": "success" if fail == 0 else "partial",
                    "phases_ok": ok,
                    "phases_fail": fail,
                    "total_cost_usd": round(total_cost, 4),
                    "total_tokens": total_tokens,
                    "total_duration_s": round(total_duration, 1),
                    "phases": phases,
                }

                # PROMOTE outcome: join the promote dream_run to its audit row
                # so red-monitor can badge "dry_run" vs "adr"/"runbook"/"skipped".
                # `first()` returns None if no promote row, preserving the
                # "absent when no promote phase" contract.
                promote_row = (
                    await session.execute(
                        text("""
                            SELECT dp.target_type,
                                   COALESCE(dp.target_adr_id, dp.target_runbook_id) AS target_id,
                                   dp.skipped_reason
                            FROM dream_promotions dp
                            JOIN dream_runs dr ON dp.dream_run_id = dr.id
                            WHERE dr.phase = 'promote' AND dr.run_date = :d
                            ORDER BY dp.id DESC LIMIT 1
                        """),
                        {"d": last_date_obj},
                    )
                ).first()

                if promote_ran and promote_row is not None:
                    target_type = promote_row[0]
                    last_run["promote_outcome"] = {
                        "dry_run": target_type == "dry_run",
                        "target_type": target_type,
                        "target_id": str(promote_row[1]) if promote_row[1] else None,
                        "reason": promote_row[2],
                    }

                return {"last_run": last_run, "history": history}
        except Exception:
            logger.warning("metrics.collect_dream_metrics.failed", exc_info=True)
            return {}

    async def collect_dream_promoted_health(self) -> list[dict[str, Any]]:
        """Per-target post-promotion health for auto-promoted ADRs and runbooks.

        ADR #4 v2 telemetry: the SQL second-gate prevents bad promotions; this
        join detects them after the fact. For each materialized promotion
        (target_adr_id IS NOT NULL or target_runbook_id IS NOT NULL), surfaces
        signals that distinguish a healthy auto-promotion (used, not
        superseded) from a regrettable one (ignored, superseded shortly after).

        Surfaced via the JSON `/dream` endpoint (per-target cardinality is
        unbounded; deliberately NOT a Prometheus labelled gauge).

        Returns one dict per row:
            target_type: 'adr' | 'runbook'
            target_id, title
            status: 'accepted' | 'superseded' | 'deprecated' | None (runbooks)
            superseded: bool (False for runbooks)
            access_count
            days_since_promotion
            days_since_last_access: None when never accessed
            promoted_at: ISO timestamp

        Sorted by promoted_at DESC (newest first). Empty list on DB error.
        """
        try:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT 'adr' AS target_type,
                                   a.id AS target_id,
                                   a.title,
                                   a.status,
                                   (a.superseded_by IS NOT NULL) AS superseded,
                                   a.access_count,
                                   a.last_accessed_at,
                                   dp.created_at AS promoted_at
                            FROM dream_promotions dp
                            JOIN adrs a ON dp.target_adr_id = a.id
                            WHERE dp.target_type = 'adr'
                              AND dp.target_adr_id IS NOT NULL
                            UNION ALL
                            SELECT 'runbook' AS target_type,
                                   r.id AS target_id,
                                   r.title,
                                   NULL AS status,
                                   FALSE AS superseded,
                                   r.access_count,
                                   r.last_accessed_at,
                                   dp.created_at AS promoted_at
                            FROM dream_promotions dp
                            JOIN runbooks r ON dp.target_runbook_id = r.id
                            WHERE dp.target_type = 'runbook'
                              AND dp.target_runbook_id IS NOT NULL
                            ORDER BY promoted_at DESC
                            """
                        )
                    )
                ).all()

                now = datetime.now(UTC)
                return [
                    {
                        "target_type": row[0],
                        "target_id": str(row[1]),
                        "title": row[2],
                        "status": row[3],
                        "superseded": bool(row[4]),
                        "access_count": int(row[5] or 0),
                        "days_since_promotion": (now - row[7]).total_seconds() / 86400,
                        "days_since_last_access": (
                            (now - row[6]).total_seconds() / 86400 if row[6] is not None else None
                        ),
                        "promoted_at": row[7].isoformat(),
                    }
                    for row in rows
                ]
        except Exception:
            logger.warning("metrics.collect_dream_promoted_health.failed", exc_info=True)
            return []

    async def collect_dream_promotions(self) -> dict[str, int]:
        """Return current dream_promotions counts grouped by target_type.

        The sidecar's flush exports this as a labelled Prometheus counter
        `brain_dream_promotions_total{target_type=...}`. Pull-based: we
        re-read absolute counts on every flush; Prometheus derives rates
        via `rate()/increase()` across scrapes. Reliable enough because
        `dream_promotions` is a durable audit table — no need for write-
        side instrumentation.

        Returns an empty dict if the table is empty or the query fails
        (metrics never crash the sidecar).
        """
        try:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT target_type, COUNT(*) AS n"
                            " FROM dream_promotions"
                            " GROUP BY target_type"
                        )
                    )
                ).all()
                return {row[0]: int(row[1]) for row in rows}
        except Exception:
            logger.warning("metrics.collect_dream_promotions.failed", exc_info=True)
            return {}
