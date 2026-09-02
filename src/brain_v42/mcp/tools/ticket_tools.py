"""MCP tools for cross-project tickets: brain_ticket_create / reply /
transition / list / get.

Coordination family — addressed, transient, stateful (spec 2026-07-04).
Formatting stays local (single consumer); shared write-confirmations come
from formatters.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError

from brain_v42.mcp.tools.formatters import format_confirmation, format_error, format_id
from brain_v42.mcp.tools.parsing import parse_uuid, resolve_entity_id
from brain_v42.mcp.tools.tool_annotations import (
    _DESTRUCTIVE_ANNOTATIONS,
    _HEARTBEAT_ANNOTATIONS,
    _READ_ANNOTATIONS,
)
from brain_v42.models.ticket import (
    ExtractionStatus,
    Ticket,
    TicketAction,
    TicketCreate,
    TicketGroups,
    TicketKind,
    TicketMessage,
    allowed_actions,
)
from brain_v42.services.ticket_service import TicketError

if TYPE_CHECKING:
    from brain_v42.services.ticket_service import TicketService

logger = structlog.get_logger(__name__)

_VALID_KINDS = ("fyi", "request")
_LIST_DEFAULT_LIMIT = 10
_LIST_MAX_LIMIT = 100


def _age_days(dt: datetime) -> str:
    days = max(0, (datetime.now(UTC) - dt).days)
    return f"{days}j"


def _ticket_line(t: Ticket, *, direction: str) -> str:
    # direction: "in" (I am the recipient) / "out" (I am the sender)
    arrow = "⬅️" if direction == "in" else "➡️"
    peer = t.from_project if direction == "in" else t.to_project
    prep = "de" if direction == "in" else "vers"
    return (
        f"{arrow} #{format_id(str(t.id))} [{t.kind.value}] {prep} {peer} : "
        f"« {t.title} » ({t.status.value} · {_age_days(t.created_at)})"
    )


def _format_group_page(
    lines: list[str],
    *,
    label: str,
    tickets: list[Ticket],
    direction: str,
    project_key: str,
    limit: int,
    offset: int,
) -> None:
    if not tickets:
        return

    page = tickets[offset : offset + limit]
    lines.append(f"\n### {label} ({len(tickets)})")
    lines.extend(_ticket_line(ticket, direction=direction) for ticket in page)

    omitted_before = min(offset, len(tickets))
    omitted_after = max(0, len(tickets) - offset - len(page))
    omitted = len(tickets) - len(page)
    if omitted == 0:
        return

    notice = f"… ({omitted} omis sur cette page; {omitted_before} avant, {omitted_after} après"
    if omitted_after:
        notice += (
            " — suite: brain_ticket_list("
            f"project_key='{project_key}', limit={limit}, offset={offset + limit})"
        )
    lines.append(notice + ")")


def _format_groups(
    groups: TicketGroups,
    project_key: str,
    *,
    limit: int,
    offset: int,
) -> str:
    total = (
        len(groups.a_traiter)
        + len(groups.a_confirmer)
        + len(groups.en_attente)
        + len(groups.awaiting_requester_confirmation)
    )
    if total == 0:
        return f"## Tickets — {project_key}\n(aucun ticket)"
    lines = [f"## Tickets — {project_key}"]
    _format_group_page(
        lines,
        label="À traiter",
        tickets=groups.a_traiter,
        direction="in",
        project_key=project_key,
        limit=limit,
        offset=offset,
    )
    _format_group_page(
        lines,
        label="À confirmer",
        tickets=groups.a_confirmer,
        direction="out",
        project_key=project_key,
        limit=limit,
        offset=offset,
    )
    _format_group_page(
        lines,
        label="En attente de l'autre côté",
        tickets=groups.en_attente,
        direction="out",
        project_key=project_key,
        limit=limit,
        offset=offset,
    )
    _format_group_page(
        lines,
        label="Livrés, en attente de confirmation du demandeur (relance : brain_ticket_reply)",
        tickets=groups.awaiting_requester_confirmation,
        direction="in",
        project_key=project_key,
        limit=limit,
        offset=offset,
    )
    return "\n".join(lines)


def _format_thread(ticket: Ticket, messages: list[TicketMessage]) -> str:
    header = (
        f"## Ticket #{format_id(str(ticket.id))} [{ticket.kind.value}] — « {ticket.title} »\n"
        f"{ticket.from_project} → {ticket.to_project} · status: {ticket.status.value}"
        f" · créé {ticket.created_at.date().isoformat()}"
    )
    if ticket.extraction_status is not None:
        header += f" · extraction: {ticket.extraction_status.value}"
    parts = [header, ticket.body]
    if messages:
        parts.append(f"### Fil ({len(messages)} message{'s' if len(messages) > 1 else ''})")
        for i, m in enumerate(messages, 1):
            suffix = f" (→ {m.status_to.value})" if m.status_to else ""
            parts.append(
                f"{i}. [{m.created_at.date().isoformat()}] {m.author_project}: {m.body}{suffix}"
            )
    actions = allowed_actions(
        ticket.kind, ticket.status, self_ticket=ticket.from_project == ticket.to_project
    )
    if actions:
        parts.append(f"Actions possibles ({', '.join(actions)}) via brain_ticket_transition")
    return "\n\n".join(parts)


def register_ticket_tools(
    mcp: Any,
    ticket_svc: TicketService,
) -> None:
    """Register the 5 brain_ticket_* MCP tools on the FastMCP server."""

    @mcp.tool(version="1.1", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_ticket_create(
        from_project: str,
        to_project: str,
        kind: str,
        title: str,
        body: str,
        extraction: str | None = None,
    ) -> str:
        """Open a cross-project ticket or a same-project note-to-self.

        kind='request': ask the target project to do something — full loop
        (they resolve, you confirm). kind='fyi': heads-up needing only an
        ack (e.g. contract change). Set from_project == to_project for a
        note-to-self that resurfaces in the next project session. The target
        sees it at its next brain_session_start. Both project keys must exist.

        extraction='skipped' opts this ticket out of the nightly
        knowledge-extraction job — use it for high-volume operational/job
        tickets (e.g. a factory daemon) that are noise, not durable knowledge.
        """
        if kind not in _VALID_KINDS:
            return format_error(f"Invalid kind '{kind}'. Valid: {list(_VALID_KINDS)}")
        if extraction is not None and extraction != ExtractionStatus.SKIPPED.value:
            return format_error(
                f"Invalid extraction '{extraction}'. Only 'skipped' (opt-out) or omit."
            )
        try:
            data = TicketCreate(
                kind=TicketKind(kind),
                title=title,
                body=body,
                from_project=from_project,
                to_project=to_project,
                extraction_status=(ExtractionStatus.SKIPPED if extraction else None),
            )
            ticket = await ticket_svc.create(data)
        except (TicketError, ValidationError) as exc:
            return format_error(str(exc))
        logger.info(
            "mcp.brain_ticket_create",
            ticket_id=str(ticket.id),
            kind=kind,
            to_project=ticket.to_project,
        )
        return format_confirmation(
            "Ticket created",
            title,
            id=str(ticket.id),
            kind=kind,
            to=ticket.to_project,
        )

    @mcp.tool(version="1.1", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_ticket_reply(
        ticket_id: str,
        author_project: str,
        body: str,
        corrects_body: str | None = None,
    ) -> str:
        """Post a message in a ticket thread (any status, participants only).

        corrects_body: replace the ticket's own body with this text. Use it when
        the body states a dead premise — a false body sits at the top of the
        view and keeps steering judgement, and no reply posted underneath
        undoes that. The replaced text is archived in this same thread message,
        so a reader can always tell a corrected body from an original one.
        `body` then carries the reason and is mandatory; an identical body is
        refused.
        """
        tid = parse_uuid(ticket_id)
        if tid is None:
            return format_error(f"Invalid UUID: {ticket_id}")
        try:
            await ticket_svc.reply(tid, author_project, body, corrects_body=corrects_body)
        except TicketError as exc:
            return format_error(str(exc))
        except ValueError as exc:  # canonicalize_project_key strict
            return format_error(str(exc))
        if corrects_body is not None:
            logger.info("mcp.brain_ticket_reply.body_corrected", ticket_id=ticket_id)
            return format_confirmation(
                "Reply posted and ticket body corrected", body, id=format_id(ticket_id)
            )
        return format_confirmation("Reply posted", body, id=format_id(ticket_id))

    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_ticket_transition(
        ticket_id: str,
        author_project: str,
        action: TicketAction,
        message: str | None = None,
    ) -> str:
        """Change a ticket's status. Actions — executor (to_project): start,
        resolve, wontfix, ack (fyi). Requester (from_project): confirm,
        reopen, cancel. Optional message is appended to the thread.
        """
        tid = parse_uuid(ticket_id)
        if tid is None:
            return format_error(f"Invalid UUID: {ticket_id}")
        try:
            updated = await ticket_svc.transition(tid, author_project, str(action), message=message)
        except TicketError as exc:
            return format_error(str(exc))
        except ValueError as exc:
            return format_error(str(exc))
        logger.info(
            "mcp.brain_ticket_transition",
            ticket_id=ticket_id,
            action=str(action),
            new_status=updated.status.value,
        )
        return format_confirmation(
            "Ticket updated",
            updated.title,
            id=format_id(ticket_id),
            status=updated.status.value,
        )

    @mcp.tool(version="1.1", annotations=_READ_ANNOTATIONS)
    async def brain_ticket_list(
        project_key: str,
        limit: int = _LIST_DEFAULT_LIMIT,
        offset: int = 0,
    ) -> str:
        """List a project's tickets grouped by needed action: à traiter
        (I'm the target), à confirmer (my requests resolved/wontfixed,
        awaiting my confirmation), en attente (the other side must act).

        Pagination is applied independently to every category. Category
        headings keep their total count, and every partial page reports the
        exact number omitted plus the next call needed to continue. Categories
        are ordered by recent activity (updated_at DESC, created_at DESC), then
        stable ticket id; follow every reported page to inspect the complete
        backlog, including deadlines.

        Args:
            project_key: Project whose incoming and outgoing tickets are listed.
            limit: Maximum tickets per category (default 10, clamped to [1, 100]).
            offset: Tickets skipped in every category (default 0, minimum 0).
        """
        limit = max(1, min(limit, _LIST_MAX_LIMIT))
        offset = max(0, offset)
        try:
            groups = await ticket_svc.list_grouped(project_key)
        except TicketError as exc:
            return format_error(str(exc))
        return _format_groups(groups, project_key, limit=limit, offset=offset)

    @mcp.tool(version="1.1", annotations=_READ_ANNOTATIONS)
    async def brain_ticket_get(ticket_id: str) -> str:
        """Full ticket view: header, body, thread, allowed actions.

        ticket_id accepts a full UUID or a unique ≥8-hex-char id prefix.
        Transitions and replies require the full UUID.
        """
        tid = await resolve_entity_id(ticket_id, ticket_svc.resolve_id_prefix, label="ticket")
        if isinstance(tid, str):
            return format_error(tid)
        result = await ticket_svc.get_with_thread(tid)
        if result is None:
            return format_error(f"Ticket '{format_id(ticket_id)}' not found")
        ticket, messages = result
        return _format_thread(ticket, messages)
