"""Input and exception redaction at the actual prepared delivery MCP boundary."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP

from brain_v42.mcp.server import prepare_tools_for_transport
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from brain_v42.models.delivery import DeliveryError

MARKER = "delivery-claim-secret-that-must-never-be-logged"


async def _app(service, profile):
    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools

    app = FastMCP("delivery-errors", mask_error_details=True)
    register_delivery_tools(app, delivery_svc=service)
    apply_tool_catalog_profile(app, profile)
    await prepare_tools_for_transport(app, None)
    return app


async def _call(app, profile, name, arguments):
    async with Client(app) as client:
        if profile == "compact":
            return await client.call_tool(
                "brain_call_tool", {"name": name, "arguments": arguments}, raise_on_error=False
            )
        return await client.call_tool(name, arguments, raise_on_error=False)


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize("operation", ["renew", "release"])
async def test_missing_argument_never_echoes_the_claim_token(profile, operation, caplog):
    service = AsyncMock()
    app = await _app(service, profile)
    result = await _call(
        app,
        profile,
        f"brain_delivery_claim_{operation}",
        {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "owner_key": "worker",
            "claim_token": MARKER,
            # The validation error's input would contain this entire payload.
        },
    )
    assert result.is_error and "invalid_arguments" in str(result.content)
    assert MARKER not in str(result.content)
    assert MARKER not in caplog.text
    service.renew_claim.assert_not_called()
    service.release_claim.assert_not_called()


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_unexpected_service_exception_never_reaches_framework_traceback(profile, caplog):
    service = AsyncMock()
    service.renew_claim.side_effect = RuntimeError(MARKER)
    app = await _app(service, profile)
    result = await _call(
        app,
        profile,
        "brain_delivery_claim_renew",
        {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "owner_key": "worker",
            "claim_token": MARKER,
            "epoch": 1,
        },
    )
    service.renew_claim.assert_awaited_once()
    assert result.is_error and "delivery_unavailable" in str(result.content)
    assert MARKER not in str(result.content)
    assert MARKER not in caplog.text
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_domain_refusal_keeps_safe_stable_code(profile, caplog):
    service = AsyncMock()
    service.release_claim.side_effect = DeliveryError(
        "claim_fenced", "delivery claim is no longer current"
    )
    app = await _app(service, profile)
    result = await _call(
        app,
        profile,
        "brain_delivery_claim_release",
        {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "owner_key": "worker",
            "claim_token": MARKER,
            "epoch": 1,
        },
    )
    assert result.is_error and "claim_fenced" in str(result.content)
    assert MARKER not in str(result.content) + caplog.text


@pytest.mark.parametrize("ticket_id", ["abcd1234", "a" * 32, "not-a-uuid"])
async def test_full_uuid_is_required_before_a_mutation(ticket_id):
    service = AsyncMock()
    app = await _app(service, "native")
    result = await _call(
        app, "native", "brain_delivery_refresh", {"ticket_id": ticket_id, "actor_project": "p"}
    )
    assert result.is_error and "invalid_arguments" in str(result.content)
    service.refresh.assert_not_called()


@pytest.mark.parametrize("epoch", [True, 1.5, "1"])
async def test_claim_epoch_rejects_coercion(epoch):
    service = AsyncMock()
    app = await _app(service, "native")
    result = await _call(
        app,
        "native",
        "brain_delivery_claim_release",
        {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "owner_key": "worker",
            "claim_token": MARKER,
            "epoch": epoch,
        },
    )
    assert result.is_error and "invalid_arguments" in str(result.content)
    service.release_claim.assert_not_called()


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_sidecar_failure_never_logs_claim_input_or_exception_text(
    profile, monkeypatch, caplog, capsys
):
    from types import SimpleNamespace

    from brain_v42.mcp import provenance_middleware
    from brain_v42.mcp.server import create_mcp_instance
    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools

    def fail_report(*_args):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(
        provenance_middleware,
        "get_http_headers",
        lambda **_kwargs: {"x-brain-agent": "delivery-boundary-test"},
    )
    monkeypatch.setattr(
        provenance_middleware, "get_activity_reporter", lambda: SimpleNamespace(report=fail_report)
    )
    app = create_mcp_instance()
    register_delivery_tools(app, delivery_svc=AsyncMock())
    apply_tool_catalog_profile(app, profile)
    await prepare_tools_for_transport(app, None)
    result = await _call(
        app,
        profile,
        "brain_delivery_claim_renew",
        {
            "ticket_id": str(uuid4()),
            "actor_project": "executor",
            "owner_key": "worker",
            "claim_token": MARKER,
        },
    )
    captured = capsys.readouterr()
    assert result.is_error and "invalid_arguments" in str(result.content)
    assert MARKER not in str(result.content) + caplog.text + captured.out + captured.err
