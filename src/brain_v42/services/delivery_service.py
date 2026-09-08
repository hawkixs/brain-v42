"""Role-aware public use cases for persisted delivery workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from pydantic import ValidationError

from brain_v42.db.tables import tickets
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import (
    ArtifactBinding,
    ClaimResult,
    ClaimState,
    ContractInput,
    ContractRevision,
    DeliveryError,
    DeliveryListFilters,
    DeliveryPage,
    DeliveryView,
    MilestoneReceipt,
    RepositoryDocumentReference,
)
from brain_v42.models.delivery_hashes import canonical_digest
from brain_v42.repositories.pg_delivery import PgDeliveryRepo, _contract_from_json, lock_workflows
from brain_v42.repositories.pg_delivery_claims import PgDeliveryClaimsRepo
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo


class DeliveryService:
    def __init__(
        self,
        repo: PgDeliveryRepo,
        *,
        settings: DeliverySettings,
        evidence_repo: PgDeliveryEvidenceRepo | None = None,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._evidence_repo = evidence_repo or PgDeliveryEvidenceRepo(repo._session_factory)
        self._claims_repo = PgDeliveryClaimsRepo(repo._session_factory)

    async def claim(
        self,
        ticket_id: UUID,
        *,
        actor_project: str,
        owner_key: str,
        work_kind: str,
        expected_workflow_version: int,
        expected_assessment_id: str,
        ttl_seconds: int = 900,
    ) -> ClaimResult:
        async with self._repo._maybe_session(None, write=True) as session:
            return await self._claims_repo.acquire(
                session,
                ticket_id,
                settings=self._settings,
                actor_project=actor_project,
                owner_key=owner_key,
                work_kind=work_kind,
                expected_workflow_version=expected_workflow_version,
                expected_assessment_id=expected_assessment_id,
                ttl_seconds=ttl_seconds,
            )

    async def renew_claim(
        self,
        ticket_id: UUID,
        *,
        actor_project: str,
        owner_key: str,
        claim_token: str,
        epoch: int,
        ttl_seconds: int = 900,
    ) -> ClaimState:
        if not self._settings.enabled:
            raise DeliveryError("delivery_disabled", "delivery workflow operations are disabled")
        async with self._repo._maybe_session(None, write=True) as session:
            return await self._claims_repo.renew(
                session,
                ticket_id,
                settings=self._settings,
                actor_project=actor_project,
                owner_key=owner_key,
                claim_token=claim_token,
                epoch=epoch,
                ttl_seconds=ttl_seconds,
            )

    async def release_claim(
        self,
        ticket_id: UUID,
        *,
        actor_project: str,
        owner_key: str,
        claim_token: str,
        epoch: int,
    ) -> ClaimState:
        if not self._settings.enabled:
            raise DeliveryError("delivery_disabled", "delivery workflow operations are disabled")
        async with self._repo._maybe_session(None, write=True) as session:
            return await self._claims_repo.release(
                session,
                ticket_id,
                settings=self._settings,
                actor_project=actor_project,
                owner_key=owner_key,
                claim_token=claim_token,
                epoch=epoch,
            )

    async def accept(
        self,
        ticket_id: UUID,
        *,
        actor_project: str,
        caller_identity: str,
        rationale: str,
        expected_revision: int,
        expected_attempt: int,
        expected_delivery_digest: str,
    ) -> MilestoneReceipt:
        """Record one explicit requester acceptance from the exact current delivery."""
        if not self._settings.enabled:
            raise DeliveryError("delivery_disabled", "delivery workflow operations are disabled")
        async with self._repo._maybe_session(None, write=True) as session:
            return await self._evidence_repo.accept(
                session,
                ticket_id,
                settings=self._settings,
                actor_project=actor_project,
                caller_identity=caller_identity,
                rationale=rationale,
                expected_revision=expected_revision,
                expected_attempt=expected_attempt,
                expected_delivery_digest=expected_delivery_digest,
            )

    async def set_contract(
        self,
        ticket_id: UUID,
        *,
        actor_project: str,
        contract: ContractInput,
        expected_revision: int,
        idempotency_key: str,
        reason: str | None = None,
    ) -> ContractRevision:
        if not self._settings.enabled:
            raise DeliveryError("delivery_disabled", "delivery workflow operations are disabled")
        if reason is not None and (
            not isinstance(reason, str) or not 1 <= len(reason) <= 4000 or not reason.strip()
        ):
            raise DeliveryError(
                "invalid_reason", "reason must contain between 1 and 4000 characters"
            )
        request_digest = canonical_digest(
            {
                "operation": "set_contract",
                "ticket_id": str(ticket_id),
                "actor_project": actor_project,
                "expected_revision": expected_revision,
                "contract": contract.model_dump(mode="json"),
                "reason": reason,
            },
            domain="request",
        )
        async with self._repo._maybe_session(None, write=True) as session:
            ticket = (
                (await session.execute(sa.select(tickets).where(tickets.c.id == ticket_id)))
                .mappings()
                .one_or_none()
            )
            if ticket is None:
                raise DeliveryError("ticket_not_found", "ticket was not found")
            if ticket["kind"] != "request" or ticket["status"] in {"closed", "acked"}:
                raise DeliveryError(
                    "ticket_not_contractable", "only active request tickets may hold a contract"
                )
            if actor_project != ticket["from_project"]:
                raise DeliveryError("not_allowed", "only the requester may set a delivery contract")
            registry = self._settings.repositories_for(ticket["to_project"])
            normalized = []
            for deliverable in contract.deliverables:
                matched = [
                    repository_id
                    for repository_id, name in registry.items()
                    if name == deliverable.repository
                ]
                if len(matched) != 1:
                    raise DeliveryError(
                        "unknown_repository",
                        "deliverable repository is not registered for the executor project",
                    )
                normalized.append(
                    type(deliverable).model_validate(
                        {**deliverable.model_dump(), "repository_id": matched[0]}
                    )
                )
            unresolved_context = []
            for reference in contract.context_refs:
                if isinstance(reference, RepositoryDocumentReference):
                    if reference.repository_id not in registry:
                        raise DeliveryError(
                            "unknown_repository",
                            "context repository is not registered for the executor project",
                        )
                unresolved_context.append(reference)
            async with lock_workflows(session, (ticket_id,), graph_write=True):
                replay = await self._repo._event_replay(
                    session,
                    operation="set_contract",
                    actor_project=actor_project,
                    key=idempotency_key,
                    digest=request_digest,
                )
                if replay is not None:
                    return _contract_from_json(replay)
                if expected_revision == 0 and ticket["status"] not in {"open", "in_progress"}:
                    raise DeliveryError(
                        "ticket_not_contractable", "only active request tickets may hold a contract"
                    )
                pinned_context = await self._repo.resolve_context_references(
                    session, unresolved_context
                )
                revision = ContractRevision(
                    ticket_id=ticket_id,
                    contract_revision=expected_revision + 1,
                    author_project=actor_project,
                    created_at=datetime.now(UTC),
                    amendment_reason=reason
                    if reason is not None
                    else ("amendment" if expected_revision else None),
                    schema_version=contract.schema_version,
                    objective=contract.objective,
                    constraints=contract.constraints,
                    acceptance_criteria=contract.acceptance_criteria,
                    priority=contract.priority,
                    context_refs=pinned_context,
                    dependencies=contract.dependencies,
                    deliverables=tuple(normalized),
                    acceptance_mode=contract.acceptance_mode,
                )
                return await self._repo.set_contract(
                    revision,
                    expected_revision=expected_revision,
                    actor_project=actor_project,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    session=session,
                )

    async def bind_pr(
        self,
        ticket_id: UUID,
        *,
        actor_project: str,
        deliverable_key: str,
        repository_id: int,
        pr_number: int,
        expected_revision: int,
        expected_workflow_version: int,
        idempotency_key: str,
    ) -> ArtifactBinding:
        if not self._settings.enabled:
            raise DeliveryError("delivery_disabled", "delivery workflow operations are disabled")
        request_digest = canonical_digest(
            {
                "operation": "bind_pr",
                "ticket_id": str(ticket_id),
                "actor_project": actor_project,
                "deliverable_key": deliverable_key,
                "repository_id": repository_id,
                "pr_number": pr_number,
                "expected_revision": expected_revision,
                "expected_workflow_version": expected_workflow_version,
            },
            domain="request",
        )
        async with self._repo._maybe_session(None, write=True) as session:
            async with lock_workflows(session, (ticket_id,), graph_write=True):
                ticket = (
                    (
                        await session.execute(
                            sa.select(tickets).where(tickets.c.id == ticket_id).with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if ticket is None:
                    raise DeliveryError("ticket_not_found", "ticket was not found")
                if actor_project != ticket["to_project"]:
                    raise DeliveryError("not_allowed", "only the executor may bind a pull request")
                ticket_registry = self._settings.repositories_for(ticket["to_project"])
                if repository_id not in ticket_registry:
                    raise DeliveryError("unknown_repository", "repository is not registered")
                return await self._repo.bind_pr(
                    ArtifactBinding(
                        ticket_id=ticket_id,
                        contract_revision=expected_revision,
                        attempt=1,
                        deliverable_key=deliverable_key,
                        repository_id=repository_id,
                        pr_number=pr_number,
                    ),
                    repository_name=ticket_registry[repository_id],
                    actor_project=actor_project,
                    expected_revision=expected_revision,
                    expected_workflow_version=expected_workflow_version,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    session=session,
                )

    async def get(
        self,
        ticket_id: UUID,
        *,
        actor_project: str,
        history_limit: int = 20,
        history_cursor: str | None = None,
    ) -> DeliveryView:
        if type(history_limit) is not int or not 1 <= history_limit <= 100:
            raise DeliveryError("invalid_limit", "history limit must be between 1 and 100")
        async with self._repo._maybe_session(None, write=False) as session:
            ticket = (
                (await session.execute(sa.select(tickets).where(tickets.c.id == ticket_id)))
                .mappings()
                .one_or_none()
            )
        if ticket is None:
            raise DeliveryError("ticket_not_found", "ticket was not found")
        if actor_project not in {ticket["from_project"], ticket["to_project"]}:
            raise DeliveryError("not_allowed", "actor is not a ticket participant")
        view = await self._repo.get_view(
            ticket_id,
            feature_enabled=self._settings.enabled,
            freshness_seconds=self._settings.freshness_seconds,
            history_limit=history_limit,
            history_cursor=history_cursor,
        )
        if view is None:
            raise DeliveryError("contract_not_found", "delivery contract was not found")
        return view

    async def list(
        self,
        *,
        actor_project: str,
        limit: int = 20,
        cursor: str | None = None,
        work: str | None = None,
        blocker: str | None = None,
        stage: str | None = None,
    ) -> DeliveryPage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DeliveryError("invalid_limit", "limit must be between 1 and 100")
        try:
            filters = DeliveryListFilters.model_validate(
                {"work": work, "blocker": blocker, "stage": stage}
            )
        except ValidationError:
            raise DeliveryError("invalid_filter", "delivery filters are invalid") from None
        return await self._repo.list_views(
            actor_project,
            feature_enabled=self._settings.enabled,
            freshness_seconds=self._settings.freshness_seconds,
            limit=limit,
            cursor=cursor,
            filters=filters,
        )

    async def refresh(self, ticket_id: UUID, *, actor_project: str) -> DeliveryView:
        if not self._settings.enabled:
            raise DeliveryError("delivery_disabled", "delivery workflow operations are disabled")
        await self.get(ticket_id, actor_project=actor_project)
        await self._repo.refresh(ticket_id)
        return await self.get(ticket_id, actor_project=actor_project)
