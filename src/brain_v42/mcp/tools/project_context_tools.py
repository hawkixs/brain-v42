"""MCP tools for brain_set_project_context, brain_update_project_focus, brain_list_projects.

Note: brain_get_project_context has been removed. Use brain_session_start instead,
which already returns project context as part of session initialization.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import structlog
from pydantic import Field

from brain_v42.db.focus_history import focus_diff
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

#: The bound of `project_contexts.current_focus`, SHARED with `brain_session_end`'s
#: `next_focus` — the same constant, not a second literal.
#:
#: It is shared because it is the same COLUMN: `next_focus` BECOMES
#: `current_focus` when the closing compare-and-swap succeeds. Two distinct
#: bounds let the unbounded writer put the project into a state the bounded
#: writer could no longer represent — an honest session then became unable to
#: close (`bfb4cf93`). This is the exact opposite of the reasoning that keeps
#: `SummaryArg` separate: `summary` writes nothing into `project_contexts`, so
#: its cap has no reason to follow this one.
#:
#: It counts CHARACTERS, like Pydantic's `maxLength`, never bytes: `brain-v42`'s
#: real focus was 9,977 characters for 10,285 BYTES, so a byte bound would
#: already be crossed by a perfectly legal focus.
ProjectFocusArg = Annotated[str, Field(max_length=NEXT_FOCUS_MAX_LENGTH)]


def _model_bound(field_name: str) -> int:
    """`ProjectContextCreate`'s own cap for one field — READ, never retyped.

    Item 8 of af3b58dd asks for a bounded schema. Four fields were already
    bounded by the model and by nothing the agent could see: `project_key` 50,
    `name` 200, `gitlab_project_path` 200, `project_group` 50. Publishing them is
    finishing the shape `ProjectFocusArg` already has one line above.

    Derived rather than copied, for the reason the `NEXT_FOCUS_MAX_LENGTH` note
    gives right above: two literals for one column drift, and the drift only
    surfaces when a value falls between them. It raises on an absent bound so a
    field that LOSES its cap on the model reddens here instead of quietly
    publishing a stale number.
    """
    for constraint in ProjectContextCreate.model_fields[field_name].metadata:
        bound = getattr(constraint, "max_length", None)
        if bound is not None:
            return int(bound)
    raise ValueError(f"ProjectContextCreate.{field_name} carries no max_length to publish")


#: The four bounds the model enforced without ever telling the caller.
ProjectKeyArg = Annotated[str, Field(max_length=_model_bound("project_key"))]
ProjectNameArg = Annotated[str, Field(max_length=_model_bound("name"))]
GitlabProjectPathArg = Annotated[str, Field(max_length=_model_bound("gitlab_project_path"))]
ProjectGroupArg = Annotated[str, Field(max_length=_model_bound("project_group"))]


def register_project_context_tools(
    mcp: Any,
    project_context_svc: ProjectContextService,
    roadmap_svc: Any | None = None,
) -> None:
    """Register brain_set_project_context, brain_update_project_focus, brain_list_projects on mcp."""

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_focus_history(
        project_key: str,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> str:
        """Read one project's focus revisions, newest first, with what each changed.

        Answers the question nothing could answer before migration 050: what did
        the focus say before it was overwritten. An erased focus shows as
        `(erased)` — that is the destructive overwrite this trail exists for, not
        a gap in it.

        Each line carries the characters added and removed against the revision
        BELOW it. `unchanged` is marked rather than filtered: a session close
        re-posting the previous prose verbatim is the normal regime, and a
        filtered row is a row somebody has to go looking for.
        """
        try:
            key = canonicalize_project_key(project_key)
        except ValueError as exc:
            return format_error(str(exc))

        rows = await project_context_svc.focus_history(key, limit=limit, offset=offset)
        if not rows:
            return f"No focus history for {key}."

        lines = [f"### Focus history — {key} ({len(rows)})"]
        for index, row in enumerate(rows):
            focus = row["focus"]
            # The row BELOW in the listing is the previous revision, the
            # listing being newest-first. The last one on the page has no
            # predecessor HERE — saying "+N/-0" against nothing would invent a
            # creation event for a revision that may simply be off the page.
            previous = rows[index + 1]["focus"] if index + 1 < len(rows) else None
            has_previous = index + 1 < len(rows) or offset > 0
            delta = focus_diff(previous, focus) if index + 1 < len(rows) else None
            shown = "(erased)" if focus is None else focus.splitlines()[0][:120]
            stamp = row["created_at"].date().isoformat()
            marker = ""
            if delta is not None:
                marker = (
                    " · unchanged"
                    if delta["unchanged"]
                    else f" · +{delta['added']}/-{delta['removed']} chars"
                )
            elif not has_previous:
                marker = " · first recorded revision"
            actor = f" · {row['actor']}" if row["actor"] else ""
            lines.append(
                f"- r{row['focus_revision']} [{row['source']}] {stamp}{actor}{marker}\n    {shown}"
            )
        return "\n".join(lines)

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_set_project_context(
        project_key: ProjectKeyArg,
        name: ProjectNameArg,
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
        gitlab_project_path: GitlabProjectPathArg | None = None,
        project_group: ProjectGroupArg | None = None,
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
