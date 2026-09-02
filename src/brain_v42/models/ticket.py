"""Pydantic models for cross-project tickets (coordination family).

Tickets are NOT knowledge entities: no embedding, no decay, no search,
no domain classification, no graph sync (spec §1,
docs/superpowers/specs/2026-07-04-cross-project-tickets-design.md).
The only bridge to memory is the extraction job (spec §6).

Self-tickets (from_project == to_project) are allowed by design: they act
as a note-to-next-session; both roles then collapse onto the same project.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from brain_v42.models.base import TimestampMixin
from brain_v42.models.project_key import canonicalize_project_key


class TicketKind(StrEnum):
    REQUEST = "request"
    FYI = "fyi"


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    WONTFIX = "wontfix"
    CLOSED = "closed"
    ACKED = "acked"


class TicketAction(StrEnum):
    START = "start"
    RESOLVE = "resolve"
    RESOLVE_PENDING = "resolve_pending"
    WONTFIX = "wontfix"
    CONFIRM = "confirm"
    REOPEN = "reopen"
    ACK = "ack"
    CANCEL = "cancel"


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    PROPOSED = "proposed"
    SKIPPED = "skipped"
    DONE = "done"


TERMINAL_STATUSES: frozenset[TicketStatus] = frozenset({TicketStatus.CLOSED, TicketStatus.ACKED})

Role = Literal["executor", "requester"]

# (kind, current_status, action) -> (required_role, new_status)
# executor = author == to_project ; requester = author == from_project.
# Any action absent from this table is illegal. Discussion (reply) is NOT a
# transition: it is allowed whatever the state.
TRANSITIONS: dict[tuple[TicketKind, TicketStatus, TicketAction], tuple[Role, TicketStatus]] = {
    # request — executor
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.START): (
        "executor",
        TicketStatus.IN_PROGRESS,
    ),
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.RESOLVE): (
        "executor",
        TicketStatus.RESOLVED,
    ),
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.WONTFIX): (
        "executor",
        TicketStatus.WONTFIX,
    ),
    (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.RESOLVE): (
        "executor",
        TicketStatus.RESOLVED,
    ),
    (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.WONTFIX): (
        "executor",
        TicketStatus.WONTFIX,
    ),
    # request — demandeur (boucle de confirmation complète)
    (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.CONFIRM): (
        "requester",
        TicketStatus.CLOSED,
    ),
    (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.CONFIRM): (
        "requester",
        TicketStatus.CLOSED,
    ),
    (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.REOPEN): (
        "requester",
        TicketStatus.OPEN,
    ),
    (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.REOPEN): (
        "requester",
        TicketStatus.OPEN,
    ),
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.CANCEL): (
        "requester",
        TicketStatus.CLOSED,
    ),
    (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.CANCEL): (
        "requester",
        TicketStatus.CLOSED,
    ),
    (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.CANCEL): (
        "requester",
        TicketStatus.CLOSED,
    ),
    (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.CANCEL): (
        "requester",
        TicketStatus.CLOSED,
    ),
    # fyi — open → acked, cancel available to the sender
    (TicketKind.FYI, TicketStatus.OPEN, TicketAction.ACK): ("executor", TicketStatus.ACKED),
    (TicketKind.FYI, TicketStatus.OPEN, TicketAction.CANCEL): ("requester", TicketStatus.CLOSED),
}

# (kind, current_status, action) -> new_status — consulted INSTEAD OF TRANSITIONS
# when from_project == to_project (self-ticket): a single party, hence no Role
# field (docs/superpowers/specs/2026-08-03-self-ticket-lifecycle-design.md §4.1).
# `resolve` closes directly (the common case becomes free); `resolve_pending`
# stays available to stop explicitly at `resolved`. The table is complete, fyi
# included: without (FYI, OPEN, ...), an fyi self-ticket would be untransitionable.
SELF_TRANSITIONS: dict[tuple[TicketKind, TicketStatus, TicketAction], TicketStatus] = {
    # request
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.START): TicketStatus.IN_PROGRESS,
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.RESOLVE): TicketStatus.CLOSED,
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.RESOLVE_PENDING): TicketStatus.RESOLVED,
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.WONTFIX): TicketStatus.CLOSED,
    (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.CANCEL): TicketStatus.CLOSED,
    (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.RESOLVE): TicketStatus.CLOSED,
    (
        TicketKind.REQUEST,
        TicketStatus.IN_PROGRESS,
        TicketAction.RESOLVE_PENDING,
    ): TicketStatus.RESOLVED,
    (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.WONTFIX): TicketStatus.CLOSED,
    (TicketKind.REQUEST, TicketStatus.IN_PROGRESS, TicketAction.CANCEL): TicketStatus.CLOSED,
    (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.CONFIRM): TicketStatus.CLOSED,
    (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.REOPEN): TicketStatus.OPEN,
    (TicketKind.REQUEST, TicketStatus.RESOLVED, TicketAction.CANCEL): TicketStatus.CLOSED,
    (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.CONFIRM): TicketStatus.CLOSED,
    (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.REOPEN): TicketStatus.OPEN,
    (TicketKind.REQUEST, TicketStatus.WONTFIX, TicketAction.CANCEL): TicketStatus.CLOSED,
    # fyi — identical to TRANSITIONS, minus the Role field (behaviour is unchanged)
    (TicketKind.FYI, TicketStatus.OPEN, TicketAction.ACK): TicketStatus.ACKED,
    (TicketKind.FYI, TicketStatus.OPEN, TicketAction.CANCEL): TicketStatus.CLOSED,
}


def allowed_actions(
    kind: TicketKind, status: TicketStatus, *, self_ticket: bool = False
) -> list[str]:
    """Legal actions (sorted) from a given state — for error messages and UX.

    self_ticket=True consults SELF_TRANSITIONS (from_project == to_project): no role
    check, `resolve` closes directly, `resolve_pending` stays available. The False
    default is preserved for backward compatibility with existing callers.
    """
    if self_ticket:
        return sorted(a.value for (k, s, a) in SELF_TRANSITIONS if k == kind and s == status)
    return sorted(a.value for (k, s, a) in TRANSITIONS if k == kind and s == status)


class TicketBase(BaseModel):
    kind: TicketKind
    title: str = Field(..., max_length=200)
    body: str
    from_project: str = Field(..., max_length=50)
    to_project: str = Field(..., max_length=50)

    @field_validator("from_project", "to_project")
    @classmethod
    def _canonicalize(cls, v: str) -> str:
        return canonicalize_project_key(v)


class TicketCreate(TicketBase):
    extraction_status: ExtractionStatus | None = None


class Ticket(TicketBase, TimestampMixin):
    id: UUID = Field(default_factory=uuid4)
    status: TicketStatus = TicketStatus.OPEN
    extraction_status: ExtractionStatus | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None

    model_config = {"from_attributes": True}


class TicketMessage(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    ticket_id: UUID
    author_project: str = Field(..., max_length=50)
    body: str
    status_to: TicketStatus | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("author_project")
    @classmethod
    def _canonicalize(cls, v: str) -> str:
        return canonicalize_project_key(v)

    model_config = {"from_attributes": True}


class TicketGroups(BaseModel):
    """View grouped by action, for brain_ticket_list and the briefing.

    awaiting_requester_confirmation: we delivered (resolved/wontfix as the
    executor) and are waiting for the requester to confirm. No legal transition
    on our side (spec 2026-08-03 §1.2) — not listed in the briefing, only
    counted (spec §2.3).
    """

    a_traiter: list[Ticket] = Field(default_factory=list)
    a_confirmer: list[Ticket] = Field(default_factory=list)
    en_attente: list[Ticket] = Field(default_factory=list)
    awaiting_requester_confirmation: list[Ticket] = Field(default_factory=list)
