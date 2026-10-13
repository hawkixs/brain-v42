"""Read admission protects collectors without consuming telemetry admission capacity."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict
from structlog.dev import ConsoleRenderer
from structlog.testing import capture_logs

from brain_v42.config import Settings
from brain_v42.credentials import verifier as verifier_module
from brain_v42.credentials.bucket import TokenBucket
from brain_v42.credentials.verifier import CredentialVerifier
from brain_v42.metrics.brain_graph_server import BrainGraphMetricsServer
from brain_v42.metrics.runtime import build_metrics_runtime
from brain_v42.metrics.slow_block_cache import SlowBlockCache
from tests.unit.credentials.test_verifier import Clock, Repo, row
from tests.unit.metrics.test_metrics_exclude import _collector
from tests.unit.test_metrics_receiver_rejection_counters import _request, _stream, _transport

TOKEN = "test-only-metrics-reader"
CLIENT = "red-monitor"
ROUTES = (
    ("/api/session-slots/v1", "_handle_session_slots"),
    ("/api/brain-graph/v1", "_handle_brain_graph"),
)


@pytest.fixture
async def server(monkeypatch: pytest.MonkeyPatch) -> BrainGraphMetricsServer:
    monkeypatch.setattr(verifier_module, "_last_lookup_at", None)
    clock = Clock()
    repo = Repo()
    repo.rows = [replace(row(TOKEN), client_id=CLIENT, families=["telemetry"])]
    core = CredentialVerifier(repo, clock=clock.utc, monotonic=clock.monotonic)
    await core.refresh()
    projection = AsyncMock()
    projection.snapshot.return_value = {
        "schema_version": "brain-graph.v1",
        "nodes": [],
        "edges": [],
    }
    result = BrainGraphMetricsServer(
        _collector(),
        MagicMock(healthcheck=AsyncMock(return_value=True)),
        graph_projection_svc=projection,
        receiver_auth="credentials",
        credential_verifier=core,
        receiver_bucket=TokenBucket(10, 20, monotonic=clock.monotonic),
        metrics_read_clients=[CLIENT],
        read_bucket=TokenBucket(10, 20, monotonic=clock.monotonic),
        slow_block_cache=SlowBlockCache(ttl_seconds=30, error_ttl_seconds=5, clock=clock.monotonic),
    )
    result.test_repo = repo
    result.test_clock = clock
    return result


async def read(
    server: BrainGraphMetricsServer,
    path: str,
    handler: str,
    *,
    headers: Any = None,
    peer: str = "203.0.113.9",
) -> web.Response:
    return await getattr(server, handler)(
        make_mocked_request("GET", path, headers=headers, transport=_transport(peer))
    )


@pytest.mark.parametrize(("path", "handler"), ROUTES)
async def test_missing_header_refuses_before_verifier_and_collectors(
    server: BrainGraphMetricsServer, path: str, handler: str
) -> None:
    server._credential_verifier.verify = AsyncMock()
    response = await read(server, path, handler)
    assert response.status == 401
    assert json.loads(response.body) == {"reason": "missing_token"}
    server._credential_verifier.verify.assert_not_awaited()
    server._collector.collect_session_slots.assert_not_awaited()
    server._graph_projection_svc.snapshot.assert_not_awaited()


@pytest.mark.parametrize(("path", "handler"), ROUTES)
@pytest.mark.parametrize(
    ("policy", "reason", "status"),
    [
        ("unknown", "unknown_token", 401),
        ("outside", "client_not_allowed", 403),
        ("empty", "client_not_allowed", 403),
        ("family", "family_denied", 403),
    ],
)
async def test_verified_reader_needs_allowlist_and_telemetry(
    server: BrainGraphMetricsServer, path: str, handler: str, policy: str, reason: str, status: int
) -> None:
    token = TOKEN
    if policy == "unknown":
        token = "test-only-unknown-reader"
    elif policy == "outside":
        server._metrics_read_clients = frozenset({"another-client"})
    elif policy == "empty":
        server._metrics_read_clients = frozenset()
    else:
        server.test_repo.rows[0] = replace(server.test_repo.rows[0], families=["read"])
        await server._credential_verifier.refresh()
    response = await read(server, path, handler, headers={"Authorization": "Bearer " + token})
    assert response.status == status
    assert json.loads(response.body) == {"reason": reason}
    server._collector.collect_session_slots.assert_not_awaited()
    server._graph_projection_svc.snapshot.assert_not_awaited()


@pytest.mark.parametrize(("path", "handler"), ROUTES)
async def test_allowed_bearer_reads_snapshot(
    server: BrainGraphMetricsServer, path: str, handler: str
) -> None:
    with capture_logs() as logs:
        response = await read(server, path, handler, headers={"Authorization": "bEaReR " + TOKEN})
    assert response.status == 200
    assert json.loads(response.body)
    assert {
        "event": "metrics.read_admitted",
        "log_level": "debug",
        "route": path,
        "client_id": CLIENT,
    } in logs


@pytest.mark.parametrize("first", ["metrics", "slots"])
async def test_session_slots_share_one_computation_with_metrics(
    server: BrainGraphMetricsServer, first: str
) -> None:
    requests = [("/metrics", "_handle_metrics"), ROUTES[0]]
    if first == "slots":
        requests.reverse()
    responses = [await read(server, path, handler, peer="127.0.0.1") for path, handler in requests]
    assert all(response.status == 200 for response in responses)
    assert all("session_slots" in json.loads(response.body) for response in responses)
    server._collector.collect_session_slots.assert_awaited_once_with()


async def test_session_slots_failed_read_recovers_after_error_ttl(
    server: BrainGraphMetricsServer,
) -> None:
    server._collector.collect_session_slots.side_effect = [
        RuntimeError("unavailable"),
        {"projects": []},
    ]
    for _ in range(2):
        response = await read(server, *ROUTES[0], peer="127.0.0.1")
        assert response.status == 200
        assert json.loads(response.body) == {}
    server._collector.collect_session_slots.assert_awaited_once_with()
    server.test_clock.advance(5.01)
    response = await read(server, *ROUTES[0], peer="127.0.0.1")
    assert json.loads(response.body)["session_slots"]["projects"] == []
    assert server._collector.collect_session_slots.await_count == 2


@pytest.mark.parametrize(("path", "handler"), ROUTES)
async def test_loopback_without_header_is_admitted(
    server: BrainGraphMetricsServer, path: str, handler: str
) -> None:
    server._credential_verifier.verify = AsyncMock()
    assert (await read(server, path, handler, peer="::1")).status == 200
    server._credential_verifier.verify.assert_not_awaited()


@pytest.mark.parametrize(("path", "handler"), ROUTES)
async def test_loopback_bad_bearer_is_checked(
    server: BrainGraphMetricsServer, path: str, handler: str
) -> None:
    response = await read(
        server, path, handler, peer="127.0.0.1", headers={"Authorization": "Bearer unknown"}
    )
    assert response.status == 401
    assert json.loads(response.body) == {"reason": "unknown_token"}


MALFORMED = [
    [("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)],
    [("Authorization", "Basic " + TOKEN)],
    [("Authorization", "")],
    [("Authorization", "Bearer")],
    [("Authorization", "Bearer ")],
    [("Authorization", "Bearer    ")],
]


@pytest.mark.parametrize(("path", "handler"), ROUTES)
@pytest.mark.parametrize("headers", MALFORMED)
@pytest.mark.parametrize("peer", ["127.0.0.1", "203.0.113.9"])
async def test_malformed_header_precedes_verification(
    server: BrainGraphMetricsServer, path: str, handler: str, headers: Any, peer: str
) -> None:
    server._credential_verifier.verify = AsyncMock()
    response = await read(server, path, handler, headers=CIMultiDict(headers), peer=peer)
    assert response.status == 401
    assert json.loads(response.body) == {"reason": "malformed_authorization"}
    server._credential_verifier.verify.assert_not_awaited()


@pytest.mark.parametrize(("path", "handler"), ROUTES)
async def test_forwarded_headers_do_not_admit_foreign_transport(
    server: BrainGraphMetricsServer, path: str, handler: str
) -> None:
    response = await read(
        server,
        path,
        handler,
        headers={"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"},
    )
    assert response.status == 401


@pytest.mark.parametrize(("path", "handler"), ROUTES)
@pytest.mark.parametrize("state", ["cold", "stale"])
async def test_unavailable_registry_refuses_well_formed_bearer(
    server: BrainGraphMetricsServer, path: str, handler: str, state: str
) -> None:
    if state == "cold":
        server._credential_verifier = CredentialVerifier(
            server.test_repo, clock=server.test_clock.utc, monotonic=server.test_clock.monotonic
        )
    else:
        server.test_clock.advance(90.01)
    server.test_repo.fail = True
    response = await read(server, path, handler, headers={"Authorization": "Bearer " + TOKEN})
    assert response.status == 503
    assert json.loads(response.body) == {"reason": "registry_unavailable"}
    assert response.headers["Retry-After"] == "1"


@pytest.mark.parametrize(("path", "handler"), ROUTES)
@pytest.mark.parametrize("state", ["cold", "stale"])
@pytest.mark.parametrize("header", [None, "Basic secret"])
async def test_local_header_refusals_precede_unavailable_registry(
    server: BrainGraphMetricsServer, path: str, handler: str, state: str, header: str | None
) -> None:
    if state == "cold":
        server._credential_verifier = CredentialVerifier(
            server.test_repo, clock=server.test_clock.utc, monotonic=server.test_clock.monotonic
        )
    else:
        server.test_clock.advance(90.01)
    server.test_repo.fail = True
    response = await read(
        server, path, handler, headers={} if header is None else {"Authorization": header}
    )
    assert response.status == 401
    assert json.loads(response.body) == {
        "reason": "missing_token" if header is None else "malformed_authorization"
    }
    assert server.test_repo.loads == 1
    assert server.test_repo.lookups == 0


@pytest.mark.parametrize(("path", "handler"), ROUTES)
@pytest.mark.parametrize("revoked", [False, True])
async def test_outage_serves_fresh_snapshot_until_ninety_seconds(
    server: BrainGraphMetricsServer, path: str, handler: str, revoked: bool
) -> None:
    if revoked:
        server.test_repo.rows[0] = replace(
            server.test_repo.rows[0], revoked_at=server.test_clock.now
        )
    server.test_repo.fail = True
    server.test_clock.advance(89)
    await server._credential_verifier.refresh()
    headers = {"Authorization": "Bearer " + TOKEN}
    assert (await read(server, path, handler, headers=headers)).status == 200
    server.test_clock.advance(1.01)
    response = await read(server, path, handler, headers=headers)
    assert response.status == 503
    assert response.headers["Retry-After"] == "1"


@pytest.mark.parametrize(("path", "handler"), ROUTES)
@pytest.mark.parametrize("outage", [False, True])
async def test_expiration_is_checked_without_refresh(
    server: BrainGraphMetricsServer, path: str, handler: str, outage: bool
) -> None:
    server.test_repo.rows[0] = replace(
        server.test_repo.rows[0], expires_at=server.test_clock.now + timedelta(seconds=1)
    )
    await server._credential_verifier.refresh()
    server.test_repo.fail = outage
    loads = server.test_repo.loads
    server.test_clock.advance(2)
    response = await read(server, path, handler, headers={"Authorization": "Bearer " + TOKEN})
    assert response.status == 401
    assert json.loads(response.body) == {"reason": "expired_token"}
    assert server.test_repo.loads == loads


@pytest.mark.parametrize(("path", "handler"), ROUTES)
async def test_revocation_is_refused_after_refresh(
    server: BrainGraphMetricsServer, path: str, handler: str
) -> None:
    server.test_repo.rows[0] = replace(server.test_repo.rows[0], revoked_at=server.test_clock.now)
    await server._credential_verifier.notify()
    response = await read(server, path, handler, headers={"Authorization": "Bearer " + TOKEN})
    assert response.status == 401
    assert json.loads(response.body) == {"reason": "revoked_token"}


@pytest.mark.parametrize("exhausted", ["read", "receiver"])
async def test_read_and_receiver_buckets_are_independent(
    server: BrainGraphMetricsServer, exhausted: str
) -> None:
    headers = {"Authorization": "Bearer " + TOKEN}
    for index in range(20):
        if exhausted == "read":
            assert (await read(server, *ROUTES[index % 2], headers=headers)).status == 200
        else:
            response = await server._handle_codex_logs(
                _request("/v1/logs", headers=headers, payload=_stream(data=b'{"resourceLogs":[]}'))
            )
            assert response.status == 200
    if exhausted == "read":
        response = await read(server, *ROUTES[0], headers=headers)
        assert response.status == 429
        assert response.headers["Retry-After"] == "1"
        assert json.loads(response.body) == {"reason": "rate_limited"}
        assert (
            await server._handle_codex_logs(
                _request("/v1/logs", headers=headers, payload=_stream(data=b'{"resourceLogs":[]}'))
            )
        ).status == 200
    else:
        assert (
            await server._handle_codex_logs(_request("/v1/logs", headers=headers))
        ).status == 429
        assert (await read(server, *ROUTES[0], headers=headers)).status == 200


@pytest.mark.parametrize(("path", "handler"), ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        None,
        {"Authorization": "Bearer " + TOKEN},
        CIMultiDict(MALFORMED[0]),
        {"Authorization": "Basic secret"},
    ],
)
async def test_without_verifier_foreign_peer_precedes_header_checks(
    server: BrainGraphMetricsServer, path: str, handler: str, headers: Any
) -> None:
    server._credential_verifier = None
    server._receiver_auth = "none"
    response = await read(server, path, handler, headers=headers)
    assert response.status == 403
    assert json.loads(response.body) == {"error": "loopback peer required"}
    server._collector.collect_session_slots.assert_not_awaited()
    server._graph_projection_svc.snapshot.assert_not_awaited()


@pytest.mark.parametrize(("path", "handler"), ROUTES)
@pytest.mark.parametrize("header", [None, "Bearer " + TOKEN, "Basic secret", ""])
async def test_without_verifier_loopback_checks_present_headers(
    server: BrainGraphMetricsServer, path: str, handler: str, header: str | None
) -> None:
    server._credential_verifier = None
    server._receiver_auth = "none"
    response = await read(
        server,
        path,
        handler,
        peer="127.0.0.1",
        headers={} if header is None else {"Authorization": header},
    )
    assert response.status == (200 if header is None else 401)


@pytest.mark.parametrize(("path", "handler"), ROUTES)
@pytest.mark.parametrize(
    "reason",
    [
        "loopback_required",
        "malformed_authorization",
        "missing_token",
        "unknown_token",
        "expired_token",
        "revoked_token",
        "registry_unavailable",
        "client_not_allowed",
        "family_denied",
        "rate_limited",
    ],
)
async def test_refusals_are_logged_without_secrets_or_receiver_observations(
    server: BrainGraphMetricsServer, path: str, handler: str, reason: str
) -> None:
    headers: Any = {"Authorization": "Bearer " + TOKEN}
    if reason == "loopback_required":
        server._credential_verifier = None
    elif reason == "malformed_authorization":
        headers = {"Authorization": "Basic " + TOKEN}
    elif reason == "missing_token":
        headers = {}
    elif reason == "unknown_token":
        headers = {"Authorization": "Bearer test-only-unknown-reader"}
    elif reason in {"expired_token", "revoked_token"}:
        field = "expires_at" if reason == "expired_token" else "revoked_at"
        server.test_repo.rows[0] = replace(
            server.test_repo.rows[0], **{field: server.test_clock.now}
        )
        await server._credential_verifier.notify()
    elif reason == "registry_unavailable":
        server.test_clock.advance(91)
    elif reason == "client_not_allowed":
        server._metrics_read_clients = frozenset()
    elif reason == "family_denied":
        server.test_repo.rows[0] = replace(server.test_repo.rows[0], families=["read"])
        await server._credential_verifier.refresh()
    else:
        for _ in range(20):
            assert server._read_bucket.allow(CLIENT)
    counts = server._rejection_counters.snapshot()
    refused = dict(server._receiver_refused)
    with capture_logs() as logs:
        response = await read(server, path, handler, headers=headers)
    assert response.status >= 400
    events = [event for event in logs if event["event"] == "metrics.read_refused"]
    assert len(events) == 1
    assert events[0]["route"] == path
    assert events[0]["reason"] == reason
    if reason in {"client_not_allowed", "family_denied", "rate_limited"}:
        assert events[0]["client_id"] == CLIENT
    rendered = ConsoleRenderer(colors=False)(None, "warning", dict(events[0]))
    for secret in (TOKEN, "test-only-unknown-reader", "Authorization", "Bearer", "Basic"):
        assert secret not in json.dumps(logs)
        assert secret not in rendered
    assert server._rejection_counters.snapshot() == counts
    assert server._receiver_refused == refused


@pytest.mark.parametrize("auth", ["none", "credentials"])
def test_both_read_routes_are_always_registered(auth: str) -> None:
    server = BrainGraphMetricsServer(
        MagicMock(),
        MagicMock(),
        graph_projection_svc=AsyncMock(),
        receiver_auth=auth,
        credential_verifier=AsyncMock() if auth == "credentials" else None,
        host="203.0.113.9",
        allow_non_loopback=True,
    )
    routes = {
        route.resource.canonical
        for route in server._build_app().router.routes()
        if route.method == "GET"
    }
    assert {path for path, _ in ROUTES} <= routes


async def test_environment_allowlist_reaches_runtime_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("METRICS_READ_CLIENTS", CLIENT)
    settings = Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:1/brain_test",
        metrics_receiver_auth="credentials",
        metrics_legacy_automation_enabled=False,
        graph_enabled=False,
        _env_file=None,
    )
    runtime = build_metrics_runtime(settings=settings, engine=MagicMock())
    server = runtime._resources.server_factory(None, None)
    principal = MagicMock(client_id=CLIENT, families=frozenset({"telemetry"}))
    server._credential_verifier.verify = AsyncMock(return_value=principal)
    server._collector.collect_session_slots = AsyncMock(return_value={"projects": []})
    assert (
        await read(server, *ROUTES[0], headers={"Authorization": "Bearer " + TOKEN})
    ).status == 200


@pytest.mark.parametrize(("path", "handler"), ROUTES)
async def test_default_allowlist_refuses_every_verified_reader(path: str, handler: str) -> None:
    verifier = AsyncMock()
    verifier.verify.return_value = MagicMock(client_id=CLIENT, families=frozenset({"telemetry"}))
    server = BrainGraphMetricsServer(
        _collector(),
        MagicMock(),
        graph_projection_svc=AsyncMock(),
        receiver_auth="credentials",
        credential_verifier=verifier,
    )
    response = await read(server, path, handler, headers={"Authorization": "Bearer " + TOKEN})
    assert response.status == 403
    assert json.loads(response.body) == {"reason": "client_not_allowed"}
