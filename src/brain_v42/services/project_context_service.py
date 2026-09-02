"""Service layer for ProjectContext — PG-only, no embedding.

ProjectContext is a configuration/state record accessed exclusively by
project_key. No semantic embedding is involved — this service is the
thinnest in the codebase.
"""

from __future__ import annotations

from typing import Any

import structlog

from brain_v42.models.project_context import ProjectContext, ProjectContextCreate
from brain_v42.repositories.pg_project_context import PgProjectContextRepo

logger = structlog.get_logger(__name__)


class ProjectContextService:
    """Business logic for project contexts.

    Wraps PgProjectContextRepo and exposes:
    - get_or_create(data) → ProjectContext
    - get_by_key(project_key) → ProjectContext | None
    - update_focus(project_key, focus, blockers) → ProjectContext | None
    - refresh_counts(project_key) → ProjectContext | None

    No embedding is computed — ProjectContext is configuration/state,
    not a knowledge artifact.
    """

    def __init__(self, pg_repo: PgProjectContextRepo, graph: Any | None = None) -> None:
        self.repo = pg_repo
        self._graph = graph

    async def get_or_create(self, data: ProjectContextCreate) -> ProjectContext:
        """Return existing context by project_key, or create a new one (upsert)."""
        logger.debug("project_context_service.get_or_create", project_key=data.project_key)
        result = await self.repo.get_or_create(data)
        if self._graph:
            try:
                await self._graph.upsert_project(data.project_key, result.id, data.name)
            except Exception:
                logger.error("graph_write_failed", entity_id=str(result.id), exc_info=True)
        return result

    async def get_by_key(self, project_key: str) -> ProjectContext | None:
        """Fetch project context by project_key. Returns None if not found."""
        logger.debug("project_context_service.get_by_key", project_key=project_key)
        return await self.repo.get_by_key(project_key)

    async def update_focus(
        self,
        project_key: str,
        focus: str,
        blockers: list[str] | None = None,
    ) -> ProjectContext | None:
        """Update current_focus and optionally blockers. Returns None if project not found."""
        logger.debug("project_context_service.update_focus", project_key=project_key)
        return await self.repo.update_focus(project_key, focus, blockers)

    async def list_all(self, project_group: str | None = None) -> list[ProjectContext]:
        """Return all project contexts, optionally filtered by group."""
        logger.debug("project_context_service.list_all", project_group=project_group)
        return await self.repo.list_all(project_group=project_group)

    async def get_keys_by_group(self, project_group: str) -> list[str]:
        """Return all project_keys that belong to the given group."""
        logger.debug("project_context_service.get_keys_by_group", project_group=project_group)
        return await self.repo.get_keys_by_group(project_group)

    async def list_groups(self) -> list[dict]:
        """Return distinct groups with project count."""
        logger.debug("project_context_service.list_groups")
        return await self.repo.list_groups()

    async def focus_history(
        self, project_key: str, *, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        """The audit trail of one project's focus, newest revision first."""
        logger.debug("project_context_service.focus_history", project_key=project_key)
        return await self.repo.focus_history(project_key, limit=limit, offset=offset)

    async def refresh_counts(self, project_key: str) -> ProjectContext | None:
        """Recompute *_count columns via cross-table COUNT queries. Returns None if not found."""
        logger.debug("project_context_service.refresh_counts", project_key=project_key)
        return await self.repo.refresh_counts(project_key)
