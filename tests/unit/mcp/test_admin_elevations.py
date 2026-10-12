"""Exercise the independently authenticated route without a database or an MCP session."""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
import structlog
from fastmcp import FastMCP

from brain_v42.credentials.reasons import refusal_counts, reset_refusal_counts
from brain_v42.credentials.verifier import CredentialRefused, VerifiedPrincipal
from brain_v42.mcp import server
from brain_v42.repositories.pg_client_credentials import ElevationRow, PgClientCredentialRepo
from tests.unit.mcp.test_credentials_http_wiring import settings
from tests.unit.test_credentials_cli import NOW, FakeRepo, elevation

SESSION_ID = UUID("00000000-0000-0000-0000-000000000003")
TOKEN = "synthetic-elevation-bearer"


@pytest.fixture(autouse=True)
def counters() -> Iterator[None]:
    from brain_v42.credentials.agent_unresolved import reset_agent_unresolved_counts
    from brain_v42.credentials.elevation_refusals import reset_elevation_refusal_counts

    reset_refusal_counts()
    reset_elevation_refusal_counts()
    reset_agent_unresolved_counts()
    yield
    reset_refusal_counts()
    reset_elevation_refusal_counts()
    reset_agent_unresolved_counts()


@pytest.fixture
def route(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakeRepo, Any]:
    from brain_v42.mcp.admin_elevations import AdminElevations

    repo = FakeRepo()
    principal = VerifiedPrincipal(
        "operator-hook", frozenset({"elevate"}), frozenset(), uuid4(), None
    )
    verifier = Mock(verify=AsyncMock(return_value=principal), time=[0.0])
    handler = AdminElevations(
        verifier=verifier,
        repository=lambda: repo,
        elevatable_client_ids=frozenset({"workstation-claude"}),
        clock=lambda: NOW,
        monotonic=lambda: verifier.time[0],
    )
    app = FastMCP("elevation-route")
    app.custom_route("/admin/elevations", methods=["POST"])(handler.handle)
    from brain_v42.mcp.credentials_http import CredentialGuard

    http_app = app.http_app()
    guarded = CredentialGuard(http_app, verifier=verifier)
    return guarded, repo, verifier


async def post(route: tuple[Any, FakeRepo, Any], **changes: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(route[0]), base_url="http://localhost"
    ) as client:
        return await client.post(
            "/admin/elevations",
            headers={"Authorization": "Bearer " + TOKEN},
            json={"session_id": str(SESSION_ID), "reason": "maintenance", **changes},
        )


async def test_grant_and_second_active_grant_use_hook_principal(route: Any) -> None:
    for _ in range(2):
        response = await post(route)
        assert response.status_code == 200
        assert response.json()["connection_count"] == 1
        assert response.json()["excluded_client_ids"] == ["red-rail"]
        assert response.json()["excluded_connection_count"] == 1
        assert response.json().keys() == {
            "elevation_id",
            "expires_at",
            "connection_count",
            "excluded_client_ids",
            "excluded_connection_count",
        }
    assert len(route[1].calls) == 2
    for _, call in route[1].calls:
        assert call["via"] == "hook"
        assert call["requested_by_client_id"] == "operator-hook"
        assert call["granted_by"] == "operator-hook"
        assert call["expires_at"] == NOW + timedelta(hours=1)
        assert call["session"] is route[1].session
        assert call["session_id"] == SESSION_ID
    assert route[2].verify.await_count == 2  # The guard did not authenticate again.


@pytest.mark.parametrize(
    "reason,status",
    [
        ("missing_token", 401),
        ("unknown_token", 401),
        ("expired_token", 401),
        ("revoked_token", 401),
        ("registry_unavailable", 503),
        ("family_denied", 403),
    ],
)
async def test_transport_refusals_count_only_auth(route: Any, reason: str, status: int) -> None:
    if reason == "family_denied":
        route[2].verify.return_value = VerifiedPrincipal(
            "reader", frozenset({"read"}), frozenset(), uuid4(), None
        )
    else:
        route[2].verify.side_effect = CredentialRefused(reason)
    response = await post(route)
    assert response.status_code == status
    assert response.json() == {"error": reason}
    assert refusal_counts() == {reason: 1}
    assert route[1].calls == []


@pytest.mark.parametrize(
    "case,reason,status",
    [
        ("agent", "session_not_operator", 409),
        ("closed", "session_not_open", 409),
        ("unknown", "session_not_open", 409),
        ("empty", "no_attributed_connection", 409),
        ("legacy", "no_attributed_connection", 409),
        ("excluded", "no_elevatable_connection", 409),
        ("ttl", "invalid_window", 422),
    ],
)
async def test_business_refusals_log_and_count_once(
    route: Any, case: str, reason: str, status: int
) -> None:
    from brain_v42.credentials.elevation_refusals import elevation_refusal_counts

    session = route[1].session
    if case == "agent":
        session.owner["nature"] = "agent"
    elif case == "closed":
        session.owner["status"] = "ended"
    elif case == "unknown":
        session.owner = {}
    elif case == "empty":
        session.links = []
    elif case == "legacy":
        session.links = [session.links[2]]
    elif case == "excluded":
        session.links = [session.links[1]]
    with structlog.testing.capture_logs() as logs:
        response = await post(route, **({"ttl_seconds": 14401} if case == "ttl" else {}))
    assert response.status_code == status
    assert response.json() == {"error": reason}
    assert logs == [
        {
            "event": "elevation.refused",
            "log_level": "warning",
            "reason": reason,
            "status": status,
            "session_id": str(SESSION_ID),
            "requesting_client_id": "operator-hook",
            "peer": "127.0.0.1",
        }
    ]
    assert elevation_refusal_counts() == {reason: 1}
    assert refusal_counts() == {}
    assert route[1].calls == []


async def test_burst_is_per_principal_and_recovers_after_ten_seconds(route: Any) -> None:
    for _ in range(3):
        assert (await post(route)).status_code == 200
    assert (await post(route)).json() == {"error": "rate_limited"}
    assert refusal_counts() == {"rate_limited": 1}
    route[2].time[0] = 10.0
    assert (await post(route)).status_code == 200


async def test_bucket_is_shared_by_rotated_credentials_but_not_other_clients(route: Any) -> None:
    for _ in range(3):
        assert (await post(route)).status_code == 200
    route[2].verify.return_value = VerifiedPrincipal(
        "operator-hook", frozenset({"elevate"}), frozenset(), uuid4(), None
    )
    assert (await post(route)).status_code == 429
    route[2].verify.return_value = VerifiedPrincipal(
        "another-hook", frozenset({"elevate"}), frozenset(), uuid4(), None
    )
    assert (await post(route)).status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": ""},
        {"reason": " "},
        {"reason": "x" * 201},
        {"session_id": "invalid"},
        {"reason": 1},
        {"unexpected": True},
    ],
)
async def test_invalid_body_never_grants(route: Any, changes: Any) -> None:
    assert (await post(route, **changes)).status_code == 422
    assert route[1].calls == []


async def test_body_is_bounded_before_json_parsing(route: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(route[0]), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/admin/elevations",
            headers={"Authorization": "Bearer " + TOKEN},
            content=b"x" * 4097,
        )
    assert response.status_code == 413
    assert route[1].calls == []


@pytest.mark.parametrize("family_denied", [False, True])
async def test_authentication_and_family_denial_precede_body_read(
    route: Any, family_denied: bool
) -> None:
    reads: list[bool] = []

    async def body() -> Any:
        reads.append(True)
        yield b"untrusted body"

    headers = {}
    if family_denied:
        headers["Authorization"] = "Bearer " + TOKEN
        route[2].verify.return_value = VerifiedPrincipal(
            "reader", frozenset({"read"}), frozenset(), uuid4(), None
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(route[0]), base_url="http://localhost"
    ) as client:
        response = await client.post("/admin/elevations", headers=headers, content=body())
    assert response.status_code == (403 if family_denied else 401)
    assert reads == []
    assert route[1].calls == []


async def test_repository_failure_is_masked_as_transport_unavailable(route: Any) -> None:
    route[1].error = RuntimeError("private database parameters")
    response = await post(route)
    assert response.status_code == 503
    assert response.json() == {"error": "registry_unavailable"}
    assert refusal_counts() == {"registry_unavailable": 1}


@pytest.mark.parametrize("mode,expected", [("credentials", 200), ("shared_token", 404)])
async def test_route_is_registered_only_in_credentials_mode(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected: int,
) -> None:
    repo = FakeRepo()
    repo.grant_elevation = AsyncMock(return_value=elevation())
    monkeypatch.setattr(server, "PgClientCredentialRepo", lambda factory: repo)
    monkeypatch.setattr(server, "get_session_factory", Mock())
    verifier = Mock(
        verify=AsyncMock(
            return_value=VerifiedPrincipal(
                "operator-hook", frozenset({"elevate"}), frozenset(), uuid4(), None
            )
        )
    )
    config = settings()
    config.brain_mcp_auth_mode = mode
    if mode == "shared_token":
        config.mcp_http_token = TOKEN
    mcp = FastMCP(mode)
    plan = server.plan_http_transport(mcp, config, credential_verifier=verifier)
    app = mcp.http_app(
        middleware=plan.middleware,
        stateless_http=plan.stateless_http,
        json_response=plan.json_response,
    )
    async with app.router.lifespan_context(app):
        assert (await post((app, repo, verifier))).status_code == expected


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/admin/elevations"),
        ("POST", "/admin/elevations/"),
        ("POST", "/admin/elevations/extra"),
    ],
)
async def test_guard_exemption_is_exact(route: Any, method: str, path: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(route[0]), base_url="http://localhost"
    ) as client:
        response = await client.request(method, path)
    assert response.status_code == 401
    assert refusal_counts() == {"missing_token": 1}


async def test_real_repository_commits_one_hook_audit_row_with_the_frozen_pairs() -> None:
    from brain_v42.credentials.verifier import CredentialVerifier
    from brain_v42.mcp.admin_elevations import AdminElevations
    from brain_v42.mcp.credentials_http import CredentialGuard
    from tests.unit.test_credentials_cli import credential

    session = FakeRepo().session
    audit: list[Any] = []

    async def execute(statement: Any) -> Any:
        result = Mock()
        if "FROM brain_sessions" in str(statement):
            result.mappings.return_value.one_or_none.return_value = session.owner
            result.one_or_none.return_value = SimpleNamespace(
                **session.owner, client_key="operator-session"
            )
        elif "FROM brain_session_connections" in str(statement):
            result.mappings.return_value.all.return_value = session.links
            result.tuples.return_value.all.return_value = [
                (row["connection_id"], row["client_id"]) for row in session.links
            ]
        elif statement.table.name == "brain_admin_elevations":
            row = ElevationRow(
                id=uuid4(),
                revoked_at=None,
                expiry_audited_at=None,
                **statement.compile().params,
            )
            result.mappings.return_value.one.return_value = asdict(row)
        elif statement.table.name == "brain_credential_audit":
            audit.append(statement.compile().params)
        return result

    session.execute = execute
    repo = PgClientCredentialRepo()

    @asynccontextmanager
    async def transaction() -> Any:
        yield session

    repo.transaction = transaction
    registry = Mock(
        active_rows=AsyncMock(
            return_value=[
                credential(
                    client_id="operator-hook",
                    families=["elevate"],
                    token_sha256=sha256(TOKEN.encode()).digest(),
                )
            ]
        )
    )
    verifier = CredentialVerifier(registry, clock=lambda: NOW, monotonic=lambda: 0.0)
    assert await verifier.refresh()
    handler = AdminElevations(
        verifier=verifier,
        repository=lambda: repo,
        elevatable_client_ids={"workstation-claude"},
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )
    mcp = FastMCP("repository-audit")
    mcp.custom_route("/admin/elevations", methods=["POST"])(handler.handle)
    app = CredentialGuard(mcp.http_app(), verifier=verifier)
    for index in range(2):
        response = await post((app, repo, verifier))
        assert response.status_code == 200
        assert len(audit) == index + 1
        assert audit[index]["event"] == "credentials.elevated"
        assert audit[index]["payload"]["via"] == "hook"
        assert audit[index]["payload"]["client_id"] == "operator-hook"
        assert audit[index]["payload"]["connection_count"] == 1
        assert audit[index]["payload"]["excluded_client_ids"] == ["red-rail"]
        assert TOKEN not in str(audit)
        assert "connection_id" not in str(audit)
