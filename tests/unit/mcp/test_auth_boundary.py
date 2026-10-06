"""Exercise credential refusals through the served FastMCP HTTP transport."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send
from structlog.testing import capture_logs

from brain_v42.credentials import verifier as verifier_module
from brain_v42.credentials.reasons import refusal_counts, reset_refusal_counts
from brain_v42.credentials.redact import short_id
from brain_v42.credentials.verifier import CredentialVerifier, VerifiedPrincipal
from brain_v42.mcp import server
from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
from brain_v42.provenance import (
    get_current_actor,
    get_current_principal,
    set_current_actor,
    set_current_principal,
)
from brain_v42.repositories.pg_client_credentials import CredentialRow
from tests.unit.mcp.test_credentials_http import NOW, TOKEN, Registry
from tests.unit.mcp.test_credentials_http_wiring import settings


class ReviewerRegistry(Registry):
    def __init__(self, issuers: tuple[str, ...] = ()) -> None:
        self.issuers = issuers

    async def active_rows(self, now: datetime) -> list[CredentialRow]:
        rows = await super().active_rows(now)
        return [replace(rows[0], client_id="red-rail-reviewer", issuers=list(self.issuers))]


@pytest.fixture(autouse=True)
def isolated_observations(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_refusal_counts()
    monkeypatch.setattr(verifier_module, "_last_lookup_at", None)


async def transport_request(
    *,
    headers: dict[str, str] | list[tuple[str, str]],
    issuers: tuple[str, ...] = (),
    stale: bool = False,
    path: str = "/mcp",
) -> httpx.Response:
    registry = ReviewerRegistry(issuers)
    elapsed = [0.0]
    verifier = CredentialVerifier(registry, clock=lambda: NOW, monotonic=lambda: elapsed[0])
    assert await verifier.refresh()
    if stale:
        registry.available = False
        elapsed[0] = 91.0
        assert not await verifier.refresh()
    mcp = FastMCP("credential-boundary")

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/identity", methods=["GET"])
    async def identity(request: Request) -> JSONResponse:
        return JSONResponse({"principal": get_current_principal(), "actor": get_current_actor()})

    plan = server.plan_http_transport(mcp, settings(), credential_verifier=verifier)
    app = mcp.http_app(middleware=plan.middleware, stateless_http=False, json_response=True)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app, client=("127.0.0.7", 4321)),
            base_url="http://localhost",
        ) as client,
    ):
        return await client.get(path, headers=headers)


async def test_reviewer_cannot_declare_another_actor() -> None:
    with capture_logs() as logs:
        response = await transport_request(
            headers={"Authorization": "Bearer " + TOKEN, "X-Brain-Agent": "red-rail"}
        )
    logs[:] = [event for event in logs if event["event"] == "mcp_auth.refused"]
    assert response.status_code == 403
    assert response.json() == {"error": "agent_mismatch"}
    assert logs == [
        {
            "event": "mcp_auth.refused",
            "log_level": "warning",
            "reason": "agent_mismatch",
            "status": 403,
            "client_id": "red-rail-reviewer",
            "declared_agent": "red-rail",
            "peer": "127.0.0.7",
            "path": "/mcp",
        }
    ]
    assert refusal_counts() == {"agent_mismatch": 1}


@pytest.mark.parametrize("agent", [None, "red-rail-reviewer", "agent:worker"])
async def test_authorized_actor_is_request_scoped(agent: str | None) -> None:
    previous_actor = get_current_actor()
    previous_principal = get_current_principal()
    headers = {"Authorization": "Bearer " + TOKEN}
    if agent is not None:
        headers["X-Brain-Agent"] = agent
    response = await transport_request(headers=headers, issuers=("agent:*",), path="/identity")
    assert response.status_code == 200
    assert response.json() == {
        "principal": "red-rail-reviewer",
        "actor": agent or "red-rail-reviewer",
    }
    assert get_current_actor() == previous_actor
    assert get_current_principal() == previous_principal


async def test_long_declared_agent_is_sanitized_and_cut_in_refusal() -> None:
    agent = "A!" * 150
    with capture_logs() as logs:
        response = await transport_request(
            headers={"Authorization": "Bearer " + TOKEN, "X-Brain-Agent": agent}
        )
    logs[:] = [event for event in logs if event["event"] == "mcp_auth.refused"]
    assert response.status_code == 403
    assert len(logs) == 1
    assert logs[0]["declared_agent"] == "_" * 64
    assert agent not in str(logs)


async def test_forged_forwarded_for_never_changes_refusal_peer() -> None:
    with capture_logs() as logs:
        response = await transport_request(headers={"X-Forwarded-For": "192.0.2.99"})
    logs[:] = [event for event in logs if event["event"] == "mcp_auth.refused"]
    assert response.status_code == 401
    assert response.json() == {"error": "missing_token"}
    assert len(logs) == 1
    assert logs[0]["peer"] == "127.0.0.7"
    assert refusal_counts() == {"missing_token": 1}


async def test_registry_down_past_90_seconds_emits_exactly_one_safe_503() -> None:
    from hashlib import sha256

    with capture_logs() as logs:
        response = await transport_request(headers={"Authorization": "Bearer " + TOKEN}, stale=True)
    logs[:] = [event for event in logs if event["event"] == "mcp_auth.refused"]
    assert response.status_code == 503
    assert response.json() == {"error": "registry_unavailable"}
    assert logs == [
        {
            "event": "mcp_auth.refused",
            "log_level": "warning",
            "reason": "registry_unavailable",
            "status": 503,
            "peer": "127.0.0.7",
            "path": "/mcp",
        }
    ]
    assert refusal_counts()["registry_unavailable"] == 1
    for secret in (TOKEN, sha256(TOKEN.encode()).hexdigest()):
        assert secret not in str(logs) + response.text


async def test_health_is_available_without_a_bearer() -> None:
    assert (await transport_request(headers={}, path="/health")).status_code == 200


@asynccontextmanager
async def public_probe_client(
    mode: str, *, credential_registry_ready: bool = False
) -> AsyncIterator[httpx.AsyncClient]:
    from tests.unit.mcp.test_credentials_http import verifier
    from tests.unit.mcp.test_dream_capability_http import (
        FakeProjectResolver,
        _registry_json,
        _settings,
    )

    mcp = server.create_mcp_instance()
    config = (
        settings()
        if mode == "credentials"
        else _settings(enforcement=mode == "dream", registry=_registry_json())
    )
    plan = server.plan_http_transport(
        mcp,
        config,
        credential_verifier=await verifier(refresh=credential_registry_ready)
        if mode == "credentials"
        else None,
        project_resolver=FakeProjectResolver() if mode == "dream" else None,
    )
    app = mcp.http_app(
        middleware=plan.middleware,
        stateless_http=plan.stateless_http,
        json_response=plan.json_response,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        yield client


@pytest.mark.parametrize("mode", ["credentials", "shared_token", "dream"])
@pytest.mark.parametrize("path", ["/healthz", "/version"])
@pytest.mark.parametrize("metadata_present", [False, True])
@pytest.mark.parametrize("bearer_present", [False, True])
async def test_public_probes_without_bearer_do_not_touch_db_or_registry(
    mode: str,
    path: str,
    metadata_present: bool,
    bearer_present: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from brain_v42.mcp.dream_capabilities import DreamCapabilityTokenVerifier

    git_sha = "a" * 40 if metadata_present else None
    image_digest = "sha256:" + "b" * 64 if metadata_present else None
    for name, value in (("BRAIN_GIT_SHA", git_sha), ("BRAIN_IMAGE_DIGEST", image_digest)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    database = Mock(side_effect=AssertionError("public probes must not open the database"))
    credential_check = AsyncMock(side_effect=AssertionError("public probes must not verify tokens"))
    dream_check = AsyncMock(
        side_effect=AssertionError("public probes must not verify Dream tokens")
    )
    monkeypatch.setattr(server, "get_session_factory", database)
    monkeypatch.setattr(server, "_probe_database", database)
    monkeypatch.setattr(CredentialVerifier, "verify", credential_check)
    monkeypatch.setattr(DreamCapabilityTokenVerifier, "verify_token", dream_check)
    async with public_probe_client(mode) as client:
        headers = {"X-Brain-Agent": "untrusted-probe-header"}
        if bearer_present:
            headers["Authorization"] = "Bearer invalid-probe-token"
        response = await client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.json() == (
        {"status": "ok"}
        if path == "/healthz"
        else {
            "project": "brain-v42",
            "version": server.package_version(),
            "git_sha": git_sha,
            "image_digest": image_digest,
        }
    )
    database.assert_not_called()
    credential_check.assert_not_awaited()
    dream_check.assert_not_awaited()


@pytest.mark.parametrize("mode", ["credentials", "shared_token", "dream"])
async def test_public_probes_leave_mcp_protected(mode: str) -> None:
    async with public_probe_client(mode) as client:
        response = await client.post("/mcp")
    assert response.status_code == 401


@pytest.mark.parametrize("mode", ["credentials", "shared_token", "dream"])
@pytest.mark.parametrize("bearer", [None, "invalid-token"])
@pytest.mark.parametrize(
    "path",
    [
        "/healthz/",
        "/healthz/extra",
        "/version/",
        "/version/extra",
        "/versions",
        "/identity",
        "/mcp",
    ],
)
async def test_public_probe_exemptions_are_exact(
    mode: str, path: str, bearer: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from brain_v42.mcp.dream_capabilities import DreamCapabilityTokenVerifier

    dream_check = AsyncMock(return_value=None)
    monkeypatch.setattr(DreamCapabilityTokenVerifier, "verify_token", dream_check)
    async with public_probe_client(mode, credential_registry_ready=True) as client:
        response = await client.get(
            path, headers={"Authorization": "Bearer " + bearer} if bearer is not None else {}
        )
    assert response.status_code == 401
    if mode == "dream" and bearer is not None:
        dream_check.assert_awaited_with(bearer)


class TwoClientRegistry(ReviewerRegistry):
    async def active_rows(self, now: datetime) -> list[CredentialRow]:
        rows = await super().active_rows(now)
        return [
            rows[0],
            replace(
                rows[0], client_id="other-client", token_sha256=sha256(b"other-token").digest()
            ),
        ]


@asynccontextmanager
async def stateful_client(
    *,
    poison_context: bool = False,
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str], list[dict[str, str | None]]]]:
    from fastmcp.server.middleware import Middleware

    seen: list[dict[str, str | None]] = []
    registry = CredentialVerifier(
        TwoClientRegistry(("agent:*",)), clock=lambda: NOW, monotonic=lambda: 0.0
    )
    assert await registry.refresh()
    mcp = FastMCP("stateful-credential-boundary")

    class StaleSessionContext(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            if poison_context and seen:
                # Simulate identity inherited from the long-lived session task.
                set_current_principal("stale-session-client")
                set_current_actor("stale-session-actor")
            return await call_next(context)

    mcp.add_middleware(StaleSessionContext())
    mcp.add_middleware(ProvenanceMiddleware())

    @mcp.tool(name="brain_search")
    async def identity() -> dict[str, str | None]:
        result = {"principal": get_current_principal(), "actor": get_current_actor()}
        seen.append(result)
        return result

    plan = server.plan_http_transport(mcp, settings(), credential_verifier=registry)
    app = mcp.http_app(middleware=plan.middleware, stateless_http=False, json_response=True)
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app, client=("127.0.0.7", 4321)),
            base_url="http://localhost",
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
        yield client, headers, seen


async def call_identity(
    client: httpx.AsyncClient, headers: dict[str, str], call_id: int
) -> httpx.Response:
    return await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": "brain_search", "arguments": {}},
        },
    )


async def test_stateful_session_refuses_foreign_client_before_forwarding() -> None:
    async with stateful_client() as (client, headers, seen):
        assert (await call_identity(client, headers, 2)).status_code == 200
        session_id = headers["Mcp-Session-Id"]
        headers["Authorization"] = "Bearer other-token"
        with capture_logs() as logs:
            response = await call_identity(client, headers, 3)
        assert response.status_code == 403
        assert response.json() == {"error": "foreign_client_attach"}
        assert len(seen) == 1
        events = [event for event in logs if event["event"] == "mcp_auth.refused"]
        assert len(events) == 1
        assert events[0] == {
            "event": "mcp_auth.refused",
            "log_level": "warning",
            "reason": "foreign_client_attach",
            "status": 403,
            "requesting_client_id": "other-client",
            "owner_client_id": "red-rail-reviewer",
            "session_id": short_id(session_id),
            "peer": "127.0.0.7",
            "path": "/mcp",
        }
        assert session_id not in str(events) + response.text
        assert refusal_counts() == {"foreign_client_attach": 1}


async def test_stateful_tool_identity_uses_its_request_and_ignores_stale_context() -> None:
    async with stateful_client(poison_context=True) as (client, headers, seen):
        first = await call_identity(client, headers, 2)
        assert first.json()["result"]["structuredContent"] == {
            "principal": "red-rail-reviewer",
            "actor": "red-rail-reviewer",
        }
        headers["X-Brain-Agent"] = "agent:second"
        second = await call_identity(client, headers, 3)
        assert second.json()["result"]["structuredContent"] == {
            "principal": "red-rail-reviewer",
            "actor": "agent:second",
        }
        assert len(seen) == 2


@asynccontextmanager
async def minting_client() -> AsyncIterator[tuple[httpx.AsyncClient, list[tuple[str, str | None]]]]:
    from brain_v42.mcp.credentials_http import CredentialGuard

    registry = CredentialVerifier(TwoClientRegistry(), clock=lambda: NOW, monotonic=lambda: 0.0)
    assert await registry.refresh()

    forwarded: list[tuple[str, str | None]] = []
    minted = 0

    async def accepted(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal minted
        session_id = Headers(scope=scope).get("mcp-session-id")
        forwarded.append((scope["method"], session_id))
        response_headers = {}
        if session_id is None:
            response_headers["Mcp-Session-Id"] = f"{minted:032x}"
            minted += 1
        await JSONResponse({"accepted": True}, headers=response_headers)(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(CredentialGuard(accepted, verifier=registry)),
        base_url="http://localhost",
    ) as client:
        yield client, forwarded


@pytest.mark.parametrize("token", [TOKEN, "other-token"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_never_minted_session_is_refused_without_forwarding(token: str, method: str) -> None:
    session_id = "never-minted-session-identifier"
    async with minting_client() as (client, forwarded):
        with capture_logs() as logs:
            response = await client.request(
                method,
                "/mcp",
                headers={"Authorization": "Bearer " + token, "Mcp-Session-Id": session_id},
            )
        assert response.status_code == 403
        assert response.json() == {"error": "foreign_client_attach"}
        assert forwarded == []
        assert logs[0]["owner_client_id"] is None
        assert logs[0]["session_id"] == short_id(session_id)
        assert logs[0]["requesting_client_id"] == (
            "red-rail-reviewer" if token == TOKEN else "other-client"
        )
        assert session_id not in str(logs) + response.text
        assert refusal_counts() == {"foreign_client_attach": 1}


async def test_session_owner_memory_evicts_least_recently_used_binding_per_client() -> None:
    async with minting_client() as (client, forwarded):
        headers = {"Authorization": "Bearer " + TOKEN}
        for index in range(256):
            initialized = await client.post("/mcp", headers=headers)
            assert initialized.headers["Mcp-Session-Id"] == f"{index:032x}"
        headers["Mcp-Session-Id"] = f"{0:032x}"
        assert (await client.get("/mcp", headers=headers)).status_code == 200
        del headers["Mcp-Session-Id"]
        assert (await client.post("/mcp", headers=headers)).status_code == 200
        headers["Mcp-Session-Id"] = f"{1:032x}"
        before = len(forwarded)
        for token in (TOKEN, "other-token"):
            headers["Authorization"] = "Bearer " + token
            with capture_logs() as logs:
                response = await client.get("/mcp", headers=headers)
            assert response.status_code == 403
            assert logs[0]["owner_client_id"] is None
        assert len(forwarded) == before
        headers["Authorization"] = "Bearer other-token"
        headers["Mcp-Session-Id"] = f"{0:032x}"
        assert (await client.get("/mcp", headers=headers)).status_code == 403
        headers["Authorization"] = "Bearer " + TOKEN
        assert (await client.get("/mcp", headers=headers)).status_code == 200
        headers["Mcp-Session-Id"] = f"{256:032x}"
        assert (await client.get("/mcp", headers=headers)).status_code == 200


async def test_client_quota_cannot_evict_or_steal_another_clients_minted_session() -> None:
    async with minting_client() as (client, forwarded):
        victim = await client.post("/mcp", headers={"Authorization": "Bearer " + TOKEN})
        victim_id = victim.headers["Mcp-Session-Id"]
        headers = {"Authorization": "Bearer other-token", "Mcp-Session-Id": victim_id}
        # Ownership must exist before the owner's first attach.
        assert (await client.get("/mcp", headers=headers)).status_code == 403
        del headers["Mcp-Session-Id"]
        for _ in range(257):
            assert (await client.post("/mcp", headers=headers)).status_code == 200
        headers["Mcp-Session-Id"] = f"{1:032x}"
        assert (await client.get("/mcp", headers=headers)).status_code == 403
        headers["Mcp-Session-Id"] = victim_id
        before = len(forwarded)
        assert (await client.get("/mcp", headers=headers)).status_code == 403
        assert len(forwarded) == before
        headers["Authorization"] = "Bearer " + TOKEN
        assert (await client.get("/mcp", headers=headers)).status_code == 200


async def test_only_an_authorized_delete_expires_the_session_binding() -> None:
    async with minting_client() as (client, forwarded):
        headers = {"Authorization": "Bearer " + TOKEN}
        initialized = await client.post("/mcp", headers=headers)
        headers["Mcp-Session-Id"] = initialized.headers["Mcp-Session-Id"]
        headers["Authorization"] = "Bearer other-token"
        assert (await client.delete("/mcp", headers=headers)).status_code == 403
        headers["Authorization"] = "Bearer " + TOKEN
        assert (await client.get("/mcp", headers=headers)).status_code == 200
        assert (await client.delete("/mcp", headers=headers)).status_code == 200
        before = len(forwarded)
        for token in (TOKEN, "other-token"):
            headers["Authorization"] = "Bearer " + token
            with capture_logs() as logs:
                response = await client.get("/mcp", headers=headers)
            assert response.status_code == 403
            assert logs[0]["owner_client_id"] is None
        assert len(forwarded) == before


@pytest.mark.parametrize(
    "authorization",
    [
        [("Authorization", "Bearer " + TOKEN), ("authorization", "Bearer " + TOKEN)],
        [("Authorization", "Bearer  " + TOKEN)],
        [("Authorization", "Bearer " + TOKEN + " extra")],
        [("Authorization", "Bearer " + TOKEN + "\t")],
        [("Authorization", "Bearer ")],
        [("Authorization", "Bearer")],
    ],
    ids=["duplicate", "two-spaces", "token-space", "token-tab", "empty", "no-space"],
)
async def test_malformed_bearer_is_refused_before_verification(
    authorization: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    verify = AsyncMock(wraps=CredentialVerifier.verify)

    async def observed_verify(self: CredentialVerifier, token: str | None) -> VerifiedPrincipal:
        return await verify(self, token)

    monkeypatch.setattr(CredentialVerifier, "verify", observed_verify)
    with capture_logs() as logs:
        response = await transport_request(headers=authorization, path="/identity")
    verify.assert_not_awaited()
    assert response.status_code == 401
    assert response.json() == {"error": "missing_token"}
    events = [event for event in logs if event["event"] == "mcp_auth.refused"]
    assert len(events) == 1
    assert events[0]["reason"] == "missing_token"
    assert refusal_counts() == {"missing_token": 1}
    assert TOKEN not in str(events) + response.text


async def test_lowercase_bearer_and_guard_identity_survive_downstream_header_differential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp import provenance_middleware

    original_headers = provenance_middleware.get_http_headers

    def divergent_headers(**kwargs: Any) -> dict[str, str]:
        return {**(original_headers(**kwargs) or {}), "x-brain-agent": "unverified-actor"}

    monkeypatch.setattr(provenance_middleware, "get_http_headers", divergent_headers)
    async with stateful_client() as (client, headers, seen):
        headers["Authorization"] = "bearer " + TOKEN
        headers["X-Brain-Agent"] = "agent:verified"
        response = await call_identity(client, headers, 2)
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"] == {
            "principal": "red-rail-reviewer",
            "actor": "agent:verified",
        }
        assert len(seen) == 1
