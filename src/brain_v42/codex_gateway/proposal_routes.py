"""Proposal triage routes over the shared ProposalService."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from brain_v42.codex_gateway.audit import log_write
from brain_v42.codex_gateway.dependencies import GatewayServices
from brain_v42.services.proposal_service import (
    ProposalApplyError,
    ProposalNotFoundError,
    ProposalNotProposedError,
    ProposalStateConflictError,
)

logger = structlog.get_logger(__name__)
_CODEX_ACTOR = "red-codex"
_CODEX_PROJECT_GROUP = "red"


async def _mutate_proposal(
    mutation: Callable[..., Awaitable[Any]],
    proposal_id: int,
    operation: str,
) -> JSONResponse:
    try:
        result = await mutation(proposal_id, project_group=_CODEX_PROJECT_GROUP)
    except ProposalNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ProposalNotProposedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProposalStateConflictError as error:
        logger.info(
            "codex_gateway.proposal_state_conflict",
            family=error.family,
            proposal_id=error.proposal_id,
            operation=error.operation,
        )
        raise HTTPException(
            status_code=409,
            detail="Proposal state changed; review required",
        ) from error
    except ProposalApplyError as error:
        logger.error(
            "codex_gateway.proposal_mutation_failed",
            family=error.family,
            proposal_id=error.proposal_id,
            operation=error.operation,
            error_type=type(error.__cause__ or error).__name__,
        )
        raise HTTPException(status_code=500, detail="Proposal mutation failed") from error

    log_write(operation, _CODEX_ACTOR, proposal_id=proposal_id)
    return JSONResponse(content=jsonable_encoder(result))


def build_proposal_router(services: GatewayServices) -> APIRouter:
    router = APIRouter(prefix="/proposals", tags=["proposals"])

    @router.post("/ticket-extraction/{proposal_id}/apply", response_model=None)
    async def apply_ticket_extraction(proposal_id: int) -> JSONResponse:
        return await _mutate_proposal(
            services.proposal.apply_ticket_extraction,
            proposal_id,
            "proposal.ticket-extraction.apply",
        )

    @router.post("/ticket-extraction/{proposal_id}/reject", response_model=None)
    async def reject_ticket_extraction(proposal_id: int) -> JSONResponse:
        return await _mutate_proposal(
            services.proposal.reject_ticket_extraction,
            proposal_id,
            "proposal.ticket-extraction.reject",
        )

    @router.post("/roadmap-curation/{proposal_id}/apply", response_model=None)
    async def apply_roadmap_curation(proposal_id: int) -> JSONResponse:
        return await _mutate_proposal(
            services.proposal.apply_roadmap_curation,
            proposal_id,
            "proposal.roadmap-curation.apply",
        )

    @router.post("/roadmap-curation/{proposal_id}/reject", response_model=None)
    async def reject_roadmap_curation(proposal_id: int) -> JSONResponse:
        return await _mutate_proposal(
            services.proposal.reject_roadmap_curation,
            proposal_id,
            "proposal.roadmap-curation.reject",
        )

    return router
