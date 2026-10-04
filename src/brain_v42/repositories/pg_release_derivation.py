"""Observer-only release derivation; plans never overwrite observed facts."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.focus_slots import close_slots_satisfied_by
from brain_v42.db.tables import (
    delivery_artifact_bindings,
    delivery_attestations,
    delivery_workflows,
    tickets,
)
from brain_v42.delivery_config import DEFAULT_REPOSITORY_REGISTRY
from brain_v42.repositories.pg_delivery import lock_workflows
from brain_v42.repositories.pg_delivery_attestations import PgDeliveryAttestationsRepo

logger = structlog.get_logger(__name__)

OBSERVER_IDENTITY = "brain-v42-delivery-observer"
BRAIN_V42_REPOSITORY_ID = next(iter(DEFAULT_REPOSITORY_REGISTRY["brain-v42"]))


class _ReleaseTag(Protocol):
    """Keep persistence independent of the provider adapter that produces tags."""

    @property
    def name(self) -> str: ...

    @property
    def sha(self) -> str: ...


class _ReleaseIdentity(Protocol):
    """Accept measured identity fields without reversing the facts dependency."""

    @property
    def release_sha(self) -> str: ...

    @property
    def package_version(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    ticket_id: UUID
    to_project: str
    contract_revision: int
    deliverable_key: str
    repository_id: int
    integration_sha: str


class PgReleaseDerivationRepo:
    """Record that a tag contains a merge; red-rail judges what it means."""

    def __init__(self) -> None:
        self._attestations = PgDeliveryAttestationsRepo()

    async def unreleased(
        self,
        session: AsyncSession,
        *,
        repository_ids: Collection[int],
        limit: int,
        exclude: Collection[tuple[UUID, str]] = (),
    ) -> list[ReleaseCandidate]:
        b, t, a, w = delivery_artifact_bindings, tickets, delivery_attestations, delivery_workflows
        key = sa.func.concat(
            "released:", b.c.ticket_id.cast(sa.Text), ":", b.c.deliverable_key, ":"
        )
        already = sa.exists().where(
            a.c.ticket_id == b.c.ticket_id,
            a.c.kind == "released",
            a.c.issuer_project == t.c.to_project,
            a.c.issuer_identity == OBSERVER_IDENTITY,
            sa.func.starts_with(a.c.idempotency_key, key),
            # A reopen binds a new merge under the same deliverable: the old row measured another one.
            a.c.payload["integration_sha"].astext == b.c.integration_sha,
        )
        stmt = (
            sa.select(
                b.c.ticket_id,
                t.c.to_project,
                b.c.contract_revision,
                b.c.deliverable_key,
                b.c.repository_id,
                b.c.integration_sha,
            )
            .join(t, t.c.id == b.c.ticket_id)
            .join(
                w,
                sa.and_(
                    w.c.ticket_id == b.c.ticket_id,
                    w.c.current_revision == b.c.contract_revision,
                    w.c.attempt == b.c.attempt,
                ),
            )
            .where(
                b.c.active.is_(True),
                b.c.integration_sha.is_not(None),
                b.c.repository_id.in_(list(repository_ids)),
                ~already,
                sa.tuple_(b.c.ticket_id, b.c.deliverable_key).not_in(list(exclude)),
            )
            .order_by(b.c.last_success_at.asc().nulls_last(), b.c.ticket_id, b.c.deliverable_key)
            .limit(limit)
        )
        return [ReleaseCandidate(**row) for row in (await session.execute(stmt)).mappings()]

    async def record_released(
        self,
        session: AsyncSession,
        candidate: ReleaseCandidate,
        tag: _ReleaseTag,
        tag_date: datetime,
    ) -> None:
        """Attest that `tag` contains the candidate's merge, if that merge is still current.

        The candidate was picked in an earlier transaction, then the observer went to the
        network: the binding may have been deactivated (a reopen) or rebound since. The ticket
        row is locked FIRST, the lock every receipt writer takes and the one `brain_slot_open`
        key-shares for a lot anchor, and the binding is re-read under it with the same predicate
        `unreleased` used. A stale candidate is skipped: it would attest a release of a merge
        that is no longer the ticket's, outside the set a concurrent open locks.
        """
        b, w = delivery_artifact_bindings, delivery_workflows
        async with lock_workflows(session, (candidate.ticket_id,)):
            current = await session.scalar(
                sa.select(
                    sa.exists().where(
                        b.c.ticket_id == candidate.ticket_id,
                        b.c.deliverable_key == candidate.deliverable_key,
                        b.c.integration_sha == candidate.integration_sha,
                        b.c.active.is_(True),
                        w.c.ticket_id == b.c.ticket_id,
                        w.c.current_revision == b.c.contract_revision,
                        w.c.attempt == b.c.attempt,
                    )
                )
            )
            if not current:
                logger.info(
                    "release_candidate_stale",
                    ticket_id=str(candidate.ticket_id),
                    deliverable_key=candidate.deliverable_key,
                    tag=tag.name,
                )
                return
            await self._record_released_locked(session, candidate, tag, tag_date)

    async def _record_released_locked(
        self,
        session: AsyncSession,
        candidate: ReleaseCandidate,
        tag: _ReleaseTag,
        tag_date: datetime,
    ) -> None:
        attestation = await self._attestations.attest(
            session,
            candidate.ticket_id,
            actor_project=candidate.to_project,
            caller_identity=OBSERVER_IDENTITY,
            kind="released",
            payload={
                "repository_id": candidate.repository_id,
                "tag": tag.name,
                "tag_sha": tag.sha,
                "integration_sha": candidate.integration_sha,
            },
            idempotency_key=f"released:{candidate.ticket_id}:{candidate.deliverable_key}:{tag.name}",
            emitted_at=tag_date,
            contract_revision=candidate.contract_revision,
        )
        # ADR #34 D5: a measured release closes the lot slots it satisfies. Here,
        # not in the generic `attest`: a caller-declared attestation is not a
        # measurement. An idempotent replay returns the existing row; closed slots
        # are skipped, so the call is idempotent too.
        await close_slots_satisfied_by(
            session, ticket_ids=[candidate.ticket_id], completing_row_id=attestation.id
        )

    async def undeployed(
        self,
        session: AsyncSession,
        *,
        repository_id: int,
        live_release_sha: str,
        limit: int,
        exclude: Collection[tuple[UUID, str]] = (),
    ) -> list[ReleaseCandidate]:
        if repository_id != BRAIN_V42_REPOSITORY_ID:
            return []
        b, t, a, w = delivery_artifact_bindings, tickets, delivery_attestations, delivery_workflows
        key = sa.func.concat(
            "deployed:",
            b.c.ticket_id.cast(sa.Text),
            ":",
            b.c.deliverable_key,
            ":",
            live_release_sha,
        )
        already = sa.exists().where(
            a.c.ticket_id == b.c.ticket_id,
            a.c.kind == "deployed",
            a.c.issuer_project == t.c.to_project,
            a.c.issuer_identity == OBSERVER_IDENTITY,
            a.c.idempotency_key == key,
            a.c.payload["integration_sha"].astext == b.c.integration_sha,
        )
        stmt = (
            sa.select(
                b.c.ticket_id,
                t.c.to_project,
                b.c.contract_revision,
                b.c.deliverable_key,
                b.c.repository_id,
                b.c.integration_sha,
            )
            .join(t, t.c.id == b.c.ticket_id)
            .join(
                w,
                sa.and_(
                    w.c.ticket_id == b.c.ticket_id,
                    w.c.current_revision == b.c.contract_revision,
                    w.c.attempt == b.c.attempt,
                ),
            )
            .where(
                b.c.active.is_(True),
                b.c.integration_sha.is_not(None),
                b.c.repository_id == repository_id,
                ~already,
                sa.tuple_(b.c.ticket_id, b.c.deliverable_key).not_in(list(exclude)),
            )
            .order_by(b.c.last_success_at.asc().nulls_last(), b.c.ticket_id, b.c.deliverable_key)
            .limit(limit)
        )
        return [ReleaseCandidate(**row) for row in (await session.execute(stmt)).mappings()]

    async def record_deployed(
        self, session: AsyncSession, candidate: ReleaseCandidate, identity: _ReleaseIdentity
    ) -> None:
        key = f"deployed:{candidate.ticket_id}:{candidate.deliverable_key}:{identity.release_sha}"
        a = delivery_attestations
        # The fenced owner checks again at publication so a replay preserves the
        # first observation time instead of conflicting with a fresh emitted_at.
        # A row for another merge under the same key is no replay: attest refuses it.
        existing = await session.scalar(
            sa.select(a.c.id).where(
                a.c.ticket_id == candidate.ticket_id,
                a.c.issuer_project == candidate.to_project,
                a.c.issuer_identity == OBSERVER_IDENTITY,
                a.c.idempotency_key == key,
                a.c.payload["integration_sha"].astext == candidate.integration_sha,
            )
        )
        if existing is not None:
            return
        await self._attestations.attest(
            session,
            candidate.ticket_id,
            actor_project=candidate.to_project,
            caller_identity=OBSERVER_IDENTITY,
            kind="deployed",
            payload={
                "repository_id": candidate.repository_id,
                "live_release_sha": identity.release_sha,
                "package_version": identity.package_version,
                "integration_sha": candidate.integration_sha,
            },
            idempotency_key=key,
            emitted_at=datetime.now(UTC),
            contract_revision=candidate.contract_revision,
        )
