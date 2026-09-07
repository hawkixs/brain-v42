"""Real PostgreSQL contracts for immutable artifact-binding observations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import (
    delivery_artifact_bindings,
    delivery_confirmations,
    delivery_snapshots,
    delivery_workflows,
    tickets,
)
from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import (
    ContractInput,
    Deliverable,
    PullRequestEvidence,
    ReviewPolicy,
)
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _binding(session_factory):
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo

    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="evidence publication",
            body="immutable provider observations",
            from_project="brain-v42",
            to_project="brain-v42",
        )
    )
    service = DeliveryService(
        PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
    )
    await service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        expected_revision=0,
        idempotency_key=f"evidence-contract-{ticket.id}",
        contract=ContractInput(
            objective="publish immutable provider observations",
            priority=1,
            acceptance_mode="automatic",
            deliverables=(
                Deliverable(
                    key="implementation",
                    repository="hawkixs/brain-v42",
                    target_branch="main",
                    required_checks=(),
                    no_checks_reason="evidence slice",
                    review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
                ),
            ),
        ),
    )
    binding = await service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=1337360966,
        pr_number=42,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"evidence-binding-{ticket.id}",
    )
    return binding


def _evidence(*, collected_at: datetime) -> PullRequestEvidence:
    return PullRequestEvidence(
        provider_id=7001,
        repository_id=1337360966,
        pr_number=42,
        author_id="executor",
        head_repository_id=9988,
        head_sha="a" * 40,
        base_sha="b" * 40,
        base_ref="main",
        state="open",
        draft=False,
        complete=True,
        collected_at=collected_at,
    )


async def test_identical_polls_deduplicate_snapshot_but_append_immutable_confirmations(
    session_factory,
) -> None:
    """Removing semantic deduplication or rewriting a confirmation breaks this contract."""
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    binding = await _binding(session_factory)
    repo = PgDeliveryEvidenceRepo(session_factory)
    started = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            first = await repo.publish_observation(
                session, binding.id, 1, _evidence(collected_at=started), started, started
            )
    async with session_factory() as session:
        async with session.begin():
            second = await repo.publish_observation(
                session,
                binding.id,
                2,
                _evidence(collected_at=started + timedelta(minutes=5)),
                started + timedelta(minutes=5),
                started + timedelta(minutes=5),
            )
    async with session_factory() as session:
        snapshots = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_snapshots)
            .where(delivery_snapshots.c.binding_id == binding.id)
        )
        confirmations = await session.execute(
            sa.select(delivery_confirmations)
            .where(delivery_confirmations.c.binding_id == binding.id)
            .order_by(delivery_confirmations.c.collection_finished_at)
        )
    rows = confirmations.mappings().all()
    assert snapshots == 1
    assert first.id != second.id
    assert rows[0]["collection_finished_at"] == started
    assert rows[1]["collection_finished_at"] == started + timedelta(minutes=5)
    assert rows[0]["snapshot_id"] == rows[1]["snapshot_id"]


async def test_error_keeps_success_pointer_and_advances_attempt_with_caller_rollback(
    session_factory,
) -> None:
    """A failed poll must not erase prior proof and caller rollback must undo publication."""
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    binding = await _binding(session_factory)
    repo = PgDeliveryEvidenceRepo(session_factory)
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            success = await repo.publish_observation(
                session, binding.id, 1, _evidence(collected_at=instant), instant, instant
            )
    async with session_factory() as session:
        transaction = await session.begin()
        await repo.record_observation_error(
            session, binding.id, 2, "provider_timeout", instant, instant + timedelta(seconds=1)
        )
        await transaction.rollback()
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(delivery_artifact_bindings).where(
                        delivery_artifact_bindings.c.id == binding.id
                    )
                )
            )
            .mappings()
            .one()
        )
        confirmation_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.binding_id == binding.id)
        )
        workflow_version = await session.scalar(
            sa.select(delivery_workflows.c.row_version).where(
                delivery_workflows.c.ticket_id == binding.ticket_id
            )
        )
    assert row["latest_success_confirmation_id"] == success.id
    assert row["latest_attempt_confirmation_id"] == success.id
    assert row["row_version"] == 2
    assert confirmation_count == 1
    assert workflow_version == 3


@pytest.mark.parametrize("method", ["success", "error"])
async def test_stale_cas_does_not_append_or_move_current_pointers(
    session_factory, method: str
) -> None:
    """Dropping the row-version fence would let either publisher win after replacement."""
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    binding = await _binding(session_factory)
    repo = PgDeliveryEvidenceRepo(session_factory)
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            if method == "success":
                call = repo.publish_observation(
                    session, binding.id, 2, _evidence(collected_at=instant), instant, instant
                )
            else:
                call = repo.record_observation_error(
                    session, binding.id, 2, "provider_timeout", instant, instant
                )
            with pytest.raises(DeliveryError, match="binding_conflict"):
                await call
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(delivery_artifact_bindings).where(
                        delivery_artifact_bindings.c.id == binding.id
                    )
                )
            )
            .mappings()
            .one()
        )
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.binding_id == binding.id)
        )
    assert count == 0
    assert row["row_version"] == 1
    assert row["latest_success_confirmation_id"] is None
    assert row["latest_attempt_confirmation_id"] is None


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("replacement", "binding_conflict"),
        ("amendment", "binding_superseded"),
        ("terminal", "binding_superseded"),
    ],
)
async def test_replaced_amended_or_terminal_binding_cannot_publish(
    session_factory, change: str, expected_code: str
) -> None:
    """Removing the generation/terminal check would move a successor's evidence pointers."""
    from brain_v42.models.delivery import ContractInput, Deliverable, DeliveryError, ReviewPolicy
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    binding = await _binding(session_factory)
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    if change == "replacement":
        service = DeliveryService(
            PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
        )
        await service.bind_pr(
            binding.ticket_id,
            actor_project="brain-v42",
            deliverable_key="implementation",
            repository_id=1337360966,
            pr_number=43,
            expected_revision=1,
            expected_workflow_version=2,
            idempotency_key=f"replacement-{binding.ticket_id}",
        )
    elif change == "amendment":
        service = DeliveryService(
            PgDeliveryRepo(session_factory), settings=DeliverySettings(enabled=True)
        )
        await service.set_contract(
            binding.ticket_id,
            actor_project="brain-v42",
            expected_revision=1,
            idempotency_key=f"amendment-{binding.ticket_id}",
            contract=ContractInput(
                objective="amended evidence contract",
                priority=1,
                acceptance_mode="automatic",
                deliverables=(
                    Deliverable(
                        key="implementation",
                        repository="hawkixs/brain-v42",
                        target_branch="main",
                        required_checks=(),
                        no_checks_reason="amended evidence slice",
                        review=ReviewPolicy(required_approvals=0, allowed_reviewers=()),
                    ),
                ),
            ),
        )
    else:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    tickets.update()
                    .where(tickets.c.id == binding.ticket_id)
                    .values(status="closed")
                )
    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(DeliveryError, match=expected_code):
                await PgDeliveryEvidenceRepo(session_factory).publish_observation(
                    session, binding.id, 1, _evidence(collected_at=instant), instant, instant
                )
    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.binding_id == binding.id)
        )
    assert count == 0


@pytest.mark.parametrize(
    "override",
    [
        {"repository_id": 777},
        {"pr_number": 77},
        {"base_ref": "release"},
        {"head_repository_id": None},
        {"complete": False},
    ],
)
async def test_invalid_provider_subject_facts_never_publish_success(
    session_factory, override
) -> None:
    """Weak identity, provenance, branch, or completeness validation is unsafe proof."""
    from brain_v42.models.delivery import DeliveryError
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    binding = await _binding(session_factory)
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(DeliveryError, match="evidence_subject_mismatch"):
                await PgDeliveryEvidenceRepo(session_factory).publish_observation(
                    session,
                    binding.id,
                    1,
                    _evidence(collected_at=instant).model_copy(update=override),
                    instant,
                    instant,
                )
    async with session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.binding_id == binding.id)
        )
    assert count == 0


async def test_success_rollback_undoes_snapshot_confirmation_and_pointers(session_factory) -> None:
    """Calling commit inside publication would make this caller rollback ineffective."""
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    binding = await _binding(session_factory)
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        transaction = await session.begin()
        await PgDeliveryEvidenceRepo(session_factory).publish_observation(
            session, binding.id, 1, _evidence(collected_at=instant), instant, instant
        )
        await transaction.rollback()
    async with session_factory() as session:
        snapshots = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_snapshots)
            .where(delivery_snapshots.c.binding_id == binding.id)
        )
        row = (
            (
                await session.execute(
                    sa.select(delivery_artifact_bindings).where(
                        delivery_artifact_bindings.c.id == binding.id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert snapshots == 0
    assert row["latest_attempt_confirmation_id"] is None
    assert row["row_version"] == 1


async def test_identical_error_codes_remain_distinct_immutable_attempts(session_factory) -> None:
    """Deduplicating failures would erase the actual collection-attempt history."""
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    binding = await _binding(session_factory)
    repo = PgDeliveryEvidenceRepo(session_factory)
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            success = await repo.publish_observation(
                session, binding.id, 1, _evidence(collected_at=instant), instant, instant
            )
    async with session_factory() as session:
        async with session.begin():
            first_error = await repo.record_observation_error(
                session, binding.id, 2, "provider_timeout", instant, instant
            )
    async with session_factory() as session:
        async with session.begin():
            second_error = await repo.record_observation_error(
                session,
                binding.id,
                3,
                "provider_timeout",
                instant + timedelta(seconds=1),
                instant + timedelta(seconds=1),
            )
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(delivery_artifact_bindings).where(
                        delivery_artifact_bindings.c.id == binding.id
                    )
                )
            )
            .mappings()
            .one()
        )
    assert first_error.id != second_error.id
    assert row["latest_success_confirmation_id"] == success.id
    assert row["latest_attempt_confirmation_id"] == second_error.id
    assert row["row_version"] == 4
