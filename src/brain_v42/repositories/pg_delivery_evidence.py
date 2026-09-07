"""Transactional PostgreSQL persistence for immutable artifact observations."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import (
    delivery_artifact_bindings,
    delivery_confirmations,
    delivery_contract_revisions,
    delivery_dependencies,
    delivery_snapshots,
    delivery_workflows,
    tickets,
)
from brain_v42.models.delivery import (
    ContractRevision,
    DeliveryError,
    ObservationConfirmation,
    PullRequestEvidence,
    RepositoryContextEvidence,
    RepositoryContextObservationConfirmation,
    RepositoryDocumentReference,
)
from brain_v42.models.delivery_hashes import canonical_digest
from brain_v42.repositories.pg_base import BasePgRepository
from brain_v42.repositories.pg_delivery import DELIVERY_GRAPH_LOCK

_TERMINAL_STATUSES = frozenset({"wontfix", "closed", "acked"})
_SAFE_ERROR_CODES = frozenset(
    {
        "provider_forbidden",
        "provider_invalid_response",
        "provider_not_found",
        "provider_rate_limited",
        "provider_timeout",
        "provider_unavailable",
    }
)


def _require_uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise DeliveryError("binding_superseded", "artifact binding is no longer publishable")
    return value


def _require_version(value: object) -> int:
    if not isinstance(value, int):
        raise DeliveryError("binding_superseded", "artifact binding is no longer publishable")
    return value


def _validated_repository_context_success(
    evidence: RepositoryContextEvidence,
    collection_started_at: datetime,
    collection_finished_at: datetime,
) -> RepositoryContextEvidence:
    """Revalidate an untrusted model instance and its confirmation before persistence."""
    try:
        validated_evidence = RepositoryContextEvidence.model_validate(
            evidence.model_dump(warnings=False)
        )
        confirmation = RepositoryContextObservationConfirmation(
            snapshot_id=uuid4(),
            evidence=validated_evidence,
            collection_started_at=collection_started_at,
            collection_finished_at=collection_finished_at,
        )
    except ValueError:
        raise DeliveryError(
            "repository_context_mismatch", "repository context confirmation is invalid"
        ) from None
    assert confirmation.evidence is not None
    return confirmation.evidence


def _validated_repository_context_error(
    code: str, collection_started_at: datetime, collection_finished_at: datetime
) -> None:
    """Validate a failed confirmation before persistence without exposing caller input."""
    try:
        RepositoryContextObservationConfirmation(
            collection_started_at=collection_started_at,
            collection_finished_at=collection_finished_at,
            outcome="error",
            error_code=code,
        )
    except ValueError:
        raise DeliveryError(
            "repository_context_mismatch", "repository context confirmation is invalid"
        ) from None


class PgDeliveryEvidenceRepo(BasePgRepository):
    """Append confirmations while the injected session retains transaction ownership."""

    async def publish_observation(
        self,
        session: AsyncSession,
        binding_id: UUID,
        expected_binding_version: int,
        evidence: PullRequestEvidence,
        collection_started_at: datetime,
        collection_finished_at: datetime,
    ) -> ObservationConfirmation:
        """Persist one successful immutable collection confirmation without committing."""
        binding, workflow = await self._lock_current_subject(
            session, binding_id, expected_binding_version
        )
        await self._validate_subject(session, binding, evidence)
        snapshot_payload = evidence.model_dump(mode="json")
        semantic_payload = dict(snapshot_payload)
        semantic_payload.pop("collected_at")
        semantic_digest = canonical_digest(semantic_payload, domain="result")
        snapshot_id = await self._snapshot_id(
            session, binding_id, semantic_digest, snapshot_payload
        )
        confirmation_id = await session.scalar(
            delivery_confirmations.insert()
            .values(
                subject_kind="artifact_binding",
                binding_id=binding_id,
                snapshot_id=snapshot_id,
                collection_started_at=collection_started_at,
                collection_finished_at=collection_finished_at,
                outcome="success",
            )
            .returning(delivery_confirmations.c.id)
        )
        assert confirmation_id is not None
        await self._advance_subject(
            session,
            binding,
            workflow,
            confirmation_id,
            collection_finished_at,
            evidence=evidence,
        )
        return ObservationConfirmation(
            id=confirmation_id,
            evidence=evidence,
            collection_started_at=collection_started_at,
            collection_finished_at=collection_finished_at,
        )

    async def record_observation_error(
        self,
        session: AsyncSession,
        binding_id: UUID,
        expected_binding_version: int,
        code: str,
        collection_started_at: datetime,
        collection_finished_at: datetime,
    ) -> ObservationConfirmation:
        """Append a safe failed collection confirmation without replacing prior proof."""
        if code not in _SAFE_ERROR_CODES:
            raise DeliveryError("invalid_error_code", "observation error code is not supported")
        binding, workflow = await self._lock_current_subject(
            session, binding_id, expected_binding_version
        )
        confirmation_id = await session.scalar(
            delivery_confirmations.insert()
            .values(
                subject_kind="artifact_binding",
                binding_id=binding_id,
                collection_started_at=collection_started_at,
                collection_finished_at=collection_finished_at,
                outcome="error",
                error_code=code,
            )
            .returning(delivery_confirmations.c.id)
        )
        assert confirmation_id is not None
        await self._advance_subject(
            session, binding, workflow, confirmation_id, collection_finished_at, evidence=None
        )
        return ObservationConfirmation(
            id=confirmation_id,
            collection_started_at=collection_started_at,
            collection_finished_at=collection_finished_at,
            outcome="error",
            error_code=code,
        )

    async def publish_repository_context(
        self,
        session: AsyncSession,
        ticket_id: UUID,
        expected_revision: int,
        expected_attempt: int,
        expected_context_set_digest: str,
        expected_context_row_version: int,
        evidence: RepositoryContextEvidence,
        collection_started_at: datetime,
        collection_finished_at: datetime,
    ) -> RepositoryContextObservationConfirmation:
        """Persist a successful repository-context observation without committing the caller."""
        evidence = _validated_repository_context_success(
            evidence, collection_started_at, collection_finished_at
        )
        workflow = await self._lock_current_context(
            session,
            ticket_id,
            expected_revision,
            expected_attempt,
            expected_context_set_digest,
            expected_context_row_version,
        )
        await self._validate_context_subject(session, workflow, evidence)
        payload = evidence.model_dump(mode="json")
        payload["facts"] = sorted(
            payload["facts"],
            key=lambda fact: (fact["repository_id"], fact["commit_sha"], fact["path"]),
        )
        semantic_digest = canonical_digest(payload, domain="result")
        snapshot_id = await self._context_snapshot_id(session, workflow, semantic_digest, payload)
        confirmation_id = await session.scalar(
            delivery_confirmations.insert()
            .values(
                subject_kind="repository_context",
                ticket_id=ticket_id,
                contract_revision=expected_revision,
                attempt=expected_attempt,
                context_set_digest=expected_context_set_digest,
                snapshot_id=snapshot_id,
                collection_started_at=collection_started_at,
                collection_finished_at=collection_finished_at,
                outcome="success",
            )
            .returning(delivery_confirmations.c.id)
        )
        confirmation_id = _require_uuid(confirmation_id)
        await self._advance_context(
            session, workflow, confirmation_id, collection_finished_at, success=True
        )
        return RepositoryContextObservationConfirmation(
            id=confirmation_id,
            snapshot_id=snapshot_id,
            evidence=evidence,
            collection_started_at=collection_started_at,
            collection_finished_at=collection_finished_at,
        )

    async def record_repository_context_error(
        self,
        session: AsyncSession,
        ticket_id: UUID,
        expected_revision: int,
        expected_attempt: int,
        expected_context_set_digest: str,
        expected_context_row_version: int,
        code: str,
        collection_started_at: datetime,
        collection_finished_at: datetime,
    ) -> RepositoryContextObservationConfirmation:
        """Append a sanitized failed context attempt without replacing prior proof."""
        if code not in _SAFE_ERROR_CODES:
            raise DeliveryError("invalid_error_code", "observation error code is not supported")
        _validated_repository_context_error(code, collection_started_at, collection_finished_at)
        workflow = await self._lock_current_context(
            session,
            ticket_id,
            expected_revision,
            expected_attempt,
            expected_context_set_digest,
            expected_context_row_version,
        )
        confirmation_id = await session.scalar(
            delivery_confirmations.insert()
            .values(
                subject_kind="repository_context",
                ticket_id=ticket_id,
                contract_revision=expected_revision,
                attempt=expected_attempt,
                context_set_digest=expected_context_set_digest,
                collection_started_at=collection_started_at,
                collection_finished_at=collection_finished_at,
                outcome="error",
                error_code=code,
            )
            .returning(delivery_confirmations.c.id)
        )
        confirmation_id = _require_uuid(confirmation_id)
        await self._advance_context(
            session,
            workflow,
            confirmation_id,
            collection_finished_at,
            success=False,
            error_code=code,
        )
        return RepositoryContextObservationConfirmation(
            id=confirmation_id,
            collection_started_at=collection_started_at,
            collection_finished_at=collection_finished_at,
            outcome="error",
            error_code=code,
        )

    async def _lock_current_subject(
        self, session: AsyncSession, binding_id: UUID, expected_binding_version: int
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Lock graph, complete direct dependency set, then binding and revalidate it."""
        await session.execute(sa.select(sa.func.pg_advisory_xact_lock_shared(DELIVERY_GRAPH_LOCK)))
        discovered = (
            (
                await session.execute(
                    sa.select(
                        delivery_artifact_bindings.c.ticket_id,
                        delivery_artifact_bindings.c.contract_revision,
                    ).where(delivery_artifact_bindings.c.id == binding_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if discovered is None:
            raise DeliveryError("binding_conflict", "artifact binding is no longer current")
        dependency_ids = await self._direct_dependency_ids(
            session, discovered["ticket_id"], discovered["contract_revision"]
        )
        ticket_ids = sorted({discovered["ticket_id"], *dependency_ids}, key=str)
        locked_tickets = await session.execute(
            sa.select(tickets)
            .where(tickets.c.id.in_(ticket_ids))
            .order_by(tickets.c.id)
            .with_for_update()
        )
        ticket_rows = {row["id"]: dict(row) for row in locked_tickets.mappings()}
        locked_workflows = await session.execute(
            sa.select(delivery_workflows)
            .where(delivery_workflows.c.ticket_id.in_(ticket_ids))
            .order_by(delivery_workflows.c.ticket_id)
            .with_for_update()
        )
        workflow_rows = {row["ticket_id"]: dict(row) for row in locked_workflows.mappings()}
        binding_row = (
            (
                await session.execute(
                    sa.select(delivery_artifact_bindings)
                    .where(delivery_artifact_bindings.c.id == binding_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if binding_row is None or binding_row["row_version"] != expected_binding_version:
            raise DeliveryError("binding_conflict", "artifact binding is no longer current")
        binding = dict(binding_row)
        if not binding["active"]:
            raise DeliveryError("binding_conflict", "artifact binding is no longer current")
        workflow = workflow_rows.get(binding["ticket_id"])
        ticket = ticket_rows.get(binding["ticket_id"])
        if (
            workflow is None
            or ticket is None
            or ticket["status"] in _TERMINAL_STATUSES
            or workflow["disposition"] != "active"
            or workflow["current_revision"] != binding["contract_revision"]
            or workflow["attempt"] != binding["attempt"]
        ):
            raise DeliveryError("binding_superseded", "artifact binding is no longer publishable")
        return binding, workflow

    async def _lock_current_context(
        self,
        session: AsyncSession,
        ticket_id: UUID,
        expected_revision: int,
        expected_attempt: int,
        expected_context_set_digest: str,
        expected_context_row_version: int,
    ) -> dict[str, object]:
        """Lock graph then the complete workflow set before revalidating context CAS."""
        await session.execute(sa.select(sa.func.pg_advisory_xact_lock_shared(DELIVERY_GRAPH_LOCK)))
        dependency_ids = await self._direct_dependency_ids(session, ticket_id, expected_revision)
        ticket_ids = sorted({ticket_id, *dependency_ids}, key=str)
        locked_tickets = await session.execute(
            sa.select(tickets)
            .where(tickets.c.id.in_(ticket_ids))
            .order_by(tickets.c.id)
            .with_for_update()
        )
        ticket_rows = {row["id"]: dict(row) for row in locked_tickets.mappings()}
        locked_workflows = await session.execute(
            sa.select(delivery_workflows)
            .where(delivery_workflows.c.ticket_id.in_(ticket_ids))
            .order_by(delivery_workflows.c.ticket_id)
            .with_for_update()
        )
        workflow_rows = {row["ticket_id"]: dict(row) for row in locked_workflows.mappings()}
        workflow = workflow_rows.get(ticket_id)
        ticket = ticket_rows.get(ticket_id)
        if workflow is None or ticket is None:
            raise DeliveryError(
                "repository_context_conflict", "repository context is no longer current"
            )
        if (
            ticket["status"] in _TERMINAL_STATUSES
            or workflow["disposition"] != "active"
            or workflow["current_revision"] != expected_revision
            or workflow["attempt"] != expected_attempt
            or workflow["context_set_digest"] != expected_context_set_digest
        ):
            raise DeliveryError(
                "repository_context_superseded", "repository context is no longer publishable"
            )
        if workflow["context_row_version"] != expected_context_row_version:
            raise DeliveryError(
                "repository_context_conflict", "repository context is no longer current"
            )
        return workflow

    async def _direct_dependency_ids(
        self, session: AsyncSession, ticket_id: object, revision: object
    ) -> Iterable[UUID]:
        result = await session.execute(
            sa.select(delivery_dependencies.c.upstream_ticket_id).where(
                delivery_dependencies.c.ticket_id == ticket_id,
                delivery_dependencies.c.contract_revision == revision,
            )
        )
        return tuple(result.scalars())

    async def _validate_subject(
        self, session: AsyncSession, binding: dict[str, object], evidence: PullRequestEvidence
    ) -> None:
        if (
            evidence.repository_id != binding["repository_id"]
            or evidence.pr_number != binding["pr_number"]
            or evidence.head_repository_id is None
            or not evidence.complete
        ):
            raise DeliveryError(
                "evidence_subject_mismatch", "provider subject differs from binding"
            )
        if evidence.base_ref != await self._target_branch(session, binding):
            raise DeliveryError(
                "evidence_subject_mismatch", "provider subject differs from binding"
            )

    async def _target_branch(self, session: AsyncSession, binding: dict[str, object]) -> str:
        row = (
            (
                await session.execute(
                    sa.select(delivery_contract_revisions.c.normalized_contract).where(
                        delivery_contract_revisions.c.ticket_id == binding["ticket_id"],
                        delivery_contract_revisions.c.contract_revision
                        == binding["contract_revision"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise DeliveryError("binding_superseded", "artifact binding is no longer publishable")
        deliverable = next(
            (
                item
                for item in row["normalized_contract"].get("deliverables", [])
                if item.get("key") == binding["deliverable_key"]
            ),
            None,
        )
        target_branch = None if deliverable is None else deliverable.get("target_branch")
        if not isinstance(target_branch, str):
            raise DeliveryError("binding_superseded", "artifact binding is no longer publishable")
        return target_branch

    async def _validate_context_subject(
        self,
        session: AsyncSession,
        workflow: dict[str, object],
        evidence: RepositoryContextEvidence,
    ) -> None:
        row = (
            (
                await session.execute(
                    sa.select(delivery_contract_revisions.c.normalized_contract).where(
                        delivery_contract_revisions.c.ticket_id == workflow["ticket_id"],
                        delivery_contract_revisions.c.contract_revision
                        == workflow["current_revision"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise DeliveryError(
                "repository_context_superseded", "repository context is no longer publishable"
            )
        contract = ContractRevision.model_validate_json(json.dumps(row["normalized_contract"]))
        expected = {
            (reference.repository_id, reference.sha, reference.path)
            for reference in contract.context_refs
            if isinstance(reference, RepositoryDocumentReference) and reference.required
        }
        facts = tuple(evidence.facts)
        identities = tuple(fact.identity() for fact in facts)
        if (
            not evidence.complete
            or not expected
            or len(facts) != len(expected)
            or len(set(identities)) != len(identities)
            or set(identities) != expected
            or any(
                fact.status != "available"
                or fact.tree_sha is None
                or fact.blob_sha is None
                or fact.mode not in {"100644", "100755"}
                for fact in facts
            )
        ):
            raise DeliveryError(
                "repository_context_mismatch", "repository context does not cover required pins"
            )

    async def _context_snapshot_id(
        self,
        session: AsyncSession,
        workflow: dict[str, object],
        semantic_digest: str,
        payload: dict[str, object],
    ) -> UUID:
        snapshot_id = await session.scalar(
            sa.select(delivery_snapshots.c.id).where(
                delivery_snapshots.c.ticket_id == workflow["ticket_id"],
                delivery_snapshots.c.contract_revision == workflow["current_revision"],
                delivery_snapshots.c.attempt == workflow["attempt"],
                delivery_snapshots.c.context_set_digest == workflow["context_set_digest"],
                delivery_snapshots.c.semantic_digest == semantic_digest,
            )
        )
        if snapshot_id is not None:
            return _require_uuid(snapshot_id)
        snapshot_id = await session.scalar(
            delivery_snapshots.insert()
            .values(
                subject_kind="repository_context",
                ticket_id=workflow["ticket_id"],
                contract_revision=workflow["current_revision"],
                attempt=workflow["attempt"],
                context_set_digest=workflow["context_set_digest"],
                semantic_digest=semantic_digest,
                evidence=payload,
            )
            .returning(delivery_snapshots.c.id)
        )
        return _require_uuid(snapshot_id)

    async def _advance_context(
        self,
        session: AsyncSession,
        workflow: dict[str, object],
        confirmation_id: UUID,
        finished_at: datetime,
        *,
        success: bool,
        error_code: str | None = None,
    ) -> None:
        values: dict[str, object] = {
            "latest_context_attempt_confirmation_id": confirmation_id,
            "context_last_attempt_at": finished_at,
            "context_last_attempt_outcome": "success" if success else "error",
            "context_last_error_code": None if success else error_code,
            "context_row_version": _require_version(workflow["context_row_version"]) + 1,
            "row_version": _require_version(workflow["row_version"]) + 1,
            "updated_at": sa.func.now(),
        }
        if success:
            values.update(
                {
                    "latest_context_success_confirmation_id": confirmation_id,
                    "context_last_success_at": finished_at,
                }
            )
        await session.execute(
            delivery_workflows.update()
            .where(
                delivery_workflows.c.ticket_id == workflow["ticket_id"],
                delivery_workflows.c.row_version == workflow["row_version"],
                delivery_workflows.c.context_row_version == workflow["context_row_version"],
            )
            .values(**values)
        )

    async def _snapshot_id(
        self,
        session: AsyncSession,
        binding_id: UUID,
        semantic_digest: str,
        payload: dict[str, object],
    ) -> UUID:
        snapshot_id = await session.scalar(
            sa.select(delivery_snapshots.c.id).where(
                delivery_snapshots.c.binding_id == binding_id,
                delivery_snapshots.c.semantic_digest == semantic_digest,
            )
        )
        if snapshot_id is not None:
            return _require_uuid(snapshot_id)
        snapshot_id = await session.scalar(
            delivery_snapshots.insert()
            .values(
                subject_kind="artifact_binding",
                binding_id=binding_id,
                semantic_digest=semantic_digest,
                evidence=payload,
            )
            .returning(delivery_snapshots.c.id)
        )
        return _require_uuid(snapshot_id)

    async def _advance_subject(
        self,
        session: AsyncSession,
        binding: dict[str, object],
        workflow: dict[str, object],
        confirmation_id: UUID,
        finished_at: datetime,
        *,
        evidence: PullRequestEvidence | None,
    ) -> None:
        values: dict[str, object] = {
            "latest_attempt_confirmation_id": confirmation_id,
            "last_attempt_at": finished_at,
            "row_version": _require_version(binding["row_version"]) + 1,
        }
        if evidence is not None:
            values.update(
                {
                    "latest_success_confirmation_id": confirmation_id,
                    "last_success_at": finished_at,
                    "state": "observed",
                    "head_sha": evidence.head_sha,
                    "base_sha": evidence.base_sha,
                    "integration_sha": evidence.integration_sha,
                }
            )
        await session.execute(
            delivery_artifact_bindings.update()
            .where(
                delivery_artifact_bindings.c.id == binding["id"],
                delivery_artifact_bindings.c.row_version == binding["row_version"],
            )
            .values(**values)
        )
        await session.execute(
            delivery_workflows.update()
            .where(
                delivery_workflows.c.ticket_id == workflow["ticket_id"],
                delivery_workflows.c.row_version == workflow["row_version"],
            )
            .values(
                row_version=_require_version(workflow["row_version"]) + 1,
                updated_at=sa.func.now(),
            )
        )
