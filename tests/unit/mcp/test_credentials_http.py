"""Exercise the credential boundary before the MCP transport consumes the body."""

from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.testing import capture_logs

from brain_v42.credentials import verifier as verifier_module
from brain_v42.credentials.agent_unresolved import (
    agent_unresolved_counts,
    reset_agent_unresolved_counts,
)
from brain_v42.credentials.reasons import refusal_counts, reset_refusal_counts
from brain_v42.credentials.verifier import CredentialVerifier
from brain_v42.provenance import get_current_principal
from brain_v42.repositories.pg_client_credentials import CredentialRow, Disposition

NOW = datetime(2026, 1, 1, tzinfo=UTC)
TOKEN = "test-credential-material"
ROW_ID = UUID("00000000-0000-0000-0000-000000000001")


class Registry:
    available = True
    issuers = ["red-rail"]

    async def project_keys(self) -> list[str]:
        return ["brain-v42"]

    async def active_rows(self, now: datetime) -> list[CredentialRow]:
        if not self.available:
            raise OSError("unavailable")
        return [
            CredentialRow(
                ROW_ID,
                "red-rail",
                sha256(TOKEN.encode()).digest(),
                ["read"],
                self.issuers,
                False,
                NOW,
                "operator",
                None,
                None,
                None,
                None,
            )
        ]

    async def disposition_by_digest(self, digest: bytes, now: datetime) -> tuple[None, Disposition]:
        return None, "unknown"


@pytest.fixture(autouse=True)
def reset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(verifier_module, "_last_lookup_at", None)
    monkeypatch.setattr(
        "brain_v42.mcp.credentials_http._seen_unresolved_agents", set(), raising=False
    )
    reset_refusal_counts()
    reset_agent_unresolved_counts()
    yield
    reset_refusal_counts()
    reset_agent_unresolved_counts()


async def verifier(*, refresh: bool = True) -> CredentialVerifier:
    result = CredentialVerifier(Registry(), clock=lambda: NOW, monotonic=lambda: 0.0)
    if refresh:
        assert await result.refresh()
    return result


async def test_adapter_returns_secret_free_verified_claims() -> None:
    from brain_v42.mcp.credentials_http import CredentialTokenVerifier

    adapter = CredentialTokenVerifier(await verifier())
    access = await adapter.verify_token(TOKEN)
    assert access is not None
    assert access.client_id == "red-rail"
    assert access.scopes == ["read"]
    assert access.claims == {"credential_id": str(ROW_ID), "issuers": ["red-rail"]}
    assert TOKEN not in repr(access)
    assert TOKEN not in access.model_dump_json()
    assert sha256(TOKEN.encode()).hexdigest() not in access.model_dump_json()


async def test_adapter_refusal_is_none_without_a_second_observation() -> None:
    from brain_v42.mcp.credentials_http import CredentialTokenVerifier

    with capture_logs() as logs:
        assert await CredentialTokenVerifier(await verifier()).verify_token("wrong") is None
    assert logs == []
    assert refusal_counts() == {}


async def test_adapter_reuses_only_the_credential_verified_for_this_request() -> None:
    from unittest.mock import AsyncMock

    from brain_v42.credentials.verifier import CredentialRefused, VerifiedPrincipal
    from brain_v42.mcp.credentials_http import CredentialGuard, CredentialTokenVerifier

    registry = AsyncMock(spec=CredentialVerifier)
    registry.project_exists.return_value = False
    registry.verify.side_effect = [
        VerifiedPrincipal("red-rail", frozenset({"read"}), frozenset(), ROW_ID, None),
        CredentialRefused("unknown_token"),
    ]
    adapter = CredentialTokenVerifier(registry)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        access = await adapter.verify_token(TOKEN)
        assert access is not None
        assert access.client_id == "red-rail"
        assert await adapter.verify_token("another-token") is None
        await ok(scope, receive, send)

    assert (await request(CredentialGuard(app, verifier=registry), token=TOKEN)).status_code == 200
    assert registry.verify.await_count == 2


async def request(
    guard: ASGIApp, *, token: str | None = None, agent: str | None = None, path: str = "/mcp"
) -> httpx.Response:
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if agent is not None:
        headers["X-Brain-Agent"] = agent
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=guard), base_url="http://localhost"
    ) as client:
        return await client.post(path, headers=headers, content=b"unread body")


async def ok(scope: Scope, receive: Receive, send: Send) -> None:
    from starlette.responses import JSONResponse

    await JSONResponse({"client_id": get_current_principal()})(scope, receive, send)


async def issuer_verifier(issuers: list[str]) -> CredentialVerifier:
    registry = Registry()
    registry.issuers = issuers
    core = CredentialVerifier(registry, clock=lambda: NOW, monotonic=lambda: 0.0)
    assert await core.refresh()
    return core


@pytest.mark.parametrize(
    "agent",
    [
        "brain-v42",
        "brain",
        "/checkout/brain_v42",
        "/checkout/brain_v42/.claude/worktrees/topic",
        "/checkout/brain_v42/.worktrees/topic/src",
    ],
)
async def test_project_issuer_accepts_existing_canonical_project(agent: str) -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard

    guard = CredentialGuard(ok, verifier=await issuer_verifier(["@project"]))
    with capture_logs() as logs:
        response = await request(guard, token=TOKEN, agent=agent)
    assert response.status_code == 200
    assert response.json() == {"client_id": "red-rail"}
    assert logs == []


@pytest.mark.parametrize(
    ("pattern", "agent", "accepted"),
    [
        ("operator", "operator", True),
        ("agent:*", "agent:reviewer", True),
        ("agent:*", "agent:", True),
        ("service:*", "service:worker", True),
        ("operator", "/checkout/operator", False),
        ("operator", "operator ", False),
        ("agent:*", "/checkout/agent:reviewer", False),
        ("agent:*", "Agent:reviewer", False),
        ("agent:?", "agent:x", False),
        ("agent:[xy]", "agent:x", False),
        ("agent:*:x", "agent:worker:x", False),
        ("*", "operator", False),
    ],
)
async def test_issuer_patterns_authorize_only_raw_exact_or_prefix_values(
    pattern: str, agent: str, accepted: bool
) -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard

    guard = CredentialGuard(ok, verifier=await issuer_verifier([pattern]))
    response = await request(guard, token=TOKEN, agent=agent)
    assert response.status_code == (200 if accepted else 403)
    if not accepted:
        assert response.json() == {"error": "agent_mismatch"}


@pytest.mark.parametrize(
    ("agent", "normalized", "reason"),
    [
        ("ReD_v1", "ReD_v1", "not_kebab"),
        ("/checkout/unknown-user", "unknown-user", "unknown_project"),
        ("/checkout/unknown-key/.worktrees/fix-x/src", "unknown-key", "unknown_project"),
        ("/checkout/" + "a" * 80, "a" * 64, "unknown_project"),
    ],
)
async def test_unresolved_project_falls_back_to_client_with_one_bounded_warning(
    agent: str, normalized: str, reason: str
) -> None:
    from starlette.responses import JSONResponse

    from brain_v42.mcp.credentials_http import CredentialGuard
    from brain_v42.provenance import get_current_actor

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["state"]["brain_actor"] == "red-rail"
        await JSONResponse({"actor": get_current_actor()})(scope, receive, send)

    guard = CredentialGuard(app, verifier=await issuer_verifier(["@project"]))
    with capture_logs() as logs:
        for _ in range(2):
            response = await request(guard, token=TOKEN, agent=agent)
            assert response.status_code == 200
            assert response.json() == {"actor": "red-rail"}
        assert agent_unresolved_counts() == {reason: 2}
        assert len(logs) == 1
        response = await request(guard, token=TOKEN, agent="another-project")
        assert response.status_code == 200
        assert response.json() == {"actor": "red-rail"}
    assert logs == [
        {
            "event": "mcp_auth.agent_unresolved",
            "log_level": "warning",
            "client_id": "red-rail",
            "agent": normalized,
            "reason": reason,
        },
        {
            "event": "mcp_auth.agent_unresolved",
            "log_level": "warning",
            "client_id": "red-rail",
            "agent": "another-project",
            "reason": "unknown_project",
        },
    ]
    assert refusal_counts() == {}


async def test_unresolved_project_warning_memo_is_bounded_per_process() -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard

    # Two guards share the process memo: reconnecting must not restart the flood.
    guards = [CredentialGuard(ok, verifier=await issuer_verifier(["@project"])) for _ in range(2)]
    with capture_logs() as logs:
        for index in range(70):
            response = await request(guards[index % 2], token=TOKEN, agent=f"unknown-{index}")
            assert response.status_code == 200
        assert (await request(guards[1], token=TOKEN, agent="unknown-0")).status_code == 200
    assert len(logs) == 64
    assert all(entry["event"] == "mcp_auth.agent_unresolved" for entry in logs)
    assert agent_unresolved_counts() == {"unknown_project": 71}


def test_unresolved_warning_memo_distinguishes_clients_and_reasons() -> None:
    from brain_v42.mcp.credentials_http import _fallback_unresolved_project_actor

    with capture_logs() as logs:
        for _ in range(2):
            assert _fallback_unresolved_project_actor("first", "unknown", "not_kebab") == "first"
        assert _fallback_unresolved_project_actor("second", "unknown", "not_kebab") == "second"
        assert _fallback_unresolved_project_actor("first", "unknown", "unknown_project") == "first"
    assert len(logs) == 3


@pytest.mark.parametrize("agent", ["ReD_v1", "/checkout/unknown-key"])
async def test_unresolved_agent_without_project_issuer_keeps_exact_refusal(agent: str) -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard

    guard = CredentialGuard(ok, verifier=await issuer_verifier([]))
    with capture_logs() as logs:
        response = await request(guard, token=TOKEN, agent=agent)
    assert response.status_code == 403
    assert response.json() == {"error": "agent_mismatch"}
    assert len(logs) == 1
    assert logs[0]["event"] == "mcp_auth.refused"
    assert logs[0]["reason"] == "agent_mismatch"
    assert agent_unresolved_counts() == {}


async def test_raw_issuer_match_precedes_project_fallback() -> None:
    from starlette.responses import JSONResponse

    from brain_v42.mcp.credentials_http import CredentialGuard
    from brain_v42.provenance import get_current_actor

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"actor": get_current_actor()})(scope, receive, send)

    guard = CredentialGuard(app, verifier=await issuer_verifier(["@project", "agent:*"]))
    with capture_logs() as logs:
        response = await request(guard, token=TOKEN, agent="agent:worker")
    assert response.json() == {"actor": "agent:worker"}
    assert logs == []


@pytest.mark.parametrize(
    ("issuers", "agent", "project"),
    [
        (["@project"], "/checkout/brain_v42/.worktrees/topic", "brain-v42"),
        (["brain"], "brain", "brain-v42"),
        (["brain*"], "brain_v42", "brain-v42"),
        ([], None, None),
        (["operator"], "operator", None),
        (["@project"], "missing-project", None),
    ],
)
async def test_guard_stores_current_actor_project_for_every_authorization_pattern(
    issuers: list[str], agent: str | None, project: str | None
) -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert "brain_actor_project" in scope["state"]
        assert scope["state"]["brain_actor_project"] == project
        await ok(scope, receive, send)

    response = await request(
        CredentialGuard(app, verifier=await issuer_verifier(issuers)), token=TOKEN, agent=agent
    )
    assert response.status_code == 200


@pytest.mark.parametrize("error", [OSError, verifier_module.CredentialRefused])
async def test_project_observation_failure_does_not_refuse_authorized_request(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard

    core = await issuer_verifier(["brain"])
    calls: list[str] = []

    def unavailable(key: str) -> bool:
        calls.append(key)
        if error is verifier_module.CredentialRefused:
            raise verifier_module.CredentialRefused("registry_unavailable")
        raise error("unavailable")

    monkeypatch.setattr(core, "project_exists", unavailable)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert "brain_actor_project" in scope["state"]
        assert scope["state"]["brain_actor_project"] is None
        await ok(scope, receive, send)

    with capture_logs() as logs:
        response = await request(CredentialGuard(app, verifier=core), token=TOKEN, agent="brain")
    assert response.status_code == 200
    assert calls == ["brain-v42"]
    assert logs == []


@pytest.mark.parametrize("token,reason", [(None, "missing_token"), ("wrong", "unknown_token")])
async def test_refusal_does_not_read_body_and_emits_once(token: str | None, reason: str) -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard

    guard = CredentialGuard(ok, verifier=await verifier())

    async def unread() -> Message:
        raise AssertionError("refused requests must not read the body")

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [] if token is None else [(b"authorization", ("Bearer " + token).encode())],
        "client": ("127.0.0.1", 1234),
    }
    with capture_logs() as logs:
        await guard(scope, unread, send)
    assert sent[0]["status"] == 401
    assert (b"www-authenticate", b"Bearer") in sent[0]["headers"]
    assert logs == [
        {
            "event": "mcp_auth.refused",
            "log_level": "warning",
            "reason": reason,
            "status": 401,
            "peer": "127.0.0.1",
            "path": "/mcp",
        }
    ]
    assert refusal_counts() == {reason: 1}


async def test_registry_unavailable_returns_503_without_a_client_id() -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard

    with capture_logs() as logs:
        response = await request(
            CredentialGuard(ok, verifier=await verifier(refresh=False)), token=TOKEN
        )
    assert response.status_code == 503
    assert response.json() == {"error": "registry_unavailable"}
    assert logs == [
        {
            "event": "mcp_auth.refused",
            "log_level": "warning",
            "reason": "registry_unavailable",
            "status": 503,
            "peer": "127.0.0.1",
            "path": "/mcp",
        }
    ]
    assert refusal_counts() == {"registry_unavailable": 1}


async def test_guard_sets_identity_then_restores_request_context() -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard

    response = await request(CredentialGuard(ok, verifier=await verifier()), token=TOKEN)
    assert response.status_code == 200
    assert response.json() == {"client_id": "red-rail"}
    assert get_current_principal() is None


async def test_non_http_and_health_do_not_verify() -> None:
    from brain_v42.mcp.credentials_http import CredentialGuard

    guard = CredentialGuard(ok, verifier=await verifier(refresh=False))
    assert (await request(guard, path="/health")).status_code == 200
    called = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        called.append(scope["type"])

    await CredentialGuard(app, verifier=await verifier(refresh=False))(
        {"type": "lifespan"}, None, None
    )
    assert called == ["lifespan"]
