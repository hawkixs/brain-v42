"""MCP tools for focus slots: brain_slot_open / list / close (ADR #34).

Reached through `brain_find_tool` under the compact profile. Opening and closing
a slot are explicit user commands; a guard mod's standing command does not cover
them (Amendment — slot relay (ADR #34), `docs/OPERATIONS.md` § Session lifecycle).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastmcp import FastMCP
from pydantic import Field

from brain_v42.mcp.tools.session_lifecycle_tools import ProjectKeyArg, SlotBodyArg
from brain_v42.mcp.tools.tool_annotations import (
    _DESTRUCTIVE_ANNOTATIONS,
    _READ_ANNOTATIONS,
    _WRITE_ANNOTATIONS,
)
from brain_v42.models.focus_slot import (
    MAX_SLOT_ANCHORS,
    SLOT_CLOSE_NOTE_MAX_LENGTH,
    SLOT_TITLE_MAX_LENGTH,
    FocusSlotCloseResult,
    FocusSlotListResult,
    FocusSlotOpenResult,
    SlotAnchor,
    SlotStatusFilter,
)

if TYPE_CHECKING:
    from brain_v42.services.focus_slot_service import FocusSlotService

SlotTitleArg = Annotated[str, Field(min_length=1, max_length=SLOT_TITLE_MAX_LENGTH)]
SlotAnchorsArg = Annotated[
    list[SlotAnchor],
    Field(
        min_length=1,
        max_length=MAX_SLOT_ANCHORS,
        description=(
            "What the slot is about, at least one: {kind:'ticket', ticket_id} (a ticket of "
            "this project), {kind:'lot', target_release:'X.Y.Z'} (a release this project "
            "plans), {kind:'pr', repository_id, pr_number} (a PR bound to a ticket of this "
            "project). Anchors never change; to change them, close and open another slot."
        ),
    ),
]
SlotNoteArg = Annotated[str, Field(min_length=1, max_length=SLOT_CLOSE_NOTE_MAX_LENGTH)]
SlotRevisionArg = Annotated[int, Field(ge=0, strict=True)]
SlotLimitArg = Annotated[int, Field(ge=1, le=100)]
SlotOffsetArg = Annotated[int, Field(ge=0)]


def register_focus_slot_tools(mcp: FastMCP, focus_slot_svc: FocusSlotService) -> None:
    """Register the three focus-slot tools."""

    @mcp.tool(annotations=_WRITE_ANNOTATIONS)
    async def brain_slot_open(
        project_key: ProjectKeyArg,
        title: SlotTitleArg,
        body: SlotBodyArg,
        anchors: SlotAnchorsArg,
    ) -> FocusSlotOpenResult:
        """Open a focus slot for one topic in flight, from an explicit user command.

        The slot starts at revision 0 and never writes the project base focus. An
        equal open (same open title, body and anchor set) is a replay. Refusals:
        `anchor_required`, `anchor_invalid`, `anchor_ticket_foreign`,
        `anchor_unbound_pr`, `anchor_lot_unplanned`, `anchor_already_received`,
        `slot_title_conflict`, `project_not_found`.
        """
        return await focus_slot_svc.open(
            project_key=project_key, title=title, body=body, anchors=anchors
        )

    @mcp.tool(annotations=_READ_ANNOTATIONS)
    async def brain_slot_list(
        project_key: ProjectKeyArg,
        status: SlotStatusFilter = "open",
        limit: SlotLimitArg = 20,
        offset: SlotOffsetArg = 0,
    ) -> FocusSlotListResult:
        """List a project's focus slots with their anchors, bound session, staleness and
        whether a closing receipt is already pending. Read only."""
        return await focus_slot_svc.list(
            project_key=project_key, status=status, limit=limit, offset=offset
        )

    @mcp.tool(annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_slot_close(
        slot_id: UUID,
        expected_revision: SlotRevisionArg,
        note: SlotNoteArg,
    ) -> FocusSlotCloseResult:
        """Close a focus slot, from an explicit user command, with a note saying why.

        Compare-and-swap on `expected_revision`; refused while a session is bound
        (`slot_bound`). A closed slot never reopens. Refusals: `slot_not_found`,
        `slot_closed`, `slot_revision_conflict` (names the current revision), `slot_bound`.
        """
        return await focus_slot_svc.close(
            slot_id=slot_id, expected_revision=expected_revision, note=note
        )
