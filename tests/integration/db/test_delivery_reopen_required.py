"""Real PostgreSQL regression for reopening an amended resolved delivery."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brain_v42.models.delivery import DeliveryError
from brain_v42.models.ticket import TicketAction, TicketStatus
from brain_v42.repositories.pg_ticket import PgTicketRepo

from .test_delivery_requester_acceptance import _contract, _publish_and_issue, _workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _guard_settings(monkeypatch) -> None:
    """Exercise the real repository guard with delivery explicitly enabled."""
    monkeypatch.setenv("BRAIN_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("BRAIN_DELIVERY_FRESHNESS_SECONDS", "3600")


async def test_resolved_amendment_requires_reopen_before_new_implementation_claim(
    session_factory,
) -> None:
    """A requester must reopen a resolved ticket before its amended work can restart."""
    ticket, binding, delivery = await _workflow(session_factory)
    await _publish_and_issue(session_factory, ticket.id, binding)
    repo = PgTicketRepo(session_factory)
    resolved = await repo.apply_transition(
        ticket.id,
        TicketStatus.RESOLVED,
        action=TicketAction.RESOLVE,
        actor_project="executor",
        expected_status=TicketStatus.OPEN,
        resolved_at=datetime.now(UTC),
        closed_at=None,
        extraction_status=None,
    )
    assert resolved is not None and resolved.status is TicketStatus.RESOLVED

    await delivery.set_contract(
        ticket.id,
        actor_project="requester",
        contract=_contract(mode="explicit"),
        expected_revision=1,
        idempotency_key=f"amend-reopen-required-{ticket.id}",
    )
    blocked = await delivery.get(ticket.id, actor_project="requester")
    assert "reopen_required" in {finding.code for finding in blocked.assessment.blockers}
    assert blocked.assessment.eligible_work == ()

    with pytest.raises(DeliveryError, match="claim_not_eligible"):
        await delivery.claim(
            ticket.id,
            actor_project="executor",
            owner_key="amended-implementation-owner",
            work_kind="implement",
            expected_workflow_version=blocked.assessment.assessment_version,
            expected_assessment_id=blocked.assessment.assessment_id,
        )

    reopened = await repo.apply_transition(
        ticket.id,
        TicketStatus.OPEN,
        action=TicketAction.REOPEN,
        actor_project="requester",
        expected_status=TicketStatus.RESOLVED,
        resolved_at=None,
        closed_at=None,
        extraction_status=None,
    )
    assert reopened is not None and reopened.status is TicketStatus.OPEN
    active = await delivery.get(ticket.id, actor_project="requester")
    assert {work.kind for work in active.assessment.eligible_work} == {"implement"}
