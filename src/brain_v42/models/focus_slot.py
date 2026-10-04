"""Focus slots: topics in flight, each anchored and guarded by its own revision (ADR #34).

The project focus (`project_contexts.current_focus`) becomes a BASE that slots
never write. A slot is anchored to a ticket, a lot or a PR, and closes on an
explicit command or on a measured receipt. These models carry no persistence;
the single writer is `brain_v42.db.focus_slots`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from brain_v42.models.brain_session import (
    AUTO_STALE_AFTER,
    BrainSession,
    BrainSessionCheckpoint,
    BrainSessionError,
)
from brain_v42.models.ticket import parse_target_release

#: Spec bounds, one constant each. Characters, not bytes (Q4, and the lesson of
#: `NEXT_FOCUS_MAX_LENGTH`: a byte bound refuses legal non-ASCII text).
SLOT_TITLE_MAX_LENGTH = 120
SLOT_BODY_MAX_LENGTH = 4_000
SLOT_CLOSE_NOTE_MAX_LENGTH = 2_000
MAX_SLOT_ANCHORS = 10
#: ADR D6 reuses the sweep's seven days: slots outlive sessions, so the 24 h
#: session flag would mark most slots stale every morning.
SLOT_STALE_AFTER: timedelta = AUTO_STALE_AFTER
RELAY_INITIATORS = ("operator", "guard_mod")

AnchorKind = Literal["ticket", "lot", "pr"]
SlotStatusFilter = Literal["open", "closed", "all"]
RelayInitiator = Literal["operator", "guard_mod"]

_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "ticket": frozenset({"ticket_id"}),
    "lot": frozenset({"target_release"}),
    "pr": frozenset({"repository_id", "pr_number"}),
}


class FocusSlotError(BrainSessionError):
    """A slot refusal with a stable code and a caller safe message."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class SlotAnchor(BaseModel):
    """What a slot is about; its exact shape is checked before persistence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AnchorKind
    ticket_id: UUID | None = None
    target_release: str | None = None
    repository_id: int | None = None
    pr_number: int | None = None

    def key(self) -> tuple[str, str | None, str | None, int | None, int | None]:
        """Identity for replay and duplicate detection, order-free across a set."""
        return (
            self.kind,
            str(self.ticket_id) if self.ticket_id is not None else None,
            self.target_release,
            self.repository_id,
            self.pr_number,
        )

    @property
    def ref(self) -> str:
        if self.kind == "ticket":
            return f"ticket:{self.ticket_id}"
        if self.kind == "lot":
            return f"lot:{self.target_release}"
        return f"pr:{self.repository_id}#{self.pr_number}"


def validate_anchor_shape(anchor: SlotAnchor) -> SlotAnchor:
    """Refuse malformed anchors before a write, using the database's shape rule."""
    given = {
        name
        for name in ("ticket_id", "target_release", "repository_id", "pr_number")
        if getattr(anchor, name) is not None
    }
    required = _REQUIRED_FIELDS[anchor.kind]
    if given != required:
        raise FocusSlotError(
            "anchor_invalid",
            f"a {anchor.kind} anchor carries exactly {sorted(required)}, got {sorted(given)}",
        )
    if anchor.kind == "lot":
        try:
            parse_target_release(anchor.target_release or "")
        except ValueError as exc:
            raise FocusSlotError("anchor_invalid", str(exc)) from exc
    if anchor.kind == "pr" and (anchor.pr_number or 0) < 1:
        raise FocusSlotError("anchor_invalid", "a pr anchor's pr_number is at least 1")
    return anchor


def slot_is_stale(
    *,
    is_open: bool,
    bound: bool,
    body_updated_at: datetime,
    last_bound_ended_at: datetime | None,
    now: datetime,
) -> bool:
    """ADR D6: open, unbound slots untouched for seven days are stale."""
    activity = max(body_updated_at, last_bound_ended_at) if last_bound_ended_at else body_updated_at
    return is_open and not bound and activity < now - SLOT_STALE_AFTER


class FocusSlot(BaseModel):
    id: UUID
    project_key: str
    title: str
    body: str
    revision: int = Field(ge=0)
    opened_at: datetime
    body_updated_at: datetime
    closed_at: datetime | None = None
    close_reason: str | None = None
    close_note: str | None = None
    anchors: list[SlotAnchor] = Field(default_factory=list)


class FocusSlotView(FocusSlot):
    """A slot as `brain_slot_list` and the briefing show it: derived, never stored."""

    bound_session_id: UUID | None = None
    is_stale: bool = False
    receipt_pending: bool = False


class FocusSlotOpenResult(BaseModel):
    slot: FocusSlot
    replayed: bool


class FocusSlotListResult(BaseModel):
    slots: list[FocusSlotView]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class FocusSlotCloseResult(BaseModel):
    slot: FocusSlot


class BrainSessionBindResult(BaseModel):
    """`brain_session_bind`: the slot revision an end or relay must send next."""

    session_id: UUID
    slot_id: UUID
    slot_revision: int = Field(ge=0)
    slot_body: str


class BrainSessionRelayResult(BaseModel):
    """`brain_session_relay`: the ended session, its successor, what the CAS wrote.

    A slot relay carries the slot after the CAS and no `focus_revision`; a base relay
    (an unbound session) carries no slot and the new BASE revision the successor must
    send. Exactly one of the two is set.
    """

    ended_session_id: UUID
    session: BrainSession
    slot: FocusSlot | None = None
    focus_revision: int | None = None
    replayed: bool
    briefing: str = ""


class PreviousSlotSession(BaseModel):
    """The last session that ended on a slot, for the bound-session briefing."""

    session_id: UUID
    ended_at: datetime
    summary: str
    decisions: list[tuple[UUID, str]] = Field(default_factory=list)
    last_checkpoint: BrainSessionCheckpoint | None = None


class SlotBriefing(BaseModel):
    open_slots: list[FocusSlotView] = Field(default_factory=list)
    bound_slot: FocusSlot | None = None
    previous: PreviousSlotSession | None = None
    to_distill: list[FocusSlot] = Field(default_factory=list)
