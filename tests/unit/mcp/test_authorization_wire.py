"""The hook's principal grants elevations without replacing the MCP actor."""

from hashlib import sha256
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from structlog.testing import capture_logs

from brain_v42.provenance import (
    get_current_actor,
    get_current_principal,
    set_current_actor,
    set_current_principal,
)
from tests.unit.mcp.test_admin_elevations import SESSION_ID, TOKEN, counters, route  # noqa: F401


async def test_hook_request_does_not_reinterpret_actor_or_attach_to_mcp_session(route: Any) -> None:  # noqa: F811
    actor, principal = get_current_actor(), get_current_principal()
    set_current_actor("operator-actor")
    set_current_principal("workstation-claude")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(route[0]), base_url="http://localhost"
        ) as client:
            response = await client.post(
                "/admin/elevations",
                headers={
                    "Authorization": "Bearer " + TOKEN,
                    "X-Brain-Agent": "untrusted-declaration",
                    "Mcp-Session-Id": "unrelated-connection",
                },
                json={"session_id": str(SESSION_ID), "reason": "maintenance"},
            )
        assert response.status_code == 200
        assert route[1].calls[0][1]["requested_by_client_id"] == "operator-hook"
        assert get_current_actor() == "operator-actor"
        assert get_current_principal() == "workstation-claude"
        assert route[2].verify.await_count == 1
    finally:
        set_current_actor(actor)
        set_current_principal(principal)


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize("operation", ["attest", "accept"])
@pytest.mark.parametrize("reason", ["family_denied", "issuer_not_allowed"])
async def test_tool_authorization_refusal_crosses_production_http_once(
    monkeypatch: pytest.MonkeyPatch, profile: str, operation: str, reason: str
) -> None:
    from fastmcp.server import http as fastmcp_http
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from brain_v42.credentials.reasons import refusal_counts, reset_refusal_counts
    from brain_v42.credentials.verifier import VerifiedPrincipal
    from brain_v42.mcp import server
    from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools
    from tests.unit.mcp.test_credentials_http_wiring import settings
    from tests.unit.mcp.test_delivery_tools import _issuer_arguments

    # The production plan substitutes this class; each parametrized app starts fresh.
    monkeypatch.setattr(fastmcp_http, "StreamableHTTPSessionManager", StreamableHTTPSessionManager)
    token = "test-wire-credential-placeholder"
    token_digest = sha256(token.encode()).hexdigest()
    registry = MagicMock(
        verify=AsyncMock(
            return_value=VerifiedPrincipal(
                "client-service",
                frozenset({"read" if reason == "family_denied" else "delivery"}),
                frozenset({"release-bot"}),
                uuid4(),
                None,
            )
        )
    )
    service = AsyncMock()
    mcp = server.create_mcp_instance()
    register_delivery_tools(mcp, service)
    apply_tool_catalog_profile(mcp, profile)
    await server.prepare_tools_for_transport(mcp, None)
    plan = server.plan_http_transport(mcp, settings(), credential_verifier=registry)
    app = mcp.http_app(
        middleware=plan.middleware, stateless_http=plan.stateless_http, json_response=True
    )
    headers = {
        "Authorization": "Bearer " + token,
        "X-Brain-Agent": "client-service",
        "Accept": "application/json, text/event-stream",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client,
    ):
        initialized = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        headers["Mcp-Session-Id"] = initialized.headers["Mcp-Session-Id"]
        notified = await client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notified.status_code == 202
        name = f"brain_delivery_{operation}"
        arguments = _issuer_arguments(operation, "unauthorized-bot")
        params = (
            {"name": name, "arguments": arguments}
            if profile == "native"
            else {
                "name": "brain_call_tool",
                "arguments": {"name": name, "arguments": arguments},
            }
        )
        reset_refusal_counts()
        with capture_logs() as logs:
            response = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["jsonrpc"] == "2.0" and body["id"] == 2
        assert body["result"]["isError"] is True
        assert body["result"]["content"] == [{"type": "text", "text": reason}]
        getattr(service, operation).assert_not_called()
        events = [event for event in logs if event["event"] == "mcp_auth.refused"]
        assert len(events) == 1
        assert (
            events[0].items()
            >= {
                "status": 403,
                "reason": reason,
                "client_id": "client-service",
                "declared_agent": "client-service",
                "tool": name,
            }.items()
        )
        assert refusal_counts() == {reason: 1}
        from brain_v42.metrics.flusher import MetricsFlusher

        assert MetricsFlusher._process_pseudo_tools({})["_mcp_auth_refused"] == {reason: 1}
        assert token not in repr(logs) + response.text
        assert token_digest not in repr(logs) + response.text
        assert not any("token" in key or "digest" in key for key in events[0])
        reset_refusal_counts()
