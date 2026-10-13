"""Read one project's current base focus and its compare-and-swap revision."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from brain_v42.mcp.tools.formatters import format_error
from brain_v42.mcp.tools.tool_annotations import _READ_ANNOTATIONS
from brain_v42.models.project_key import canonicalize_project_key

if TYPE_CHECKING:
    from brain_v42.services.project_context_service import ProjectContextService


class ProjectFocusRead(BaseModel):
    """Current base focus and its revision, read together from project context."""

    project_key: str
    focus: str | None
    focus_revision: int
    focus_updated_at: str | None
    chars: int


def _utc_iso(value: datetime | None) -> str | None:
    """Render timestamps consistently as ISO 8601 UTC, including a UTC suffix."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def register_project_focus_read_tools(mcp: Any, project_context_svc: ProjectContextService) -> None:
    """Register the atomic read used before replacing a project's base focus."""

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_project_focus_get(project_key: str) -> ProjectFocusRead:
        """Read one project's current base focus with its revision, in one statement.

        Pass `focus_revision` as `expected_focus_revision` on the write that replaces the
        base focus (unbound session end/relay, `brain_update_project_focus`); `chars` is
        the length the 70 % shrink guard measures; this has no session lifecycle effect.
        """
        try:
            key = canonicalize_project_key(project_key)
        except ValueError as exc:
            format_error(str(exc))

        row = await project_context_svc.get_by_key(key)
        if row is None:
            format_error(
                f"Unknown project '{key}'. Use brain_list_projects to find a valid project key."
            )
        focus = row.current_focus
        return ProjectFocusRead(
            project_key=row.project_key,
            focus=focus,
            focus_revision=row.focus_revision,
            focus_updated_at=_utc_iso(row.focus_updated_at),
            chars=len(focus) if focus is not None else 0,
        )
