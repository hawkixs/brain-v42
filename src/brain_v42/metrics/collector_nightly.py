"""Nightly-ops collectors for MetricsCollector — the sidecar's `nightly` section.

Replicates the morning check for the red-monitor panel (ticket de1ad785):
killswitches from the systemd drop-in, pending extract (scoped to this
sidecar's own project), last dream failure (pool-wide, with attribution).

The `roadmap` block (`proposed_pending`/`applied_24h`/`applied_total`/
`rejected_total`) was REMOVED here (ticket 69949ffc thread, 2026-09-23):
red-monitor no longer reads it, and `roadmap_curation_proposals` carries no
project column to scope it by (unlike `ticket_extraction_proposals`, which
has `target_project`). This does not touch the Dream `roadmap` phase, its
curation tables, or the graph `roadmap` group — those are governed separately
by decision 11dbb4a1.

`last_failure` is deliberately POOL-WIDE: Dream runs a pool of ~10 projects
against this one `dream_runs` table, and a reader needs to see any project's
recent fail/timeout, not just this sidecar's own. What changed (ticket
69949ffc) is ATTRIBUTION — the row now carries its own `project_key`, plus a
7-day window and only `fail`/`timeout` statuses, excluding rows whose
project_key is NULL (written before migration 042) or blank (the second,
unfixed dream_runs writer — ticket 7336a2d5) — so a reader is never left
guessing, or worse, assuming a failure belongs to brain-v42 when it does not.

collector_dream pattern: NEVER crashes the sidecar — each block (killswitch
file, DB) degrades independently; if everything fails the method returns {} and
the server omits the section.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

from brain_v42.dream_killswitches import KILLSWITCHES_PATH, parse_killswitches

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

#: This deployment's own project — the sidecar has one instance, serving one
#: project's cockpit page, even though the pool it observes (dream_runs,
#: ticket_extraction_proposals) spans ~10 projects (ticket 69949ffc).
_SIDECAR_PROJECT_KEY = "brain-v42"


class _NightlyCollectorsMixin:
    """Nightly-ops read-side collectors mixed into MetricsCollector."""

    # Provided by MetricsCollector.__init__ (declared for type-checkers only).
    if TYPE_CHECKING:
        _session_factory: async_sessionmaker[AsyncSession]

    async def collect_nightly_ops(
        self,
        killswitches_path: Path | None = None,
        project_key: str = _SIDECAR_PROJECT_KEY,
    ) -> dict[str, Any]:
        """The /metrics payload's `nightly` section — {} if all is unavailable."""
        result: dict[str, Any] = {}

        # ── killswitches (fichier local, hors DB) ────────────────────────
        ks_path = killswitches_path or KILLSWITCHES_PATH
        killswitches: dict[str, bool] | None
        try:
            killswitches = parse_killswitches(ks_path.read_text())
        except OSError:
            logger.warning("metrics.nightly.killswitches_unreadable", path=str(ks_path))
            killswitches = None

        # ── DB : extract / last failure ───────────────────────────────────
        # Assembly lives INSIDE the try: a row of unexpected shape degrades
        # like a SQL error (the sidecar never crashes).
        try:
            async with self._session_factory() as session:
                extract_pending = (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM ticket_extraction_proposals "
                            "WHERE status = 'proposed' AND target_project = :project_key"
                        ),
                        {"project_key": project_key},
                    )
                ).scalar_one()
                failure_row = (
                    await session.execute(
                        text(
                            "SELECT project_key, run_date, phase, error_message, created_at "
                            "FROM dream_runs "
                            "WHERE status IN ('fail', 'timeout') "
                            "AND created_at >= NOW() - INTERVAL '7 days' "
                            "AND project_key IS NOT NULL "
                            "AND project_key <> '' "
                            "ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                ).first()

            result["killswitches"] = killswitches
            result["extract"] = {"proposed_pending": int(extract_pending)}
            if failure_row is not None:
                row_project_key, run_date, phase, error_message, created_at = failure_row
                result["last_failure"] = {
                    "project_key": row_project_key,
                    "run_date": str(run_date),
                    "phase": phase,
                    "error": error_message,
                    "created_at": created_at.isoformat() if created_at else None,
                }
            else:
                result["last_failure"] = None
        except Exception:
            logger.warning("metrics.collect_nightly_ops.db_failed", exc_info=True)
            if killswitches is None:
                return {}
            return {"killswitches": killswitches}
        return result
