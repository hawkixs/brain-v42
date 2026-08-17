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
# Toute action absente de cette table est illégale. La discussion (reply)
# n'est PAS une transition : elle est permise quel que soit l'état.
TRANSITIONS: dict[tuple[TicketKind, TicketStatus, TicketAction], tuple[Role, TicketStatus]] = {
    # request — exécutant
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
    # fyi — open → acked, cancel possible par l'émetteur
    (TicketKind.FYI, TicketStatus.OPEN, TicketAction.ACK): ("executor", TicketStatus.ACKED),
    (TicketKind.FYI, TicketStatus.OPEN, TicketAction.CANCEL): ("requester", TicketStatus.CLOSED),
}

# (kind, current_status, action) -> new_status — consultée à la PLACE de TRANSITIONS
# quand from_project == to_project (self-ticket) : une seule partie, donc pas de
# champ Role (docs/superpowers/specs/2026-08-03-self-ticket-lifecycle-design.md §4.1).
# `resolve` ferme directement (le cas courant devient gratuit) ; `resolve_pending`
# reste disponible pour s'arrêter explicitement à `resolved`. La table est complète,
# fyi compris : sans (FYI, OPEN, ...), un self-ticket fyi deviendrait intransitionnable.
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
    # fyi — identique à TRANSITIONS, champ Role en moins (le comportement ne change pas)
    (TicketKind.FYI, TicketStatus.OPEN, TicketAction.ACK): TicketStatus.ACKED,
    (TicketKind.FYI, TicketStatus.OPEN, TicketAction.CANCEL): TicketStatus.CLOSED,
}


def allowed_actions(
    kind: TicketKind, status: TicketStatus, *, self_ticket: bool = False
) -> list[str]:
    """Actions légales (triées) depuis un état donné — pour les messages d'erreur et l'UX.

    self_ticket=True consulte SELF_TRANSITIONS (from_project == to_project) : pas de
    contrôle de rôle, `resolve` ferme directement, `resolve_pending` reste disponible.
    Défaut False préservé pour la rétrocompatibilité des appelants existants.
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
    """Vue groupée par action pour brain_ticket_list / le briefing.

    awaiting_requester_confirmation: nous avons livré (resolved/wontfix comme
    exécutant) et attendons la confirmation du demandeur. Aucune transition
    légale de notre côté (spec 2026-08-03 §1.2) — pas listé dans le briefing,
    compté seulement (spec §2.3).
    """

    a_traiter: list[Ticket] = Field(default_factory=list)
    a_confirmer: list[Ticket] = Field(default_factory=list)
    en_attente: list[Ticket] = Field(default_factory=list)
    awaiting_requester_confirmation: list[Ticket] = Field(default_factory=list)
