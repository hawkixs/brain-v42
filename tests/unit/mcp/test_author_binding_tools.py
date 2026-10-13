"""Each write observes its declared author before calling the unchanged service."""

import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import SecretStr

from brain_v42.mcp.tools import delivery_tools, ticket_tools
from brain_v42.mcp.tools.formatters import format_confirmation, format_id
from brain_v42.models.delivery import DeliveryError, DeliveryRefreshResult
from brain_v42.models.ticket import Ticket, TicketGroups, TicketMessage, TicketStatus
from tests.unit.models.test_delivery_summary import summary_view

TICKET_ID = str(uuid4())


@pytest.mark.parametrize("operation", ["create", "reply", "transition", "plan"])
async def test_ticket_write_observes_author_and_preserves_result(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer = MagicMock()
    monkeypatch.setattr(ticket_tools, "observe_declared_project", observer, raising=False)
    ticket = Ticket(
        from_project="declared",
        to_project="recipient",
        kind="request",
        title="Title",
        body="Body",
        status=TicketStatus.OPEN,
    )
    message = TicketMessage(ticket_id=ticket.id, author_project="declared", body="planned")
    service = MagicMock()
    returned = {"create": ticket, "reply": message, "transition": ticket, "plan": message}[
        operation
    ]

    async def result(*args: Any, **kwargs: Any) -> Any:
        observer.assert_called_once_with(f"brain_ticket_{operation}", "declared")
        return returned

    method = AsyncMock(side_effect=result)
    setattr(service, operation, method)
    app = FastMCP("author-binding")
    ticket_tools.register_ticket_tools(app, service)
    tool = await app.get_tool(f"brain_ticket_{operation}")
    arguments: dict[str, Any] = {"ticket_id": TICKET_ID, "author_project": "declared"}
    expected = format_confirmation("Reply posted", "Body", id=format_id(TICKET_ID))
    if operation == "create":
        arguments = {
            "from_project": "declared",
            "to_project": "recipient",
            "kind": "request",
            "title": "Title",
            "body": "Body",
        }
        expected = format_confirmation(
            "Ticket created", "Title", id=str(ticket.id), kind="request", to="recipient"
        )
    elif operation == "reply":
        arguments["body"] = "Body"
    elif operation == "transition":
        arguments["action"] = "start"
        expected = format_confirmation(
            "Ticket updated", "Title", id=format_id(TICKET_ID), status=ticket.status.value
        )
    else:
        expected = format_confirmation(
            "Ticket planned: planned", "planned", id=format_id(TICKET_ID)
        )
    assert tool is not None
    assert await tool.fn(**arguments) == expected
    method.assert_awaited_once()


DELIVERY_WRITES = [
    (
        "contract_set",
        "set_contract",
        {"contract": {}, "expected_revision": 0, "idempotency_key": "key", "reason": "Reason"},
    ),
    (
        "bind_pr",
        "bind_pr",
        {
            "deliverable_key": "pr",
            "repository_id": 1,
            "pr_number": 2,
            "expected_revision": 1,
            "expected_workflow_version": 1,
            "idempotency_key": "key",
        },
    ),
    ("refresh", "refresh", {}),
    (
        "claim",
        "claim",
        {
            "owner_key": "owner",
            "work_kind": "implement",
            "expected_workflow_version": 1,
            "expected_assessment_id": "a" * 64,
        },
    ),
    (
        "claim_renew",
        "renew_claim",
        {"owner_key": "owner", "claim_token": SecretStr("token"), "epoch": 1},
    ),
    (
        "claim_release",
        "release_claim",
        {"owner_key": "owner", "claim_token": SecretStr("token"), "epoch": 1},
    ),
    (
        "accept",
        "accept",
        {
            "rationale": "Reason",
            "expected_revision": 1,
            "expected_attempt": 1,
            "expected_delivery_digest": "a" * 64,
        },
    ),
    (
        "attest",
        "attest",
        {
            "kind": "review",
            "payload": {},
            "idempotency_key": "key",
            "emitted_at": "2026-10-13T00:00:00Z",
        },
    ),
]


@pytest.mark.parametrize(("operation", "method", "arguments"), DELIVERY_WRITES)
@pytest.mark.parametrize("fails", [False, True])
async def test_delivery_write_observes_author_and_preserves_outcome(
    operation: str,
    method: str,
    arguments: dict[str, Any],
    fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = MagicMock()
    monkeypatch.setattr(delivery_tools, "observe_declared_project", observer, raising=False)
    monkeypatch.setattr(delivery_tools, "get_current_actor", lambda: "issuer")
    monkeypatch.setattr(delivery_tools, "_checked_issuer", lambda *args, **kwargs: "issuer")
    returned = summary_view() if operation == "refresh" else SimpleNamespace(result="unchanged")
    error = DeliveryError("revision_conflict", "unchanged error")

    async def result(*args: Any, **kwargs: Any) -> Any:
        observer.assert_called_once_with(f"brain_delivery_{operation}", "declared")
        assert kwargs["actor_project"] == "declared"
        if fails:
            raise error
        return returned

    service = AsyncMock()
    getattr(service, method).side_effect = result
    app = FastMCP("author-binding")
    delivery_tools.register_delivery_tools(app, service)
    tool = await app.get_tool(f"brain_delivery_{operation}")
    assert tool is not None
    if fails:
        with pytest.raises(DeliveryError) as caught:
            await tool.fn(ticket_id=TICKET_ID, actor_project="declared", **arguments)
        assert caught.value is error
    else:
        actual = await tool.fn(ticket_id=TICKET_ID, actor_project="declared", **arguments)
        if operation == "refresh":
            assert actual == DeliveryRefreshResult(view=returned)
        else:
            assert actual is returned


@pytest.mark.parametrize("operation", ["get", "list", "attestation_list"])
async def test_delivery_reads_do_not_observe(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer = MagicMock()
    monkeypatch.setattr(delivery_tools, "observe_declared_project", observer, raising=False)
    service = AsyncMock()
    service.list.return_value = SimpleNamespace(items=(), next_cursor=None, omitted_count=0)
    app = FastMCP("author-binding")
    delivery_tools.register_delivery_tools(app, service)
    tool = await app.get_tool(f"brain_delivery_{operation}")
    assert tool is not None
    args = {"actor_project": "declared"}
    if operation == "get":
        args["ticket_id"] = TICKET_ID
    await tool.fn(**args)
    observer.assert_not_called()


@pytest.mark.parametrize("operation", ["get", "list"])
async def test_ticket_reads_do_not_observe(operation: str, monkeypatch: pytest.MonkeyPatch) -> None:
    observer = MagicMock()
    monkeypatch.setattr(ticket_tools, "observe_declared_project", observer, raising=False)
    service = MagicMock()
    service.get_with_thread = AsyncMock(return_value=None)
    service.list_grouped = AsyncMock(
        return_value=TicketGroups(a_traiter=[], a_confirmer=[], en_attente=[])
    )
    app = FastMCP("author-binding")
    ticket_tools.register_ticket_tools(app, service)
    tool = await app.get_tool(f"brain_ticket_{operation}")
    assert tool is not None
    # A missing ticket raises ToolError: the read still must not observe anything.
    with contextlib.suppress(ToolError):
        await tool.fn(
            **({"ticket_id": TICKET_ID} if operation == "get" else {"project_key": "declared"})
        )
    observer.assert_not_called()
