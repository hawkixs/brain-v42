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
    MAX_CHECKPOINT_TEXT,
    BrainSessionAbandonResult,
    BrainSessionCaptureResult,
    BrainSessionCheckpointResult,
    BrainSessionEndResult,
    BrainSessionHeartbeatResult,
    BrainSessionListResult,
    BrainSessionResumeResult,
    BrainSessionStartResult,
)
from brain_v42.models.focus_slot import (
    SLOT_BODY_MAX_LENGTH,
    BrainSessionBindResult,
    BrainSessionRelayResult,
    RelayInitiator,
)

if TYPE_CHECKING:
    from brain_v42.services.brain_session_service import BrainSessionService

#: `(project_key, session_id)`. The session entered the signature with M-C: the
#: briefing renders that session's last checkpoints, and a project-scoped loader
#: could not know which session asked. Passed by BOTH start and resume — a start
#: that REPLAYS an open session returns a session that may already hold judgment,
#: and hiding it there would be the one place the reader most expects it.
BriefingLoader = Callable[[str, UUID], Awaitable[str]]
CapturedKnowledgeIdsArg = Annotated[
    list[UUID],
    Field(min_length=1, max_length=MAX_CAPTURED_KNOWLEDGE_IDS),
]
RelayKnowledgeIdsArg = Annotated[list[UUID], Field(max_length=MAX_CAPTURED_KNOWLEDGE_IDS)]
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
CheckpointSeqArg = Annotated[
    int,
    Field(
        ge=1,
        description=(
            "Monotone sequence number supplied by the CALLER. Reuse the same seq to replay "
            "a checkpoint idempotently; reusing it with different content is refused."
        ),
    ),
]
CheckpointTextArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_CHECKPOINT_TEXT,
        description=(
            "Judgment text, refused rather than truncated past its bound. Write what a "
            "reader would need to resume without you."
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
#: A slot body, a bound session's next_focus written into its slot, or a relay
#: handover: 4,000 CHARACTERS (Q4), like `NEXT_FOCUS_MAX_LENGTH` counted in
#: characters, a separate contract from the base cap.
SlotBodyArg = Annotated[str, Field(min_length=1, max_length=SLOT_BODY_MAX_LENGTH)]
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
        return await briefing_loader(project_key, session_id)
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
    """Register the ten explicit Brain session lifecycle tools."""

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

    @mcp.tool(version="4.0", annotations=_WRITE_ANNOTATIONS)
    async def brain_session_checkpoint(
        session_id: UUID,
        expected_client_key: ExpectedClientKeyArg,
        seq: CheckpointSeqArg,
        progress: CheckpointTextArg,
        next_step: CheckpointTextArg,
        blocker: CheckpointTextArg | None = None,
    ) -> BrainSessionCheckpointResult:
        """Publish one semantic checkpoint of an open session, in a single call.

        An agent tracer is the only session the server opens or closes on
        its own; no hook and no auto-close may invoke this lifecycle
        boundary.

        This is NOT a lifecycle command and NOT a presence signal. It writes no
        heartbeat, touches no focus, attributes no artifact, and opens or closes
        nothing. Liveness already comes from the observation stamped by every tool
        call; what a checkpoint records is JUDGMENT — where the work stands, what
        blocks it, what comes next — published together so a reader can tell a
        complete snapshot from a partial one.

        Send the same `seq` again to replay a call safely: an identical payload is
        absorbed and returns `replayed: true`, while the same `seq` carrying
        different content is refused rather than silently dropped.
        """
        return await brain_session_svc.checkpoint(
            session_id=session_id,
            expected_client_key=expected_client_key,
            seq=seq,
            progress=progress,
            next_step=next_step,
            blocker=blocker,
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

        For a bound session, `expected_focus_revision` is the SLOT revision (from
        bind, resume, or relay's `slot.revision`), never `started_focus_revision`.

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

    @mcp.tool(version="4.0", annotations=_TERMINAL_ANNOTATIONS)
    async def brain_session_relay(
        session_id: UUID,
        expected_client_key: ExpectedClientKeyArg,
        summary: SummaryArg,
        handover: SlotBodyArg,
        expected_slot_revision: FocusRevisionArg,
        new_client_key: ClientKeyArg,
        initiator: RelayInitiator,
        knowledge_ids: RelayKnowledgeIdsArg | None = None,
        nothing_to_capture_reason: ReasonArg | None = None,
    ) -> BrainSessionRelayResult:
        """Relay a bound session onto its slot: capture, end and start its successor at once.

        One transaction: the named `knowledge_ids` are captured into the old session,
        the `handover` becomes the slot body under a compare-and-swap on
        `expected_slot_revision`, the old session ends, and a successor opens on the
        same slot under `new_client_key`; its briefing follows. A closed slot or a
        stale revision changes nothing and leaves the session open. An equal replay
        returns the same successor with `replayed = true`; any other payload on an
        ended session is `terminal_conflict`. Work without an anchor cannot be
        relayed (`relay_requires_slot`): give it a slot first. The successor's next
        end or relay must send `result.slot.revision` as its expected revision: for
        a bound session it is the SLOT revision (from bind, resume, or relay's
        `slot.revision`), never `started_focus_revision`.

        A guard mod the operator explicitly enabled may make this call as a standing
        user command, and only this one (Amendment — slot relay (ADR #34)); with
        `initiator = 'guard_mod'` it is refused while
        `BRAIN_SESSION_RELAY_GUARD_MOD_ENABLED` is false.

        An agent tracer is the only session the server opens or closes on
        its own; no hook and no auto-close may invoke this lifecycle
        boundary.
        """
        result = await brain_session_svc.relay(
            session_id=session_id,
            expected_client_key=expected_client_key,
            summary=summary,
            handover=handover,
            expected_slot_revision=expected_slot_revision,
            new_client_key=new_client_key,
            initiator=initiator,
            knowledge_ids=knowledge_ids,
            nothing_to_capture_reason=nothing_to_capture_reason,
        )
        briefing = await _load_briefing_safely(
            briefing_loader,
            project_key=result.session.project_key,
            session_id=result.session.id,
        )
        return result.model_copy(update={"briefing": briefing})

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

    @mcp.tool(version="4.0", annotations=_WRITE_ANNOTATIONS)
    async def brain_session_bind(
        session_id: UUID,
        expected_client_key: ExpectedClientKeyArg,
        slot_id: UUID,
    ) -> BrainSessionBindResult:
        """Bind an open operator session to one focus slot, from an explicit user command.

        Once per session; binding the same slot again is a replay. From then on the
        session's `end` writes that slot only, never the project base:
        `expected_focus_revision` is the slot revision returned here. Refusals:
        `session_not_open`, `session_is_agent_trace`, `session_already_bound`,
        `slot_not_found`, `slot_closed`, `slot_project_mismatch`, `slot_busy`.

        An agent tracer is the only session the server opens or closes on
        its own; no hook and no auto-close may invoke this lifecycle
        boundary.
        """
        return await brain_session_svc.bind(
            session_id=session_id,
            expected_client_key=expected_client_key,
            slot_id=slot_id,
        )

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
