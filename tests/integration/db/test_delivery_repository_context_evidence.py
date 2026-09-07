"""Real PostgreSQL contracts for immutable repository-context publication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from traceback import format_exception
from uuid import uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import (
    delivery_confirmations,
    delivery_snapshots,
    delivery_workflows,
    tickets,
)
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import (
    ContractInput,
    Deliverable,
    RepositoryContextEvidence,
    RepositoryContextObservationConfirmation,
    RepositoryDocumentFact,
    RepositoryDocumentReference,
    ReviewPolicy,
)
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40
_SHA_D = "d" * 40


def _contract(*, refs: tuple[RepositoryDocumentReference, ...]) -> ContractInput:
    return ContractInput(
        objective="publish immutable repository context",
        priority=1,
        acceptance_mode="automatic",
        context_refs=refs,
        deliverables=(
            Deliverable(
                key="implementation",
                repository="hawkixs/brain-v42",
                target_branch="main",
                required_checks=(),
                no_checks_reason="repository context publication",
                review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
            ),
        ),
    )


async def _workflow(session_factory, *, refs: tuple[RepositoryDocumentReference, ...]):
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="repository context publication",
            body="immutable context observations before a pull request",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    await DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    ).set_contract(
        ticket.id,
        actor_project="brain-v42",
        expected_revision=0,
        idempotency_key=f"context-contract-{ticket.id}",
        contract=_contract(refs=refs),
    )
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket.id)
                )
            )
            .mappings()
            .one()
        )
    return ticket, dict(row)


def _refs() -> tuple[RepositoryDocumentReference, ...]:
    return (
        RepositoryDocumentReference(
            kind="repository_document", repository_id=1337360966, sha=_SHA_A, path="docs/one.md"
        ),
        RepositoryDocumentReference(
            kind="repository_document", repository_id=1337360966, sha=_SHA_B, path="docs/two.md"
        ),
    )


def _evidence(
    *, complete: bool = True, second_path: str = "docs/two.md"
) -> RepositoryContextEvidence:
    return RepositoryContextEvidence(
        complete=complete,
        facts=(
            RepositoryDocumentFact(
                repository_id=1337360966,
                commit_sha=_SHA_A,
                path="docs/one.md",
                tree_sha=_SHA_C,
                blob_sha=_SHA_D,
                mode="100644",
                status="available",
            ),
            RepositoryDocumentFact(
                repository_id=1337360966,
                commit_sha=_SHA_B,
                path=second_path,
                tree_sha=_SHA_C,
                blob_sha=_SHA_D,
                mode="100755",
                status="available",
            ),
        ),
    )


async def _publish(repo, session, row, evidence, started, finished):
    return await repo.publish_repository_context(
        session,
        row["ticket_id"],
        row["current_revision"],
        row["attempt"],
        row["context_set_digest"],
        row["context_row_version"],
        evidence,
        started,
        finished,
    )


async def test_context_success_before_pr_requires_every_exact_pin_and_advances_versions(
    session_factory,
) -> None:
    """Removing a required pin, provenance field, or version advance makes context proof unsafe."""
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    ticket, row = await _workflow(session_factory, refs=_refs())
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            confirmation = await _publish(
                PgDeliveryEvidenceRepo(session_factory), session, row, _evidence(), instant, instant
            )
    async with session_factory() as session:
        current = (
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket.id)
                )
            )
            .mappings()
            .one()
        )
        snapshots = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_snapshots)
            .where(delivery_snapshots.c.ticket_id == ticket.id)
        )
    assert confirmation.outcome == "success"
    assert snapshots == 1
    assert current["latest_context_success_confirmation_id"] == confirmation.id
    assert current["latest_context_attempt_confirmation_id"] == confirmation.id
    assert current["context_row_version"] == 2
    assert current["row_version"] == 2


@pytest.mark.parametrize(
    "evidence",
    [
        RepositoryContextEvidence(complete=False, facts=()),
        RepositoryContextEvidence(complete=False, facts=_evidence().facts),
        RepositoryContextEvidence(complete=True, facts=(_evidence().facts[0],)),
        _evidence(second_path="docs/not-the-pin.md"),
        _evidence().model_copy(update={"facts": (_evidence().facts[0], _evidence().facts[0])}),
        _evidence().model_copy(
            update={"facts": (_evidence().facts[0], _evidence().facts[1], _evidence().facts[0])}
        ),
        _evidence().model_copy(
            update={
                "facts": (
                    _evidence().facts[0].model_copy(update={"tree_sha": None}),
                    _evidence().facts[1],
                )
            }
        ),
        _evidence().model_copy(
            update={
                "facts": (
                    _evidence().facts[0].model_copy(update={"status": "missing"}),
                    _evidence().facts[1],
                )
            }
        ),
        _evidence().model_copy(
            update={
                "facts": (
                    _evidence().facts[0].model_copy(update={"repository_id": 7}),
                    _evidence().facts[1],
                )
            }
        ),
    ],
)
async def test_incomplete_or_wrong_context_subject_never_publishes_success(
    session_factory, evidence
) -> None:
    """Weak required-pin validation would falsely make an unfinished collection complete."""
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    ticket, row = await _workflow(session_factory, refs=_refs())
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(DeliveryError, match="repository_context_mismatch"):
                await _publish(
                    PgDeliveryEvidenceRepo(session_factory),
                    session,
                    row,
                    evidence,
                    instant,
                    instant,
                )
    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.ticket_id == ticket.id)
        )
    assert count == 0


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence().model_copy(
            update={
                "facts": (
                    _evidence().facts[0].model_copy(update={"tree_sha": "invalid-sha"}),
                    _evidence().facts[1],
                )
            }
        ),
        _evidence().model_copy(update={"complete": "not-a-bool"}),
    ],
)
async def test_context_publisher_revalidates_bypassed_dto_before_writing(
    session_factory, evidence
) -> None:
    """Bypassed Pydantic validation must not turn malformed context proof into persisted success."""
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    ticket, row = await _workflow(session_factory, refs=_refs())
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(DeliveryError, match="repository_context_mismatch") as raised:
                await _publish(
                    PgDeliveryEvidenceRepo(session_factory),
                    session,
                    row,
                    evidence,
                    instant,
                    instant,
                )
    assert "invalid-sha" not in raised.value.message
    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.ticket_id == ticket.id)
        )
        snapshots = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_snapshots)
            .where(delivery_snapshots.c.ticket_id == ticket.id)
        )
    assert count == snapshots == 0


async def test_context_publisher_sanitizes_bypassed_dto_traceback() -> None:
    """Chaining DTO validation errors would disclose untrusted provider input in logs."""
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery_evidence import _validated_repository_context_success

    malformed = _evidence().model_copy(
        update={
            "facts": (
                _evidence().facts[0].model_copy(update={"tree_sha": "raw-boundary-sentinel"}),
                _evidence().facts[1],
            )
        }
    )
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    with pytest.raises(DeliveryError) as raised:
        _validated_repository_context_success(malformed, instant, instant)
    assert "raw-boundary-sentinel" not in "".join(format_exception(raised.value))


@pytest.mark.parametrize("method", ["success", "error"])
@pytest.mark.parametrize("interval", ["naive", "inverted"])
async def test_context_publication_validates_intervals_before_mutating(
    session_factory, method: str, interval: str
) -> None:
    """A caller catching ValueError must not commit a context snapshot, confirmation, or pointer."""
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    ticket, row = await _workflow(session_factory, refs=_refs())
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    started = instant.replace(tzinfo=None) if interval == "naive" else instant
    finished = started if interval == "naive" else instant - timedelta(seconds=1)
    escaped: Exception | None = None
    async with session_factory() as session:
        async with session.begin():
            try:
                if method == "success":
                    await _publish(
                        PgDeliveryEvidenceRepo(session_factory),
                        session,
                        row,
                        _evidence(),
                        started,
                        finished,
                    )
                else:
                    await PgDeliveryEvidenceRepo(session_factory).record_repository_context_error(
                        session,
                        ticket.id,
                        row["current_revision"],
                        row["attempt"],
                        row["context_set_digest"],
                        row["context_row_version"],
                        "provider_timeout",
                        started,
                        finished,
                    )
            except ValueError:
                pass
            except DeliveryError as error:
                escaped = error
    async with session_factory() as session:
        current = (
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket.id)
                )
            )
            .mappings()
            .one()
        )
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.ticket_id == ticket.id)
        )
        snapshots = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_snapshots)
            .where(delivery_snapshots.c.ticket_id == ticket.id)
        )
    assert count == snapshots == 0
    assert current["latest_context_success_confirmation_id"] is None
    assert current["latest_context_attempt_confirmation_id"] is None
    assert current["row_version"] == current["context_row_version"] == 1
    assert isinstance(escaped, DeliveryError)
    assert escaped.code == "repository_context_mismatch"


async def test_identical_context_reobservation_deduplicates_snapshot_and_appends_confirmation(
    session_factory,
) -> None:
    """Removing context semantic deduplication or confirmation append-only history breaks audit proof."""
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    ticket, row = await _workflow(session_factory, refs=_refs())
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    repo = PgDeliveryEvidenceRepo(session_factory)
    async with session_factory() as session:
        async with session.begin():
            first = await _publish(repo, session, row, _evidence(), instant, instant)
    row["context_row_version"] = 2
    async with session_factory() as session:
        async with session.begin():
            second = await _publish(
                repo,
                session,
                row,
                _evidence(),
                instant + timedelta(minutes=1),
                instant + timedelta(minutes=1),
            )
    async with session_factory() as session:
        snapshots = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_snapshots)
            .where(delivery_snapshots.c.ticket_id == ticket.id)
        )
    assert first.id != second.id
    assert first.snapshot_id == second.snapshot_id
    assert snapshots == 1
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    sa.select(delivery_confirmations)
                    .where(delivery_confirmations.c.ticket_id == ticket.id)
                    .order_by(delivery_confirmations.c.collection_finished_at)
                )
            )
            .mappings()
            .all()
        )
    assert rows[0]["collection_finished_at"] == instant
    assert rows[1]["collection_finished_at"] == instant + timedelta(minutes=1)
    assert rows[0]["snapshot_id"] == rows[1]["snapshot_id"]


async def test_context_errors_keep_success_and_have_distinct_attempt_ids(session_factory) -> None:
    """Collapsing failed polls or clearing the success pointer loses immutable evidence history."""
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    ticket, row = await _workflow(session_factory, refs=_refs())
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    repo = PgDeliveryEvidenceRepo(session_factory)
    async with session_factory() as session:
        async with session.begin():
            success = await _publish(repo, session, row, _evidence(), instant, instant)
    row["context_row_version"] = 2
    async with session_factory() as session:
        async with session.begin():
            first = await repo.record_repository_context_error(
                session,
                ticket.id,
                1,
                1,
                row["context_set_digest"],
                2,
                "provider_timeout",
                instant,
                instant,
            )
    async with session_factory() as session:
        async with session.begin():
            second = await repo.record_repository_context_error(
                session,
                ticket.id,
                1,
                1,
                row["context_set_digest"],
                3,
                "provider_timeout",
                instant,
                instant + timedelta(seconds=1),
            )
    async with session_factory() as session:
        current = (
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket.id)
                )
            )
            .mappings()
            .one()
        )
    assert first.id != second.id
    assert current["latest_context_success_confirmation_id"] == success.id
    assert current["latest_context_attempt_confirmation_id"] == second.id
    assert current["context_last_error_code"] == "provider_timeout"


@pytest.mark.parametrize(
    "confirmation",
    [
        lambda instant: RepositoryContextObservationConfirmation(
            snapshot_id=None,
            evidence=_evidence(),
            collection_started_at=instant,
            collection_finished_at=instant,
        ),
        lambda instant: RepositoryContextObservationConfirmation(
            snapshot_id=uuid4(),
            evidence=_evidence(),
            error_code="provider_timeout",
            collection_started_at=instant,
            collection_finished_at=instant,
        ),
        lambda instant: RepositoryContextObservationConfirmation(
            snapshot_id=uuid4(),
            evidence=RepositoryContextEvidence(complete=False, facts=_evidence().facts),
            collection_started_at=instant,
            collection_finished_at=instant,
        ),
        lambda instant: RepositoryContextObservationConfirmation(
            snapshot_id=uuid4(),
            evidence=_evidence(),
            collection_started_at=instant.replace(tzinfo=None),
            collection_finished_at=instant.replace(tzinfo=None),
        ),
        lambda instant: RepositoryContextObservationConfirmation(
            snapshot_id=uuid4(),
            evidence=_evidence(),
            outcome="error",
            error_code="provider_timeout",
            collection_started_at=instant,
            collection_finished_at=instant,
        ),
    ],
)
async def test_context_confirmation_dto_rejects_ambiguous_or_unproved_shapes(
    session_factory, confirmation
) -> None:
    """Relaxing outcome, interval, or completeness guards makes frozen proof ambiguous."""
    del session_factory
    with pytest.raises(ValueError):
        confirmation(datetime(2026, 9, 7, 12, tzinfo=UTC))


async def test_reversed_context_fact_order_reuses_the_same_semantic_snapshot(
    session_factory,
) -> None:
    """Hashing collection order would turn an identical required-pin set into duplicate proof."""
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    ticket, row = await _workflow(session_factory, refs=_refs())
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    repo = PgDeliveryEvidenceRepo(session_factory)
    async with session_factory() as session:
        async with session.begin():
            first = await _publish(repo, session, row, _evidence(), instant, instant)
    reversed_evidence = RepositoryContextEvidence(
        complete=True, facts=tuple(reversed(_evidence().facts))
    )
    async with session_factory() as session:
        async with session.begin():
            second = await _publish(
                repo,
                session,
                {**row, "context_row_version": 2},
                reversed_evidence,
                instant + timedelta(minutes=1),
                instant + timedelta(minutes=1),
            )
    assert first.snapshot_id == second.snapshot_id
    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_snapshots)
            .where(delivery_snapshots.c.ticket_id == ticket.id)
        )
    assert count == 1


@pytest.mark.parametrize("method", ["success", "error"])
async def test_stale_context_cas_never_appends_or_moves_pointers(
    session_factory, method: str
) -> None:
    """Dropping the full context CAS lets an older collection replace the current workflow proof."""
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    ticket, row = await _workflow(session_factory, refs=_refs())
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    repo = PgDeliveryEvidenceRepo(session_factory)
    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(DeliveryError, match="repository_context_conflict"):
                if method == "success":
                    await _publish(
                        repo,
                        session,
                        {**row, "context_row_version": 2},
                        _evidence(),
                        instant,
                        instant,
                    )
                else:
                    await repo.record_repository_context_error(
                        session,
                        ticket.id,
                        1,
                        1,
                        row["context_set_digest"],
                        2,
                        "provider_timeout",
                        instant,
                        instant,
                    )
    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.ticket_id == ticket.id)
        )
        current = (
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket.id)
                )
            )
            .mappings()
            .one()
        )
    assert count == 0
    assert current["latest_context_success_confirmation_id"] is None
    assert current["latest_context_attempt_confirmation_id"] is None
    assert current["row_version"] == current["context_row_version"] == 1


@pytest.mark.parametrize("outcome", ["amendment", "attempt", "terminal"])
@pytest.mark.parametrize("method", ["success", "error"])
async def test_superseded_context_cannot_publish_success_or_error(
    session_factory, outcome: str, method: str
) -> None:
    """Skipping generation or terminal fences would attach evidence to a superseded context."""
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    ticket, row = await _workflow(session_factory, refs=_refs())
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    if outcome == "amendment":
        await DeliveryService(
            PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
        ).set_contract(
            ticket.id,
            actor_project="brain-v42",
            expected_revision=1,
            idempotency_key=f"context-amend-{ticket.id}",
            contract=_contract(refs=_refs()),
        )
    elif outcome == "attempt":
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delivery_workflows.update()
                    .where(delivery_workflows.c.ticket_id == ticket.id)
                    .values(attempt=2, row_version=2, context_row_version=2)
                )
    else:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    tickets.update().where(tickets.c.id == ticket.id).values(status="closed")
                )
    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(DeliveryError, match="repository_context_superseded"):
                repo = PgDeliveryEvidenceRepo(session_factory)
                if method == "success":
                    await _publish(repo, session, row, _evidence(), instant, instant)
                else:
                    await repo.record_repository_context_error(
                        session,
                        ticket.id,
                        row["current_revision"],
                        row["attempt"],
                        row["context_set_digest"],
                        row["context_row_version"],
                        "provider_timeout",
                        instant,
                        instant,
                    )


@pytest.mark.parametrize("method", ["success", "error"])
async def test_context_publication_respects_caller_rollback(session_factory, method: str) -> None:
    """A hidden repository commit would make a caller-owned rollback ineffective."""
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    ticket, row = await _workflow(session_factory, refs=_refs())
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        transaction = await session.begin()
        repo = PgDeliveryEvidenceRepo(session_factory)
        if method == "success":
            await _publish(repo, session, row, _evidence(), instant, instant)
        else:
            await repo.record_repository_context_error(
                session,
                ticket.id,
                1,
                1,
                row["context_set_digest"],
                1,
                "provider_timeout",
                instant,
                instant,
            )
        await transaction.rollback()
    async with session_factory() as session:
        current = (
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket.id)
                )
            )
            .mappings()
            .one()
        )
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.ticket_id == ticket.id)
        )
        snapshots = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_snapshots)
            .where(delivery_snapshots.c.ticket_id == ticket.id)
        )
    assert count == 0
    assert snapshots == 0
    assert current["latest_context_attempt_confirmation_id"] is None
    assert current["row_version"] == current["context_row_version"] == 1
