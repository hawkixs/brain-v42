"""Tests for --http-server CLI arg and HTTP transport routing.

TDD: written BEFORE the implementation (RED phase).

Design note on test seams:
  Test 1+2: _apply_http_server_arg() is a tiny module-level helper exposed so
    the guard logic is independently testable without subprocess overhead.
  Test 3: _run_mcp(mcp, settings) is extracted from the run_server closure so
    the real dispatch code is importable and kwarg drift is genuinely caught.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

_FAKE_PG_URL = "postgresql+asyncpg://brain:brain@localhost:5433/brain"
_RUNTIME_ENV_KEYS = frozenset(
    {
        "brain_code_mode",
        "brain_dream_capability_enforcement",
        "brain_mcp_transport",
        "graph_enabled",
        "graph_ledger_write_enabled",
        "graph_projector_enabled",
        "graph_projector_neo4j_password",
        "graph_projector_neo4j_url",
        "graph_projector_neo4j_user",
        "mcp_http_dream_tokens",
        "mcp_http_host",
        "mcp_http_port",
        "mcp_http_token",
        "neo4j_password",
        "neo4j_url",
        "neo4j_user",
    }
)


@pytest.fixture(autouse=True)
def _isolate_runtime_environment(monkeypatch: Any) -> Any:
    from brain_v42.config import get_settings

    for key in tuple(os.environ):
        if key.casefold() in _RUNTIME_ENV_KEYS:
            monkeypatch.delenv(key)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test 1: --http-server arg selects HTTP transport via env
# ---------------------------------------------------------------------------


def test_http_server_arg_selects_http_transport(monkeypatch: Any) -> None:
    """_apply_http_server_arg() must set BRAIN_MCP_TRANSPORT=http when --http-server present."""

    from brain_v42.config import get_settings
    from brain_v42.mcp import server

    monkeypatch.setattr(sys, "argv", ["brain_v42.mcp.server", "--http-server"])
    monkeypatch.setenv("POSTGRES_URL", _FAKE_PG_URL)
    monkeypatch.delenv("BRAIN_MCP_TRANSPORT", raising=False)
    get_settings.cache_clear()

    server._apply_http_server_arg()

    settings = get_settings()
    assert settings.brain_mcp_transport == "http", (
        f"Expected 'http', got {settings.brain_mcp_transport!r}"
    )

    get_settings.cache_clear()
    monkeypatch.delenv("BRAIN_MCP_TRANSPORT", raising=False)


def test_http_server_arg_overrides_conflicting_transport(monkeypatch: Any) -> None:
    """The explicit production flag must not be neutralised by inherited env."""
    from brain_v42.config import get_settings
    from brain_v42.mcp import server

    monkeypatch.setattr(sys, "argv", ["brain_v42.mcp.server", "--http-server"])
    monkeypatch.setenv("POSTGRES_URL", _FAKE_PG_URL)
    monkeypatch.setenv("BRAIN_MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("brain_mcp_transport", "stdio")
    get_settings.cache_clear()

    server._apply_http_server_arg()

    assert get_settings().brain_mcp_transport == "http"

    get_settings.cache_clear()


def test_no_http_server_arg_defaults_to_stdio(monkeypatch: Any) -> None:
    """_apply_http_server_arg() must NOT set env when --http-server absent."""
    from brain_v42.config import get_settings
    from brain_v42.mcp import server

    monkeypatch.setattr(sys, "argv", ["brain_v42.mcp.server"])
    monkeypatch.setenv("POSTGRES_URL", _FAKE_PG_URL)
    monkeypatch.delenv("BRAIN_MCP_TRANSPORT", raising=False)
    get_settings.cache_clear()

    server._apply_http_server_arg()

    settings = get_settings()
    assert settings.brain_mcp_transport == "stdio", (
        f"Expected 'stdio', got {settings.brain_mcp_transport!r}"
    )

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test 2: _run_mcp() HTTP branch calls run_http_async with correct kwargs
# ---------------------------------------------------------------------------


def test_run_server_http_branch_calls_run_http_async_with_kwargs(
    monkeypatch: Any,
) -> None:
    """_run_mcp() with http transport must await run_http_async with the correct kwargs.

    Calls the REAL _run_mcp() (not a reimplementation) so kwarg drift in
    production code is genuinely caught. Patches mcp.run_http_async to capture
    actual kwargs and asserts the full contract:
    - transport="http"
    - stateless_http from settings (False by default since the transport
      identity work: the server must mint an Mcp-Session-Id so concurrent
      clients of one binary stop collapsing into a single panel row)
    - json_response=True
    - uvicorn_config={"timeout_graceful_shutdown": 10}
    - host and port from settings
    Also verifies run_async (stdio) is NOT called.
    """
    from brain_v42.config import Settings, get_settings
    from brain_v42.mcp import server

    monkeypatch.setenv("POSTGRES_URL", _FAKE_PG_URL)
    monkeypatch.delenv("BRAIN_MCP_TRANSPORT", raising=False)
    get_settings.cache_clear()

    settings = Settings(
        postgres_url=_FAKE_PG_URL,
        brain_mcp_transport="http",
    )

    captured: dict[str, Any] = {}

    async def fake_run_http_async(**kwargs: Any) -> None:
        captured.update(kwargs)

    stdio_called = False

    async def fake_run_async(**kwargs: Any) -> None:
        nonlocal stdio_called
        stdio_called = True

    monkeypatch.setattr(server.mcp, "run_http_async", fake_run_http_async)
    monkeypatch.setattr(server.mcp, "run_async", fake_run_async)

    # Call the REAL _run_mcp — no reimplementation
    asyncio.run(server._run_mcp(server.mcp, settings))

    assert captured.get("transport") == "http", f"transport not http: {captured}"
    assert captured.get("stateless_http") is False, f"stateless_http not False: {captured}"
    assert captured.get("json_response") is True, f"json_response not True: {captured}"
    assert captured.get("uvicorn_config") == {"timeout_graceful_shutdown": 10}, (
        f"uvicorn_config mismatch: {captured}"
    )
    assert captured.get("host") == settings.mcp_http_host, f"host mismatch: {captured}"
    assert captured.get("port") == settings.mcp_http_port, f"port mismatch: {captured}"
    assert not stdio_called, "run_async(stdio) must NOT be called in http branch"

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test 3: http_app builds without error (smoke)
# ---------------------------------------------------------------------------


def test_http_app_builds() -> None:
    """mcp.http_app(stateless_http=True, json_response=True) returns a Starlette app.

    Smoke assertion: the HTTP layer constructs and exposes the /mcp route.
    No real socket or uvicorn needed.
    """
    from brain_v42.mcp import server

    app = server.mcp.http_app(stateless_http=True, json_response=True)

    assert app is not None

    route_paths = [getattr(r, "path", None) for r in getattr(app, "routes", [])]
    assert any("/mcp" in str(p) for p in route_paths if p is not None), (
        f"No /mcp route found among: {route_paths}"
    )


def test_session_idle_timeout_is_injected_and_guarded(monkeypatch: Any) -> None:
    """The idle deadline must reach the manager, and its guard must bite.

    FastMCP builds ``StreamableHTTPSessionManager`` without passing
    ``session_idle_timeout``: without this injection, the state of a session whose
    client dies without a DELETE survives until the process restarts.
    """
    import inspect as _inspect

    from fastmcp.server import http as fastmcp_http

    from brain_v42.mcp.server import (
        SessionIdleTimeoutUnavailableError,
        _install_session_idle_timeout,
    )

    original = fastmcp_http.StreamableHTTPSessionManager

    # POSITIVE CONTROL — the parameter exists upstream and reaches the constructor.
    assert "session_idle_timeout" in _inspect.signature(original.__init__).parameters
    captured: dict[str, Any] = {}

    class _Spy:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(fastmcp_http, "StreamableHTTPSessionManager", _Spy)
    # _Spy does not have the parameter: the guard must refuse to set a deadline it
    # could not honour.
    with pytest.raises(SessionIdleTimeoutUnavailableError):
        _install_session_idle_timeout(900.0)

    # POSITIVE CONTROL on a base that does accept it.
    class _Acceptable:
        def __init__(self, session_idle_timeout: float | None = None, **kwargs: Any) -> None:
            captured["session_idle_timeout"] = session_idle_timeout

    monkeypatch.setattr(fastmcp_http, "StreamableHTTPSessionManager", _Acceptable)
    _install_session_idle_timeout(900.0)
    fastmcp_http.StreamableHTTPSessionManager(app=object())
    assert captured["session_idle_timeout"] == 900.0

    monkeypatch.setattr(fastmcp_http, "StreamableHTTPSessionManager", original)


def test_stateless_can_be_restored_by_settings(monkeypatch: Any) -> None:
    """The rollback is a setting, not a code edit."""
    from brain_v42.config import Settings, get_settings
    from brain_v42.mcp import server

    monkeypatch.setenv("POSTGRES_URL", _FAKE_PG_URL)
    get_settings.cache_clear()
    settings = Settings(
        postgres_url=_FAKE_PG_URL,
        brain_mcp_transport="http",
        mcp_http_stateless=True,
    )
    captured: dict[str, Any] = {}

    async def fake_run_http_async(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(server.mcp, "run_http_async", fake_run_http_async)
    # ``_configure_http_security`` refuses to arm the SAME server twice, and
    # ``server.mcp`` is a module object shared by the whole file. Without this
    # reset, the test fails on that guard — not on what it claims to measure.
    server._http_security_configured_servers.discard(server.mcp)
    asyncio.run(server._run_mcp(server.mcp, settings))

    assert captured.get("stateless_http") is True
