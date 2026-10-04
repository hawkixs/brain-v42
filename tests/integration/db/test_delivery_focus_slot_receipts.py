"""Receipt auto-close (spec §5): integration receipts and observer releases, nothing else.

Writes slot rows, which can never be deleted: this module runs on the module-private head
database, like the other slot-writing modules, and uses the project `executor`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.focus_slots import close_slots_satisfied_by
from brain_v42.db.tables import (
    brain_sessions,
    delivery_artifact_bindings,
    delivery_attestations,
    delivery_workflows,
    focus_slot_history,
    focus_slots,
)
from brain_v42.models.brain_session import BrainSessionFocusOutcome
from brain_v42.models.focus_slot import FocusSlotError, SlotAnchor
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_release_derivation import (
    OBSERVER_IDENTITY,
    PgReleaseDerivationRepo,
    ReleaseCandidate,
)
from brain_v42.repositories.pg_ticket import PgTicketRepo
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slot_binding import sessions
from tests.integration.db.test_delivery_focus_slot_relay import relay
from tests.integration.db.test_delivery_focus_slots import base_state, ensure_project, service
from tests.integration.db.test_delivery_receipt_issuance import RID, C, _contract
from tests.integration.db.test_delivery_requester_acceptance import (
    _now,
    _observer,
    _proof,
    _service,
)

# Fixtures are rebound by assignment: a plain import would be shadowed by the parameters.
session_factory = _slots.session_factory

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
NOW = datetime(2026, 10, 3, tzinfo=UTC)
EXECUTOR = "executor"


async def workflow(factory, *, pr_number: int | None = None):
    """A delivery workflow of `executor` bound to its own PR number, so no other test's
    receipt on PR 42 makes a fresh PR anchor already received."""
    await ensure_project(factory, EXECUTOR)
    number = pr_number or 10_000 + uuid4().int % 80_000
    ticket = await PgTicketRepo(factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"slot receipt {uuid4()}",
            body="b",
            from_project="requester",
            to_project=EXECUTOR,
        )
    )
    delivery = _service(factory)
    await delivery.set_contract(
        ticket.id,
        actor_project="requester",
        expected_revision=0,
        idempotency_key=f"contract-{ticket.id}",
        contract=_contract(),
    )
    binding = await delivery.bind_pr(
        ticket.id,
        actor_project=EXECUTOR,
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=number,
        expected_revision=1,
        expected_workflow_version=1,
        idempotency_key=f"binding-{ticket.id}",
    )
    return ticket, binding, delivery, number


async def integrate_in(factory, session, ticket_id: UUID, binding, pr_number: int):
    """Issue the integration receipt inside the caller's open transaction."""
    now = await _now(session)
    evidence = _proof(now).model_copy(update={"pr_number": pr_number})
    await PgDeliveryEvidenceRepo(factory).publish_observation(
        session, binding.id, binding.binding_version, evidence, now, now
    )
    receipt = await _observer(factory).issue_integration_receipt(session, ticket_id)
    assert receipt is not None
    return receipt


async def integrate(factory, ticket_id: UUID, binding, pr_number: int):
    async with factory() as session, session.begin():
        return await integrate_in(factory, session, ticket_id, binding, pr_number)


async def release_in(session, ticket_id: UUID, tag: str) -> None:
    """Record the observer's release inside the caller's open transaction."""
    candidate = ReleaseCandidate(
        ticket_id=ticket_id,
        to_project=EXECUTOR,
        contract_revision=1,
        deliverable_key="implementation",
        repository_id=RID,
        integration_sha=C,
    )
    await PgReleaseDerivationRepo().record_released(
        session, candidate, SimpleNamespace(name=tag, sha="d" * 40), NOW
    )


async def release(factory, ticket_id: UUID, tag: str) -> None:
    async with factory() as session, session.begin():
        await release_in(session, ticket_id, tag)


async def slot(factory, slot_id: UUID):
    async with factory() as session:
        return (
            (await session.execute(sa.select(focus_slots).where(focus_slots.c.id == slot_id)))
            .mappings()
            .one()
        )


async def sources(factory, slot_id: UUID) -> list[str]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    sa.select(focus_slot_history.c.source)
                    .where(focus_slot_history.c.slot_id == slot_id)
                    .order_by(focus_slot_history.c.revision)
                )
            ).scalars()
        )


async def pending(factory, slot_id: UUID) -> bool:
    listed = await service(factory).list(EXECUTOR, "open", limit=100)
    return next(item.receipt_pending for item in listed.slots if item.id == slot_id)


async def plan(factory, ticket_id: UUID, release_name: str) -> None:
    assert (
        await PgTicketRepo(factory).set_target_release(
            ticket_id, author_project=EXECUTOR, expected=None, new=release_name, message="plan"
        )
        is not None
    )


async def open_slot(factory, *anchors: SlotAnchor, title: str = "slot"):
    return await service(factory).open(EXECUTOR, f"{title} {uuid4().hex[:6]}", "b", list(anchors))


async def test_an_integration_receipt_closes_a_ticket_slot(session_factory):
    ticket, binding, _, number = await workflow(session_factory)
    opened = await open_slot(session_factory, SlotAnchor(kind="ticket", ticket_id=ticket.id))
    before = await base_state(session_factory, EXECUTOR)
    receipt = await integrate(session_factory, ticket.id, binding, number)
    closed = await slot(session_factory, opened.slot.id)
    assert (closed["close_reason"], closed["revision"]) == (f"receipt:{receipt.id}", 1)
    assert closed["close_note"] is None and closed["closed_at"] is not None
    assert await sources(session_factory, opened.slot.id) == ["slot_open", "slot_close"]
    assert await base_state(session_factory, EXECUTOR) == before  # S1


async def test_a_pr_slot_closes_on_its_bound_tickets_integration(session_factory):
    ticket, binding, _, number = await workflow(session_factory)
    opened = await open_slot(
        session_factory, SlotAnchor(kind="pr", repository_id=RID, pr_number=number)
    )
    receipt = await integrate(session_factory, ticket.id, binding, number)
    assert (await slot(session_factory, opened.slot.id))["close_reason"] == f"receipt:{receipt.id}"


async def test_a_lot_decides_a_mixed_slot_and_closes_on_the_observer_release(session_factory):
    ticket, binding, _, number = await workflow(session_factory)
    await plan(session_factory, ticket.id, "8.4.1")
    opened = await open_slot(
        session_factory,
        SlotAnchor(kind="lot", target_release="8.4.1"),
        SlotAnchor(kind="ticket", ticket_id=ticket.id),
    )
    await integrate(session_factory, ticket.id, binding, number)
    assert (await slot(session_factory, opened.slot.id))["closed_at"] is None  # Q2
    assert await pending(session_factory, opened.slot.id) is False
    await release(session_factory, ticket.id, "v8.4.1")
    closed = await slot(session_factory, opened.slot.id)
    assert closed["close_reason"].startswith("receipt:") and closed["revision"] == 1


async def test_a_replayed_observer_release_is_idempotent(session_factory):
    ticket, binding, _, number = await workflow(session_factory)
    await plan(session_factory, ticket.id, "8.4.3")
    await integrate(session_factory, ticket.id, binding, number)  # a real release candidate
    opened = await open_slot(session_factory, SlotAnchor(kind="lot", target_release="8.4.3"))
    await release(session_factory, ticket.id, "v8.4.3")
    await release(session_factory, ticket.id, "v8.4.3")
    assert await sources(session_factory, opened.slot.id) == ["slot_open", "slot_close"]


async def test_a_declared_or_foreign_release_closes_nothing(session_factory):
    ticket, _, delivery, _ = await workflow(session_factory)
    await plan(session_factory, ticket.id, "8.4.2")
    opened = await open_slot(session_factory, SlotAnchor(kind="lot", target_release="8.4.2"))
    payload = {"repository_id": RID, "tag": "v8.4.2", "tag_sha": "d" * 40, "integration_sha": C}
    await delivery.attest(
        ticket.id,
        actor_project=EXECUTOR,
        caller_identity="caller-declaration",
        kind="released",
        payload=payload,
        idempotency_key=f"declared-{ticket.id}",
        emitted_at=NOW,
        contract_revision=1,
    )
    await ensure_project(session_factory, "brain-v42")
    foreign = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"foreign {uuid4()}",
            body="b",
            from_project="requester",
            to_project="brain-v42",
        )
    )
    foreign_delivery = _service(session_factory)
    await foreign_delivery.set_contract(
        foreign.id,
        actor_project="requester",
        expected_revision=0,
        idempotency_key=f"contract-{foreign.id}",
        contract=_contract(),
    )
    await foreign_delivery.attest(
        foreign.id,
        actor_project="brain-v42",
        caller_identity=OBSERVER_IDENTITY,
        kind="released",
        payload=payload,
        idempotency_key=f"foreign-{foreign.id}",
        emitted_at=NOW,
        contract_revision=1,
    )
    assert (await slot(session_factory, opened.slot.id))["closed_at"] is None
    assert await pending(session_factory, opened.slot.id) is False
    async with session_factory.begin() as session:  # the predicate, not the hook's absence
        closed = await close_slots_satisfied_by(
            session, ticket_ids=[ticket.id, foreign.id], completing_row_id=uuid4()
        )
    assert closed == []


async def observer_release(factory, delivery, ticket_id: UUID, *, actor: str, tag: str, key: str):
    """The generic, unhooked `attest`, signed by the observer identity: it writes the row the
    lot predicate reads without running a receipt hook, so a test controls what the slot sees."""
    await delivery.attest(
        ticket_id,
        actor_project=actor,
        caller_identity=OBSERVER_IDENTITY,
        kind="released",
        payload={"repository_id": RID, "tag": tag, "tag_sha": "d" * 40, "integration_sha": C},
        idempotency_key=f"{key}-{ticket_id}",
        emitted_at=NOW,
        contract_revision=1,
    )


async def assert_lot_stays_open(factory, slot_id: UUID, *ticket_ids: UUID) -> None:
    assert (await slot(factory, slot_id))["closed_at"] is None
    assert await pending(factory, slot_id) is False
    async with factory.begin() as session:  # the predicate, not the hook's absence
        closed = await close_slots_satisfied_by(
            session, ticket_ids=list(ticket_ids), completing_row_id=uuid4()
        )
    assert closed == []


async def test_a_release_attested_by_the_wrong_issuer_project_closes_nothing(session_factory):
    """Right issuer identity, right tag, right ticket: only the issuer project is wrong."""
    ticket, _, delivery, _ = await workflow(session_factory)
    await plan(session_factory, ticket.id, "8.4.4")
    opened = await open_slot(session_factory, SlotAnchor(kind="lot", target_release="8.4.4"))
    await observer_release(
        session_factory, delivery, ticket.id, actor="requester", tag="v8.4.4", key="wrong-issuer"
    )
    await assert_lot_stays_open(session_factory, opened.slot.id, ticket.id)


async def test_a_release_on_a_ticket_of_another_project_closes_nothing(session_factory):
    """Right issuer identity, right tag, issuer project = the slot's: only the ticket is foreign."""
    planned, _, _, _ = await workflow(session_factory)
    await plan(session_factory, planned.id, "8.4.5")
    opened = await open_slot(session_factory, SlotAnchor(kind="lot", target_release="8.4.5"))
    await ensure_project(session_factory, "brain-v42")
    foreign = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"foreign {uuid4()}",
            body="b",
            from_project=EXECUTOR,
            to_project="brain-v42",
        )
    )
    delivery = _service(session_factory)
    await delivery.set_contract(
        foreign.id,
        actor_project=EXECUTOR,
        expected_revision=0,
        idempotency_key=f"contract-{foreign.id}",
        contract=_contract(),
    )
    await observer_release(
        session_factory, delivery, foreign.id, actor=EXECUTOR, tag="v8.4.5", key="foreign-ticket"
    )
    await assert_lot_stays_open(session_factory, opened.slot.id, planned.id, foreign.id)


async def test_a_catch_up_close_names_the_completing_row_not_the_hooks_row(session_factory):
    """A lot slot already pending (its release row exists, no hook ran) closes when ANOTHER
    receipt of the project arrives; `close_reason` names the release, not that receipt."""
    planned, _, delivery, _ = await workflow(session_factory)
    await plan(session_factory, planned.id, "8.4.6")
    opened = await open_slot(session_factory, SlotAnchor(kind="lot", target_release="8.4.6"))
    await observer_release(
        session_factory, delivery, planned.id, actor=EXECUTOR, tag="v8.4.6", key="catch-up"
    )
    assert await pending(session_factory, opened.slot.id) is True
    async with session_factory() as session:
        attestation_id = await session.scalar(
            sa.select(delivery_attestations.c.id).where(
                delivery_attestations.c.ticket_id == planned.id
            )
        )
    other, binding, _, number = await workflow(session_factory)
    receipt = await integrate(session_factory, other.id, binding, number)
    reason = (await slot(session_factory, opened.slot.id))["close_reason"]
    assert reason == f"receipt:{attestation_id}" and reason != f"receipt:{receipt.id}"


async def test_a_receipt_of_an_older_revision_does_not_satisfy_a_reopened_ticket(session_factory):
    """The workflow join is the only guard against a reopened ticket counting as received."""
    ticket, binding, delivery, number = await workflow(session_factory)
    first = await open_slot(
        session_factory,
        SlotAnchor(kind="ticket", ticket_id=ticket.id),
        SlotAnchor(kind="pr", repository_id=RID, pr_number=number),
    )
    await integrate(session_factory, ticket.id, binding, number)
    assert (await slot(session_factory, first.slot.id))["closed_at"] is not None
    await delivery.set_contract(
        ticket.id,
        actor_project="requester",
        expected_revision=1,
        idempotency_key=f"recontract-{ticket.id}",
        contract=_contract(),
    )
    # The receipt at revision 1 is still in `delivery_receipts`; revision 2 has none.
    ticket_slot = await open_slot(session_factory, SlotAnchor(kind="ticket", ticket_id=ticket.id))
    pr_slot = await open_slot(
        session_factory, SlotAnchor(kind="pr", repository_id=RID, pr_number=number)
    )
    async with session_factory.begin() as session:
        closed = await close_slots_satisfied_by(
            session, ticket_ids=[ticket.id], completing_row_id=uuid4()
        )
    assert closed == []
    for opened in (ticket_slot, pr_slot):
        assert (await slot(session_factory, opened.slot.id))["closed_at"] is None
        assert await pending(session_factory, opened.slot.id) is False


async def test_a_receipt_of_an_older_attempt_does_not_satisfy_a_ticket_or_pr_anchor(
    session_factory,
):
    """Same contract revision, newer attempt (a reopen): the attempt join rejects the receipt."""
    ticket, binding, _, number = await workflow(session_factory)
    first = await open_slot(session_factory, SlotAnchor(kind="ticket", ticket_id=ticket.id))
    await integrate(session_factory, ticket.id, binding, number)
    assert (await slot(session_factory, first.slot.id))["closed_at"] is not None
    # What a reopen does, minus the unrelated side effects: attempt 2 at revision 1, with the
    # binding carried to the new attempt so that only the receipt's attempt can disqualify it.
    async with session_factory.begin() as session:
        await session.execute(
            delivery_workflows.update()
            .where(delivery_workflows.c.ticket_id == ticket.id)
            .values(attempt=2)
        )
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.ticket_id == ticket.id)
            .values(attempt=2)
        )
    ticket_slot = await open_slot(session_factory, SlotAnchor(kind="ticket", ticket_id=ticket.id))
    pr_slot = await open_slot(
        session_factory, SlotAnchor(kind="pr", repository_id=RID, pr_number=number)
    )
    async with session_factory.begin() as session:
        closed = await close_slots_satisfied_by(
            session, ticket_ids=[ticket.id], completing_row_id=uuid4()
        )
    assert closed == []
    for opened in (ticket_slot, pr_slot):
        assert (await slot(session_factory, opened.slot.id))["closed_at"] is None
        assert await pending(session_factory, opened.slot.id) is False


async def test_ruling_1_end_to_end_a_bound_session_meets_a_receipt_close(session_factory):
    ticket, binding, _, number = await workflow(session_factory)
    opened = await open_slot(
        session_factory, SlotAnchor(kind="ticket", ticket_id=ticket.id), title="bound"
    )
    svc = sessions(session_factory)
    first = await svc.start(EXECUTOR, f"op-{uuid4().hex[:8]}")
    await svc.bind(first.session.id, first.session.client_key, opened.slot.id)
    await integrate(session_factory, ticket.id, binding, number)
    with pytest.raises(
        FocusSlotError, match=r"^slot_closed: .*expected_focus_revision=0 \(the pre-close"
    ):
        await svc.end(first.session.id, first.session.client_key, "s", "after close", 1)
    ended = await svc.end(first.session.id, first.session.client_key, "s", "after close", 0)
    assert ended.focus_outcome is BrainSessionFocusOutcome.CONFLICT


async def test_ruling_1_a_bound_session_can_still_be_abandoned_after_a_receipt_close(
    session_factory,
):
    ticket, binding, _, number = await workflow(session_factory)
    opened = await open_slot(
        session_factory, SlotAnchor(kind="ticket", ticket_id=ticket.id), title="abandon"
    )
    svc = sessions(session_factory)
    started = await svc.start(EXECUTOR, f"op-{uuid4().hex[:8]}")
    await svc.bind(started.session.id, started.session.client_key, opened.slot.id)
    await integrate(session_factory, ticket.id, binding, number)
    await svc.abandon(started.session.id, started.session.client_key, "receipt closed the slot")
    async with session_factory() as session:
        status = await session.scalar(
            sa.select(brain_sessions.c.status).where(brain_sessions.c.id == started.session.id)
        )
    assert status == "abandoned"


async def test_a_receipt_racing_a_relay_ends_consistent_either_way(session_factory):
    ticket, binding, _, number = await workflow(session_factory)
    opened = await open_slot(
        session_factory, SlotAnchor(kind="ticket", ticket_id=ticket.id), title="race"
    )
    svc = sessions(session_factory)
    started = await svc.start(EXECUTOR, f"op-{uuid4().hex[:8]}")
    session_id, key = started.session.id, started.session.client_key
    await svc.bind(session_id, key, opened.slot.id)
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            relay(session_factory, session_id, key),
            integrate(session_factory, ticket.id, binding, number),
            return_exceptions=True,
        ),
        timeout=60,  # a deadlock would hang; PostgreSQL would also abort one side
    )
    assert not isinstance(outcomes[1], BaseException)
    closed = await slot(session_factory, opened.slot.id)
    assert closed["closed_at"] is not None
    async with session_factory() as session:
        old_status = await session.scalar(
            sa.select(brain_sessions.c.status).where(brain_sessions.c.id == session_id)
        )
        successors = await session.scalar(
            sa.select(sa.func.count())
            .select_from(brain_sessions)
            .where(brain_sessions.c.relayed_from_session_id == session_id)
        )
    if isinstance(outcomes[0], BaseException):  # the close won: nothing relayed (S8)
        assert getattr(outcomes[0], "code", None) == "slot_closed"
        assert (old_status, successors) == ("open", 0)
        assert await sources(session_factory, opened.slot.id) == ["slot_open", "slot_close"]
    else:  # the relay won, then the receipt closed the relayed slot
        assert (old_status, successors) == ("ended", 1)
        assert await sources(session_factory, opened.slot.id) == [
            "slot_open",
            "session_relay",
            "slot_close",
        ]


async def test_a_relay_waits_for_an_uncommitted_receipt_close_then_refuses(session_factory):
    """The deterministic order the gather above only hits by luck: the receipt holds the slot
    lock, a second connection's relay queues behind it, and refuses once the close commits."""
    ticket, binding, _, number = await workflow(session_factory)
    opened = await open_slot(
        session_factory, SlotAnchor(kind="ticket", ticket_id=ticket.id), title="held"
    )
    svc = sessions(session_factory)
    started = await svc.start(EXECUTOR, f"op-{uuid4().hex[:8]}")
    session_id, key = started.session.id, started.session.client_key
    await svc.bind(session_id, key, opened.slot.id)
    async with session_factory() as session, session.begin():
        await integrate_in(session_factory, session, ticket.id, binding, number)
        queued = asyncio.create_task(relay(session_factory, session_id, key))
        await asyncio.sleep(0.5)
        assert not queued.done()  # blocked on the slot row the receipt has locked
    with pytest.raises(FocusSlotError, match="^slot_closed: "):
        await asyncio.wait_for(queued, timeout=30)
    assert await sources(session_factory, opened.slot.id) == ["slot_open", "slot_close"]


async def test_a_relay_that_wins_first_leaves_the_receipt_to_close_the_relayed_slot(
    session_factory,
):
    ticket, binding, _, number = await workflow(session_factory)
    opened = await open_slot(
        session_factory, SlotAnchor(kind="ticket", ticket_id=ticket.id), title="relay first"
    )
    svc = sessions(session_factory)
    started = await svc.start(EXECUTOR, f"op-{uuid4().hex[:8]}")
    await svc.bind(started.session.id, started.session.client_key, opened.slot.id)
    await relay(session_factory, started.session.id, started.session.client_key)
    await integrate(session_factory, ticket.id, binding, number)
    assert await sources(session_factory, opened.slot.id) == [
        "slot_open",
        "session_relay",
        "slot_close",
    ]
