"""Typed delivery tools for external orchestrators; no execution-agent control."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastmcp import FastMCP
from pydantic import Field, SecretStr

from brain_v42.mcp.delivery_transport import _DeliveryRegistry
from brain_v42.mcp.tools.tool_annotations import (
    _HEARTBEAT_ANNOTATIONS,
    _READ_ANNOTATIONS,
    _WRITE_ANNOTATIONS,
)
from brain_v42.models.delivery import (
    ArtifactBinding,
    ClaimResult,
    ClaimState,
    ContractInput,
    ContractRevision,
    DeliveryError,
    DeliveryPage,
    DeliveryRefreshResult,
    DeliveryView,
    MilestoneReceipt,
)
from brain_v42.provenance import UNEXPANDED_ACTOR, UNKNOWN_ACTOR, get_current_actor
from brain_v42.services.delivery_service import DeliveryService

TicketId = Annotated[
    str,
    Field(
        strict=True,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    ),
]
Project = Annotated[str, Field(strict=True, min_length=1, max_length=200, pattern=r"\S")]
Owner = Annotated[str, Field(strict=True, min_length=1, max_length=200, pattern=r"\S")]
Key = Annotated[str, Field(strict=True, min_length=1, max_length=200, pattern=r"\S")]
Reason = Annotated[str, Field(strict=True, min_length=1, max_length=4000, pattern=r"\S")]
Positive = Annotated[int, Field(strict=True, gt=0)]
Revision = Annotated[int, Field(strict=True, ge=0)]
Limit = Annotated[int, Field(strict=True, ge=1, le=100)]
TTL = Annotated[int, Field(strict=True, ge=60, le=3600)]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Cursor = Annotated[str, Field(strict=True, min_length=1, max_length=1000)]
ClaimToken = Annotated[SecretStr, Field(min_length=1, max_length=1000)]
Work = Literal["implement", "repair", "review", "integrate", "accept"]
Stage = Literal["awaiting_artifact", "proposed", "verified", "integrated"]


def register_delivery_tools(mcp: FastMCP, delivery_svc: DeliveryService) -> None:
    """Register nine versioned operations backed by one role-aware PG service."""
    delivery = _DeliveryRegistry(mcp)

    @delivery.tool(version="1.0", annotations=_WRITE_ANNOTATIONS)
    async def brain_delivery_contract_set(
        ticket_id: TicketId,
        actor_project: Project,
        contract: ContractInput,
        expected_revision: Revision,
        idempotency_key: Key,
        reason: Reason,
    ) -> ContractRevision:
        """Create or amend the requester's delivery contract and retain its reason."""
        return await delivery_svc.set_contract(
            UUID(ticket_id),
            actor_project=actor_project,
            contract=contract,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            reason=reason,
        )

    @delivery.tool(version="1.0", annotations=_WRITE_ANNOTATIONS)
    async def brain_delivery_bind_pr(
        ticket_id: TicketId,
        actor_project: Project,
        deliverable_key: Annotated[str, Field(strict=True, min_length=1, max_length=64)],
        repository_id: Positive,
        pr_number: Positive,
        expected_revision: Positive,
        expected_workflow_version: Positive,
        idempotency_key: Key,
    ) -> ArtifactBinding:
        """Bind the executor's registered GitHub PR to an exact contract generation."""
        return await delivery_svc.bind_pr(
            UUID(ticket_id),
            actor_project=actor_project,
            deliverable_key=deliverable_key,
            repository_id=repository_id,
            pr_number=pr_number,
            expected_revision=expected_revision,
            expected_workflow_version=expected_workflow_version,
            idempotency_key=idempotency_key,
        )

    @delivery.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_delivery_get(
        ticket_id: TicketId,
        actor_project: Project,
        history_limit: Limit = 20,
        history_cursor: Cursor | None = None,
    ) -> DeliveryView:
        """Read persisted delivery evidence, freshness, blockers and immutable history."""
        return await delivery_svc.get(
            UUID(ticket_id),
            actor_project=actor_project,
            history_limit=history_limit,
            history_cursor=history_cursor,
        )

    @delivery.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_delivery_list(
        actor_project: Project,
        limit: Limit = 20,
        cursor: Cursor | None = None,
        work: Work | None = None,
        blocker: Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,99}$")]
        | None = None,
        stage: Stage | None = None,
    ) -> DeliveryPage:
        """List a participant's observed deliveries, filtered before bounded pagination."""
        return await delivery_svc.list(
            actor_project=actor_project,
            limit=limit,
            cursor=cursor,
            work=work,
            blocker=blocker,
            stage=stage,
        )

    @delivery.tool(version="1.0", annotations=_WRITE_ANNOTATIONS)
    async def brain_delivery_refresh(
        ticket_id: TicketId, actor_project: Project
    ) -> DeliveryRefreshResult:
        """Queue independent observation and return the currently persisted delivery view."""
        view = await delivery_svc.refresh(UUID(ticket_id), actor_project=actor_project)
        return DeliveryRefreshResult(view=view)

    @delivery.tool(version="1.0", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_delivery_claim(
        ticket_id: TicketId,
        actor_project: Project,
        owner_key: Owner,
        work_kind: Work,
        expected_workflow_version: Positive,
        expected_assessment_id: Digest,
        ttl_seconds: TTL = 900,
    ) -> ClaimResult:
        """Atomically claim eligible external work; returns the secret token once.

        Brain fences claim mutations with the epoch. The external orchestrator
        must fence its own execution; claim expiry never stops an agent.
        """
        return await delivery_svc.claim(
            UUID(ticket_id),
            actor_project=actor_project,
            owner_key=owner_key,
            work_kind=work_kind,
            expected_workflow_version=expected_workflow_version,
            expected_assessment_id=expected_assessment_id,
            ttl_seconds=ttl_seconds,
        )

    @delivery.tool(version="1.0", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_delivery_claim_renew(
        ticket_id: TicketId,
        actor_project: Project,
        owner_key: Owner,
        claim_token: ClaimToken,
        epoch: Positive,
        ttl_seconds: TTL = 900,
    ) -> ClaimState:
        """Renew only the matching, unexpired external owner's token and fencing epoch."""
        return await delivery_svc.renew_claim(
            UUID(ticket_id),
            actor_project=actor_project,
            owner_key=owner_key,
            claim_token=claim_token.get_secret_value(),
            epoch=epoch,
            ttl_seconds=ttl_seconds,
        )

    @delivery.tool(version="1.0", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_delivery_claim_release(
        ticket_id: TicketId,
        actor_project: Project,
        owner_key: Owner,
        claim_token: ClaimToken,
        epoch: Positive,
    ) -> ClaimState:
        """Release only the exact live claim; a stale external owner cannot release its successor."""
        return await delivery_svc.release_claim(
            UUID(ticket_id),
            actor_project=actor_project,
            owner_key=owner_key,
            claim_token=claim_token.get_secret_value(),
            epoch=epoch,
        )

    @delivery.tool(version="1.0", annotations=_WRITE_ANNOTATIONS)
    async def brain_delivery_accept(
        ticket_id: TicketId,
        actor_project: Project,
        rationale: Reason,
        expected_revision: Positive,
        expected_attempt: Positive,
        expected_delivery_digest: Digest,
    ) -> MilestoneReceipt:
        """Record the requester's acceptance of exact current integration evidence.

        The caller label comes from X-Brain-Agent under the existing admin trust
        boundary. An unknown caller cannot approve a delivery.
        """
        caller = get_current_actor()
        if not caller.strip() or caller in {UNKNOWN_ACTOR, UNEXPANDED_ACTOR}:
            raise DeliveryError("invalid_acceptance", "a declared requester caller is required")
        return await delivery_svc.accept(
            UUID(ticket_id),
            actor_project=actor_project,
            caller_identity=caller,
            rationale=rationale,
            expected_revision=expected_revision,
            expected_attempt=expected_attempt,
            expected_delivery_digest=expected_delivery_digest,
        )
