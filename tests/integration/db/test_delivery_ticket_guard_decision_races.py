"""Completion revalidates proof after real PostgreSQL lock waits."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import decisions, delivery_workflows
from brain_v42.models.ticket import TicketStatus
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService
from brain_v42.services.ticket_service import TicketService, TicketTransitionConflictError

from .test_delivery_receipt_issuance import RID, _contract
from .test_delivery_requester_acceptance import (
    _accept,
    _now,
    _publish_and_issue,
    _settings,
    _workflow,
)
from .test_delivery_ticket_guard_attachment_races import _wait_for_blocking_close
from .test_delivery_ticket_guard_edges import _eligible
from .test_delivery_ticket_guards import _receipt_count

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("BRAIN_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("BRAIN_DELIVERY_FRESHNESS_SECONDS", "3600")


class _PauseAfterMutation(PgDeliveryRepo):
    def __init__(self, factory, entered, release):
        super().__init__(factory)
        self.entered, self.release = entered, release
        self.pid = None

    async def _pause(self, session):
        self.pid = await session.scalar(sa.text("SELECT pg_backend_pid()"))
        self.entered.set()
        await self.release.wait()

    async def set_contract(self, *args, **kwargs):
        result = await super().set_contract(*args, **kwargs)
        await self._pause(kwargs["session"])
        return result

    async def bind_pr(self, *args, **kwargs):
        result = await super().bind_pr(*args, **kwargs)
        await self._pause(kwargs["session"])
        return result


@pytest.mark.parametrize("mutation", ["amend", "rebind"])
async def test_waiting_completion_rechecks_real_committed_contract_or_binding_change(
    session_factory, mutation
):
    ticket, binding, delivery = await _workflow(session_factory)
    integration = await _publish_and_issue(session_factory, ticket.id, binding)
    await _accept(delivery, ticket.id, integration)
    await _eligible(session_factory, ticket.id, "cross_resolve")
    view = await delivery.get(ticket.id, actor_project="requester")
    entered, release = asyncio.Event(), asyncio.Event()
    repo = _PauseAfterMutation(session_factory, entered, release)
    writer = DeliveryService(repo, settings=_settings())
    if mutation == "amend":
        operation = writer.set_contract(
            ticket.id,
            actor_project="requester",
            expected_revision=1,
            idempotency_key=f"amend-race-{ticket.id}",
            contract=_contract(mode="explicit"),
        )
    else:
        operation = writer.bind_pr(
            ticket.id,
            actor_project="executor",
            deliverable_key="implementation",
            repository_id=RID,
            pr_number=43,
            expected_revision=1,
            expected_workflow_version=view.assessment.assessment_version,
            idempotency_key=f"rebind-race-{ticket.id}",
        )
    mutator = asyncio.create_task(operation)
    close = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        close = asyncio.create_task(
            TicketService(PgTicketRepo(session_factory), MagicMock()).transition(
                ticket.id, "executor", "resolve", message="old proof must not pass"
            )
        )
        async with session_factory() as observer:
            await _wait_for_blocking_close(observer, setter_pid=repo.pid, close=close)
        release.set()
        await asyncio.wait_for(mutator, 5)
        with pytest.raises(
            TicketTransitionConflictError, match="delivery_requirements_unsatisfied"
        ):
            await asyncio.wait_for(close, 5)
        actual = await PgTicketRepo(session_factory).get_by_id(ticket.id)
        assert actual.status is TicketStatus.OPEN
        assert await PgTicketRepo(session_factory).get_messages(ticket.id) == []
        assert await _receipt_count(session_factory, ticket.id) == 2
    finally:
        release.set()
        for task in (mutator, close):
            if task is not None:
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError, TicketTransitionConflictError):
                    await task


async def test_completion_uses_pg_clock_after_required_source_wait(session_factory, monkeypatch):
    monkeypatch.setenv("BRAIN_DELIVERY_FRESHNESS_SECONDS", "2")
    async with session_factory() as session, session.begin():
        source = (
            await session.execute(
                decisions.insert()
                .values(
                    title="completion deadline", description="retained source", reasoning="fixture"
                )
                .returning(decisions.c.id)
            )
        ).scalar_one()
    ticket, binding, delivery = await _workflow(session_factory, source=source)
    integration = await _publish_and_issue(session_factory, ticket.id, binding, freshness=2)
    await _eligible(session_factory, ticket.id, "cross_resolve")
    deadline = integration.proof.artifact_proofs[0].collection_finished_at + timedelta(seconds=2)
    close = None
    async with session_factory() as blocker, session_factory() as observer:
        transaction = await blocker.begin()
        try:
            await blocker.execute(
                sa.select(decisions.c.id).where(decisions.c.id == source).with_for_update()
            )
            pid = await blocker.scalar(sa.text("SELECT pg_backend_pid()"))
            close = asyncio.create_task(
                TicketService(PgTicketRepo(session_factory), MagicMock()).transition(
                    ticket.id, "executor", "resolve", message="expired while waiting"
                )
            )
            await _wait_for_blocking_close(observer, setter_pid=pid, close=close)
            started = await observer.scalar(
                sa.text(
                    "SELECT min(xact_start) FROM pg_stat_activity WHERE datname=current_database() "
                    "AND :pid = ANY(pg_blocking_pids(pid))"
                ),
                {"pid": pid},
            )
            assert isinstance(started, datetime) and started < deadline
            await asyncio.sleep(2.1)
            released = await _now(blocker)
            assert started < deadline <= released
            await transaction.commit()
            with pytest.raises(
                TicketTransitionConflictError, match="delivery_requirements_unsatisfied"
            ):
                await asyncio.wait_for(close, 5)
        finally:
            if blocker.in_transaction():
                await blocker.rollback()
            if close is not None:
                if not close.done():
                    close.cancel()
                with suppress(asyncio.CancelledError, TicketTransitionConflictError):
                    await close
    assert (await PgTicketRepo(session_factory).get_by_id(ticket.id)).status is TicketStatus.OPEN
    assert await PgTicketRepo(session_factory).get_messages(ticket.id) == []
    assert await _receipt_count(session_factory, ticket.id) == 1


async def test_reopen_invalidates_actual_repository_context_confirmation(session_factory):
    from .test_delivery_repository_context_evidence import _evidence, _publish, _refs
    from .test_delivery_requester_acceptance import _observer

    ticket, binding, delivery = await _workflow(session_factory, refs=_refs())
    async with session_factory() as session, session.begin():
        row = (
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket.id)
                )
            )
            .mappings()
            .one()
        )
        now = await _now(session)
        confirmation = await _publish(
            _observer(session_factory), session, row, _evidence(), now, now
        )
    integration = await _publish_and_issue(session_factory, ticket.id, binding)
    assert integration is not None
    service = TicketService(PgTicketRepo(session_factory), MagicMock())
    await service.transition(ticket.id, "executor", "resolve")
    await service.transition(ticket.id, "requester", "reopen")
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
    assert confirmation.id is not None
    assert current["attempt"] == 2
    assert current["context_row_version"] > row["context_row_version"]
    assert current["latest_context_success_confirmation_id"] is None
    assert current["latest_context_attempt_confirmation_id"] is None
    assert current["context_last_success_at"] is None
    assert current["context_last_attempt_at"] is None
    assert current["context_due_at"] is not None
    view = await delivery.get(ticket.id, actor_project="requester")
    assert len(view.contexts) == 2
    assert all(item.status == "missing" for item in view.contexts)
    assert await _receipt_count(session_factory, ticket.id) == 1
