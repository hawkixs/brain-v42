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
    ClaimState,
    ContextPredicate,
    ContractRevision,
    DeliveryError,
    DeliveryPage,
    DeliveryView,
    EvaluationInput,
    PinnedBrainEntityReference,
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
        ("id", "title", "decision", "rationale", "status", "project_key", "superseded_by"),
    ),
    "learning": (
        learnings,
        ("id", "title", "content", "category", "confidence", "project_key", "superseded_by"),
    ),
    "snippet": (snippets, ("id", "title", "content", "language", "project_key", "file_path")),
    "runbook": (runbooks, ("id", "title", "content", "project_key", "version")),
    "adr": (adrs, ("id", "number", "title", "decision", "status", "project_key", "superseded_by")),
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

    async def bind_pr(
        self,
        binding: ArtifactBinding,
        *,
        actor_project: str,
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
                        repository_name="",
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
                        payload={},
                    )
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
                dependencies=(),
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
