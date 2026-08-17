"""Cross-project briefing service — Spec C MVP β.

Combines Neo4j domain topology (which domains is this project active in,
which entities from OTHER projects share them) with PG display briefs.
Read-only. Neo4j faults degrade to "no section" via GraphService's
fault-tolerant _run_read; PG faults propagate to the caller, which is
expected to catch and omit the section (session_tools graceful-degrade).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
import structlog

logger = structlog.get_logger(__name__)

_DISPLAY_TRUNCATE = 60

# label → (table, display column). Keep in sync with db/tables.py short fields.
_BRIEF_SQL: dict[str, sa.TextClause] = {
    "Decision": sa.text(
        "SELECT id, title AS display, created_at FROM decisions WHERE id = ANY(CAST(:ids AS uuid[]))"
    ),
    "Learning": sa.text(
        "SELECT id, topic AS display, created_at FROM learnings WHERE id = ANY(CAST(:ids AS uuid[]))"
    ),
    "Snippet": sa.text(
        "SELECT id, title AS display, created_at FROM snippets WHERE id = ANY(CAST(:ids AS uuid[]))"
    ),
    "Runbook": sa.text(
        "SELECT id, title AS display, created_at FROM runbooks WHERE id = ANY(CAST(:ids AS uuid[]))"
    ),
    "ADR": sa.text(
        "SELECT id, title AS display, created_at FROM adrs WHERE id = ANY(CAST(:ids AS uuid[]))"
    ),
}


@dataclass(frozen=True)
class CrossEntry:
    project_key: str
    entity_type: str
    display: str
    created_at: datetime | None


@dataclass(frozen=True)
class CrossProjectBlock:
    domains: list[str]
    entries: list[CrossEntry]


class CrossProjectBriefingService:
    # Convention: `self._sf` mirrors dream_run_service.py.
    def __init__(
        self,
        session_factory: Any,
        graph: Any,
        *,
        top_n: int = 2,
        entries_max: int = 5,
    ) -> None:
        self._sf = session_factory
        self._graph = graph
        self._top_n = top_n
        self._entries_max = entries_max

    async def fetch_block(self, project_key: str) -> CrossProjectBlock | None:
        """Top cross-project entries for the briefing, or None (section omitted)."""
        domains = await self._graph.fetch_active_domains(project_key, self._top_n)
        if not domains:
            return None
        candidates = await self._graph.fetch_cross_project_entity_ids(
            domains, exclude_project_key=project_key
        )
        # label -> {entity_id: source_project}; unknown labels (Feature, Plan...) skipped
        by_label: dict[str, dict[str, str]] = {}
        for row in candidates:
            label = next((lb for lb in row.get("labels", []) if lb in _BRIEF_SQL), None)
            if label is None:
                continue
            by_label.setdefault(label, {})[str(row["id"])] = row["project_key"]
        if not by_label:
            return None

        entries: list[CrossEntry] = []
        async with self._sf() as session:
            for label, id_to_project in by_label.items():
                result = await session.execute(
                    _BRIEF_SQL[label], {"ids": list(id_to_project.keys())}
                )
                for r in result.mappings().all():
                    display = r["display"] or ""
                    if len(display) > _DISPLAY_TRUNCATE:
                        display = display[:_DISPLAY_TRUNCATE] + "…"
                    entries.append(
                        CrossEntry(
                            project_key=id_to_project[str(r["id"])],
                            entity_type=label,
                            display=display,
                            created_at=r["created_at"],
                        )
                    )
        if not entries:
            return None
        entries.sort(key=lambda e: (e.created_at is not None, e.created_at), reverse=True)
        return CrossProjectBlock(domains=domains, entries=entries[: self._entries_max])
