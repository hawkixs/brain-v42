"""Fenced, transaction-owned claims for delivery workflow work."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import delivery_workflows
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import ClaimResult, ClaimState, DeliveryError, EligibleWork
from brain_v42.models.delivery_evaluator import evaluate_delivery
from brain_v42.models.ticket import Ticket
from brain_v42.repositories.pg_base import BasePgRepository
from brain_v42.repositories.pg_delivery import PgDeliveryRepo

_WORK_KINDS = frozenset({"implement", "repair", "review", "integrate", "accept"})
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 3600
ClaimWorkKind = Literal["implement", "repair", "review", "integrate", "accept"]


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_ttl(ttl_seconds: int) -> None:
    if type(ttl_seconds) is not int or not _MIN_TTL_SECONDS <= ttl_seconds <= _MAX_TTL_SECONDS:
        raise DeliveryError("invalid_claim_ttl", "claim TTL must be between 60 and 3600 seconds")


def _validate_owner(owner_key: str) -> None:
    if not owner_key.strip() or len(owner_key) > 200:
        raise DeliveryError("invalid_claim_owner", "claim owner is invalid")


def _validate_claim_credentials(owner_key: str, claim_token: str, epoch: int) -> None:
    _validate_owner(owner_key)
    if not isinstance(claim_token, str) or not claim_token or len(claim_token) > 1000:
        raise DeliveryError("invalid_claim_token", "claim token is invalid")
    if type(epoch) is not int or epoch < 0:
        raise DeliveryError("invalid_claim_epoch", "claim epoch is invalid")


def _actor_for_work(ticket: Ticket, work_kind: str) -> str:
    if work_kind not in _WORK_KINDS:
        raise DeliveryError("invalid_claim_work", "claim work kind is not supported")
    return ticket.from_project if work_kind == "accept" else ticket.to_project


class PgDeliveryClaimsRepo(BasePgRepository):
    """Persist opaque owner leases while callers retain transaction ownership."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        super().__init__(session_factory)
        self._delivery_repo = PgDeliveryRepo(session_factory)

    async def acquire(
        self,
        session: AsyncSession,
        ticket_id: UUID,
        *,
        settings: DeliverySettings,
        actor_project: str,
        owner_key: str,
        work_kind: str,
        expected_workflow_version: int,
        expected_assessment_id: str,
        ttl_seconds: int = 900,
    ) -> ClaimResult:
        """Acquire one current eligible work item under the shared decision lock scope."""
        if not settings.enabled:
            raise DeliveryError("delivery_disabled", "delivery workflow operations are disabled")
        _validate_ttl(ttl_seconds)
        _validate_owner(owner_key)
        locked = await self._delivery_repo.load_locked_decision_inputs(
            session,
            ticket_id,
            feature_enabled=settings.enabled,
            freshness_seconds=settings.freshness_seconds,
        )
        inputs = locked.inputs
        if inputs is None:
            raise DeliveryError("contract_not_found", "delivery contract was not found")
        assessment = evaluate_delivery(inputs, now=locked.decision_time)
        if (
            expected_workflow_version != inputs.workflow_version
            or expected_assessment_id != assessment.assessment_id
        ):
            raise DeliveryError("claim_stale", "displayed delivery assessment is no longer current")
        expected_actor = _actor_for_work(locked.ticket, work_kind)
        if actor_project != expected_actor:
            raise DeliveryError("not_allowed", "actor does not hold the required delivery role")
        work = next((item for item in assessment.eligible_work if item.kind == work_kind), None)
        if work is None:
            raise DeliveryError("claim_not_eligible", "delivery work is not currently eligible")
        workflow = dict(
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket_id)
                )
            )
            .mappings()
            .one()
        )
        expires_at = workflow["claim_expires_at"]
        if expires_at is not None and expires_at > locked.decision_time:
            raise DeliveryError("claim_unavailable", "delivery work is already claimed")
        token = secrets.token_urlsafe(32)
        digest = _token_digest(token)
        current_epoch = workflow["claim_epoch"]
        if not isinstance(current_epoch, int):
            raise RuntimeError("delivery claim epoch is invalid")
        epoch = current_epoch + 1
        claim_expires_at = locked.decision_time + timedelta(seconds=ttl_seconds)
        await session.execute(
            delivery_workflows.update()
            .where(delivery_workflows.c.ticket_id == ticket_id)
            .values(
                claim_owner=owner_key,
                claim_kind=work_kind,
                claim_digest=digest,
                claim_expires_at=claim_expires_at,
                claim_epoch=epoch,
                updated_at=sa.func.now(),
            )
        )
        return ClaimResult(
            ticket_id=ticket_id,
            assessment_id=assessment.assessment_id,
            work=EligibleWork(kind=cast(ClaimWorkKind, work_kind), role=work.role),
            epoch=epoch,
            expires_at=claim_expires_at,
            claim_token=token,
        )

    async def renew(
        self,
        session: AsyncSession,
        ticket_id: UUID,
        *,
        settings: DeliverySettings,
        actor_project: str,
        owner_key: str,
        claim_token: str,
        epoch: int,
        ttl_seconds: int = 900,
    ) -> ClaimState:
        """Extend a live lease after exact role, owner, token and epoch checks."""
        _validate_ttl(ttl_seconds)
        _validate_claim_credentials(owner_key, claim_token, epoch)
        workflow, decision_time, ticket = await self._locked_claim(session, ticket_id, settings)
        self._validate_live_claim(
            workflow,
            ticket=ticket,
            decision_time=decision_time,
            actor_project=actor_project,
            owner_key=owner_key,
            claim_token=claim_token,
            epoch=epoch,
        )
        expires_at = decision_time + timedelta(seconds=ttl_seconds)
        await session.execute(
            delivery_workflows.update()
            .where(delivery_workflows.c.ticket_id == ticket_id)
            .values(claim_expires_at=expires_at, updated_at=sa.func.now())
        )
        return ClaimState(epoch=epoch, owner=owner_key, expires_at=expires_at)

    async def release(
        self,
        session: AsyncSession,
        ticket_id: UUID,
        *,
        settings: DeliverySettings,
        actor_project: str,
        owner_key: str,
        claim_token: str,
        epoch: int,
    ) -> ClaimState:
        """Release a live lease after exact role, owner, token and epoch checks."""
        _validate_claim_credentials(owner_key, claim_token, epoch)
        workflow, decision_time, ticket = await self._locked_claim(session, ticket_id, settings)
        self._validate_live_claim(
            workflow,
            ticket=ticket,
            decision_time=decision_time,
            actor_project=actor_project,
            owner_key=owner_key,
            claim_token=claim_token,
            epoch=epoch,
        )
        await session.execute(
            delivery_workflows.update()
            .where(delivery_workflows.c.ticket_id == ticket_id)
            .values(
                claim_owner=None,
                claim_kind=None,
                claim_digest=None,
                claim_expires_at=None,
                updated_at=sa.func.now(),
            )
        )
        return ClaimState(epoch=epoch, owner=None, expires_at=None)

    async def _locked_claim(
        self, session: AsyncSession, ticket_id: UUID, settings: DeliverySettings
    ) -> tuple[Mapping[str, Any], datetime, Ticket]:
        locked = await self._delivery_repo.load_locked_decision_inputs(
            session,
            ticket_id,
            feature_enabled=settings.enabled,
            freshness_seconds=settings.freshness_seconds,
        )
        workflow = dict(
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket_id)
                )
            )
            .mappings()
            .one_or_none()
            or {}
        )
        if not workflow:
            raise DeliveryError("contract_not_found", "delivery contract was not found")
        return workflow, locked.decision_time, locked.ticket

    @staticmethod
    def _validate_live_claim(
        workflow: Mapping[str, Any],
        *,
        ticket: Ticket,
        decision_time: datetime,
        actor_project: str,
        owner_key: str,
        claim_token: str,
        epoch: int,
    ) -> None:
        stored_kind = workflow["claim_kind"]
        stored_digest = workflow["claim_digest"]
        expires_at = workflow["claim_expires_at"]
        if (
            workflow["claim_owner"] != owner_key
            or stored_kind is None
            or stored_digest is None
            or expires_at is None
            or workflow["claim_epoch"] != epoch
            or expires_at <= decision_time
            or actor_project != _actor_for_work(ticket, stored_kind)
            or not hmac.compare_digest(str(stored_digest), _token_digest(claim_token))
        ):
            raise DeliveryError("claim_fenced", "delivery claim is no longer current")
