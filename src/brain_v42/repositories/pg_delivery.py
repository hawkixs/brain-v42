"""Transactional PostgreSQL persistence for delivery contracts and proposed PR bindings."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import (
    adrs,
    decisions,
    delivery_artifact_bindings,
    delivery_confirmations,
    delivery_contract_revisions,
    delivery_dependencies,
    delivery_events,
    delivery_receipts,
    delivery_snapshots,
    delivery_workflows,
    indexed_plans,
    learnings,
    runbooks,
    snippets,
    tickets,
)
from brain_v42.models.delivery import (
    ArtifactBinding,
    BindingEvidence,
    BrainEntityReference,
    ClaimState,
    ContextPredicate,
    ContextReference,
    ContractRevision,
    DeliveryError,
    DeliveryPage,
    DeliveryView,
    DependencyPredicate,
    EvaluationInput,
    MilestoneReceipt,
    ObservationConfirmation,
    PinnedBrainEntityReference,
    PinnedContextReference,
    PullRequestEvidence,
    RepositoryContextEvidence,
    RepositoryDocumentReference,
    context_reference_digest,
    context_reference_identity,
)
from brain_v42.models.delivery_evaluator import evaluate_delivery
from brain_v42.models.delivery_hashes import canonical_digest, delivery_digest
from brain_v42.models.ticket import Ticket
from brain_v42.repositories.pg_base import BasePgRepository

# Namespaces are fixed and distinct: graph edits must serialize independently
# from the observer's future fenced owner lock.
DELIVERY_GRAPH_LOCK = 7_311_041_001
DELIVERY_OBSERVER_LOCK = 7_311_041_002

_CONTEXT_TABLES: dict[str, tuple[sa.Table, tuple[str, ...]]] = {
    "decision": (
        decisions,
        (
            "id",
            "title",
            "description",
            "reasoning",
            "alternatives",
            "consequences",
            "status",
            "project_key",
            "merged_into",
        ),
    ),
    "learning": (
        learnings,
        (
            "id",
            "topic",
            "insight",
            "source",
            "source_type",
            "confidence",
            "project_key",
            "tags",
            "merged_into",
        ),
    ),
    "snippet": (
        snippets,
        (
            "id",
            "title",
            "intention",
            "code",
            "language",
            "dependencies",
            "usage_example",
            "gotchas",
            "project_key",
            "tags",
            "merged_into",
        ),
    ),
    "runbook": (
        runbooks,
        (
            "id",
            "title",
            "description",
            "project_key",
            "trigger",
            "prerequisites",
            "steps",
            "rollback_steps",
            "estimated_duration",
            "tags",
            "merged_into",
        ),
    ),
    "adr": (
        adrs,
        (
            "id",
            "number",
            "title",
            "context",
            "decision",
            "consequences",
            "project_key",
            "tags",
            "status",
            "superseded_by",
            "merged_into",
        ),
    ),
    "plan": (indexed_plans, ("id", "title", "content", "status", "project_key", "plan_type")),
}


@dataclass(frozen=True, slots=True)
class LockedDeliveryDecisionScope:
    """Caller-owned transaction locks retained for one delivery decision."""

    ticket: Ticket


@dataclass(frozen=True, slots=True)
class LockedDeliveryDecisionInputs:
    """Current hydrated decision facts sampled at one PostgreSQL instant."""

    ticket: Ticket
    inputs: EvaluationInput | None
    decision_time: datetime


@asynccontextmanager
async def lock_workflows(
    session: AsyncSession, ticket_ids: Iterable[UUID], *, graph_write: bool = False
) -> AsyncIterator[None]:
    """Acquire graph then deterministic ticket/workflow row locks in one transaction."""
    lock = sa.func.pg_advisory_xact_lock if graph_write else sa.func.pg_advisory_xact_lock_shared
    await session.execute(sa.select(lock(DELIVERY_GRAPH_LOCK)))
    ids = sorted(set(ticket_ids), key=str)
    if ids:
        await session.execute(
            sa.select(tickets.c.id)
            .where(tickets.c.id.in_(ids))
            .order_by(tickets.c.id)
            .with_for_update()
        )
        await session.execute(
            sa.select(delivery_workflows.c.ticket_id)
            .where(delivery_workflows.c.ticket_id.in_(ids))
            .order_by(delivery_workflows.c.ticket_id)
            .with_for_update()
        )
    yield


class PgDeliveryRepo(BasePgRepository):
    """Persist immutable contract revisions; callers may retain transaction ownership."""

    async def _event_replay(
        self, session: AsyncSession, *, operation: str, actor_project: str, key: str, digest: str
    ) -> dict[str, Any] | None:
        row = (
            (
                await session.execute(
                    sa.select(delivery_events).where(
                        delivery_events.c.operation == operation,
                        delivery_events.c.actor_project == actor_project,
                        delivery_events.c.idempotency_key == key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if row["request_digest"] != digest:
            raise DeliveryError(
                "idempotency_key_reused", "idempotency key was already used for different content"
            )
        return dict(row["result"])

    async def set_contract(
        self,
        contract: ContractRevision,
        *,
        expected_revision: int,
        actor_project: str,
        idempotency_key: str,
        request_digest: str,
        session: AsyncSession | None = None,
    ) -> ContractRevision:
        async with self._maybe_session(session, write=True) as sess:
            replay = await self._event_replay(
                sess,
                operation="set_contract",
                actor_project=actor_project,
                key=idempotency_key,
                digest=request_digest,
            )
            if replay is not None:
                return _contract_from_json(replay)
            async with lock_workflows(sess, (contract.ticket_id,), graph_write=True):
                replay = await self._event_replay(
                    sess,
                    operation="set_contract",
                    actor_project=actor_project,
                    key=idempotency_key,
                    digest=request_digest,
                )
                if replay is not None:
                    return _contract_from_json(replay)
                await _validate_dependencies(sess, contract, actor_project)
                current = (
                    (
                        await sess.execute(
                            sa.select(delivery_workflows)
                            .where(delivery_workflows.c.ticket_id == contract.ticket_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if current is None:
                    if expected_revision != 0 or contract.contract_revision != 1:
                        raise DeliveryError(
                            "revision_conflict", "contract revision is no longer current"
                        )
                    await sess.execute(
                        delivery_workflows.insert().values(
                            ticket_id=contract.ticket_id,
                            current_revision=1,
                            context_set_digest=_context_set_digest(contract),
                            context_due_at=datetime.now(UTC),
                        )
                    )
                else:
                    if (
                        expected_revision != current["current_revision"]
                        or contract.contract_revision != current["current_revision"] + 1
                    ):
                        raise DeliveryError(
                            "revision_conflict", "contract revision is no longer current"
                        )
                    await sess.execute(
                        delivery_workflows.update()
                        .where(delivery_workflows.c.ticket_id == contract.ticket_id)
                        .values(
                            current_revision=contract.contract_revision,
                            row_version=current["row_version"] + 1,
                            context_row_version=current["context_row_version"] + 1,
                            context_set_digest=_context_set_digest(contract),
                            context_due_at=datetime.now(UTC),
                            latest_context_success_confirmation_id=None,
                            latest_context_attempt_confirmation_id=None,
                            updated_at=sa.func.now(),
                        )
                    )
                stored = contract.model_dump(mode="json")
                await sess.execute(
                    delivery_contract_revisions.insert().values(
                        ticket_id=contract.ticket_id,
                        contract_revision=contract.contract_revision,
                        normalized_contract=stored,
                        content_digest=contract.content_digest,
                        context_set_digest=_context_set_digest(contract),
                        author_project=contract.author_project,
                        amendment_reason=contract.amendment_reason,
                        created_at=contract.created_at,
                    )
                )
                for dependency in contract.dependencies:
                    await sess.execute(
                        delivery_dependencies.insert().values(
                            ticket_id=contract.ticket_id,
                            contract_revision=contract.contract_revision,
                            upstream_ticket_id=dependency.ticket_id,
                            upstream_revision=dependency.contract_revision,
                            upstream_attempt=dependency.attempt,
                            milestone=dependency.milestone,
                        )
                    )
                result = contract.model_dump(mode="json")
                await sess.execute(
                    delivery_events.insert().values(
                        operation="set_contract",
                        actor_project=actor_project,
                        ticket_id=contract.ticket_id,
                        idempotency_key=idempotency_key,
                        request_digest=request_digest,
                        result=result,
                        payload={"expected_revision": expected_revision},
                    )
                )
                return contract

    async def resolve_context_references(
        self, session: AsyncSession, references: Iterable[ContextReference]
    ) -> tuple[PinnedContextReference, ...]:
        """Freeze supported Brain sources while the caller owns the decision transaction."""
        original = tuple(references)
        pinned: dict[tuple[str, UUID], PinnedBrainEntityReference] = {}
        brain_references = sorted(
            (reference for reference in original if isinstance(reference, BrainEntityReference)),
            key=lambda reference: (
                _context_table_rank(reference.entity_type),
                str(reference.entity_id),
            ),
        )
        for reference in brain_references:
            table, fields = _CONTEXT_TABLES[reference.entity_type]
            row = (
                (
                    await session.execute(
                        sa.select(table)
                        .where(table.c.id == reference.entity_id)
                        .with_for_update(read=True)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise DeliveryError("context_unavailable", "required Brain context is unavailable")
            content = {
                field: row[field] for field in fields if field in row and row[field] is not None
            }
            snapshot = json.dumps(
                content, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
            )
            digest = canonical_digest(
                {
                    "entity_type": reference.entity_type,
                    "entity_id": str(reference.entity_id),
                    "content": snapshot,
                },
                domain="result",
            )
            pinned[(reference.entity_type, reference.entity_id)] = PinnedBrainEntityReference(
                kind="brain_entity",
                entity_type=reference.entity_type,
                entity_id=reference.entity_id,
                required=reference.required,
                content_snapshot=snapshot,
                content_digest=digest,
            )
        resolved: list[PinnedContextReference] = []
        for source_reference in original:
            if isinstance(source_reference, BrainEntityReference):
                resolved.append(pinned[(source_reference.entity_type, source_reference.entity_id)])
            else:
                resolved.append(source_reference)
        return tuple(resolved)

    async def bind_pr(
        self,
        binding: ArtifactBinding,
        *,
        repository_name: str,
        actor_project: str,
        expected_revision: int,
        expected_workflow_version: int,
        idempotency_key: str,
        request_digest: str,
        session: AsyncSession | None = None,
    ) -> ArtifactBinding:
        async with self._maybe_session(session, write=True) as sess:
            replay = await self._event_replay(
                sess,
                operation="bind_pr",
                actor_project=actor_project,
                key=idempotency_key,
                digest=request_digest,
            )
            if replay is not None:
                return ArtifactBinding.model_validate(replay)
            async with lock_workflows(sess, (binding.ticket_id,), graph_write=True):
                replay = await self._event_replay(
                    sess,
                    operation="bind_pr",
                    actor_project=actor_project,
                    key=idempotency_key,
                    digest=request_digest,
                )
                if replay is not None:
                    return ArtifactBinding.model_validate(replay)
                workflow = (
                    (
                        await sess.execute(
                            sa.select(delivery_workflows)
                            .where(delivery_workflows.c.ticket_id == binding.ticket_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                ticket = (
                    (
                        await sess.execute(
                            sa.select(tickets)
                            .where(tickets.c.id == binding.ticket_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if workflow is None:
                    raise DeliveryError("contract_not_found", "delivery contract was not found")
                if ticket is None:
                    raise DeliveryError("ticket_not_found", "ticket was not found")
                if ticket["kind"] != "request" or ticket["status"] in {
                    "wontfix",
                    "closed",
                    "acked",
                }:
                    raise DeliveryError(
                        "ticket_not_contractable", "ticket cannot accept a pull request"
                    )
                if actor_project != ticket["to_project"]:
                    raise DeliveryError("not_allowed", "only the executor may bind a pull request")
                if (
                    expected_revision != workflow["current_revision"]
                    or expected_workflow_version != workflow["row_version"]
                ):
                    raise DeliveryError(
                        "revision_conflict", "delivery workflow is no longer current"
                    )
                revision = (
                    (
                        await sess.execute(
                            sa.select(delivery_contract_revisions.c.normalized_contract).where(
                                delivery_contract_revisions.c.ticket_id == binding.ticket_id,
                                delivery_contract_revisions.c.contract_revision
                                == workflow["current_revision"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                contract = _contract_from_json(dict(revision["normalized_contract"]))
                deliverable = next(
                    (item for item in contract.deliverables if item.key == binding.deliverable_key),
                    None,
                )
                if deliverable is None:
                    raise DeliveryError(
                        "unknown_deliverable", "deliverable key is not in the current contract"
                    )
                if (
                    deliverable.repository_id != binding.repository_id
                    or deliverable.repository != repository_name
                ):
                    raise DeliveryError(
                        "repository_mismatch", "binding repository differs from its deliverable"
                    )
                binding = ArtifactBinding(
                    ticket_id=binding.ticket_id,
                    contract_revision=workflow["current_revision"],
                    attempt=workflow["attempt"],
                    deliverable_key=binding.deliverable_key,
                    repository_id=binding.repository_id,
                    pr_number=binding.pr_number,
                )
                current = (
                    (
                        await sess.execute(
                            sa.select(delivery_artifact_bindings)
                            .where(
                                delivery_artifact_bindings.c.ticket_id == binding.ticket_id,
                                delivery_artifact_bindings.c.contract_revision
                                == binding.contract_revision,
                                delivery_artifact_bindings.c.attempt == binding.attempt,
                                delivery_artifact_bindings.c.deliverable_key
                                == binding.deliverable_key,
                                delivery_artifact_bindings.c.active.is_(True),
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if current is not None:
                    await sess.execute(
                        delivery_artifact_bindings.update()
                        .where(delivery_artifact_bindings.c.id == current["id"])
                        .values(active=False, row_version=current["row_version"] + 1)
                    )
                await sess.execute(
                    delivery_artifact_bindings.insert().values(
                        id=binding.id,
                        ticket_id=binding.ticket_id,
                        contract_revision=binding.contract_revision,
                        attempt=binding.attempt,
                        deliverable_key=binding.deliverable_key,
                        repository_id=binding.repository_id,
                        repository_name=repository_name,
                        pr_number=binding.pr_number,
                        state=binding.state,
                        active=True,
                        head_sha=None,
                        base_sha=None,
                        integration_sha=None,
                        row_version=binding.binding_version,
                        due_at=datetime.now(UTC),
                    )
                )
                result = binding.model_dump(mode="json")
                await sess.execute(
                    delivery_events.insert().values(
                        operation="bind_pr",
                        actor_project=actor_project,
                        ticket_id=binding.ticket_id,
                        idempotency_key=idempotency_key,
                        request_digest=request_digest,
                        result=result,
                        payload={
                            "expected_revision": expected_revision,
                            "expected_workflow_version": expected_workflow_version,
                        },
                    )
                )
                await sess.execute(
                    delivery_workflows.update()
                    .where(
                        delivery_workflows.c.ticket_id == binding.ticket_id,
                        delivery_workflows.c.current_revision == expected_revision,
                        delivery_workflows.c.row_version == expected_workflow_version,
                    )
                    .values(row_version=expected_workflow_version + 1, updated_at=sa.func.now())
                )
                return binding

    async def load_inputs(
        self,
        ticket_id: UUID,
        *,
        feature_enabled: bool,
        freshness_seconds: int,
        session: AsyncSession | None = None,
    ) -> EvaluationInput | None:
        async with self._maybe_session(session, write=False) as sess:
            workflow = (
                (
                    await sess.execute(
                        sa.select(delivery_workflows).where(
                            delivery_workflows.c.ticket_id == ticket_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if workflow is None:
                return None
            ticket = (
                (await sess.execute(sa.select(tickets).where(tickets.c.id == ticket_id)))
                .mappings()
                .one()
            )
            revision = (
                (
                    await sess.execute(
                        sa.select(delivery_contract_revisions).where(
                            delivery_contract_revisions.c.ticket_id == ticket_id,
                            delivery_contract_revisions.c.contract_revision
                            == workflow["current_revision"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            contract = _contract_from_json(dict(revision["normalized_contract"]))
            contract_digest = _required_contract_digest(contract)
            binding_rows = (
                (
                    await sess.execute(
                        sa.select(delivery_artifact_bindings).where(
                            delivery_artifact_bindings.c.ticket_id == ticket_id,
                            delivery_artifact_bindings.c.contract_revision
                            == workflow["current_revision"],
                            delivery_artifact_bindings.c.attempt == workflow["attempt"],
                            delivery_artifact_bindings.c.active.is_(True),
                        )
                    )
                )
                .mappings()
                .all()
            )
            bindings = tuple([await _binding_evidence(sess, row) for row in binding_rows])
            current_delivery_digest = delivery_digest(
                contract_digest=contract_digest,
                attempt=workflow["attempt"],
                active_bindings=bindings,
            )
            integration_receipt = await _matching_receipt(
                sess,
                ticket_id=ticket_id,
                contract_revision=workflow["current_revision"],
                attempt=workflow["attempt"],
                milestone="integration",
                contract_digest=contract_digest,
                delivery_digest=current_delivery_digest,
            )
            fulfillment_receipt = await _matching_receipt(
                sess,
                ticket_id=ticket_id,
                contract_revision=workflow["current_revision"],
                attempt=workflow["attempt"],
                milestone="fulfilled",
                contract_digest=contract_digest,
                delivery_digest=current_delivery_digest,
            )
            upstream_revision = delivery_contract_revisions.alias("upstream_revision")
            dependency_rows = (
                (
                    await sess.execute(
                        sa.select(
                            delivery_dependencies,
                            delivery_workflows.c.current_revision,
                            delivery_workflows.c.attempt.label("current_attempt"),
                            delivery_workflows.c.disposition,
                            upstream_revision.c.content_digest,
                            upstream_revision.c.normalized_contract,
                        )
                        .join(
                            delivery_workflows,
                            delivery_workflows.c.ticket_id
                            == delivery_dependencies.c.upstream_ticket_id,
                        )
                        .join(
                            upstream_revision,
                            sa.and_(
                                upstream_revision.c.ticket_id
                                == delivery_dependencies.c.upstream_ticket_id,
                                upstream_revision.c.contract_revision
                                == delivery_workflows.c.current_revision,
                            ),
                        )
                        .where(
                            delivery_dependencies.c.ticket_id == ticket_id,
                            delivery_dependencies.c.contract_revision
                            == workflow["current_revision"],
                        )
                    )
                )
                .mappings()
                .all()
            )
            dependencies: list[DependencyPredicate] = []
            for row in dependency_rows:
                upstream_contract = _contract_from_json(dict(row["normalized_contract"]))
                upstream_contract_digest = _required_contract_digest(upstream_contract)
                upstream_binding_rows = (
                    (
                        await sess.execute(
                            sa.select(delivery_artifact_bindings).where(
                                delivery_artifact_bindings.c.ticket_id == row["upstream_ticket_id"],
                                delivery_artifact_bindings.c.contract_revision
                                == row["current_revision"],
                                delivery_artifact_bindings.c.attempt == row["current_attempt"],
                                delivery_artifact_bindings.c.active.is_(True),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                upstream_bindings = tuple(
                    [
                        await _binding_evidence(sess, binding_row)
                        for binding_row in upstream_binding_rows
                    ]
                )
                upstream_delivery_digest = delivery_digest(
                    contract_digest=upstream_contract_digest,
                    attempt=row["current_attempt"],
                    active_bindings=upstream_bindings,
                )
                receipt = None
                if (
                    row["disposition"] not in {"cancelled", "wontfix"}
                    and row["upstream_revision"] == row["current_revision"]
                    and row["upstream_attempt"] == row["current_attempt"]
                ):
                    receipt = await _matching_receipt(
                        sess,
                        ticket_id=row["upstream_ticket_id"],
                        contract_revision=row["upstream_revision"],
                        attempt=row["upstream_attempt"],
                        milestone="integration"
                        if row["milestone"] == "integrated"
                        else "fulfilled",
                        contract_digest=upstream_contract_digest,
                        delivery_digest=upstream_delivery_digest,
                    )
                dependencies.append(
                    DependencyPredicate(
                        ticket_id=row["upstream_ticket_id"],
                        contract_revision=row["upstream_revision"],
                        attempt=row["upstream_attempt"],
                        milestone=row["milestone"],
                        current_contract_revision=row["current_revision"],
                        current_attempt=row["current_attempt"],
                        current_contract_digest=row["content_digest"],
                        current_delivery_digest=upstream_delivery_digest,
                        current_disposition=row["disposition"],
                        receipt=receipt,
                    )
                )
            contexts = tuple(await _context_predicates(sess, contract, workflow))
            return EvaluationInput(
                contract=contract,
                attempt=workflow["attempt"],
                workflow_version=workflow["row_version"],
                coordination_status=ticket["status"],
                coordination_disposition=workflow["disposition"],
                is_self_ticket=ticket["from_project"] == ticket["to_project"],
                active_bindings=bindings,
                contexts=contexts,
                dependencies=tuple(dependencies),
                integration_receipt=integration_receipt,
                fulfillment_receipt=fulfillment_receipt,
                feature_enabled=feature_enabled,
                freshness_seconds=freshness_seconds,
                claim=ClaimState(
                    epoch=workflow["claim_epoch"],
                    owner=workflow["claim_owner"],
                    expires_at=workflow["claim_expires_at"],
                ),
                executor_identity=ticket["to_project"],
            )

    async def lock_decision_scope(
        self, session: AsyncSession, ticket_id: UUID
    ) -> LockedDeliveryDecisionScope:
        """Acquire the full deterministic lock scope for a caller-owned decision.

        The graph lock precedes discovery.  Discovery identifies the current
        revision's direct dependencies without locking an individual ticket;
        only then are every involved ticket and workflow row locked by UUID.
        Required Brain sources follow in fixed table/UUID order.
        """
        await session.execute(sa.select(sa.func.pg_advisory_xact_lock_shared(DELIVERY_GRAPH_LOCK)))
        discovered = (
            (
                await session.execute(
                    sa.select(delivery_workflows.c.current_revision).where(
                        delivery_workflows.c.ticket_id == ticket_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        dependency_ids: tuple[UUID, ...] = ()
        if discovered is not None:
            dependency_ids = tuple(
                (
                    await session.execute(
                        sa.select(delivery_dependencies.c.upstream_ticket_id).where(
                            delivery_dependencies.c.ticket_id == ticket_id,
                            delivery_dependencies.c.contract_revision
                            == discovered["current_revision"],
                        )
                    )
                ).scalars()
            )
        ticket_ids = sorted({ticket_id, *dependency_ids}, key=str)
        locked_tickets = await session.execute(
            sa.select(tickets)
            .where(tickets.c.id.in_(ticket_ids))
            .order_by(tickets.c.id)
            .with_for_update()
        )
        ticket_rows = {row["id"]: dict(row) for row in locked_tickets.mappings()}
        ticket_row = ticket_rows.get(ticket_id)
        if ticket_row is None:
            raise DeliveryError("ticket_not_found", "ticket was not found")
        locked_workflows = await session.execute(
            sa.select(delivery_workflows)
            .where(delivery_workflows.c.ticket_id.in_(ticket_ids))
            .order_by(delivery_workflows.c.ticket_id)
            .with_for_update()
        )
        workflow_rows = {row["ticket_id"]: dict(row) for row in locked_workflows.mappings()}
        workflow = workflow_rows.get(ticket_id)
        if workflow is not None:
            revision = (
                (
                    await session.execute(
                        sa.select(delivery_contract_revisions.c.normalized_contract).where(
                            delivery_contract_revisions.c.ticket_id == ticket_id,
                            delivery_contract_revisions.c.contract_revision
                            == workflow["current_revision"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            await self._lock_required_brain_sources(
                session, _contract_from_json(dict(revision["normalized_contract"]))
            )
        return LockedDeliveryDecisionScope(ticket=Ticket.model_validate(ticket_row))

    async def load_locked_decision_inputs(
        self,
        session: AsyncSession,
        ticket_id: UUID,
        *,
        feature_enabled: bool,
        freshness_seconds: int,
    ) -> LockedDeliveryDecisionInputs:
        """Always acquire decision locks before hydrating current inputs and PG time."""
        scope = await self.lock_decision_scope(session, ticket_id)
        return await self._load_decision_inputs_under_scope(
            session,
            scope,
            feature_enabled=feature_enabled,
            freshness_seconds=freshness_seconds,
        )

    async def _load_decision_inputs_under_scope(
        self,
        session: AsyncSession,
        scope: LockedDeliveryDecisionScope,
        *,
        feature_enabled: bool,
        freshness_seconds: int,
    ) -> LockedDeliveryDecisionInputs:
        """Rehydrate after caller mutations while the previously acquired scope is retained."""
        decision_time = await session.scalar(sa.select(sa.func.clock_timestamp()))
        if not isinstance(decision_time, datetime):
            raise RuntimeError("PostgreSQL did not return a decision timestamp")
        inputs = await self.load_inputs(
            scope.ticket.id,
            feature_enabled=feature_enabled,
            freshness_seconds=freshness_seconds,
            session=session,
        )
        return LockedDeliveryDecisionInputs(
            ticket=scope.ticket,
            inputs=inputs,
            decision_time=decision_time,
        )

    async def _lock_required_brain_sources(
        self, session: AsyncSession, contract: ContractRevision
    ) -> None:
        """Lock each required Brain source after tickets/workflows in stable order."""
        references = sorted(
            (
                reference
                for reference in contract.context_refs
                if isinstance(reference, PinnedBrainEntityReference) and reference.required
            ),
            key=lambda reference: (
                _context_table_rank(reference.entity_type),
                str(reference.entity_id),
            ),
        )
        for reference in references:
            table, _fields = _CONTEXT_TABLES[reference.entity_type]
            await session.execute(
                sa.select(table.c.id)
                .where(table.c.id == reference.entity_id)
                .with_for_update(read=True)
            )

    async def get_view(
        self,
        ticket_id: UUID,
        *,
        feature_enabled: bool,
        freshness_seconds: int,
        session: AsyncSession | None = None,
    ) -> DeliveryView | None:
        inputs = await self.load_inputs(
            ticket_id,
            feature_enabled=feature_enabled,
            freshness_seconds=freshness_seconds,
            session=session,
        )
        if inputs is None:
            return None
        assessment = evaluate_delivery(inputs, now=datetime.now(UTC))
        return DeliveryView(
            contract=inputs.contract,
            assessment=assessment,
            bindings=inputs.active_bindings,
            contexts=inputs.contexts,
            integration_receipt=inputs.integration_receipt,
            fulfillment_receipt=inputs.fulfillment_receipt,
        )

    async def refresh(self, ticket_id: UUID, *, session: AsyncSession | None = None) -> None:
        """Queue workflow context and active bindings without publishing evidence."""
        async with self._maybe_session(session, write=True) as sess:
            async with lock_workflows(sess, (ticket_id,)):
                now = datetime.now(UTC)
                await sess.execute(
                    delivery_workflows.update()
                    .where(delivery_workflows.c.ticket_id == ticket_id)
                    .values(context_due_at=now, updated_at=sa.func.now())
                )
                await sess.execute(
                    delivery_artifact_bindings.update()
                    .where(
                        delivery_artifact_bindings.c.ticket_id == ticket_id,
                        delivery_artifact_bindings.c.active.is_(True),
                    )
                    .values(due_at=now)
                )

    async def list_views(
        self,
        actor_project: str,
        *,
        feature_enabled: bool,
        freshness_seconds: int,
        limit: int = 20,
        cursor: str | None = None,
        session: AsyncSession | None = None,
    ) -> DeliveryPage:
        async with self._maybe_session(session, write=False) as sess:
            filters: list[sa.ColumnElement[bool]] = [
                sa.or_(
                    tickets.c.from_project == actor_project,
                    tickets.c.to_project == actor_project,
                )
            ]
            if cursor:
                filters.append(delivery_workflows.c.ticket_id > UUID(cursor))
            stmt = (
                sa.select(delivery_workflows.c.ticket_id)
                .join(tickets)
                .where(*filters)
                .order_by(delivery_workflows.c.ticket_id)
                .limit(limit + 1)
            )
            ids = list((await sess.execute(stmt)).scalars())
            page_ids = ids[:limit]
            remaining = await sess.scalar(
                sa.select(sa.func.count())
                .select_from(delivery_workflows.join(tickets))
                .where(*filters)
            )
            next_cursor = str(page_ids[-1]) if len(ids) > limit else None
            views = [
                await self.get_view(
                    identifier,
                    feature_enabled=feature_enabled,
                    freshness_seconds=freshness_seconds,
                    session=sess,
                )
                for identifier in page_ids
            ]
            return DeliveryPage(
                items=tuple(view for view in views if view is not None),
                next_cursor=next_cursor,
                omitted_count=max((remaining or 0) - len(page_ids), 0),
            )


def _context_set_digest(contract: ContractRevision) -> str:
    refs = sorted(
        (context_reference_identity(ref), context_reference_digest(ref))
        for ref in contract.context_refs
        if isinstance(ref, RepositoryDocumentReference) and ref.required
    )
    return canonical_digest({"references": refs}, domain="result")


async def _validate_dependencies(
    session: AsyncSession, contract: ContractRevision, actor_project: str
) -> None:
    """Validate pinned upstream generations and reject cycles while graph writes serialize."""
    frontier = {dependency.ticket_id for dependency in contract.dependencies}
    for dependency in contract.dependencies:
        upstream = (
            (
                await session.execute(
                    sa.select(
                        delivery_workflows.c.current_revision, delivery_workflows.c.attempt
                    ).where(delivery_workflows.c.ticket_id == dependency.ticket_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if upstream is None or (upstream["current_revision"], upstream["attempt"]) != (
            dependency.contract_revision,
            dependency.attempt,
        ):
            raise DeliveryError("dependency_unavailable", "upstream generation is unavailable")
        projects = (
            (
                await session.execute(
                    sa.select(tickets.c.from_project, tickets.c.to_project).where(
                        tickets.c.id == dependency.ticket_id
                    )
                )
            )
            .mappings()
            .one()
        )
        if actor_project not in {projects["from_project"], projects["to_project"]}:
            raise DeliveryError("dependency_not_visible", "upstream dependency is not visible")

    seen: set[UUID] = set()
    while frontier:
        if contract.ticket_id in frontier:
            raise DeliveryError("dependency_cycle", "delivery dependency would create a cycle")
        frontier -= seen
        if not frontier:
            return
        seen |= frontier
        frontier = set(
            (
                await session.execute(
                    sa.select(delivery_dependencies.c.upstream_ticket_id).where(
                        delivery_dependencies.c.ticket_id.in_(frontier)
                    )
                )
            ).scalars()
        )


def _context_table_rank(entity_type: str) -> int:
    return tuple(_CONTEXT_TABLES).index(entity_type)


def _contract_from_json(payload: dict[str, Any]) -> ContractRevision:
    """Restore strict timestamp typing after PostgreSQL JSONB transport."""
    restored = dict(payload)
    created = restored.get("created_at")
    if isinstance(created, str):
        restored["created_at"] = datetime.fromisoformat(created.replace("Z", "+00:00"))
    return ContractRevision.model_validate(restored)


def _required_contract_digest(contract: ContractRevision) -> str:
    """Return the normalized digest guaranteed by stored contract validation."""
    if contract.content_digest is None:
        raise ValueError("stored contract lacks a content digest")
    return contract.content_digest


async def _matching_receipt(
    session: AsyncSession,
    *,
    ticket_id: UUID,
    contract_revision: int,
    attempt: int,
    milestone: Literal["integration", "fulfilled"],
    contract_digest: str,
    delivery_digest: str,
) -> MilestoneReceipt | None:
    """Restore one receipt only when its stored row and payload share the current identity."""
    row = (
        (
            await session.execute(
                sa.select(delivery_receipts).where(
                    delivery_receipts.c.ticket_id == ticket_id,
                    delivery_receipts.c.contract_revision == contract_revision,
                    delivery_receipts.c.attempt == attempt,
                    delivery_receipts.c.milestone == milestone,
                    delivery_receipts.c.delivery_digest == delivery_digest,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    receipt = MilestoneReceipt.model_validate_json(json.dumps(row["payload"]))
    if (
        receipt.id != row["id"]
        or receipt.ticket_id != ticket_id
        or receipt.contract_revision != contract_revision
        or receipt.attempt != attempt
        or receipt.milestone != milestone
        or receipt.contract_digest != contract_digest
        or receipt.delivery_digest != delivery_digest
        or receipt.issued_at != row["issued_at"]
        or receipt.acceptance_basis != row["basis"]
    ):
        return None
    return receipt


async def _binding_evidence(session: AsyncSession, row: sa.RowMapping) -> BindingEvidence:
    """Hydrate retained success proof independently from the latest collection attempt."""
    success_confirmation = None
    snapshot_id = None
    success_id = row["latest_success_confirmation_id"]
    if success_id is not None:
        success = (
            (
                await session.execute(
                    sa.select(delivery_confirmations, delivery_snapshots.c.evidence)
                    .join(
                        delivery_snapshots,
                        delivery_snapshots.c.id == delivery_confirmations.c.snapshot_id,
                    )
                    .where(delivery_confirmations.c.id == success_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if success is not None:
            evidence = PullRequestEvidence.model_validate_json(json.dumps(success["evidence"]))
            snapshot_id = success["snapshot_id"]
            success_confirmation = ObservationConfirmation(
                id=success["id"],
                evidence=evidence,
                collection_started_at=success["collection_started_at"],
                collection_finished_at=success["collection_finished_at"],
                outcome="success",
            )
    latest_id = row["latest_attempt_confirmation_id"]
    latest = None
    if latest_id is not None:
        latest = (
            (
                await session.execute(
                    sa.select(delivery_confirmations.c.outcome).where(
                        delivery_confirmations.c.id == latest_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    return BindingEvidence(
        binding=ArtifactBinding(
            id=row["id"],
            ticket_id=row["ticket_id"],
            contract_revision=row["contract_revision"],
            attempt=row["attempt"],
            deliverable_key=row["deliverable_key"],
            repository_id=row["repository_id"],
            pr_number=row["pr_number"],
            state=row["state"],
            head_sha=row["head_sha"],
            base_sha=row["base_sha"],
            integration_sha=row["integration_sha"],
            binding_version=row["row_version"],
        ),
        confirmation=success_confirmation,
        snapshot_id=snapshot_id,
        success_confirmation_id=success_id,
        latest_attempt_confirmation_id=latest_id,
        last_attempt_at=row["last_attempt_at"],
        last_success_at=row["last_success_at"],
        last_attempt_outcome="never" if latest is None else latest["outcome"],
    )


async def _context_predicates(
    session: AsyncSession, contract: ContractRevision, workflow: sa.RowMapping
) -> list[ContextPredicate]:
    """Hydrate repository proof from its retained success and current attempt pointers."""
    predicates: list[ContextPredicate] = []
    success_id = workflow["latest_context_success_confirmation_id"]
    latest_id = workflow["latest_context_attempt_confirmation_id"]
    success = None
    success_evidence = None
    if success_id is not None:
        success = (
            (
                await session.execute(
                    sa.select(delivery_confirmations, delivery_snapshots.c.evidence)
                    .join(
                        delivery_snapshots,
                        delivery_snapshots.c.id == delivery_confirmations.c.snapshot_id,
                    )
                    .where(delivery_confirmations.c.id == success_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if success is not None:
            success_evidence = RepositoryContextEvidence.model_validate(success["evidence"])
    latest = None
    if latest_id is not None:
        latest = (
            (
                await session.execute(
                    sa.select(delivery_confirmations.c.outcome).where(
                        delivery_confirmations.c.id == latest_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    for reference in contract.context_refs:
        identity = context_reference_identity(reference)
        if isinstance(reference, RepositoryDocumentReference):
            fact = next(
                (
                    candidate
                    for candidate in (() if success_evidence is None else success_evidence.facts)
                    if candidate.identity()
                    == (reference.repository_id, reference.sha, reference.path)
                ),
                None,
            )
            proof = (
                RepositoryContextEvidence(complete=True, facts=(fact,))
                if fact is not None and fact.status == "available"
                else None
            )
            status: Literal["available", "missing", "error"]
            if latest is not None and latest["outcome"] == "error":
                status = "error"
            elif proof is not None and success is not None:
                status = "available"
            else:
                status = "missing"
            predicates.append(
                ContextPredicate(
                    reference_identity=identity,
                    current_digest=context_reference_digest(reference)
                    if proof is not None
                    else None,
                    status=status,
                    snapshot_id=None if success is None else success["snapshot_id"],
                    success_confirmation_id=success_id,
                    latest_attempt_confirmation_id=latest_id,
                    collection_started_at=None
                    if success is None
                    else success["collection_started_at"],
                    collection_finished_at=None
                    if success is None
                    else success["collection_finished_at"],
                    evidence=proof,
                )
            )
        elif isinstance(reference, PinnedBrainEntityReference):
            table, fields = _CONTEXT_TABLES[reference.entity_type]
            row = (
                (await session.execute(sa.select(table).where(table.c.id == reference.entity_id)))
                .mappings()
                .one_or_none()
            )
            if row is None:
                predicates.append(
                    ContextPredicate(
                        reference_identity=identity, current_digest=None, status="missing"
                    )
                )
            else:
                content = {
                    field: row[field] for field in fields if field in row and row[field] is not None
                }
                snapshot = json.dumps(
                    content, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
                )
                digest = canonical_digest(
                    {
                        "entity_type": reference.entity_type,
                        "entity_id": str(reference.entity_id),
                        "content": snapshot,
                    },
                    domain="result",
                )
                predicates.append(
                    ContextPredicate(
                        reference_identity=identity,
                        current_digest=digest,
                        status="available" if digest == reference.content_digest else "changed",
                    )
                )
    return predicates
