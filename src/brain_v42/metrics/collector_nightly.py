"""Nightly-ops collectors for MetricsCollector — section `nightly` du sidecar.

Réplique le check matinal pour le panel red-monitor (ticket de1ad785) :
killswitches du drop-in systemd, proposals roadmap en attente de review
(dont les merges retenus par le juge — 39fc6a9 : ils restent 'proposed'),
extract en attente, dernier échec dream.

Pattern collector_dream : ne crashe JAMAIS le sidecar — chaque bloc
(fichier killswitches, DB) dégrade indépendamment ; si tout échoue la
méthode rend {} et le server omet la section.
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


class _NightlyCollectorsMixin:
    """Nightly-ops read-side collectors mixed into MetricsCollector."""

    # Provided by MetricsCollector.__init__ (declared for type-checkers only).
    if TYPE_CHECKING:
        _session_factory: async_sessionmaker[AsyncSession]

    async def collect_nightly_ops(self, killswitches_path: Path | None = None) -> dict[str, Any]:
        """Section `nightly` du payload /metrics — {} si tout est indisponible."""
        result: dict[str, Any] = {}

        # ── killswitches (fichier local, hors DB) ────────────────────────
        ks_path = killswitches_path or KILLSWITCHES_PATH
        killswitches: dict[str, bool] | None
        try:
            killswitches = parse_killswitches(ks_path.read_text())
        except OSError:
            logger.warning("metrics.nightly.killswitches_unreadable", path=str(ks_path))
            killswitches = None

        # ── DB : roadmap / extract / last failure ────────────────────────
        # L'assemblage vit DANS le try : une row de forme inattendue dégrade
        # comme une erreur SQL (le sidecar ne crashe jamais).
        try:
            async with self._session_factory() as session:
                status_rows = (
                    await session.execute(
                        text(
                            "SELECT status, COUNT(*) FROM roadmap_curation_proposals "
                            "GROUP BY status"
                        )
                    )
                ).all()
                applied_24h = (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM roadmap_curation_proposals "
                            "WHERE status = 'applied' "
                            "AND applied_at >= NOW() - INTERVAL '24 hours'"
                        )
                    )
                ).scalar_one()
                extract_pending = (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM ticket_extraction_proposals "
                            "WHERE status = 'proposed'"
                        )
                    )
                ).scalar_one()
                failure_row = (
                    await session.execute(
                        text(
                            "SELECT run_date, phase, error_message, created_at "
                            "FROM dream_runs WHERE status = 'fail' "
                            "ORDER BY created_at DESC LIMIT 1"
                        )
                    )
                ).first()

            by_status: dict[str, int] = {row[0]: row[1] for row in status_rows}
            result["killswitches"] = killswitches
            result["roadmap"] = {
                "proposed_pending": by_status.get("proposed", 0),
                "applied_total": by_status.get("applied", 0),
                "applied_24h": int(applied_24h),
                "rejected_total": by_status.get("rejected", 0),
            }
            result["extract"] = {"proposed_pending": int(extract_pending)}
            if failure_row is not None:
                run_date, phase, error_message, created_at = failure_row
                result["last_failure"] = {
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
