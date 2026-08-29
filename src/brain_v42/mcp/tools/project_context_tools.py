"""MCP tools for brain_set_project_context, brain_update_project_focus, brain_list_projects.

Note: brain_get_project_context has been removed. Use brain_session_start instead,
which already returns project context as part of session initialization.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import structlog
from pydantic import Field

from brain_v42.mcp.tools.formatters import (
    format_confirmation,
    format_error,
    format_projects_list,
)
from brain_v42.mcp.tools.session_lifecycle_tools import NEXT_FOCUS_MAX_LENGTH
from brain_v42.mcp.tools.tool_annotations import (
    _DESTRUCTIVE_ANNOTATIONS,
    _READ_ANNOTATIONS,
)
from brain_v42.models.project_context import ProjectContextCreate
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.services.roadmap_service import (
    ProjectFocusConflictError,
    ProjectFocusNotFoundError,
    ProjectFocusValidationError,
)

if TYPE_CHECKING:
    from brain_v42.services.project_context_service import ProjectContextService

logger = structlog.get_logger(__name__)
FocusRevisionArg = Annotated[int, Field(ge=0, strict=True)]

#: La borne de `project_contexts.current_focus`, PARTAGÉE avec `next_focus` de
#: `brain_session_end` — la même constante, pas un second littéral.
#:
#: Elle est partagée parce que c'est la même COLONNE : `next_focus` DEVIENT
#: `current_focus` quand le compare-and-swap de la fermeture réussit. Deux
#: bornes distinctes laissaient l'écrivain non borné mettre le projet dans un
#: état que l'écrivain borné ne savait plus représenter — une session honnête
#: devenait alors incapable de fermer (`bfb4cf93`). C'est l'inverse exact du
#: raisonnement qui garde `SummaryArg` séparé : `summary` n'écrit rien dans
#: `project_contexts`, donc son plafond n'a aucune raison de suivre celui-ci.
#:
#: Elle compte des CARACTÈRES, comme `maxLength` de Pydantic, jamais des octets :
#: le focus réel de `brain-v42` faisait 9 977 caractères pour 10 285 OCTETS, donc
#: une borne en octets serait déjà franchie sur un focus parfaitement légal.
ProjectFocusArg = Annotated[str, Field(max_length=NEXT_FOCUS_MAX_LENGTH)]


def register_project_context_tools(
    mcp: Any,
    project_context_svc: ProjectContextService,
    roadmap_svc: Any | None = None,
) -> None:
    """Register brain_set_project_context, brain_update_project_focus, brain_list_projects on mcp."""

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_set_project_context(
        project_key: str,
        name: str,
        description: str,
        languages: list[str] | None = None,
        frameworks: list[str] | None = None,
        databases: list[str] | None = None,
        code_style: str | None = None,
        git_workflow: str | None = None,
        test_strategy: str | None = None,
        current_phase: str | None = None,
        current_focus: ProjectFocusArg | None = None,
        blockers: list[str] | None = None,
        related_projects: list[str] | None = None,
        plan_scan_paths: list[str] | None = None,
        gitlab_project_path: str | None = None,
        project_group: str | None = None,
    ) -> str:
        """Set or initialize a project context (upsert by project_key).

        Uses upsert semantics: if project_key already exists, updates the
        record with the new values. Otherwise, creates a new record.

        Args:
            project_key: Canonical project key (kebab-case, e.g. 'brain-v42').
            name: Human-readable project name.
            description: What the project is and does.
            languages: Programming languages in use (e.g. ['python']).
            frameworks: Frameworks in use (e.g. ['fastmcp', 'sqlalchemy']).
            databases: Data stores in use (e.g. ['postgresql', 'neo4j']).
            code_style: Coding conventions summary.
            git_workflow: Branching/commit conventions summary.
            test_strategy: Testing approach summary.
            current_phase: Current lifecycle phase (free text).
            current_focus: Focus prose — capped at the same character bound as
                every MCP focus writer; do not restate machine-measurable state.
            blockers: Current blockers, one string each.
            related_projects: Project keys this one relates to.
            plan_scan_paths: Filesystem paths scanned for plan indexing.
            gitlab_project_path: GitLab path (group/name) when mirrored.
            project_group: Optional group name to assign this project to a group.
        """
        logger.debug("mcp.brain_set_project_context", project_key=project_key)
        data = ProjectContextCreate(
            project_key=project_key,
            name=name,
            description=description,
            languages=languages or [],
            frameworks=frameworks or [],
            databases=databases or [],
            code_style=code_style,
            git_workflow=git_workflow,
            test_strategy=test_strategy,
            current_phase=current_phase,
            current_focus=current_focus,
            blockers=blockers or [],
            related_projects=related_projects or [],
            plan_scan_paths=plan_scan_paths or [],
            gitlab_project_path=gitlab_project_path,
            project_group=project_group,
        )
        await project_context_svc.get_or_create(data)
        return format_confirmation("Project context set", "", project_key=project_key)

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_list_projects(
        project_group: str | None = None,
    ) -> str:
        """List all known projects with their current focus and phase.

        Args:
            project_group: Optional group name to filter projects belonging to that group.
        """
        contexts = await project_context_svc.list_all(project_group=project_group)
        return format_projects_list(contexts)

    @mcp.tool(version="2.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_update_project_focus(
        project_key: str,
        current_focus: ProjectFocusArg,
        expected_focus_revision: FocusRevisionArg,
        blockers: list[str] | None = None,
        feature_status: dict[str, str] | None = None,
        unpin: list[str] | None = None,
    ) -> str:
        """Atomically update project focus and optional roadmap state with CAS.

        Args:
            project_key: The project key to update.
            current_focus: New current focus description.
            expected_focus_revision: Revision returned by session start/resume.
            blockers: Optional list of current blockers.
            feature_status: Optional dict mapping feature name -> new status.
                Example: {"Core Monitoring": "deployed", "GPU Collector": "building"}
                Features in this dict are automatically pinned.
            unpin: Optional list of feature names to unpin (set pinned=false).
        """
        project_key = canonicalize_project_key(project_key, strict=False)
        logger.debug("mcp.brain_update_project_focus", project_key=project_key)
        if roadmap_svc is None:
            return format_error("Atomic project focus coordinator unavailable; no write performed")
        try:
            outcome = await roadmap_svc.update_project_focus(
                project_key,
                current_focus,
                expected_focus_revision=expected_focus_revision,
                blockers=blockers,
                feature_status=feature_status,
                unpin=unpin,
            )
        except ProjectFocusConflictError as exc:
            return format_error(
                f"Focus conflict: current revision {exc.current_revision}, "
                f"current focus: {exc.current_focus!r}. Resume the session and retry."
            )
        except (ProjectFocusNotFoundError, ProjectFocusValidationError) as exc:
            return format_error(str(exc))

        # Auto-update CLAUDE.md dynamic section (best-effort, may fail in CI)
        try:
            from brain_v42.config import get_settings  # noqa: PLC0415
            from brain_v42.services.claude_md_writer import update_claude_md  # noqa: PLC0415

            settings = get_settings()
            claude_md_path = settings.claude_md_paths.get(project_key)
            if claude_md_path:
                updated = update_claude_md(claude_md_path, outcome.current_focus)
                if updated:
                    logger.info("Updated CLAUDE.md for project %s", project_key)
        except Exception:
            logger.debug("CLAUDE.md auto-update skipped (settings unavailable)")

        return format_confirmation(
            "Focus updated",
            "",
            project_key=project_key,
            focus=outcome.current_focus,
            focus_revision=outcome.focus_revision,
            features_updated=len(outcome.features_updated),
            features_unpinned=len(outcome.features_unpinned),
        )

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_list_project_groups() -> str:
        """List all project groups with their project count.

        Returns groups defined in the system with how many projects
        belong to each group. Use a group name with brain_search(project_group=...)
        to search across all projects in a group.
        """
        groups = await project_context_svc.list_groups()
        if not groups:
            return "No project groups defined."
        lines = ["## Project Groups\n"]
        for g in groups:
            lines.append(f"- **{g['group']}**: {g['count']} projects")
        return "\n".join(lines)
