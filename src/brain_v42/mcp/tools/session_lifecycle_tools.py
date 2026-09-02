"""MCP adapters for user-controlled Brain session lifecycles."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

import structlog
from fastmcp import FastMCP
from pydantic import Field

from brain_v42.mcp.tools.tool_annotations import (
    _HEARTBEAT_ANNOTATIONS,
    _READ_ANNOTATIONS,
    _TERMINAL_ANNOTATIONS,
    _WRITE_ANNOTATIONS,
)
from brain_v42.models.brain_session import (
    MAX_CAPTURED_KNOWLEDGE_IDS,
    BrainSessionAbandonResult,
    BrainSessionCaptureResult,
    BrainSessionEndResult,
    BrainSessionHeartbeatResult,
    BrainSessionListResult,
    BrainSessionResumeResult,
    BrainSessionStartResult,
)

if TYPE_CHECKING:
    from brain_v42.services.brain_session_service import BrainSessionService

BriefingLoader = Callable[[str], Awaitable[str]]
CapturedKnowledgeIdsArg = Annotated[
    list[UUID],
    Field(min_length=1, max_length=MAX_CAPTURED_KNOWLEDGE_IDS),
]
ProjectKeyArg = Annotated[str, Field(min_length=1, max_length=50)]
ClientKeyArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        description=(
            "Stable identity for one intended session. Reuse it for every retry of that "
            "session; use a distinct stable key for each parallel session."
        ),
    ),
]
ExpectedClientKeyArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        description=(
            "Client identity expected for the addressed session UUID. The pair must match "
            "before any session mutation. This is an isolation guard, not authentication."
        ),
    ),
]
SummaryArg = Annotated[str, Field(min_length=1, max_length=10_000)]

#: The cap on `next_focus`, extracted so the briefing can ANNOUNCE the remaining
#: margin instead of letting it be discovered through a refusal, at closing time,
#: after the work. Two literals would diverge at the first change and the
#: briefing would promise a margin validation would not recognize.
#:
#: It counts CHARACTERS, not bytes — that is what Pydantic's `maxLength`
#: measures. `brain-v42`'s focus was 9,977 characters for 10,285 BYTES on
#: 2026-08-22: a byte bound would already be crossed.
#:
#: It is NOT shared with `SummaryArg`, which happens to be the same value today:
#: they are two distinct contracts, and coupling them would move one by changing
#: the other.
NEXT_FOCUS_MAX_LENGTH = 10_000

FocusArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=NEXT_FOCUS_MAX_LENGTH,
        description=(
            "Jugement et engagements pour la session suivante — ce qui n'est pas "
            "dérivable automatiquement (ex : ne pas publier ce brouillon et pourquoi, "
            "une échéance et sa raison, une décision opérateur à respecter). N'y "
            "recopie pas l'état mesurable (révision de schéma, HEAD git, arbre propre, "
            "résultat de suite de tests) : il est déjà recalculé à chaque briefing "
            "depuis la source réelle, et une copie manuelle se périme en silence."
        ),
    ),
]
ReasonArg = Annotated[str, Field(min_length=1, max_length=2_000)]
FocusRevisionArg = Annotated[int, Field(ge=0, strict=True)]
ListLimitArg = Annotated[int, Field(ge=1, le=100)]
ListOffsetArg = Annotated[int, Field(ge=0)]
#: The FOUR persisted statuses of `brain_sessions`, plus TWO derived filters.
#:
#: `closed_inactive` was missing here for the whole life of 046: the state
#: existed in both `CHECK`s, the sweep knew how to set it, the service and the
#: repository knew how to filter it — only this published literal ignored it. A
#: state reachable in the database and unrequestable by a client (`24ca3b73`).
#:
#: `stale` and `all` are NOT statuses: `stale` is derived from
#: `last_heartbeat_at` on open sessions, `all` is the absence of a filter. This
#: list must therefore cover `BrainSessionStatus` in full — that is what
#: `test_the_filter_covers_every_persisted_status` pins, by deriving its
#: expectations from the enumeration rather than copying them here.
SessionStatusFilter = Literal["open", "stale", "ended", "abandoned", "closed_inactive", "all"]

logger = structlog.get_logger(__name__)
_BRIEFING_UNAVAILABLE = "## Session Briefing\n(briefing unavailable; session remains open)"


async def _load_briefing_safely(
    briefing_loader: BriefingLoader,
    *,
    project_key: str,
    session_id: UUID,
) -> str:
    """Keep a persisted session observable when the optional briefing fails."""
    try:
        return await briefing_loader(project_key)
    except Exception as exc:
        logger.warning(
            "brain_session_briefing_unavailable",
            project_key=project_key,
            session_id=str(session_id),
            error=str(exc),
        )
        return _BRIEFING_UNAVAILABLE


def register_session_lifecycle_tools(
    mcp: FastMCP,
    brain_session_svc: BrainSessionService,
    briefing_loader: BriefingLoader,
) -> None:
    """Register the seven explicit Brain session lifecycle tools."""

    @mcp.tool(version="4.0", annotations=_WRITE_ANNOTATIONS)
    async def brain_session_start(
        project_key: ProjectKeyArg,
        client_key: ClientKeyArg,
    ) -> BrainSessionStartResult:
        """Start or replay a concurrent session from an explicit user command.

        An agent tracer is the only session the server opens or closes on
        its own; no hook and no auto-close may invoke this lifecycle
        boundary.
        """
        result = await brain_session_svc.start(
            project_key=project_key,
            client_key=client_key,
        )
        briefing = await _load_briefing_safely(
            briefing_loader,
            project_key=result.session.project_key,
            session_id=result.session.id,
        )
        return result.model_copy(update={"briefing": briefing})

    @mcp.tool(version="4.0", output_schema=None, annotations=_WRITE_ANNOTATIONS)
    async def brain_session_capture(
        session_id: UUID,
        expected_client_key: ExpectedClientKeyArg,
        knowledge_ids: CapturedKnowledgeIdsArg,
    ) -> BrainSessionCaptureResult:
        """Attach durable artifacts from an explicit user command — to repair or refine.

        No longer a compulsory ritual. With derived capture armed, artifacts
        created during the session are attributed without this call, and `end`
        no longer demands a receipt. What this tool is for now: attaching
        something the derivation could not see — created outside the window,
        on another connection, or in another project — and saying so on the
        record.

        It never steals: an artifact already attributed stays where it is.

        An agent tracer is the only session the server opens or closes on
        its own; no hook and no auto-close may invoke this lifecycle
        boundary.
        """
        return await brain_session_svc.capture(
            session_id=session_id,
            expected_client_key=expected_client_key,
            knowledge_ids=knowledge_ids,
        )

    @mcp.tool(version="4.0", output_schema=None, annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_session_heartbeat(
        session_id: UUID,
        expected_client_key: ExpectedClientKeyArg,
    ) -> BrainSessionHeartbeatResult:
        """Refresh an open session from an explicit user command.

        An agent tracer is the only session the server opens or closes on
        its own; no hook and no auto-close may invoke this lifecycle
        boundary.
        """
        return await brain_session_svc.heartbeat(
            session_id=session_id,
            expected_client_key=expected_client_key,
        )

    @mcp.tool(version="4.0", annotations=_TERMINAL_ANNOTATIONS)
    async def brain_session_end(
        session_id: UUID,
        expected_client_key: ExpectedClientKeyArg,
        summary: SummaryArg,
        next_focus: FocusArg,
        expected_focus_revision: FocusRevisionArg,
        nothing_to_capture_reason: ReasonArg | None = None,
    ) -> BrainSessionEndResult:
        """End a session from an explicit user command, on judgement alone.

        `nothing_to_capture_reason` is now OPTIONAL. The old rule — a non-empty
        ledger XOR a written reason — measured whether the client had DECLARED
        its work. Derived capture feeds that signal from the server, and a check
        is hollow the moment the thing it checks can influence its own signal;
        worse, it made a session whose ledger the server had filled impossible
        to close. Give a reason if you have one to give; a blank one is still
        refused, because saying nothing and saying "   " are not the same act.

        What `end` still requires is what the server cannot produce for you:
        a summary and a next focus. It REPORTS `unattributed_in_window` —
        artifacts of this project created during the session that belong to no
        ledger — as a measure, never a gate. It cannot refuse a close, and a
        session cannot improve it by doing nothing.

        An agent tracer is the only session the server opens or closes on
        its own; no hook and no auto-close may invoke this lifecycle
        boundary.
        """
        return await brain_session_svc.end(
            session_id=session_id,
            expected_client_key=expected_client_key,
            summary=summary,
            next_focus=next_focus,
            expected_focus_revision=expected_focus_revision,
            nothing_to_capture_reason=nothing_to_capture_reason,
        )

    @mcp.tool(version="4.0", output_schema=None, annotations=_READ_ANNOTATIONS)
    async def brain_session_list(
        project_key: ProjectKeyArg | None = None,
        status: SessionStatusFilter = "open",
        limit: ListLimitArg = 20,
        offset: ListOffsetArg = 0,
    ) -> BrainSessionListResult:
        """List sessions only in response to an explicit user command.

        An agent tracer is the only session the server opens or closes on
        its own; no hook and no auto-close may invoke this lifecycle
        boundary.
        """
        return await brain_session_svc.list(
            project_key=project_key,
            status=status,
            limit=limit,
            offset=offset,
        )

    @mcp.tool(version="4.0", annotations=_READ_ANNOTATIONS)
    async def brain_session_resume(
        session_id: UUID,
        expected_client_key: ExpectedClientKeyArg,
    ) -> BrainSessionResumeResult:
        """Resume an open session from an explicit user command.

        An agent tracer is the only session the server opens or closes on
        its own; no hook and no auto-close may invoke this lifecycle
        boundary.
        """
        result = await brain_session_svc.resume(
            session_id=session_id,
            expected_client_key=expected_client_key,
        )
        briefing = await _load_briefing_safely(
            briefing_loader,
            project_key=result.session.project_key,
            session_id=result.session.id,
        )
        return result.model_copy(update={"briefing": briefing})

    @mcp.tool(version="4.0", output_schema=None, annotations=_TERMINAL_ANNOTATIONS)
    async def brain_session_abandon(
        session_id: UUID,
        expected_client_key: ExpectedClientKeyArg,
        reason: ReasonArg,
    ) -> BrainSessionAbandonResult:
        """Abandon a session only from an explicit user command.

        An agent tracer is the only session the server opens or closes on
        its own; no hook and no auto-close may invoke this lifecycle
        boundary.
        """
        return await brain_session_svc.abandon(
            session_id=session_id,
            expected_client_key=expected_client_key,
            reason=reason,
        )
