"""Exercise credential refusals through the served FastMCP HTTP transport."""

from dataclasses import replace
from datetime import datetime

import httpx
import pytest
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from structlog.testing import capture_logs

from brain_v42.credentials import verifier as verifier_module
from brain_v42.credentials.reasons import refusal_counts, reset_refusal_counts
from brain_v42.credentials.verifier import CredentialVerifier
from brain_v42.mcp import server
from brain_v42.provenance import get_current_actor, get_current_principal
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
    headers: dict[str, str],
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
