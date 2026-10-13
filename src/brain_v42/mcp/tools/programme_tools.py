"""MCP tools for programme progress: brain_programme_* (contract programmes/v1).

Contract `docs/contracts/programmes_v1.md` (tickets 8a063256, 24c1f172). One call
replaces a message to the coordinating session: the owner of a gate reports it,
and the cockpit shows it to the operator through the sidecar's `GET /programmes`
and red-monitor's relay. No tool takes an argument naming the writer: the service
reads the caller's project from the credential, and that project signs the write.

Reached through `brain_find_tool` under the compact profile.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from brain_v42.mcp.tools.tool_annotations import _READ_ANNOTATIONS, _WRITE_ANNOTATIONS
from brain_v42.models.programme import (
    DEPENDS_ON_MAX,
    GATE_KEY_MAX,
    LINE_MAX,
    PROGRAMME_KEY_MAX,
    TITLE_MAX,
    UNIT_MAX,
    GateState,
    ProgrammeState,
)

if TYPE_CHECKING:
    from brain_v42.services.programme_service import ProgrammeService

ProgrammeKey = Annotated[
    str, Field(min_length=1, max_length=PROGRAMME_KEY_MAX, description="Programme key.")
]
GateKey = Annotated[str, Field(min_length=1, max_length=GATE_KEY_MAX, description="Gate key.")]
Title = Annotated[str, Field(min_length=1, max_length=TITLE_MAX)]
Line = Annotated[str, Field(min_length=1, max_length=LINE_MAX)]
Position = Annotated[int, Field(ge=0)]


class ProgrammeProgress(BaseModel):
    """Sub-progress shown on the critical line, never counted in the programme bar."""

    model_config = ConfigDict(extra="forbid")

    done: int = Field(ge=0)
    total: int = Field(ge=1, le=100_000)
    unit: str = Field(min_length=1, max_length=UNIT_MAX)


def register_programme_tools(mcp: FastMCP, programme_svc: ProgrammeService) -> None:
    """Register the four programme writes and the one read."""

    @mcp.tool(annotations=_WRITE_ANNOTATIONS)
    async def brain_programme_upsert(
        key: ProgrammeKey,
        title: Title | None = None,
        state: ProgrammeState | None = None,
        position: Position | None = None,
    ) -> dict[str, Any]:
        """Create a programme, or change its title, state or order.

        The project that creates a programme coordinates it: only that project
        changes it afterwards. A new programme needs a title and starts `active`;
        `done` folds it in the cockpit, `paused` dims it. Returns the programme as
        the sidecar publishes it. Refusals: `invalid_key`, `invalid_text`,
        `invalid_state`, `not_coordinator`, `programme_limit_reached` (at most 8
        active or paused), `caller_unidentified`.
        """
        return await programme_svc.upsert_programme(
            key=key, title=title, state=state, position=position
        )

    @mcp.tool(annotations=_WRITE_ANNOTATIONS)
    async def brain_programme_gate_upsert(
        programme: ProgrammeKey,
        gate: GateKey,
        title: Title | None = None,
        owner: Annotated[
            str | None,
            Field(
                max_length=50,
                description="Project key of the gate's owner: it may report the gate.",
            ),
        ] = None,
        depends_on: Annotated[
            list[str] | None,
            Field(
                max_length=DEPENDS_ON_MAX,
                description="Keys of gates of the same programme this gate waits for.",
            ),
        ] = None,
        weight: Annotated[
            int | None,
            Field(ge=1, le=100, description="Relative remaining effort for the critical path."),
        ] = None,
        position: Position | None = None,
        state: GateState | None = None,
    ) -> dict[str, Any]:
        """Create a gate or change its structure: the coordinating project only.

        A new gate needs a title and an owner, and starts `todo` unless `state`
        says otherwise. `depends_on` names existing gates of the programme; a cycle
        is refused. Returns the programme as published. Refusals: `not_coordinator`,
        `gate_incomplete`, `owner_unknown`, `unknown_dependency`, `dependency_cycle`,
        `gate_limit_reached` (32), `programme_too_large` (16 KiB published).
        """
        return await programme_svc.upsert_gate(
            programme=programme,
            gate=gate,
            title=title,
            owner=owner,
            depends_on=depends_on,
            weight=weight,
            position=position,
            state=state,
        )

    @mcp.tool(annotations=_WRITE_ANNOTATIONS)
    async def brain_programme_gate_report(
        programme: ProgrammeKey,
        gate: GateKey,
        state: GateState | None = None,
        progress: ProgrammeProgress | None = None,
        clear_progress: bool = False,
        note: Line | None = None,
        clear_note: bool = False,
    ) -> dict[str, Any]:
        """Report a gate: its owner or the coordinating project, in one call.

        A state change becomes the programme's `last_change`. Every accepted report
        refreshes the gate's `updated_at`, so reporting the same state again keeps
        the gate from reading stale. `note` is one line of at most 200 characters.
        Refusals: `not_gate_owner`, `gate_not_found`, `invalid_text`,
        `invalid_progress`, `nothing_to_report`, `conflicting_arguments`.
        """
        return await programme_svc.report_gate(
            programme=programme,
            gate=gate,
            state=state,
            progress=None if progress is None else progress.model_dump(),
            clear_progress=clear_progress,
            note=note,
            clear_note=clear_note,
        )

    @mcp.tool(annotations=_WRITE_ANNOTATIONS)
    async def brain_programme_request(
        programme: ProgrammeKey,
        gate: GateKey,
        ask: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=LINE_MAX,
                description="What the operator must do, one line. Omit to lift the request.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Raise a request to the operator on a gate, or lift it (no `ask`).

        The operator sees it under "for you" in the cockpit until the gate's owner
        or the coordinating project lifts it. The same ask raised again keeps its
        `since`; a different ask is a new request. Refusals: `not_gate_owner`,
        `gate_not_found`, `invalid_text`.
        """
        return await programme_svc.request(programme=programme, gate=gate, ask=ask)

    @mcp.tool(annotations=_READ_ANNOTATIONS)
    async def brain_programme_list(
        include_done: bool = False,
        key: ProgrammeKey | None = None,
    ) -> dict[str, Any]:
        """List programmes as the sidecar publishes them (active and paused by default).

        `include_done` adds every done programme; `key` reads one programme,
        whatever its state. Read only.
        """
        return {
            "programmes": await programme_svc.list_programmes(include_done=include_done, key=key)
        }
