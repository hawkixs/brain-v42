"""Persisted current-generation work ordering and compare-and-swap rescheduling."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import delivery_artifact_bindings, delivery_workflows, tickets
from brain_v42.delivery_observer.ownership import ObserverOwnership
from brain_v42.models.ticket import TicketStatus
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.ticket_service import TicketService
from tests.integration.db.delivery_observer_cases import ObserverCase
from tests.integration.db.delivery_observer_cases import (
    observer_queue_isolation as observer_queue_isolation,
)
from tests.integration.db.test_delivery_receipt_publication import _contract

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.usefixtures("observer_queue_isolation"),
]


def _queue():
    from brain_v42.repositories.pg_delivery_queue import PgDeliveryQueue

    return PgDeliveryQueue()


async def test_due_order_is_stable_oldest_first_with_exclusions_and_limit(engine, session_factory):
    queue = _queue()
    case = ObserverCase(engine, session_factory)
    bindings = [(await case.create(number=i + 40))[1] for i in range(3)]
    ordered = [bindings[1], bindings[2], bindings[0]]
    base = datetime.now(UTC) - timedelta(minutes=10)
    async with session_factory.begin() as session:
        for index, binding in enumerate(ordered):
            await session.execute(
                delivery_artifact_bindings.update()
                .where(delivery_artifact_bindings.c.id == binding.id)
                .values(due_at=base + timedelta(seconds=index))
            )
    owner = ObserverOwnership(engine)
    assert await owner.acquire()
    try:
        async with owner.transaction() as session:
            jobs = await queue.due(session, limit=2)
            assert [job.binding.id for job in jobs] == [binding.id for binding in ordered[:2]]
            rest = await queue.due(session, limit=2, exclude={job.identity for job in jobs})
            assert [job.binding.id for job in rest] == [ordered[2].id]
    finally:
        await owner.release()


@pytest.mark.parametrize("disposition", ["fulfilled", "cancelled", "wontfix"])
async def test_nonactive_workflow_never_produces_due_jobs(engine, session_factory, disposition):
    queue = _queue()
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create(context=True)
    async with session_factory.begin() as session:
        await session.execute(
            delivery_workflows.update()
            .where(delivery_workflows.c.ticket_id == ticket.id)
            .values(disposition=disposition)
        )
        assert not await queue.due(session)


@pytest.mark.parametrize("status", ["wontfix", "closed", "acked"])
async def test_terminal_ticket_is_excluded_even_if_workflow_still_active(
    engine, session_factory, status
):
    queue = _queue()
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create()
    async with session_factory.begin() as session:
        await session.execute(
            tickets.update()
            .where(tickets.c.id == ticket.id)
            .values(status=status, closed_at=sa.func.now())
        )
        assert not await queue.due(session)


async def test_real_reopen_excludes_historical_attempt_even_with_stale_active_flag(
    engine, session_factory, monkeypatch
):
    monkeypatch.setenv("BRAIN_DELIVERY_ENABLED", "true")
    queue = _queue()
    case = ObserverCase(engine, session_factory)
    ticket, binding, _ = await case.create()
    async with case.runtime() as runtime:
        assert (await runtime.run_once()).collected == 1
    lifecycle = TicketService(PgTicketRepo(session_factory), MagicMock())
    await lifecycle.transition(ticket.id, "brain-v42", "resolve_pending")
    reopened = await lifecycle.transition(ticket.id, "brain-v42", "reopen")
    assert reopened.status is TicketStatus.OPEN
    async with session_factory.begin() as session:
        assert (
            await session.scalar(
                sa.select(delivery_workflows.c.attempt).where(
                    delivery_workflows.c.ticket_id == ticket.id
                )
            )
            == binding.attempt + 1
        )
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.id == binding.id)
            .values(active=True, due_at=sa.func.clock_timestamp())
        )
        assert not await queue.due(session)


async def test_historical_active_flag_cannot_schedule_old_revision(engine, session_factory):
    queue = _queue()
    case = ObserverCase(engine, session_factory)
    ticket, binding, _ = await case.create()
    await case.service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        expected_revision=1,
        idempotency_key=f"observer-amend-{ticket.id}",
        contract=_contract(),
    )
    async with session_factory.begin() as session:
        # Reproduce a stale flag without modifying immutable binding identity.
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.id == binding.id)
            .values(active=True)
        )
        assert not await queue.due(session)


async def test_schedule_only_changes_due_time_and_preserves_a_new_refresh(engine, session_factory):
    queue = _queue()
    case = ObserverCase(engine, session_factory)
    ticket, binding, _ = await case.create()
    async with session_factory.begin() as session:
        (job,) = await queue.due(session)
        assert await queue.schedule_after(
            session, job, delay_seconds=90, expected_version=job.version
        )
    after, _, _, workflow_after = await case.state(binding)
    assert after["row_version"] == binding.binding_version
    assert after["due_at"] > datetime.now(UTC) + timedelta(seconds=80)
    await case.refresh(ticket.id)
    refreshed, _, _, workflow_refreshed = await case.state(binding)
    async with session_factory.begin() as session:
        assert not await queue.schedule_after(
            session, job, delay_seconds=90, expected_version=job.version
        )
    final, _, _, final_workflow = await case.state(binding)
    assert final["due_at"] == refreshed["due_at"]
    assert (
        workflow_after["row_version"]
        == workflow_refreshed["row_version"]
        == final_workflow["row_version"]
    )
    assert final["row_version"] == binding.binding_version


async def test_context_takes_precedence_over_its_binding_and_optionals_do_not_queue(
    engine, session_factory
):
    queue = _queue()
    case = ObserverCase(engine, session_factory)
    required, _, _ = await case.create(context=True)
    await case.create(optional=True, bind=False)
    async with session_factory() as session:
        jobs = await queue.due(session)
    assert (
        len(jobs) == 1 and jobs[0].kind == "repository_context" and jobs[0].ticket_id == required.id
    )


async def test_queue_carries_prior_complete_evidence_after_failed_attempt(engine, session_factory):
    queue = _queue()
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create()
    async with case.runtime() as runtime:
        assert (await runtime.run_once()).collected == 1
    for _ in range(2):
        await case.refresh(ticket.id)
        case.status = 500
        async with case.runtime() as runtime:
            assert (await runtime.run_once()).failed == 1
    await case.refresh(ticket.id)
    async with session_factory() as session:
        (job,) = await queue.due(session)
    assert job.previous is not None and job.previous.complete
    assert job.failure_count == 2
