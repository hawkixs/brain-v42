"""The integration HTTP harness must return the SDK transport to later tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import FastMCP
from mcp.server import streamable_http_manager

from brain_v42.config import Settings
from brain_v42.mcp import server
from tests.integration.mcp import test_session_capture_e2e as harness


@pytest.mark.asyncio
async def test_capture_harness_restores_the_termination_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the real fixture's lifecycle without a database or listening socket."""
    original = streamable_http_manager.StreamableHTTPServerTransport
    # Preserve isolation even when the teardown assertion below fails.
    monkeypatch.setattr(streamable_http_manager, "StreamableHTTPServerTransport", original)
    built = SimpleNamespace(
        mcp=FastMCP("capture-harness-isolation"),
        settings=Settings(
            postgres_url="postgresql+asyncpg://test:test@127.0.0.1:1/brain_test",
            brain_mcp_transport="http",
            mcp_http_token="test-token",
            mcp_http_stateless=False,
        ),
        metrics_collector=None,
        services={},
    )
    monkeypatch.setattr(server, "build_server", lambda: built)
    monkeypatch.setattr(server, "prepare_tools_for_transport", AsyncMock())
    monkeypatch.setattr(harness, "_free_port", lambda: 12345)
    monkeypatch.setattr(harness, "_stop_or_fail", AsyncMock())
    fake_server = Mock(started=True, serve=AsyncMock())
    monkeypatch.setattr(harness.uvicorn, "Server", lambda config: fake_server)

    fixture = harness.mcp_base_url.__wrapped__(None)
    try:
        await anext(fixture)
        assert streamable_http_manager.StreamableHTTPServerTransport is not original
    finally:
        await fixture.aclose()

    assert streamable_http_manager.StreamableHTTPServerTransport is original
