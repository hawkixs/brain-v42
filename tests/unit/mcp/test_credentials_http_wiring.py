"""The served HTTP plan must use the registry boundary and own its refresh task."""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastmcp import FastMCP

from brain_v42.config import Settings
from brain_v42.mcp import server


def settings() -> Settings:
    return Settings(
        postgres_url="postgresql+asyncpg://localhost/unused",
        brain_mcp_transport="http",
        brain_mcp_auth_mode="credentials",
        mcp_http_token="",
        _env_file=None,
    )


def test_registry_composition_is_dormant_in_shared_token_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = Mock()
    monkeypatch.setattr(server, "get_session_factory", factory)
    config = settings()
    config.brain_mcp_transport = "http"
    config.brain_mcp_auth_mode = "shared_token"
    assert server.build_credential_verifier(config) is None
    factory.assert_not_called()


def test_credentials_mode_refuses_a_stdio_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stdio server has no transport boundary: credentials mode there would run unguarded."""
    from brain_v42.mcp.http_security import HttpAuthConfigurationError

    factory = Mock()
    monkeypatch.setattr(server, "get_session_factory", factory)
    config = settings()
    config.brain_mcp_transport = "stdio"
    config.brain_mcp_auth_mode = "credentials"
    with pytest.raises(HttpAuthConfigurationError, match="BRAIN_MCP_TRANSPORT=http"):
        server.build_credential_verifier(config)
    factory.assert_not_called()


def test_registry_composition_builds_a_local_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from brain_v42.credentials.verifier import CredentialVerifier

    factory = Mock()
    monkeypatch.setattr(server, "get_session_factory", Mock(return_value=factory))
    result = server.build_credential_verifier(settings())
    assert isinstance(result, CredentialVerifier)
    assert result._repository._session_factory is factory


def test_credentials_plan_uses_one_verifier_and_no_shared_token_guard() -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard, CredentialTokenVerifier
    from brain_v42.mcp.http_security import HostOriginGuard, RequestBodyLimitGuard

    mcp = FastMCP("credentials-plan")
    verifier = Mock()
    plan = server.plan_http_transport(mcp, settings(), credential_verifier=verifier)
    assert isinstance(mcp.auth, CredentialTokenVerifier)
    assert mcp.auth.verifier is verifier
    assert [entry.cls for entry in plan.middleware] == [
        HostOriginGuard,
        CredentialGuard,
        RequestBodyLimitGuard,
    ]
    assert plan.middleware[1].kwargs["verifier"] is verifier
    assert plan.stateless_http is False


async def test_run_mcp_passes_the_injected_registry_to_the_shared_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard
    from tests.unit.mcp.test_credentials_http import verifier

    mcp = FastMCP("credential-run")

    @mcp.tool
    async def brain_search() -> str:
        return "read"

    run_http = AsyncMock()
    monkeypatch.setattr(mcp, "run_http_async", run_http)
    registry = await verifier()
    await server._run_mcp(mcp, settings(), credential_verifier=registry)
    assert (await mcp._list_tools())[0].tags == {"family:read"}
    run_http.assert_awaited_once()
    assert run_http.call_args.kwargs["uvicorn_config"]["proxy_headers"] is False
    assert run_http.call_args.kwargs["uvicorn_config"]["forwarded_allow_ips"] == ""
    guard = next(
        entry for entry in run_http.call_args.kwargs["middleware"] if entry.cls is CredentialGuard
    )
    assert guard.kwargs["verifier"] is registry


@pytest.mark.parametrize(
    "conflict,value",
    [
        ("mcp_http_stateless", True),
        ("mcp_http_allow_unauthenticated", True),
        ("mcp_http_token", "conflicting-shared-token"),
        ("brain_dream_capability_enforcement", True),
    ],
)
async def test_credentials_configuration_is_checked_before_process_hooks(
    monkeypatch: pytest.MonkeyPatch,
    conflict: str,
    value: bool | str,
) -> None:
    config = settings()
    setattr(config, conflict, value)
    idle = Mock()
    monkeypatch.setattr(server, "_install_session_idle_timeout", idle)
    with pytest.raises(server.HttpAuthConfigurationError):
        server.plan_http_transport(FastMCP("invalid"), config, credential_verifier=Mock())
    idle.assert_not_called()


async def test_refresh_is_owned_and_cancelled_by_app_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def loop() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    verifier = Mock(refresh=AsyncMock(return_value=True), run_refresh_loop=loop)
    services = {
        "access_logger": None,
        "neo4j_driver": None,
        "credential_verifier": verifier,
    }
    config = settings()
    config.decay_enabled = False
    monkeypatch.setattr(server, "dispose_engine", AsyncMock())
    monkeypatch.setattr(server, "close_neo4j_driver", AsyncMock())
    monkeypatch.setattr(server, "close_activity_reporter", AsyncMock())
    monkeypatch.setattr(server, "get_session_factory", Mock())
    monkeypatch.setattr(server, "AuditDrainer", Mock(return_value=Mock(run=lambda stop: loop())))
    monkeypatch.setattr(server, "CredentialListener", Mock(return_value=Mock(run=loop)))
    async with server.app_lifecycle(config, services, None):
        verifier.refresh.assert_awaited_once()
        await asyncio.wait_for(started.wait(), 1)
    assert cancelled.is_set()


async def test_failed_initial_refresh_still_serves_health_and_503() -> None:
    from tests.unit.mcp.test_credentials_http import verifier

    registry = await verifier(refresh=False)
    mcp = FastMCP("unavailable-registry")
    plan = server.plan_http_transport(mcp, settings(), credential_verifier=registry)
    app = mcp.http_app(
        middleware=plan.middleware,
        stateless_http=plan.stateless_http,
        json_response=plan.json_response,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        response = await client.post("/mcp", headers={"Authorization": "Bearer unavailable"})
    assert response.status_code == 503
    assert response.json() == {"error": "registry_unavailable"}


async def test_stateful_http_tool_receives_verified_identity() -> None:
    from fastmcp.server.dependencies import get_access_token

    from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
    from brain_v42.provenance import get_current_actor, get_current_principal
    from tests.unit.mcp.test_credentials_http import TOKEN, verifier

    mcp = FastMCP("verified-identity")
    mcp.add_middleware(ProvenanceMiddleware())

    @mcp.tool(name="brain_search")
    async def identity() -> dict[str, object]:
        access = get_access_token()
        assert access is not None
        return {
            "principal": get_current_principal(),
            "actor": get_current_actor(),
            "client_id": access.client_id,
            "families": access.scopes,
            "issuers": access.claims["issuers"],
        }

    plan = server.plan_http_transport(mcp, settings(), credential_verifier=await verifier())
    app = mcp.http_app(middleware=plan.middleware, stateless_http=False, json_response=True)
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"}
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
        await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        response = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "brain_search", "arguments": {}},
            },
        )
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"] == {
        "principal": "red-rail",
        "actor": "red-rail",
        "client_id": "red-rail",
        "families": ["read"],
        "issuers": ["red-rail"],
    }
