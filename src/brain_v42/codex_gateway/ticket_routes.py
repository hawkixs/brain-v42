"""Ticket management routes backed by the canonical TicketService."""

from __future__ import annotations

from typing import Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from brain_v42.codex_gateway.audit import log_write
from brain_v42.codex_gateway.dependencies import GatewayServices
from brain_v42.codex_gateway.schemas import (
    TicketCreatePayload,
    TicketReplyPayload,
    TicketTransitionPayload,
)
from brain_v42.models.ticket import TicketCreate, allowed_actions
from brain_v42.services.ticket_service import (
    IllegalTransitionError,
    TicketError,
    TicketNotFoundError,
    TicketTransitionConflictError,
)


def _raise_ticket_error(error: Exception) -> NoReturn:
    status_code = 404 if isinstance(error, TicketNotFoundError) else 422
    raise HTTPException(status_code=status_code, detail=str(error)) from error


async def _transition_conflict(
    services: GatewayServices,
    ticket_id: UUID,
    error: Exception,
) -> JSONResponse:
    current = await services.ticket.get_with_thread(ticket_id)
    actions: list[str] = []
    if current is not None:
        ticket, _messages = current
        actions = allowed_actions(
            ticket.kind, ticket.status, self_ticket=ticket.from_project == ticket.to_project
        )
    return JSONResponse(
        status_code=409,
        content={"detail": str(error), "allowed_actions": actions},
    )


def build_ticket_router(services: GatewayServices) -> APIRouter:
    router = APIRouter(prefix="/tickets", tags=["tickets"])

    @router.post("", status_code=201, response_model=None)
    async def create_ticket(payload: TicketCreatePayload) -> Any:
        try:
            ticket = await services.ticket.create(TicketCreate(**payload.model_dump()))
        except (TicketError, ValueError) as error:
            _raise_ticket_error(error)
        log_write(
            "ticket.create",
            payload.from_project,
            ticket_id=str(ticket.id),
            to_project=ticket.to_project,
        )
        return ticket

    @router.post("/{ticket_id}/reply", response_model=None)
    async def reply_to_ticket(ticket_id: UUID, payload: TicketReplyPayload) -> Any:
        try:
            message = await services.ticket.reply(
                ticket_id,
                payload.actor_project,
                payload.body,
            )
        except (TicketError, ValueError) as error:
            _raise_ticket_error(error)
        log_write("ticket.reply", payload.actor_project, ticket_id=str(ticket_id))
        return message

    @router.post("/{ticket_id}/transition", response_model=None)
    async def transition_ticket(ticket_id: UUID, payload: TicketTransitionPayload) -> Any:
        try:
            ticket = await services.ticket.transition(
                ticket_id,
                payload.actor_project,
                payload.action,
                message=payload.message,
            )
        except (IllegalTransitionError, TicketTransitionConflictError) as error:
            return await _transition_conflict(services, ticket_id, error)
        except (TicketError, ValueError) as error:
            _raise_ticket_error(error)
        log_write(
            "ticket.transition",
            payload.actor_project,
            ticket_id=str(ticket_id),
            action=payload.action,
        )
        return jsonable_encoder(ticket)

    return router
