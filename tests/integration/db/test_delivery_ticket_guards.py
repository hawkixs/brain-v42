"""Real PostgreSQL RED contracts for canonical delivery ticket guards."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import delivery_artifact_bindings, delivery_receipts, delivery_workflows
from brain_v42.models.delivery import DeliveryError
from brain_v42.models.ticket import TicketAction, TicketCreate, TicketKind, TicketStatus
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.ticket_service import TicketService

from .test_delivery_requester_acceptance import (
    _accept,
    _publish_and_issue,
    _workflow,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _guard_settings(monkeypatch) -> None:
    """Keep direct-repository positive guard paths independent of host delivery settings."""
    monkeypatch.setenv("BRAIN_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("BRAIN_DELIVERY_FRESHNESS_SECONDS", "3600")


async def _status(factory, ticket_id):
    return await PgTicketRepo(factory).get_by_id(ticket_id)


async def _workflow_row(factory, ticket_id):
    async with factory() as session:
        return dict(
            (
                await session.execute(
                    sa.select(
                        delivery_workflows.c.disposition,
                        delivery_workflows.c.attempt,
                        delivery_workflows.c.claim_owner,
                        delivery_workflows.c.claim_kind,
                        delivery_workflows.c.claim_digest,
                        delivery_workflows.c.claim_expires_at,
                        delivery_workflows.c.claim_epoch,
                    ).where(delivery_workflows.c.ticket_id == ticket_id)
                )
            )
            .mappings()
            .one()
        )


async def _receipt_count(factory, ticket_id) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(delivery_receipts)
                .where(delivery_receipts.c.ticket_id == ticket_id)
            )
            or 0
        )


async def _active_binding_count(factory, ticket_id) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(delivery_artifact_bindings)
                .where(
                    delivery_artifact_bindings.c.ticket_id == ticket_id,
                    delivery_artifact_bindings.c.active.is_(True),
                )
            )
            or 0
        )


async def _cross_receipts(factory, *, fulfilled: bool):
    ticket, binding, delivery = await _workflow(factory)
    integration = await _publish_and_issue(factory, ticket.id, binding)
    if fulfilled:
        fulfillment = await _accept(delivery, ticket.id, integration)
        assert fulfillment is not None and fulfillment.milestone == "fulfilled"
    return ticket, delivery


async def test_direct_repository_cross_resolve_requires_real_current_integration_receipt(
    session_factory,
) -> None:
    """Direct repository completion must not bypass the contracted delivery proof."""
    ticket, _binding, _delivery = await _workflow(session_factory)
    repo = PgTicketRepo(session_factory)

    with pytest.raises(DeliveryError, match="delivery_requirements_unsatisfied"):
        await repo.apply_transition(
            ticket.id,
            TicketStatus.RESOLVED,
            action=TicketAction.RESOLVE,
            actor_project="executor",
            expected_status=TicketStatus.OPEN,
            resolved_at=datetime.now(UTC),
            closed_at=None,
            extraction_status=None,
        )

    actual = await _status(session_factory, ticket.id)
    assert actual is not None and actual.status is TicketStatus.OPEN
    assert (await _workflow_row(session_factory, ticket.id))["disposition"] == "active"


async def test_direct_repository_cross_resolve_accepts_an_actual_fresh_integration_receipt(
    session_factory,
) -> None:
    """A freshly issued integration receipt is the positive cross-resolve prerequisite."""
    ticket, _delivery = await _cross_receipts(session_factory, fulfilled=False)
    assert await _receipt_count(session_factory, ticket.id) == 1

    updated = await PgTicketRepo(session_factory).apply_transition(
        ticket.id,
        TicketStatus.RESOLVED,
        action=TicketAction.RESOLVE,
        actor_project="executor",
        expected_status=TicketStatus.OPEN,
        resolved_at=datetime.now(UTC),
        closed_at=None,
        extraction_status=None,
    )

    assert updated is not None and updated.status is TicketStatus.RESOLVED


async def test_direct_repository_cross_confirm_requires_current_fulfillment_and_marks_success(
    session_factory,
) -> None:
    """A close following cross confirmation records success in the same workflow generation."""
    ticket, _delivery = await _cross_receipts(session_factory, fulfilled=True)
    assert await _receipt_count(session_factory, ticket.id) == 2
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
    assert resolved is not None

    closed = await repo.apply_transition(
        ticket.id,
        TicketStatus.CLOSED,
        action=TicketAction.CONFIRM,
        actor_project="requester",
        expected_status=TicketStatus.RESOLVED,
        resolved_at=resolved.resolved_at,
        closed_at=datetime.now(UTC),
        extraction_status=None,
    )

    assert closed is not None and closed.status is TicketStatus.CLOSED
    assert (await _workflow_row(session_factory, ticket.id))["disposition"] == "fulfilled"


async def test_contracted_transition_refuses_a_wrong_actor_after_real_receipt_preconditions(
    session_factory,
) -> None:
    """The guard uses the actor locked with the ticket, rather than the caller's intended role."""
    ticket, _delivery = await _cross_receipts(session_factory, fulfilled=False)
    assert await _receipt_count(session_factory, ticket.id) == 1

    with pytest.raises(DeliveryError, match="delivery_not_allowed"):
        await PgTicketRepo(session_factory).apply_transition(
            ticket.id,
            TicketStatus.RESOLVED,
            action=TicketAction.RESOLVE,
            actor_project="requester",
            expected_status=TicketStatus.OPEN,
            resolved_at=datetime.now(UTC),
            closed_at=None,
            extraction_status=None,
        )

    actual = await _status(session_factory, ticket.id)
    assert actual is not None and actual.status is TicketStatus.OPEN


async def test_contracted_self_resolve_requires_the_single_locked_participant(
    session_factory,
) -> None:
    """Self tickets do not relax the guard to arbitrary actors."""
    ticket, binding, delivery = await _workflow(
        session_factory, requester="executor", executor="executor"
    )
    integration = await _publish_and_issue(session_factory, ticket.id, binding)
    receipt = await _accept(
        delivery,
        ticket.id,
        integration,
        actor_project="executor",
        caller_identity="self-client",
    )
    assert receipt is not None and receipt.milestone == "fulfilled"

    with pytest.raises(DeliveryError, match="delivery_not_allowed"):
        await PgTicketRepo(session_factory).apply_transition(
            ticket.id,
            TicketStatus.CLOSED,
            action=TicketAction.RESOLVE,
            actor_project="outsider",
            expected_status=TicketStatus.OPEN,
            resolved_at=None,
            closed_at=datetime.now(UTC),
            extraction_status=None,
        )

    actual = await _status(session_factory, ticket.id)
    assert actual is not None and actual.status is TicketStatus.OPEN


@pytest.mark.parametrize(
    ("action", "new_status"),
    ((TicketAction.START, TicketStatus.CLOSED), (TicketAction.CANCEL, TicketStatus.RESOLVED)),
)
async def test_guard_refuses_inconsistent_action_and_result_without_side_effects(
    session_factory, action, new_status
) -> None:
    """The status passed to the repository is never authority over canonical action rules."""
    ticket, _delivery = await _cross_receipts(session_factory, fulfilled=True)
    before = await _workflow_row(session_factory, ticket.id)
    receipts = await _receipt_count(session_factory, ticket.id)

    with pytest.raises(DeliveryError, match="delivery_transition_invalid"):
        await PgTicketRepo(session_factory).apply_transition(
            ticket.id,
            new_status,
            action=action,
            actor_project="requester",
            expected_status=TicketStatus.OPEN,
            resolved_at=None,
            closed_at=datetime.now(UTC),
            extraction_status=None,
        )

    actual = await _status(session_factory, ticket.id)
    assert actual is not None and actual.status is TicketStatus.OPEN
    assert await _workflow_row(session_factory, ticket.id) == before
    assert await _receipt_count(session_factory, ticket.id) == receipts


@pytest.mark.parametrize(
    ("payload", "label"),
    (
        ({"status": TicketStatus.CLOSED.value}, "status"),
        ({"kind": TicketKind.FYI.value}, "kind"),
        ({"from_project": "outsider"}, "from_project"),
        ({"to_project": "outsider"}, "to_project"),
    ),
)
async def test_direct_generic_lifecycle_or_identity_update_cannot_bypass_a_contracted_ticket_guard(
    session_factory, payload, label
) -> None:
    """The inherited generic update must not be a second completion path."""
    ticket, _binding, _delivery = await _workflow(session_factory)
    repo = PgTicketRepo(session_factory)

    with pytest.raises(DeliveryError, match="delivery_transition_required"):
        await repo.update(ticket.id, payload)

    actual = await _status(session_factory, ticket.id)
    assert actual is not None and actual.status is TicketStatus.OPEN
    assert actual.kind is TicketKind.REQUEST, label
    assert actual.from_project == "requester", label
    assert actual.to_project == "executor", label


async def test_contracted_transition_requires_both_action_and_actor(session_factory) -> None:
    """A contracted row fails closed even when a caller supplies the old CAS arguments."""
    ticket, _binding, _delivery = await _workflow(session_factory)
    with pytest.raises(DeliveryError, match="delivery_transition_required"):
        await PgTicketRepo(session_factory).apply_transition(
            ticket.id,
            TicketStatus.RESOLVED,
            expected_status=TicketStatus.OPEN,
            resolved_at=datetime.now(UTC),
            closed_at=None,
            extraction_status=None,
        )
    actual = await _status(session_factory, ticket.id)
    assert actual is not None and actual.status is TicketStatus.OPEN


async def test_disabled_delivery_feature_still_refuses_contracted_success(
    session_factory, monkeypatch
) -> None:
    """Pausing observation must not turn a historical receipt into a completion bypass."""
    ticket, _delivery = await _cross_receipts(session_factory, fulfilled=False)
    assert await _receipt_count(session_factory, ticket.id) == 1
    monkeypatch.setenv("BRAIN_DELIVERY_ENABLED", "false")

    with pytest.raises(DeliveryError, match="delivery_requirements_unsatisfied|delivery_disabled"):
        await PgTicketRepo(session_factory).apply_transition(
            ticket.id,
            TicketStatus.RESOLVED,
            action=TicketAction.RESOLVE,
            actor_project="executor",
            expected_status=TicketStatus.OPEN,
            resolved_at=datetime.now(UTC),
            closed_at=None,
            extraction_status=None,
        )

    actual = await _status(session_factory, ticket.id)
    assert actual is not None and actual.status is TicketStatus.OPEN


async def test_contracted_generic_body_update_remains_compatible(session_factory) -> None:
    """The bypass closure is limited to lifecycle and identity columns."""
    ticket, _binding, _delivery = await _workflow(session_factory)

    updated = await PgTicketRepo(session_factory).update(ticket.id, {"body": "ordinary update"})

    assert updated is not None and updated["body"] == "ordinary update"
    actual = await _status(session_factory, ticket.id)
    assert actual is not None and actual.status is TicketStatus.OPEN


async def test_contracted_generic_extraction_update_remains_compatible(session_factory) -> None:
    """Extraction bookkeeping is not a lifecycle bypass."""
    ticket, _binding, _delivery = await _workflow(session_factory)
    updated = await PgTicketRepo(session_factory).update(
        ticket.id, {"extraction_status": "skipped"}
    )
    assert updated is not None and updated["extraction_status"] == "skipped"


async def test_wontfix_confirm_closes_unsuccessfully_without_manufacturing_receipts(
    session_factory,
) -> None:
    """Confirming an executor WONTFIX acknowledges the disposition, never delivery success."""
    ticket, _binding, _delivery = await _workflow(session_factory)
    service = TicketService(PgTicketRepo(session_factory), project_context_repo=MagicMock())
    wontfix = await service.transition(ticket.id, "executor", "wontfix")
    assert wontfix.status is TicketStatus.WONTFIX
    assert await _receipt_count(session_factory, ticket.id) == 0

    closed = await PgTicketRepo(session_factory).apply_transition(
        ticket.id,
        TicketStatus.CLOSED,
        action=TicketAction.CONFIRM,
        actor_project="requester",
        expected_status=TicketStatus.WONTFIX,
        resolved_at=wontfix.resolved_at,
        closed_at=datetime.now(UTC),
        extraction_status=None,
    )

    assert closed is not None and closed.status is TicketStatus.CLOSED
    assert await _receipt_count(session_factory, ticket.id) == 0
    assert (await _workflow_row(session_factory, ticket.id))["disposition"] == "wontfix"


async def test_reopen_advances_generation_and_clears_claim_without_erasing_receipt_history(
    session_factory,
) -> None:
    """Reopen makes a new generation after a valid delivery while retaining its historical proof."""
    ticket, delivery = await _cross_receipts(session_factory, fulfilled=False)
    view = await delivery.get(ticket.id, actor_project="requester")
    assert {item.kind for item in view.assessment.eligible_work} == {"accept"}
    claim = await delivery.claim(
        ticket.id,
        actor_project="requester",
        owner_key="reopen-owner",
        work_kind="accept",
        expected_workflow_version=view.assessment.assessment_version,
        expected_assessment_id=view.assessment.assessment_id,
    )
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
    assert resolved is not None
    before = await _workflow_row(session_factory, ticket.id)
    receipt_count = await _receipt_count(session_factory, ticket.id)
    assert before["claim_owner"] == "reopen-owner"
    assert before["claim_epoch"] == claim.epoch
    assert await _active_binding_count(session_factory, ticket.id) == 1

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
    after = await _workflow_row(session_factory, ticket.id)
    assert after["disposition"] == "active"
    assert after["attempt"] == before["attempt"] + 1
    assert after["claim_epoch"] == before["claim_epoch"] + 1
    assert after["claim_owner"] is None
    assert after["claim_kind"] is None
    assert after["claim_digest"] is None
    assert after["claim_expires_at"] is None
    assert await _receipt_count(session_factory, ticket.id) == receipt_count
    assert await _active_binding_count(session_factory, ticket.id) == 0
    with pytest.raises(DeliveryError, match="claim_fenced"):
        await delivery.renew_claim(
            ticket.id,
            actor_project="requester",
            owner_key="reopen-owner",
            claim_token=claim.claim_token,
            epoch=claim.epoch,
        )
    with pytest.raises(DeliveryError, match="claim_fenced"):
        await delivery.release_claim(
            ticket.id,
            actor_project="requester",
            owner_key="reopen-owner",
            claim_token=claim.claim_token,
            epoch=claim.epoch,
        )


async def test_legacy_direct_repository_transition_keeps_existing_missing_action_compatibility(
    session_factory,
) -> None:
    """Uncontracted callers retain the repository CAS API without delivery guard arguments."""
    ticket = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title="legacy transition remains compatible",
            body="no workflow row",
            from_project="requester",
            to_project="executor",
        )
    )

    updated = await PgTicketRepo(session_factory).apply_transition(
        ticket.id,
        TicketStatus.RESOLVED,
        expected_status=TicketStatus.OPEN,
        resolved_at=datetime.now(UTC),
        closed_at=None,
        extraction_status=None,
    )
    assert updated is not None and updated.status is TicketStatus.RESOLVED


async def test_missing_legacy_ticket_preserves_compare_and_swap_none_semantics(
    session_factory,
) -> None:
    """The graph-first guard must not convert a missing legacy row into a delivery error."""
    updated = await PgTicketRepo(session_factory).apply_transition(
        uuid4(),
        TicketStatus.RESOLVED,
        expected_status=TicketStatus.OPEN,
        resolved_at=datetime.now(UTC),
        closed_at=None,
        extraction_status=None,
    )
    assert updated is None


async def test_stale_expected_status_does_not_mutate_contracted_workflow(session_factory) -> None:
    """A stale CAS must not apply any delivery disposition mutation."""
    ticket, _delivery = await _cross_receipts(session_factory, fulfilled=False)
    before = await _workflow_row(session_factory, ticket.id)
    with pytest.raises(DeliveryError, match="delivery_transition_conflict"):
        await PgTicketRepo(session_factory).apply_transition(
            ticket.id,
            TicketStatus.RESOLVED,
            action=TicketAction.RESOLVE,
            actor_project="executor",
            expected_status=TicketStatus.IN_PROGRESS,
            resolved_at=datetime.now(UTC),
            closed_at=None,
            extraction_status=None,
        )
    assert await _workflow_row(session_factory, ticket.id) == before


@pytest.mark.parametrize(
    ("action", "status", "needs_fulfillment"),
    (
        (TicketAction.RESOLVE_PENDING, TicketStatus.RESOLVED, False),
        (TicketAction.RESOLVE, TicketStatus.CLOSED, True),
        (TicketAction.CONFIRM, TicketStatus.CLOSED, True),
    ),
)
async def test_self_completion_actions_accept_their_real_receipt_prerequisites(
    session_factory, action, status, needs_fulfillment
) -> None:
    """Self completion derives its receipt requirement from the canonical action."""
    ticket, binding, delivery = await _workflow(
        session_factory, requester="executor", executor="executor"
    )
    integration = await _publish_and_issue(session_factory, ticket.id, binding)
    if needs_fulfillment:
        accepted = await _accept(
            delivery,
            ticket.id,
            integration,
            actor_project="executor",
            caller_identity="self-guard-test",
        )
        assert accepted is not None and accepted.milestone == "fulfilled"
    expected = TicketStatus.OPEN
    if action is TicketAction.CONFIRM:
        pending = await TicketService(PgTicketRepo(session_factory), MagicMock()).transition(
            ticket.id, "executor", "resolve_pending"
        )
        assert pending.status is TicketStatus.RESOLVED
        expected = TicketStatus.RESOLVED
    updated = await PgTicketRepo(session_factory).apply_transition(
        ticket.id,
        status,
        action=action,
        actor_project="executor",
        expected_status=expected,
        resolved_at=datetime.now(UTC) if status is TicketStatus.RESOLVED else None,
        closed_at=datetime.now(UTC) if status is TicketStatus.CLOSED else None,
        extraction_status=None,
    )
    assert updated is not None and updated.status is status
