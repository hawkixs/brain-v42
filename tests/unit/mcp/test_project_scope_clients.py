"""Existing client authentication modes acquire no implicit credential scope."""

from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import AuthorizationError

from brain_v42.credentials.author_binding import author_binding_counts, reset_author_binding_counts
from brain_v42.credentials.reasons import refusal_counts, reset_refusal_counts
from brain_v42.mcp import server, tool_families
from brain_v42.mcp.client_principal import resolve_client_principal
from brain_v42.mcp.credentials_http import CredentialTokenVerifier
from brain_v42.mcp.tool_families import TOOL_FAMILIES, FamilyAuthorizationMiddleware
from brain_v42.provenance import get_current_principal
from tests.unit.credentials.test_verifier import Clock, Repo, row, verifier
from tests.unit.mcp.test_credentials_http_wiring import settings
from tests.unit.mcp.test_tool_families import _scope_context


@pytest.fixture(autouse=True)
def clean_counts() -> Iterator[None]:
    reset_author_binding_counts()
    reset_refusal_counts()
    yield
    reset_author_binding_counts()
    reset_refusal_counts()


async def _registry(
    client_id: str, families: list[str], issuers: list[str], *, projects: list[str] | None = None
):
    repo, clock = Repo(), Clock()
    repo.projects = ["brain-v42", "outside"]
    repo.rows = [
        replace(
            row("client-fixture"),
            client_id=client_id,
            families=families,
            issuers=issuers,
            allowed_projects=projects,
        )
    ]
    core = verifier(repo, clock)
    assert await core.refresh()
    return core


async def _http_call(
    mcp,
    config,
    name: str,
    arguments: dict[str, Any],
    *,
    registry=None,
    bearer="client-fixture",
    agent=None,
):
    plan = server.plan_http_transport(mcp, config, credential_verifier=registry)
    app = mcp.http_app(middleware=plan.middleware, stateless_http=False, json_response=True)
    headers = {"Authorization": "Bearer " + bearer, "Accept": "application/json, text/event-stream"}
    if agent is not None:
        headers["X-Brain-Agent"] = agent
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
                    "clientInfo": {"name": "scope-client", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        headers["Mcp-Session-Id"] = initialized.headers["Mcp-Session-Id"]
        await client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        response = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
    assert response.status_code == 200
    return response.json()["result"]


@pytest.mark.parametrize("elevated", [True, False])
async def test_operator_http_admin_still_requires_elevation(
    monkeypatch: pytest.MonkeyPatch, elevated: bool
) -> None:
    repository = MagicMock(has_active_elevation=AsyncMock(return_value=elevated))
    monkeypatch.setattr(server, "PgClientCredentialRepo", lambda *args: repository)
    monkeypatch.setattr(server, "get_session_factory", MagicMock())
    mcp = server.create_mcp_instance()
    handler = AsyncMock(return_value="deleted")

    @mcp.tool
    async def brain_delete() -> str:
        """Exercise the operator elevation boundary."""
        return await handler()

    config = settings()
    config.brain_credential_project_scope_mode = "enforce"
    result = await _http_call(
        mcp, config, "brain_delete", {}, registry=await _registry("operator", ["read", "write"], [])
    )
    assert result.get("isError", False) is not elevated
    assert handler.await_count == int(elevated)
    assert author_binding_counts() == {}
    assert refusal_counts() == ({} if elevated else {"family_denied": 1})


@pytest.mark.parametrize("name", ["brain_search", "brain_learn"])
async def test_shared_token_read_write_behavior_and_anonymous_author_are_unchanged(
    name: str,
) -> None:
    mcp = server.create_mcp_instance()

    @mcp.tool(name=name)
    async def identity() -> dict[str, str | None]:
        """Expose identity without manufacturing a credential author."""
        return {"author_client_id": get_current_principal()}

    config = settings()
    config.brain_mcp_auth_mode = "shared_token"
    config.mcp_http_token = "shared-fixture"
    config.brain_credential_project_scope_mode = "enforce"
    result = await _http_call(mcp, config, name, {}, bearer="shared-fixture")
    assert result["structuredContent"] == {"author_client_id": None}
    assert not result.get("isError", False)
    assert author_binding_counts() == refusal_counts() == {}


async def test_stdio_keeps_no_credential_principal_or_scope() -> None:
    mcp = server.create_mcp_instance()

    @mcp.tool
    async def brain_learn() -> dict[str, str | None]:
        """The in-process transport has stdio's unauthenticated middleware chain."""
        return {"author_client_id": get_current_principal()}

    assert not any(isinstance(m, FamilyAuthorizationMiddleware) for m in mcp.middleware)
    async with Client(mcp) as client:
        result = await client.call_tool("brain_learn", {})
    assert result.data == {"author_client_id": None}
    assert author_binding_counts() == refusal_counts() == {}


async def test_dream_phase_and_gateway_restrictions_remain_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp import dream_capabilities
    from tests.unit.mcp.test_dream_capabilities import (
        _capability_middleware,
        _scoped_access_token,
    )

    access = _scoped_access_token(phase="scan")
    monkeypatch.setattr(dream_capabilities, "get_access_token", lambda: access)
    middleware = _capability_middleware(dream_capabilities)
    assert resolve_client_principal(access) is None
    for name in ("brain_learn", "brain_call_tool"):
        handler = AsyncMock()
        with pytest.raises(AuthorizationError, match="Dream capability authorization denied"):
            await middleware.on_call_tool(_scope_context(name), handler)
        handler.assert_not_awaited()
    assert author_binding_counts() == refusal_counts() == {}


@pytest.mark.parametrize("name", sorted(TOOL_FAMILIES))
@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
async def test_null_scope_principal_reaches_every_tool_family_as_before(
    monkeypatch: pytest.MonkeyPatch, name: str, mode: str
) -> None:
    core = await _registry("coordination", ["read", "write", "delivery"], [])
    access = await CredentialTokenVerifier(core).verify_token("client-fixture")
    assert resolve_client_principal(access).allowed_projects is None
    monkeypatch.setattr(tool_families, "get_access_token", lambda: access)
    monkeypatch.setattr(
        tool_families, "get_http_headers", lambda **kwargs: {"mcp-session-id": "connection"}
    )
    middleware = FamilyAuthorizationMiddleware(
        elevation_checker=AsyncMock(return_value=True), scope_mode=mode
    )
    handler = AsyncMock(return_value="unchanged")
    assert await middleware.on_call_tool(_scope_context(name), handler) == "unchanged"
    handler.assert_awaited_once()
    assert author_binding_counts() == refusal_counts() == {}


async def test_cockpit_programme_read_is_unrestricted_but_writes_need_write_family() -> None:
    mcp = server.create_mcp_instance()

    @mcp.tool
    async def brain_programme_list() -> str:
        """Read programme progress."""
        return "programmes"

    config = settings()
    config.brain_credential_project_scope_mode = "enforce"
    registry = await _registry("cockpit", ["read"], [])
    result = await _http_call(mcp, config, "brain_programme_list", {}, registry=registry)
    assert not result.get("isError", False)
    assert author_binding_counts() == refusal_counts() == {}
    principal = resolve_client_principal(
        await CredentialTokenVerifier(registry).verify_token("client-fixture")
    )
    assert not await FamilyAuthorizationMiddleware().allows_tool(
        principal, "brain_programme_upsert"
    )


async def test_delivery_observer_github_auth_and_reserved_issuer_remain_separate(tmp_path) -> None:
    from brain_v42.db.focus_slots import OBSERVER_IDENTITY
    from brain_v42.delivery_config import DeliverySettings
    from brain_v42.delivery_observer.auth import GitHubAuthProvider
    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools
    from tests.unit.delivery_observer.test_transport import _transport

    private = tmp_path / "observer.env"
    private.write_text("BRAIN_DELIVERY_GITHUB_TOKEN=observer-fixture\n")
    private.chmod(0o600)
    config = DeliverySettings(observer_env_path=private)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200))
    ) as http:
        provider = GitHubAuthProvider(config, _transport(http, settings=config))
        assert "Authorization" in await provider.authorization_headers()
    mcp = server.create_mcp_instance()
    service = MagicMock(attest=AsyncMock())
    register_delivery_tools(mcp, service)
    config = settings()
    config.brain_credential_project_scope_mode = "enforce"
    result = await _http_call(
        mcp,
        config,
        "brain_delivery_attest",
        {
            "ticket_id": "00000000-0000-0000-0000-000000000001",
            "actor_project": "brain-v42",
            "kind": "review",
            "payload": {},
            "idempotency_key": "observer",
            "emitted_at": "2026-10-17T00:00:00Z",
            "issuer": OBSERVER_IDENTITY,
        },
        registry=await _registry("delivery-observer", ["delivery"], ["brain-v42"]),
        agent="brain-v42",
    )
    assert result["isError"]
    assert "issuer_identity_reserved" in str(result["content"])
    service.attest.assert_not_awaited()
    assert author_binding_counts() == refusal_counts() == {}


async def test_codex_gateway_static_bearer_bypasses_mcp_without_synthesizing_author(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.codex_gateway._support import build_gateway_fixture
    from tests.unit.codex_gateway.test_routes import _request

    middleware = AsyncMock(side_effect=AssertionError("gateway must not use MCP authorization"))
    monkeypatch.setattr(FamilyAuthorizationMiddleware, "on_call_tool", middleware)
    fixture = build_gateway_fixture()
    create = fixture.ticket_repo.create

    async def create_without_author(data):
        assert get_current_principal() is None
        assert getattr(data, "author_client_id", None) is None
        return await create(data)

    monkeypatch.setattr(fixture.ticket_repo, "create", create_without_author)
    response = await _request(
        fixture.app,
        "POST",
        "/api/tickets",
        {
            "kind": "request",
            "title": "Gateway",
            "body": "Ship it",
            "from_project": "red-codex",
            "to_project": "brain-v42",
        },
    )
    assert response.status_code == 201
    middleware.assert_not_awaited()
    assert get_current_principal() is None
    assert author_binding_counts() == refusal_counts() == {}


async def test_red_rail_null_scope_attestation_keeps_issuer_and_actor() -> None:
    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools
    from tests.unit.mcp.test_delivery_tools import _issuer_service

    mcp = server.create_mcp_instance()
    service = _issuer_service()
    register_delivery_tools(mcp, service)
    config = settings()
    config.brain_credential_project_scope_mode = "enforce"
    result = await _http_call(
        mcp,
        config,
        "brain_delivery_attest",
        {
            "ticket_id": "00000000-0000-0000-0000-000000000001",
            "actor_project": "brain-v42",
            "kind": "review",
            "payload": {},
            "idempotency_key": "rail",
            "emitted_at": "2026-10-17T00:00:00Z",
            "issuer": "red-rail",
        },
        registry=await _registry("red-rail", ["delivery"], ["brain-v42", "red-rail"]),
        agent="brain-v42",
    )
    assert not result.get("isError", False)
    assert service.attest.await_args.kwargs["caller_identity"] == "red-rail"
    assert service.attest.await_args.kwargs["actor_project"] == "brain-v42"
    assert author_binding_counts() == refusal_counts() == {}


@pytest.mark.parametrize("target,actor", [("brain-v42", "outside"), ("outside", "brain-v42")])
async def test_project_issuer_cannot_substitute_actor_scope_for_target_scope(
    target: str, actor: str
) -> None:
    mcp = server.create_mcp_instance()
    invoked = []

    @mcp.tool
    async def brain_learn(project_key: str) -> str:
        """Check the guard's real @project provenance before writing."""
        invoked.append(project_key)
        return "written"

    config = settings()
    config.brain_credential_project_scope_mode = "enforce"
    result = await _http_call(
        mcp,
        config,
        "brain_learn",
        {"project_key": target},
        registry=await _registry("scoped-client", ["write"], ["@project"], projects=["brain-v42"]),
        agent=actor,
    )
    assert result["isError"]
    assert "scope_denied" in str(result["content"])
    assert invoked == []
    assert author_binding_counts() == {"brain_learn:scope_mismatch": 1}
    assert refusal_counts() == {"scope_denied": 1}
