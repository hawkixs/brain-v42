"""Real PostgreSQL contracts for immutable artifact-binding observations."""

from __future__ import annotations

import traceback
from datetime import UTC, datetime, timedelta, tzinfo

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
    CheckAttempt,
    ContractInput,
    Deliverable,
    DeliveryError,
    PullRequestEvidence,
    ReviewPolicy,
)
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class _TimezoneWithoutOffset(tzinfo):
    def utcoffset(self, value: datetime | None) -> None:
        return None

    def dst(self, value: datetime | None) -> None:
        return None

    def tzname(self, value: datetime | None) -> str:
        return "naive-with-tzinfo"


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


def _malformed_nested_evidence(*, collected_at: datetime, sentinel: str) -> PullRequestEvidence:
    """Build a model_copy-only invalid nested DTO, as an untrusted collector can."""
    check = CheckAttempt(
        record_id=1,
        provider_id=7001,
        kind="check_run",
        name="test-unit",
        app_slug="github-actions",
        head_sha="a" * 40,
        conclusion="success",
    )
    return _evidence(collected_at=collected_at).model_copy(
        update={"checks": (check.model_copy(update={"head_sha": sentinel}),)}
    )


async def _publication_state(session_factory, binding):
    async with session_factory() as session:
        binding_row = (
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
        workflow_row = (
            (
                await session.execute(
                    sa.select(delivery_workflows).where(
                        delivery_workflows.c.ticket_id == binding.ticket_id
                    )
                )
            )
            .mappings()
            .one()
        )
        snapshots = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_snapshots)
            .where(delivery_snapshots.c.binding_id == binding.id)
        )
        confirmations = await session.scalar(
            sa.select(sa.func.count())
            .select_from(delivery_confirmations)
            .where(delivery_confirmations.c.binding_id == binding.id)
        )
    return binding_row, workflow_row, snapshots, confirmations


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


@pytest.mark.parametrize(
    "case",
    [
        "root_sha",
        "nested_check_sha",
        "success_naive_interval",
        "error_naive_interval",
        "success_mixed_interval",
        "error_mixed_interval",
        "success_pseudo_aware_interval",
        "error_pseudo_aware_interval",
    ],
)
async def test_invalid_observations_raise_safely_before_caller_commit_and_leave_no_state(
    session_factory, case: str
) -> None:
    """Validation after inserts would let a caller commit a corrupt observation."""
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    binding = await _binding(session_factory)
    (
        before_binding,
        before_workflow,
        before_snapshots,
        before_confirmations,
    ) = await _publication_state(session_factory, binding)
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    started = instant
    finished = instant
    sentinel = "sentinel" + "g" * 32
    evidence = _evidence(collected_at=instant)
    if case == "root_sha":
        evidence = evidence.model_copy(update={"head_sha": sentinel})
    elif case == "nested_check_sha":
        evidence = _malformed_nested_evidence(collected_at=instant, sentinel=sentinel)
    elif "naive_interval" in case:
        started = started.replace(tzinfo=None)
        finished = finished.replace(tzinfo=None)
    elif "mixed_interval" in case:
        finished = finished.astimezone().replace(tzinfo=None) + timedelta(seconds=1)
    elif "pseudo_aware_interval" in case:
        started = datetime(2026, 9, 7, 12, tzinfo=_TimezoneWithoutOffset())
        finished = datetime(2026, 9, 7, 12, tzinfo=_TimezoneWithoutOffset())

    caught: Exception | None = None
    async with session_factory() as session:
        async with session.begin():
            try:
                if case.startswith("error_"):
                    await PgDeliveryEvidenceRepo(session_factory).record_observation_error(
                        session,
                        binding.id,
                        1,
                        "provider_timeout",
                        started,
                        finished,
                    )
                else:
                    await PgDeliveryEvidenceRepo(session_factory).publish_observation(
                        session, binding.id, 1, evidence, started, finished
                    )
            except Exception as error:  # The caller intentionally commits after a rejected poll.
                caught = error

    assert isinstance(caught, DeliveryError)
    if case in {"root_sha", "nested_check_sha"}:
        assert sentinel not in str(caught)
        assert sentinel not in "".join(traceback.format_exception(caught))
    after_binding, after_workflow, snapshots, confirmations = await _publication_state(
        session_factory, binding
    )
    assert snapshots == before_snapshots == 0
    assert confirmations == before_confirmations == 0
    assert after_binding["row_version"] == before_binding["row_version"]
    assert after_binding["latest_success_confirmation_id"] is None
    assert after_binding["latest_attempt_confirmation_id"] is None
    assert after_workflow["row_version"] == before_workflow["row_version"]


@pytest.mark.parametrize("method", ["success", "error"])
async def test_reversed_aware_interval_is_rejected_before_artifact_publication(
    session_factory, method: str
) -> None:
    """The prior SQL check rejects this too; the publisher must now reject before locking/writing."""
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    binding = await _binding(session_factory)
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(DeliveryError):
                if method == "success":
                    await PgDeliveryEvidenceRepo(session_factory).publish_observation(
                        session,
                        binding.id,
                        1,
                        _evidence(collected_at=instant),
                        instant + timedelta(seconds=1),
                        instant,
                    )
                else:
                    await PgDeliveryEvidenceRepo(session_factory).record_observation_error(
                        session,
                        binding.id,
                        1,
                        "provider_timeout",
                        instant + timedelta(seconds=1),
                        instant,
                    )
    _binding_row, _workflow_row, snapshots, confirmations = await _publication_state(
        session_factory, binding
    )
    assert snapshots == 0
    assert confirmations == 0


async def test_valid_success_and_error_continue_to_round_trip_through_strict_hydration(
    session_factory,
) -> None:
    """Prevalidation must preserve valid success/error publication and retained proof hydration."""
    from brain_v42.repositories.pg_delivery import PgDeliveryRepo
    from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo

    binding = await _binding(session_factory)
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    repo = PgDeliveryEvidenceRepo(session_factory)
    async with session_factory() as session:
        async with session.begin():
            success = await repo.publish_observation(
                session, binding.id, 1, _evidence(collected_at=instant), instant, instant
            )
    async with session_factory() as session:
        async with session.begin():
            failure = await repo.record_observation_error(
                session,
                binding.id,
                2,
                "provider_timeout",
                instant,
                instant + timedelta(seconds=1),
            )

    hydrated = await PgDeliveryRepo(session_factory).load_inputs(
        binding.ticket_id, feature_enabled=True, freshness_seconds=600
    )

    evidence = hydrated.active_bindings[0]
    assert evidence.confirmation is not None
    assert evidence.confirmation.id == success.id
    assert evidence.confirmation.evidence is not None
    assert evidence.confirmation.evidence.head_sha == "a" * 40
    assert evidence.latest_attempt_confirmation_id == failure.id
    assert evidence.last_attempt_outcome == "error"


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
