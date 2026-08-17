"""Fail-closed guard: refuse to create knowledge under an unknown project.

Mirrors the check ``TicketService.create`` already performs (see
``brain_v42.services.ticket_service`` — "leçon du drift brain_v42/brain-v42":
never create a phantom ``project_context`` by typo). Reused here by
``LearningService``, ``DecisionService``, ``SnippetService``, ``RunbookService``
and ``ADRService`` so a missing or unknown ``project_key`` fails the write
loudly instead of committing an orphan invisible to every project-scoped view
(briefing, roadmap, ``brain_list_projects``).

``project_key`` is expected to already be canonicalized (the ``Create``
models mix in ``ProjectKeyCanonicalMixin``, which runs before this guard
ever sees the value) — known aliases like ``brain`` / ``brain_v42`` are
resolved to ``brain-v42`` by the time this function is called.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.services.ticket_service import UnknownProjectError

if TYPE_CHECKING:
    from brain_v42.repositories.pg_project_context import PgProjectContextRepo


async def require_known_project(
    project_context_repo: PgProjectContextRepo | None,
    project_key: str | None,
    *,
    session: AsyncSession | None = None,
) -> None:
    """Raise ``UnknownProjectError`` unless ``project_key`` names an existing project.

    ``project_context_repo=None`` disables the guard — that collaborator is
    optional at construction so the many existing unit-test doubles that
    build these services without it keep exercising unrelated behavior
    unchanged. Every production wiring site (MCP server, Codex gateway,
    benchmark script, ticket-extraction CLI) injects it, so the guard is
    active on every real write path.

    When ``session`` is provided, the lookup reuses it instead of opening a
    second connection — required by the proposal-service atomic apply path,
    which passes its own transaction into ``LearningService.create`` /
    ``DecisionService.create``.
    """
    if project_context_repo is None:
        return
    if (
        project_key is None
        or await project_context_repo.get_by_key(project_key, session=session) is None
    ):
        raise UnknownProjectError(
            f"Unknown project {project_key!r} — create it first "
            f"(brain_set_project_context) or check the key (brain_list_projects)"
        )
