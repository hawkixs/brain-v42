"""Transactional PostgreSQL persistence for delivery contracts and proposed PR bindings."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import (
    adrs,
    decisions,
    delivery_artifact_bindings,
    delivery_contract_revisions,
    delivery_dependencies,
    delivery_events,
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
    PinnedBrainEntityReference,
    PinnedContextReference,
    RepositoryDocumentReference,
    context_reference_digest,
    context_reference_identity,
)
from brain_v42.models.delivery_evaluator import evaluate_delivery
from brain_v42.models.delivery_hashes import canonical_digest
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
            bindings = tuple(
                BindingEvidence(
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
                    confirmation=None,
                    last_attempt_at=row["last_attempt_at"],
                    last_success_at=row["last_success_at"],
                    last_attempt_outcome="never",
                )
                for row in binding_rows
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
            dependencies = tuple(
                DependencyPredicate(
                    ticket_id=row["upstream_ticket_id"],
                    contract_revision=row["upstream_revision"],
                    attempt=row["upstream_attempt"],
                    milestone=row["milestone"],
                    current_contract_revision=row["current_revision"],
                    current_attempt=row["current_attempt"],
                    current_contract_digest=row["content_digest"],
                    current_delivery_digest=canonical_digest(
                        {
                            "ticket_id": str(row["upstream_ticket_id"]),
                            "contract_revision": row["current_revision"],
                            "attempt": row["current_attempt"],
                        },
                        domain="result",
                    ),
                    current_disposition=row["disposition"],
                    receipt=None,
                )
                for row in dependency_rows
            )
            contexts = tuple(await _context_predicates(sess, contract))
            return EvaluationInput(
                contract=contract,
                attempt=workflow["attempt"],
                workflow_version=workflow["row_version"],
                coordination_status=ticket["status"],
                coordination_disposition=workflow["disposition"],
                is_self_ticket=ticket["from_project"] == ticket["to_project"],
                active_bindings=bindings,
                contexts=contexts,
                dependencies=dependencies,
                integration_receipt=None,
                fulfillment_receipt=None,
                feature_enabled=feature_enabled,
                freshness_seconds=freshness_seconds,
                claim=ClaimState(
                    epoch=workflow["claim_epoch"],
                    owner=workflow["claim_owner"],
                    expires_at=workflow["claim_expires_at"],
                ),
                executor_identity=ticket["to_project"],
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
            stmt = (
                sa.select(delivery_workflows.c.ticket_id)
                .join(tickets)
                .where(
                    sa.or_(
                        tickets.c.from_project == actor_project,
                        tickets.c.to_project == actor_project,
                    )
                )
                .order_by(delivery_workflows.c.ticket_id)
                .limit(limit + 1)
            )
            if cursor:
                stmt = stmt.where(delivery_workflows.c.ticket_id > UUID(cursor))
            ids = list((await sess.execute(stmt)).scalars())
            next_cursor = str(ids[limit]) if len(ids) > limit else None
            views = [
                await self.get_view(
                    identifier,
                    feature_enabled=feature_enabled,
                    freshness_seconds=freshness_seconds,
                    session=sess,
                )
                for identifier in ids[:limit]
            ]
            return DeliveryPage(
                items=tuple(view for view in views if view is not None), next_cursor=next_cursor
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


async def _context_predicates(
    session: AsyncSession, contract: ContractRevision
) -> list[ContextPredicate]:
    predicates: list[ContextPredicate] = []
    for reference in contract.context_refs:
        identity = context_reference_identity(reference)
        if isinstance(reference, RepositoryDocumentReference):
            predicates.append(
                ContextPredicate(reference_identity=identity, current_digest=None, status="missing")
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
