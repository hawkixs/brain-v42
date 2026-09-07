"""Real database and transport regressions for delivery ticket completion."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest
import sqlalchemy as sa
from fastmcp import Client, FastMCP
from pydantic import SecretStr

from brain_v42.codex_gateway.app import create_app
from brain_v42.codex_gateway.dependencies import GatewayServices
from brain_v42.db.tables import delivery_workflows, tickets
from brain_v42.mcp.tools.ticket_tools import register_ticket_tools
from brain_v42.models.delivery import DeliveryError
from brain_v42.models.delivery_evaluator import evaluate_delivery
from brain_v42.models.ticket import (
    ExtractionStatus,
    TicketAction,
    TicketCreate,
    TicketKind,
    TicketStatus,
)
from brain_v42.repositories.delivery_ticket_guard import DeliveryTransitionMutation
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.ticket_service import TicketService, TicketTransitionConflictError
from tests.unit.codex_gateway._support import GATEWAY_TOKEN

from .test_delivery_receipt_issuance import _contract
from .test_delivery_requester_acceptance import (
    _accept,
    _now,
    _publish_and_issue,
    _service,
    _workflow,
)
from .test_delivery_ticket_guards import _receipt_count, _workflow_row

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("BRAIN_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("BRAIN_DELIVERY_FRESHNESS_SECONDS", "3600")


async def _eligible(factory, ticket_id, action):
    async with factory() as session:
        inputs = await PgDeliveryRepo(factory).load_locked_decision_inputs(
            session, ticket_id, feature_enabled=True, freshness_seconds=3600
        )
        assert inputs.inputs is not None
        assessment = evaluate_delivery(
            inputs.inputs.model_copy(update={"requested_completion_action": action}),
            now=inputs.decision_time,
        )
        assert assessment.completion_eligible_now


async def test_message_failure_rolls_back_observed_ticket_and_workflow_mutations(
    session_factory, monkeypatch
):
    ticket, binding, delivery = await _workflow(
        session_factory, requester="executor", executor="executor"
    )
    receipt = await _publish_and_issue(session_factory, ticket.id, binding)
    await _accept(delivery, ticket.id, receipt, actor_project="executor")
    await _eligible(session_factory, ticket.id, "self_resolve")
    before = await _workflow_row(session_factory, ticket.id)
    reached = []
    original = DeliveryTransitionMutation.apply

    async def probe(self, session, ticket_id):
        await original(self, session, ticket_id)
        actual = (
            await session.execute(
                sa.select(tickets.c.status, delivery_workflows.c.disposition)
                .join(delivery_workflows, tickets.c.id == delivery_workflows.c.ticket_id)
                .where(tickets.c.id == ticket_id)
            )
        ).one()
        reached.append(tuple(actual))

    monkeypatch.setattr(DeliveryTransitionMutation, "apply", probe)
    repo = PgTicketRepo(session_factory)
    with pytest.raises(sa.exc.DBAPIError):
        await repo.apply_transition(
            ticket.id,
            TicketStatus.CLOSED,
            action=TicketAction.RESOLVE,
            actor_project="executor",
            expected_status=TicketStatus.OPEN,
            resolved_at=None,
            closed_at=datetime.now(UTC),
            extraction_status=None,
            message_author="x" * 51,
            message_body="fail after both state mutations",
        )
    assert reached == [("closed", "fulfilled")]
    assert await _workflow_row(session_factory, ticket.id) == before
    assert (await repo.get_by_id(ticket.id)).status is TicketStatus.OPEN
    assert await repo.get_messages(ticket.id) == []
    assert await _receipt_count(session_factory, ticket.id) == 2


async def test_provider_outage_keeps_accepted_history_but_refuses_new_completion(session_factory):
    ticket, binding, delivery = await _workflow(session_factory)
    receipt = await _publish_and_issue(session_factory, ticket.id, binding)
    await _accept(delivery, ticket.id, receipt)
    await _eligible(session_factory, ticket.id, "cross_resolve")
    async with session_factory() as session, session.begin():
        now = await _now(session)
        await PgDeliveryEvidenceRepo(session_factory).record_observation_error(
            session, binding.id, binding.binding_version + 1, "provider_unavailable", now, now
        )
    view = await delivery.get(ticket.id, actor_project="requester")
    assert view.assessment.observation_health == "error"
    assert view.assessment.contract_fulfilled
    assert view.fulfillment_receipt is not None
    with pytest.raises(TicketTransitionConflictError, match="delivery_requirements_unsatisfied"):
        await TicketService(PgTicketRepo(session_factory), MagicMock()).transition(
            ticket.id, "executor", "resolve"
        )
    assert await _receipt_count(session_factory, ticket.id) == 2
    assert (await PgTicketRepo(session_factory).get_by_id(ticket.id)).status is TicketStatus.OPEN


@pytest.mark.parametrize(
    "kind, action, final_status", [("request", "cancel", "closed"), ("fyi", "ack", "acked")]
)
async def test_terminal_legacy_ticket_cannot_acquire_contract(
    session_factory, kind, action, final_status
):
    repo = PgTicketRepo(session_factory)
    ticket = await repo.create(
        TicketCreate(
            kind=TicketKind(kind),
            title="terminal contract",
            body="fixture",
            from_project="executor",
            to_project="executor",
        )
    )
    closed = await TicketService(repo, MagicMock()).transition(ticket.id, "executor", action)
    assert closed.status.value == final_status
    with pytest.raises(DeliveryError, match="ticket_not_contractable"):
        await _service(session_factory).set_contract(
            ticket.id,
            actor_project="executor",
            expected_revision=0,
            idempotency_key=f"terminal-{ticket.id}",
            contract=_contract(),
        )


async def test_cancelled_contracted_ticket_is_terminal_and_preserves_extraction_opt_out(
    session_factory,
):
    ticket, _binding, _delivery = await _workflow(session_factory)
    repo = PgTicketRepo(session_factory)
    await repo.update(ticket.id, {"extraction_status": "skipped"})
    closed = await TicketService(repo, MagicMock()).transition(ticket.id, "requester", "cancel")
    assert closed.status is TicketStatus.CLOSED
    assert closed.extraction_status is ExtractionStatus.SKIPPED
    assert (await _workflow_row(session_factory, ticket.id))["disposition"] == "cancelled"
    with pytest.raises(DeliveryError, match="delivery_transition_invalid"):
        await repo.apply_transition(
            ticket.id,
            TicketStatus.OPEN,
            action=TicketAction.REOPEN,
            actor_project="requester",
            expected_status=TicketStatus.CLOSED,
            resolved_at=None,
            closed_at=None,
            extraction_status=None,
        )
    assert await _receipt_count(session_factory, ticket.id) == 0


async def test_actual_mcp_refuses_completion_with_safe_business_error(session_factory):
    ticket, _binding, _delivery = await _workflow(session_factory)
    repo = PgTicketRepo(session_factory)
    app = FastMCP("delivery-guard-transport", mask_error_details=True)
    register_ticket_tools(app, ticket_svc=TicketService(repo, MagicMock()))
    async with Client(app) as client:
        result = await client.call_tool(
            "brain_ticket_transition",
            {
                "ticket_id": str(ticket.id),
                "author_project": "executor",
                "action": "resolve",
                "message": "must not be recorded",
            },
            raise_on_error=False,
        )
    assert result.is_error
    assert "delivery_requirements_unsatisfied" in str(result.content)
    assert "Traceback" not in str(result.content)
    assert (await repo.get_by_id(ticket.id)).status is TicketStatus.OPEN
    assert await repo.get_messages(ticket.id) == []


@pytest.mark.parametrize("actor, status", [("executor", 409), ("outsider", 422)])
async def test_actual_gateway_refuses_contracted_self_completion(session_factory, actor, status):
    ticket, _binding, _delivery = await _workflow(
        session_factory, requester="executor", executor="executor"
    )
    repo = PgTicketRepo(session_factory)
    unused = MagicMock()
    services = GatewayServices(
        ticket=TicketService(repo, MagicMock()),
        learning=unused,
        entity_maintenance=unused,
        feature=unused,
        proposal=unused,
        killswitch=unused,
    )
    app = create_app(services=services, token=SecretStr(GATEWAY_TOKEN))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://gateway"
    ) as client:
        response = await client.post(
            f"/api/tickets/{ticket.id}/transition",
            json={"actor_project": actor, "action": "resolve", "message": "must not be recorded"},
            headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"},
        )
    assert response.status_code == status, response.text
    assert "delivery_" in response.json()["detail"]
    if status == 409:
        assert "resolve" in response.json()["allowed_actions"]
    assert (await repo.get_by_id(ticket.id)).status is TicketStatus.OPEN
    assert await repo.get_messages(ticket.id) == []
