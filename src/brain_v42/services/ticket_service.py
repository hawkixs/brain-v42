"""Business rules for cross-project tickets.

Enforce: project registry validation at create, participant checks,
and the pure transition table from models.ticket. Side effects:
resolved_at / closed_at timestamps and extraction_status=pending on
terminal states (spec §3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.models.ticket import (
    SELF_TRANSITIONS,
    TERMINAL_STATUSES,
    TRANSITIONS,
    ExtractionStatus,
    Ticket,
    TicketAction,
    TicketCreate,
    TicketGroups,
    TicketMessage,
    TicketStatus,
    allowed_actions,
)

if TYPE_CHECKING:
    from brain_v42.repositories.pg_project_context import PgProjectContextRepo
    from brain_v42.repositories.pg_ticket import PgTicketRepo

logger = structlog.get_logger(__name__)


class TicketError(Exception):
    """Base for user-facing ticket errors (tools render str(exc))."""


class UnknownProjectError(TicketError):
    pass


class TicketNotFoundError(TicketError):
    pass


class NotAllowedError(TicketError):
    pass


class IllegalTransitionError(TicketError):
    pass


class TicketTransitionConflictError(TicketError):
    """The ticket status changed after the transition rules were evaluated."""


class TicketService:
    def __init__(
        self,
        repo: PgTicketRepo,
        project_context_repo: PgProjectContextRepo,
    ) -> None:
        self._repo = repo
        self._ctx_repo = project_context_repo

    async def create(self, data: TicketCreate) -> Ticket:
        # Refus si projet inconnu — leçon du drift brain_v42/brain-v42 :
        # aucune création de projet fantôme par typo (spec §2).
        for key in (data.from_project, data.to_project):
            if await self._ctx_repo.get_by_key(key) is None:
                raise UnknownProjectError(
                    f"Unknown project '{key}' — create it first "
                    f"(brain_set_project_context) or check the key "
                    f"(brain_list_projects)"
                )
        ticket = await self._repo.create(data)
        logger.info(
            "ticket.created",
            ticket_id=str(ticket.id),
            kind=ticket.kind.value,
            from_project=ticket.from_project,
            to_project=ticket.to_project,
        )
        return ticket

    async def reply(self, ticket_id: UUID, author_project: str, body: str) -> TicketMessage:
        author = canonicalize_project_key(author_project)
        ticket = await self._get_or_raise(ticket_id)
        if author not in (ticket.from_project, ticket.to_project):
            raise NotAllowedError(
                f"'{author}' is not a participant of this ticket "
                f"({ticket.from_project} → {ticket.to_project})"
            )
        return await self._repo.add_message(ticket_id, author, body)

    async def transition(
        self,
        ticket_id: UUID,
        author_project: str,
        action: str,
        message: str | None = None,
    ) -> Ticket:
        author = canonicalize_project_key(author_project)
        ticket = await self._get_or_raise(ticket_id)
        try:
            act = TicketAction(action)
        except ValueError:
            valid = sorted(a.value for a in TicketAction)
            raise IllegalTransitionError(f"unknown action '{action}' — valid: {valid}") from None

        # Self-ticket (from_project == to_project) : une seule partie, donc pas de
        # contrôle de rôle (spec §1.1, §4.1) — SELF_TRANSITIONS remplace TRANSITIONS.
        self_ticket = ticket.from_project == ticket.to_project
        if self_ticket:
            new_status = SELF_TRANSITIONS.get((ticket.kind, ticket.status, act))
            if new_status is None:
                raise self._illegal(ticket, act, self_ticket=True)
        else:
            rule = TRANSITIONS.get((ticket.kind, ticket.status, act))
            if rule is None:
                raise self._illegal(ticket, act, self_ticket=False)
            role, new_status = rule
            expected = ticket.to_project if role == "executor" else ticket.from_project
            if author != expected:
                raise NotAllowedError(
                    f"'{act.value}' is reserved to the {role} ('{expected}'); author was '{author}'"
                )

        now = datetime.now(UTC)
        resolved_at = ticket.resolved_at
        extraction = ticket.extraction_status
        closed_at = ticket.closed_at
        if new_status is TicketStatus.RESOLVED:
            resolved_at = now
        if new_status is TicketStatus.OPEN:  # reopen
            resolved_at = None
        if new_status in TERMINAL_STATUSES:
            closed_at = now
            # Respecte un opt-out posé à la création : ne pas écraser 'skipped'.
            if extraction is not ExtractionStatus.SKIPPED:
                extraction = ExtractionStatus.PENDING

        updated = await self._repo.apply_transition(
            ticket_id,
            new_status,
            expected_status=ticket.status,
            resolved_at=resolved_at,
            closed_at=closed_at,
            extraction_status=extraction,
            message_author=author if message else None,
            message_body=message if message else None,
        )
        if updated is None:
            raise TicketTransitionConflictError("Ticket changed concurrently; reload and retry")
        return updated

    async def get_with_thread(self, ticket_id: UUID) -> tuple[Ticket, list[TicketMessage]] | None:
        ticket = await self._repo.get_by_id(ticket_id)
        if ticket is None:
            return None
        messages = await self._repo.get_messages(ticket_id)
        return ticket, messages

    async def resolve_id_prefix(self, prefix_hex: str) -> list[UUID]:
        """Resolve a git-style short id prefix to matching ticket ids."""
        return await self._repo.resolve_id_prefix(prefix_hex)

    async def list_grouped(self, project_key: str) -> TicketGroups:
        key = canonicalize_project_key(project_key, strict=False)
        return await self._repo.list_grouped(key)

    async def _get_or_raise(self, ticket_id: UUID) -> Ticket:
        ticket = await self._repo.get_by_id(ticket_id)
        if ticket is None:
            raise TicketNotFoundError(f"Ticket '{ticket_id}' not found")
        return ticket

    @staticmethod
    def _illegal(ticket: Ticket, act: TicketAction, *, self_ticket: bool) -> IllegalTransitionError:
        allowed = allowed_actions(ticket.kind, ticket.status, self_ticket=self_ticket)
        hint = ", ".join(allowed) if allowed else "none (terminal state)"
        return IllegalTransitionError(
            f"'{act.value}' is illegal from status '{ticket.status.value}' "
            f"(kind={ticket.kind.value}); allowed: {hint}"
        )
