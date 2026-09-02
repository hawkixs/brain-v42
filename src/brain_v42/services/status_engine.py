"""StatusEngine — monotonic status heuristic for features."""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class StatusEngine:
    """Compute feature status from signal types. Pure logic, no I/O."""

    STATUS_ORDER: list[str] = [
        "planned",
        "research",
        "design",
        "building",
        "deployed",
        "done",
    ]

    SIGNAL_STATUS_MAP: dict[str, str | None] = {
        "learning": "research",
        "decision": "research",
        "snippet": "research",
        "runbook": "design",
        "adr": "design",
        "plan": "design",
        "mr_opened": "building",
        "push": None,
        "mr_merged": "deployed",
        "pipeline_success": "deployed",
        "pipeline_failure": None,
    }

    def compute_status(self, current_status: str, signal_type: str, pinned: bool) -> str:
        """Return the new status. Never goes backward. Respects pinned.

        Statuses outside STATUS_ORDER (e.g. 'archived', 'legacy') are
        returned unchanged — they are off-scale and must never be degraded
        or promoted by incoming signals.
        """
        if pinned:
            return current_status

        # Off-scale status (archived, legacy): never demoted/promoted by signals.
        if current_status not in self.STATUS_ORDER:
            return current_status

        proposed = self.SIGNAL_STATUS_MAP.get(signal_type)
        if proposed is None:
            return current_status

        current_idx = self.STATUS_ORDER.index(current_status)
        proposed_idx = self.STATUS_ORDER.index(proposed)
        if proposed_idx > current_idx:
            return proposed
        return current_status
