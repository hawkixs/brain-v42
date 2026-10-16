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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_run_server_http_branch_calls_run_http_async_with_kwargs(
    monkeypatch: Any,
    log_format: str,
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
    - uvicorn_config={"timeout_graceful_shutdown": 10, "proxy_headers": False,
      "forwarded_allow_ips": ""} so the peer always comes from the socket
      (plus log_config=None in both modes to preserve sanitized logging)
    - show_banner=False only in JSON mode to keep every line parseable
    - host and port from settings
    Also verifies run_async (stdio) is NOT called.
    """
    from unittest.mock import AsyncMock

    from brain_v42.config import Settings, get_settings
    from brain_v42.mcp import server

    monkeypatch.setenv("POSTGRES_URL", _FAKE_PG_URL)
    monkeypatch.delenv("BRAIN_MCP_TRANSPORT", raising=False)
    get_settings.cache_clear()

    settings = Settings(
        postgres_url=_FAKE_PG_URL,
        brain_mcp_transport="http",
        mcp_http_token="test-token",
        brain_log_format=log_format,
    )

    run_http = AsyncMock()

    stdio_called = False

    async def fake_run_async(**kwargs: Any) -> None:
        nonlocal stdio_called
        stdio_called = True

    monkeypatch.setattr(server.mcp, "run_http_async", run_http)
    monkeypatch.setattr(server.mcp, "run_async", fake_run_async)
    server._http_security_configured_servers.discard(server.mcp)

    # Call the REAL _run_mcp — no reimplementation
    asyncio.run(server._run_mcp(server.mcp, settings))

    run_http.assert_awaited_once()
    captured = run_http.call_args.kwargs

    assert captured.get("transport") == "http", f"transport not http: {captured}"
    assert captured.get("stateless_http") is False, f"stateless_http not False: {captured}"
    assert captured.get("json_response") is True, f"json_response not True: {captured}"
    assert captured.get("uvicorn_config") == {
        "timeout_graceful_shutdown": 10,
        "proxy_headers": False,
        "forwarded_allow_ips": "",
        "log_config": None,
    }, f"uvicorn_config mismatch: {captured}"
    assert captured.get("host") == settings.mcp_http_host, f"host mismatch: {captured}"
    assert captured.get("port") == settings.mcp_http_port, f"port mismatch: {captured}"
    assert (
        captured.get("show_banner") is False
        if log_format == "json"
        else "show_banner" not in captured
    )
    assert not stdio_called, "run_async(stdio) must NOT be called in http branch"

    get_settings.cache_clear()


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_stdio_banner_is_suppressed_only_for_json_logs(monkeypatch: Any, log_format: str) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from brain_v42.config import Settings
    from brain_v42.mcp import server

    settings = Settings(postgres_url=_FAKE_PG_URL, brain_log_format=log_format)
    mcp = MagicMock()
    mcp.run_async = AsyncMock()
    monkeypatch.setattr(server, "prepare_tools_for_transport", AsyncMock())
    monkeypatch.setattr(server, "_install_signal_handlers", lambda *_: None)

    asyncio.run(server._run_mcp(mcp, settings))

    mcp.run_async.assert_awaited_once_with(
        transport="stdio", **({"show_banner": False} if log_format == "json" else {})
    )


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
        def __init__(
            self,
            session_idle_timeout: float | None = None,
            max_sessions: int = 10000,
            **kwargs: Any,
        ) -> None:
            captured["session_idle_timeout"] = session_idle_timeout
            captured["max_sessions"] = max_sessions

        def _admit_session(self, requestor: Any) -> str | None:
            return None

    monkeypatch.setattr(fastmcp_http, "StreamableHTTPSessionManager", _Acceptable)
    _install_session_idle_timeout(900.0, 42)
    fastmcp_http.StreamableHTTPSessionManager(app=object())
    assert captured["session_idle_timeout"] == 900.0
    assert captured["max_sessions"] == 42

    monkeypatch.setattr(fastmcp_http, "StreamableHTTPSessionManager", original)


@pytest.mark.parametrize(
    "missing", ["session_idle_timeout", "max_sessions", "_admit_session", "requestor"]
)
def test_session_bounds_refuse_upstream_signature_drift(monkeypatch: Any, missing: str) -> None:
    from fastmcp.server import http as fastmcp_http

    from brain_v42.mcp.server import (
        SessionIdleTimeoutUnavailableError,
        _install_session_idle_timeout,
    )

    class Drifted:
        def __init__(
            self, session_idle_timeout: float | None = None, max_sessions: int = 10000
        ) -> None:
            pass

        def _admit_session(self, requestor: Any) -> str | None:
            return None

    if missing == "session_idle_timeout":
        monkeypatch.setattr(Drifted, "__init__", lambda self, max_sessions=10000: None)
    elif missing == "max_sessions":
        monkeypatch.setattr(Drifted, "__init__", lambda self, session_idle_timeout=None: None)
    elif missing == "_admit_session":
        monkeypatch.delattr(Drifted, "_admit_session")
    else:
        monkeypatch.setattr(Drifted, "_admit_session", lambda self: None)
    monkeypatch.setattr(fastmcp_http, "StreamableHTTPSessionManager", Drifted)

    with pytest.raises(SessionIdleTimeoutUnavailableError, match=missing):
        _install_session_idle_timeout(3600.0, 2)


def test_session_bounds_update_without_stacking_and_preserve_explicit_values() -> None:
    from fastmcp.server import http as fastmcp_http

    from brain_v42.mcp.server import _install_session_idle_timeout

    original = fastmcp_http.StreamableHTTPSessionManager
    _install_session_idle_timeout(3600.0, 2)
    installed = fastmcp_http.StreamableHTTPSessionManager
    _install_session_idle_timeout(7200.0, 7)
    assert fastmcp_http.StreamableHTTPSessionManager is installed
    assert installed.__bases__ == (original,)
    manager = installed(app=object())
    assert manager.session_idle_timeout == 7200.0
    assert manager.max_sessions == 7

    explicit = installed(app=object(), session_idle_timeout=123.0, max_sessions=3)
    assert explicit.session_idle_timeout == 123.0
    assert explicit.max_sessions == 3


def test_stateful_http_plan_passes_both_session_bounds(monkeypatch: Any) -> None:
    from unittest.mock import Mock

    from fastmcp import FastMCP

    from brain_v42.config import Settings
    from brain_v42.mcp import server

    install = Mock()
    monkeypatch.setattr(server, "_install_session_idle_timeout", install)
    settings = Settings(
        postgres_url=_FAKE_PG_URL,
        mcp_http_token="test-token",
        mcp_http_session_idle_seconds=1234.0,
        mcp_http_max_sessions=42,
        _env_file=None,
    )
    server.plan_http_transport(FastMCP("bounds-wiring"), settings)
    install.assert_called_once_with(1234.0, 42)


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "session-bounds-test", "version": "1"},
    },
}


async def _open_session(client: Any) -> str:
    response = await client.post("/mcp", json=_INITIALIZE)
    assert response.status_code == 200, response.text
    session_id = response.headers["mcp-session-id"]
    response = await client.post(
        "/mcp",
        headers={"mcp-session-id": session_id},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert response.status_code == 202, response.text
    return session_id


async def _ping_session(client: Any, session_id: str) -> Any:
    return await client.post(
        "/mcp",
        headers={"mcp-session-id": session_id},
        json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
    )


@asynccontextmanager
async def session_http_app(max_sessions: int) -> AsyncIterator[Any]:
    """Exercise admission, cancellation and cleanup through the real SDK and ASGI."""
    import httpx
    from fastmcp import FastMCP

    from brain_v42.mcp.server import _install_session_idle_timeout

    _install_session_idle_timeout(3600.0, max_sessions)
    mcp = FastMCP("session-bounds")
    entered = {name: asyncio.Event() for name in ("a", "b")}
    release = asyncio.Event()

    @mcp.tool
    async def hold(name: str) -> str:
        entered[name].set()
        await release.wait()
        return name

    app = mcp.http_app(stateless_http=False, json_response=True)
    endpoint = next(
        route.endpoint for route in app.routes if hasattr(route.endpoint, "session_manager")
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers={"Accept": "application/json, text/event-stream"},
        ) as client,
    ):
        try:
            yield endpoint.session_manager, client, entered, release
        finally:
            release.set()


async def test_session_cap_evicts_the_least_recently_active_idle_session(
    monkeypatch: Any,
) -> None:
    from unittest.mock import Mock

    from brain_v42.mcp import server

    log = Mock()
    monkeypatch.setattr(server, "logger", log)
    async with session_http_app(2) as (manager, client, _, _):
        a = await _open_session(client)
        b = await _open_session(client)
        # Activity, rather than creation order, chooses the victim: refresh A then B.
        assert (await _ping_session(client, a)).status_code == 200
        assert (await _ping_session(client, b)).status_code == 200
        transport = manager._server_instances[a]
        assert transport.idle_scope.deadline < manager._server_instances[b].idle_scope.deadline

        c = await _open_session(client)

        assert (await _ping_session(client, a)).status_code == 404
        assert (await _ping_session(client, b)).status_code == 200
        assert (await _ping_session(client, c)).status_code == 200
        assert a not in manager._session_owners
        assert transport.idle_scope.cancel_called
        for _ in range(100):
            if transport.is_terminated:
                break
            await asyncio.sleep(0.01)
        assert transport.is_terminated
        assert len(manager._server_instances) == 2
        assert manager.session_evictions == 1
        evictions = [
            call
            for call in log.warning.call_args_list
            if call.args[0] == "brain_v42.server.session_evicted"
        ]
        assert len(evictions) == 1
        assert evictions[0].kwargs["session_id"] == a[:8]
        assert evictions[0].kwargs["evictions"] == 1


async def test_session_cap_skips_an_already_cancelled_idle_session() -> None:
    async with session_http_app(1) as (manager, client, _, _):
        session_id = await _open_session(client)
        transport = manager._server_instances[session_id]
        transport.idle_scope.cancel()
        assert transport.idle_scope.cancel_called

        # Stay synchronous so the SDK cannot discard the cancelled session yet.
        # SDK admission refusal is what handle_request maps to HTTP 503.
        assert manager._admit_session(None) is None
        assert set(manager._server_instances) == {session_id}
        assert manager.session_evictions == 0


async def test_session_cap_preserves_every_session_with_a_request_in_flight() -> None:
    async with session_http_app(2) as (manager, client, entered, release):
        sessions = [await _open_session(client), await _open_session(client)]
        calls = [
            asyncio.create_task(
                client.post(
                    "/mcp",
                    headers={"mcp-session-id": session_id},
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "hold", "arguments": {"name": name}},
                    },
                )
            )
            for name, session_id in zip(("a", "b"), sessions, strict=True)
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
            assert all(t._requests_in_flight > 0 for t in manager._server_instances.values())
            response = await client.post("/mcp", json=_INITIALIZE)
            assert response.status_code == 503
            assert set(manager._server_instances) == set(sessions)
            assert manager.session_evictions == 0
        finally:
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*calls), 2)
        assert all(response.status_code == 200 for response in responses)


@pytest.mark.parametrize("max_sessions", [5])
async def test_session_capacity_warning_fires_once_per_crossing(
    max_sessions: int, monkeypatch: Any
) -> None:
    from unittest.mock import Mock

    from brain_v42.mcp import server

    log = Mock()
    monkeypatch.setattr(server, "logger", log)
    async with session_http_app(max_sessions) as (manager, client, _, _):

        def warnings() -> int:
            return sum(
                call.args[0] == "brain_v42.server.session_capacity"
                for call in log.warning.call_args_list
            )

        for _ in range(3):
            await _open_session(client)
        assert warnings() == 0
        await _open_session(client)
        assert warnings() == 1
        newest = [await _open_session(client), await _open_session(client)]
        assert warnings() == 1  # Including eviction while remaining above the threshold.
        for session_id in newest:
            response = await client.delete("/mcp", headers={"mcp-session-id": session_id})
            assert response.status_code == 200
        assert len(manager._server_instances) == 3
        await _open_session(client)
        assert warnings() == 2


@pytest.mark.parametrize(
    "missing", ["_server_instances", "_session_owners", "idle_scope", "_requests_in_flight"]
)
async def test_session_eviction_private_attribute_drift_falls_back_and_logs_once(
    monkeypatch: Any, missing: str
) -> None:
    from unittest.mock import Mock

    from brain_v42.mcp import server

    log = Mock()
    monkeypatch.setattr(server, "logger", log)
    async with session_http_app(2) as (manager, client, _, _):
        a = await _open_session(client)
        b = await _open_session(client)
        target = manager if missing.startswith("_s") else manager._server_instances[a]
        with monkeypatch.context() as drift:
            drift.delattr(target, missing)
            # Direct admission isolates the guard from unrelated SDK uses of the
            # deliberately removed attribute. Rejection is what handle_request maps to 503.
            assert manager._admit_session(None) is None
            assert manager._admit_session(None) is None
        assert set(manager._server_instances) == {a, b}
        assert manager.session_evictions == 0
        errors = [
            call
            for call in log.error.call_args_list
            if call.args[0] == "brain_v42.server.session_eviction_unavailable"
        ]
        assert len(errors) == 1


def test_stateless_can_be_restored_by_settings(monkeypatch: Any) -> None:
    """The rollback is a setting, not a code edit."""
    from brain_v42.config import Settings, get_settings
    from brain_v42.mcp import server

    monkeypatch.setenv("POSTGRES_URL", _FAKE_PG_URL)
    get_settings.cache_clear()
    settings = Settings(
        postgres_url=_FAKE_PG_URL,
        brain_mcp_transport="http",
        mcp_http_token="test-token",
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


@pytest.mark.parametrize(
    "host,origin,status",
    [
        ("192.0.2.4:8765", None, 200),
        ("192.0.2.4:9999", None, 421),
        ("192.0.2.5:8765", None, 421),
        ("evil.example.test:8765", None, 421),
        ("127.0.0.1:9000", None, 200),
        ("localhost:8765", None, 200),
        ("[::1]:8765", None, 200),
        ("192.0.2.4:8765", "http://192.0.2.4:8765", 200),
        ("192.0.2.4:8765", "http://192.0.2.4:9999", 403),
        ("192.0.2.4:8765", "http://192.0.2.5:8765", 403),
        ("192.0.2.4:8765", "http://evil.example.test:8765", 403),
        ("192.0.2.4:8765", "http://localhost:3000", 200),
    ],
)
async def test_http_plan_wires_allowed_hosts_and_keeps_loopback(
    host: str, origin: str | None, status: int
) -> None:
    import httpx
    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from brain_v42.config import Settings
    from brain_v42.mcp import server
    from tests.unit.mcp.test_credentials_http import verifier

    config = Settings(
        postgres_url=_FAKE_PG_URL,
        brain_mcp_transport="http",
        brain_mcp_auth_mode="credentials",
        mcp_http_host="0.0.0.0",
        mcp_http_allow_non_loopback=True,
        mcp_http_allowed_hosts="192.0.2.4:8765",
        _env_file=None,
    )
    mcp = FastMCP("allowed-hosts-plan")

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    plan = server.plan_http_transport(
        mcp, config, credential_verifier=await verifier(refresh=False)
    )
    app = mcp.http_app(middleware=plan.middleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        headers = {"Host": host}
        if origin is not None:
            headers["Origin"] = origin
        response = await client.get("/health", headers=headers)
    assert response.status_code == status
