"""Transactional PostgreSQL persistence for immutable artifact observations."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

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
from brain_v42.models.delivery import DeliveryError, ObservationConfirmation, PullRequestEvidence
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
