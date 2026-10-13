"""Persisted, current-generation due work for the independent delivery observer."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import (
    delivery_artifact_bindings as bindings,
)
from brain_v42.db.tables import (
    delivery_confirmations as confirmations,
)
from brain_v42.db.tables import (
    delivery_contract_revisions as revisions,
)
from brain_v42.db.tables import (
    delivery_snapshots as snapshots,
)
from brain_v42.db.tables import (
    delivery_workflows as workflows,
)
from brain_v42.db.tables import (
    tickets,
)
from brain_v42.models.delivery import ArtifactBinding, ContractRevision, PullRequestEvidence
from brain_v42.repositories.pg_delivery import DELIVERY_OBSERVER_LOCK, _contract_from_json


def _current_revision(w: sa.Table, r: sa.Table) -> sa.ColumnElement[bool]:
    """Keep queue and liveness reads on the same workflow generation."""
    return sa.and_(r.c.ticket_id == w.c.ticket_id, r.c.contract_revision == w.c.current_revision)


def _eligible_ticket(w: sa.Table, t: sa.Table) -> sa.ColumnElement[bool]:
    """Define active observer subjects once for scheduling and liveness."""
    return sa.and_(
        w.c.disposition == "active",
        t.c.kind == "request",
        t.c.status.in_(("open", "in_progress", "resolved")),
    )


def _context_due(w: sa.Table, r: sa.Table) -> sa.ColumnElement[bool]:
    has_context = r.c.normalized_contract.contains(
        {"context_refs": [{"kind": "repository_document", "required": True}]}
    )
    return sa.and_(
        has_context,
        w.c.context_due_at.is_not(None),
        w.c.context_due_at <= sa.func.clock_timestamp(),
    )


@dataclass(frozen=True, slots=True)
class ObserverLiveness:
    lock_held: bool
    overdue: int
    last_confirmation_age_seconds: int | None
    healthy: bool


async def read_observer_liveness(
    session: AsyncSession, *, overdue_after_seconds: int
) -> ObserverLiveness:
    """Measure lock ownership, overdue eligible work, and newest persisted attempt."""
    if type(overdue_after_seconds) is not int or overdue_after_seconds < 0:
        raise ValueError("overdue threshold must be a non-negative integer")
    w, r, b, t = workflows, revisions, bindings, tickets
    eligible = _eligible_ticket(w, t)
    current_revision = _current_revision(w, r)
    due_before = sa.func.clock_timestamp() - timedelta(seconds=overdue_after_seconds)
    overdue_artifacts = (
        sa.select(b.c.id)
        .select_from(
            b.join(
                w,
                sa.and_(
                    b.c.ticket_id == w.c.ticket_id,
                    b.c.contract_revision == w.c.current_revision,
                    b.c.attempt == w.c.attempt,
                ),
            )
            .join(r, current_revision)
            .join(t, t.c.id == w.c.ticket_id)
        )
        .where(eligible, b.c.active.is_(True), b.c.due_at < due_before, ~_context_due(w, r))
    )
    overdue_context = (
        sa.select(w.c.ticket_id)
        .select_from(w.join(r, current_revision).join(t, t.c.id == w.c.ticket_id))
        .where(eligible, _context_due(w, r), w.c.context_due_at < due_before)
    )
    overdue = int(
        await session.scalar(
            sa.select(sa.func.count()).select_from(
                sa.union_all(overdue_artifacts, overdue_context).subquery("overdue_subjects")
            )
        )
        or 0
    )
    lock = DELIVERY_OBSERVER_LOCK
    lock_held = bool(
        await session.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND objsubid = 1 AND classid::bigint = (CAST(:key AS bigint) >> 32) "
                "AND objid::bigint = (:key & 4294967295))"
            ),
            {"key": lock},
        )
    )
    row = (
        await session.execute(
            sa.select(
                sa.func.max(confirmations.c.collection_finished_at).label("finished_at"),
                sa.func.clock_timestamp().label("now"),
            )
        )
    ).one()
    if row.finished_at is None:
        age_seconds = None
    else:
        age_seconds = max(0, int((row.now - row.finished_at).total_seconds()))
    healthy = lock_held and overdue == 0
    return ObserverLiveness(lock_held, overdue, age_seconds, healthy)


@dataclass(frozen=True, slots=True)
class ObservationJob:
    kind: Literal["artifact_binding", "repository_context"]
    subject_id: UUID
    ticket_id: UUID
    attempt: int
    version: int
    due_at: datetime
    captured_at: datetime
    contract: ContractRevision
    project_key: str
    context_set_digest: str
    binding: ArtifactBinding | None
    previous: PullRequestEvidence | None
    failure_count: int

    @property
    def identity(self) -> str:
        return f"{self.kind}:{self.subject_id}"


class PgDeliveryQueue:
    """Caller supplies the owned transaction; queue operations never open sessions."""

    async def due(
        self,
        session: AsyncSession,
        *,
        project_key: str | None = None,
        limit: int = 100,
        exclude: set[str] | frozenset[str] = frozenset(),
        on_undecodable: Callable[[str, str], None] | None = None,
    ) -> tuple[ObservationJob, ...]:
        """Return the due jobs, oldest first.

        A row whose stored JSON cannot be decoded poisons only itself: with
        ``on_undecodable`` it is skipped and reported as ``(identity, error
        type name)`` -- never the message, which may echo stored content --
        so one bad row cannot hide the others. Without the listener the
        decoding error propagates, as before.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("observer queue limit must be between 1 and 100")
        w, r, b = workflows, revisions, bindings
        failed = confirmations.alias("failed_confirmations")
        # failure_count runs per due row before the outer LIMIT; migration 062's partial error indexes make it an index lookup.
        artifact_failures = (
            sa.select(sa.func.count())
            .select_from(failed)
            .where(
                failed.c.outcome == "error",
                failed.c.subject_kind == "artifact_binding",
                failed.c.binding_id == b.c.id,
                sa.or_(
                    b.c.last_success_at.is_(None),
                    failed.c.collection_finished_at > b.c.last_success_at,
                ),
            )
            .correlate(b)
            .scalar_subquery()
        )
        context_failures = (
            sa.select(sa.func.count())
            .select_from(failed)
            .where(
                failed.c.outcome == "error",
                failed.c.subject_kind == "repository_context",
                failed.c.ticket_id == w.c.ticket_id,
                failed.c.contract_revision == w.c.current_revision,
                failed.c.attempt == w.c.attempt,
                failed.c.context_set_digest == w.c.context_set_digest,
                sa.or_(
                    w.c.context_last_success_at.is_(None),
                    failed.c.collection_finished_at > w.c.context_last_success_at,
                ),
            )
            .correlate(w)
            .scalar_subquery()
        )
        current_revision = _current_revision(w, r)
        eligible = _eligible_ticket(w, tickets)
        context_due = _context_due(w, r)
        common = [
            w.c.ticket_id,
            w.c.attempt,
            w.c.context_set_digest,
            r.c.normalized_contract.label("contract"),
            tickets.c.to_project.label("project_key"),
        ]
        artifact = (
            sa.select(
                sa.literal("artifact_binding").label("kind"),
                b.c.id.label("subject_id"),
                *common,
                b.c.row_version.label("version"),
                b.c.due_at.label("due_at"),
                sa.func.to_jsonb(b.table_valued()).label("binding_row"),
                snapshots.c.evidence.label("previous"),
                b.c.last_success_at.label("last_success_at"),
                sa.func.least(artifact_failures, 6).label("failure_count"),
            )
            .select_from(
                b.join(
                    w,
                    sa.and_(
                        b.c.ticket_id == w.c.ticket_id,
                        b.c.contract_revision == w.c.current_revision,
                        b.c.attempt == w.c.attempt,
                    ),
                )
                .join(r, current_revision)
                .join(tickets, tickets.c.id == w.c.ticket_id)
                .outerjoin(confirmations, confirmations.c.id == b.c.latest_success_confirmation_id)
                .outerjoin(snapshots, snapshots.c.id == confirmations.c.snapshot_id)
            )
            .where(
                eligible,
                b.c.active.is_(True),
                b.c.due_at <= sa.func.clock_timestamp(),
                ~context_due,
            )
        )
        context = (
            sa.select(
                sa.literal("repository_context").label("kind"),
                w.c.ticket_id.label("subject_id"),
                *common,
                w.c.context_row_version.label("version"),
                w.c.context_due_at.label("due_at"),
                sa.cast(sa.null(), JSONB).label("binding_row"),
                sa.cast(sa.null(), JSONB).label("previous"),
                w.c.context_last_success_at.label("last_success_at"),
                sa.func.least(context_failures, 6).label("failure_count"),
            )
            .select_from(w.join(r, current_revision).join(tickets, tickets.c.id == w.c.ticket_id))
            .where(eligible, context_due)
        )
        due = sa.union_all(artifact, context).subquery("due")
        query = sa.select(due, sa.func.clock_timestamp().label("captured_at"))
        if project_key is not None:
            query = query.where(due.c.project_key == project_key)
        if exclude:
            query = query.where(sa.func.concat(due.c.kind, ":", due.c.subject_id).not_in(exclude))
        query = query.order_by(
            due.c.due_at,
            sa.case((due.c.kind == "repository_context", 0), else_=1),
            due.c.ticket_id,
            due.c.subject_id,
        ).limit(limit)
        jobs: list[ObservationJob] = []
        for row in (await session.execute(query)).mappings():
            try:
                contract = _contract_from_json(row["contract"])
                binding = None
                if row["binding_row"] is not None:
                    raw = row["binding_row"]
                    binding = ArtifactBinding.model_validate_json(
                        json.dumps(
                            {
                                key: raw[key]
                                for key in (
                                    "id",
                                    "ticket_id",
                                    "contract_revision",
                                    "attempt",
                                    "deliverable_key",
                                    "repository_id",
                                    "pr_number",
                                    "state",
                                    "head_sha",
                                    "base_sha",
                                    "integration_sha",
                                )
                            }
                            | {"binding_version": raw["row_version"]}
                        )
                    )
                previous = (
                    None
                    if row["previous"] is None
                    else PullRequestEvidence.model_validate_json(json.dumps(row["previous"]))
                )
                failure_count = int(row["failure_count"])
            except (ValueError, KeyError, TypeError) as error:
                if on_undecodable is None:
                    raise
                on_undecodable(f"{row['kind']}:{row['subject_id']}", type(error).__name__)
                continue
            jobs.append(
                ObservationJob(
                    kind=row["kind"],
                    subject_id=row["subject_id"],
                    ticket_id=row["ticket_id"],
                    attempt=row["attempt"],
                    version=row["version"],
                    due_at=row["due_at"],
                    captured_at=row["captured_at"],
                    contract=contract,
                    project_key=row["project_key"],
                    context_set_digest=row["context_set_digest"],
                    binding=binding,
                    previous=previous,
                    failure_count=failure_count,
                )
            )
        return tuple(jobs)

    async def schedule_after(
        self,
        session: AsyncSession,
        job: ObservationJob,
        *,
        delay_seconds: float,
        expected_version: int,
    ) -> bool:
        """CAS only the captured due value; a racing refresh must remain queued."""
        if not 0 < delay_seconds <= 86400:
            raise ValueError("observer retry delay must be positive and bounded")
        next_due = sa.func.clock_timestamp() + timedelta(seconds=delay_seconds)
        generation = sa.exists(
            sa.select(1)
            .select_from(workflows)
            .where(
                workflows.c.ticket_id == job.ticket_id,
                workflows.c.current_revision == job.contract.contract_revision,
                workflows.c.attempt == job.attempt,
                workflows.c.disposition == "active",
            )
        )
        if job.binding is not None:
            query = (
                bindings.update()
                .where(
                    bindings.c.id == job.subject_id,
                    bindings.c.row_version == expected_version,
                    bindings.c.due_at == job.due_at,
                    bindings.c.active.is_(True),
                    generation,
                )
                .values(due_at=next_due)
                .returning(bindings.c.id)
            )
        else:
            query = (
                workflows.update()
                .where(
                    workflows.c.ticket_id == job.ticket_id,
                    workflows.c.current_revision == job.contract.contract_revision,
                    workflows.c.attempt == job.attempt,
                    workflows.c.context_set_digest == job.context_set_digest,
                    workflows.c.context_row_version == expected_version,
                    workflows.c.context_due_at == job.due_at,
                    workflows.c.disposition == "active",
                )
                .values(context_due_at=next_due)
                .returning(workflows.c.ticket_id)
            )
        return await session.scalar(query) is not None
