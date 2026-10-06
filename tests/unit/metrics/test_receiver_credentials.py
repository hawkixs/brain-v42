"""Authenticate push receivers before trusting representation or reading payloads."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request
from pydantic import ValidationError
from structlog.testing import capture_logs

from brain_v42.config import Settings
from brain_v42.credentials import verifier as verifier_module
from brain_v42.credentials.bucket import TokenBucket
from brain_v42.credentials.verifier import CredentialVerifier
from brain_v42.metrics.server import MetricsServer
from tests.unit.credentials.test_verifier import Clock, Repo, row
from tests.unit.test_metrics_receiver_rejection_counters import _request, _stream, _transport

TOKEN = "test-only-telemetry-credential"
ROUTES = (
    ("/v1/logs", "_handle_codex_logs", "codex_logs"),
    ("/v1/logs/claude", "_handle_claude_logs", "claude_logs"),
    ("/v1/client-activity", "_handle_client_activity", "client_activity"),
)


def settings(**values: Any) -> Settings:
    return Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:1/brain_test",
        _env_file=None,
        **values,
    )


def test_receiver_auth_default_and_closed_vocabulary() -> None:
    assert settings().metrics_receiver_auth == "none"
    assert settings(metrics_receiver_auth="credentials").metrics_receiver_auth == "credentials"
    with pytest.raises(ValidationError, match="metrics_receiver_auth|METRICS_RECEIVER_AUTH"):
        settings(metrics_receiver_auth="shared-token")


def test_credentials_do_not_bypass_the_non_loopback_bind_opt_in() -> None:
    with pytest.raises(ValidationError, match="METRICS_ALLOW_NON_LOOPBACK"):
        settings(metrics_host="203.0.113.1", metrics_receiver_auth="credentials")
    assert (
        settings(
            metrics_host="203.0.113.1",
            metrics_allow_non_loopback=True,
            metrics_receiver_auth="credentials",
        ).metrics_receiver_auth
        == "credentials"
    )


async def make_server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client_id: str = "brain-v42-mcp",
    families: list[str] | None = None,
) -> tuple[MetricsServer, Repo, Clock]:
    monkeypatch.setattr(verifier_module, "_last_lookup_at", None)
    clock = Clock()
    repo = Repo()
    repo.rows.append(replace(row(TOKEN), client_id=client_id, families=families or ["telemetry"]))
    core = CredentialVerifier(repo, clock=clock.utc, monotonic=clock.monotonic)
    await core.refresh()
    server = MetricsServer(
        MagicMock(),
        MagicMock(),
        host="203.0.113.1",
        allow_non_loopback=True,
        receiver_auth="credentials",
        credential_verifier=core,
        receiver_bucket=TokenBucket(10, 20, monotonic=clock.monotonic),
    )
    return server, repo, clock


def test_credentials_configuration_requires_a_verifier() -> None:
    with pytest.raises(ValueError, match="verifier"):
        MetricsServer(MagicMock(), MagicMock(), receiver_auth="credentials")


@pytest.mark.parametrize(("path", "handler", "receiver"), ROUTES)
@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("missing_token", 401),
        ("unknown_token", 401),
        ("revoked_token", 401),
        ("expired_token", 401),
        ("registry_unavailable", 503),
    ],
)
async def test_core_refusals_precede_body_and_representation_checks(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    handler: str,
    receiver: str,
    reason: str,
    status: int,
) -> None:
    server, repo, clock = await make_server(monkeypatch)
    legacy_counts = server._rejection_counters.snapshot()
    headers = {"Content-Type": "text/plain", "Content-Length": "99999999"}
    if reason != "missing_token":
        headers["Authorization"] = "Bearer " + TOKEN
    if reason == "unknown_token":
        headers["Authorization"] = "Bearer test-only-unknown"
    elif reason == "revoked_token":
        repo.rows[0] = replace(repo.rows[0], revoked_at=clock.now)
        await server._credential_verifier.notify()
    elif reason == "expired_token":
        repo.rows[0] = replace(repo.rows[0], expires_at=clock.now + timedelta(seconds=1))
        await server._credential_verifier.notify()
        clock.advance(2)
    elif reason == "registry_unavailable":
        clock.advance(91)
    payload = MagicMock()
    payload.iter_chunked.side_effect = AssertionError("must not read an unauthorised body")
    with capture_logs() as logs:
        response = await getattr(server, handler)(_request(path, headers=headers, payload=payload))
    assert response.status == status
    assert json.loads(response.body)["reason"] == reason
    assert server._receiver_refused == {reason: 1}
    assert server._rejection_counters.snapshot() == legacy_counts
    assert logs == [
        {
            "event": "metrics_receiver.refused",
            "receiver": receiver,
            "reason": reason,
            "status": status,
            "log_level": "warning",
        }
    ]
    assert TOKEN not in json.dumps(logs)
    payload.iter_chunked.assert_not_called()


@pytest.mark.parametrize(("path", "handler", "receiver"), ROUTES)
async def test_telemetry_family_is_required_on_every_route(
    monkeypatch: pytest.MonkeyPatch, path: str, handler: str, receiver: str
) -> None:
    server, _, _ = await make_server(monkeypatch, families=["read", "write"])
    legacy_counts = server._rejection_counters.snapshot()
    response = await getattr(server, handler)(
        _request(path, headers={"Authorization": "Bearer " + TOKEN})
    )
    assert response.status == 403
    assert server._receiver_refused == {"family_denied": 1}
    assert server._rejection_counters.snapshot() == legacy_counts


async def test_activity_route_restricts_principal_without_changing_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _, _ = await make_server(monkeypatch, client_id="workstation-otel")
    legacy_counts = server._rejection_counters.snapshot()
    response = await server._handle_client_activity(
        _request(
            "/v1/client-activity",
            headers={"Authorization": "Bearer " + TOKEN, "X-Brain-Agent": "brain-v42-mcp"},
        )
    )
    assert response.status == 403
    assert server._receiver_refused == {"client_not_allowed": 1}
    assert server._rejection_counters.snapshot() == legacy_counts


@pytest.mark.parametrize(("path", "handler", "receiver"), ROUTES)
async def test_authenticated_foreign_peer_can_ingest(
    monkeypatch: pytest.MonkeyPatch, path: str, handler: str, receiver: str
) -> None:
    server, _, _ = await make_server(monkeypatch)
    body = (
        b'{"observations":[{"actor":"codex","calls":1}]}'
        if receiver == "client_activity"
        else b'{"resourceLogs":[]}'
    )
    response = await getattr(server, handler)(
        _request(
            path,
            headers={"Authorization": "Bearer " + TOKEN},
            transport=_transport("203.0.113.5"),
            payload=_stream(data=body),
        )
    )
    assert response.status == 200
    assert server._receiver_refused == {}
    assert all(not counts for counts in server._rejection_counters.snapshot().values())


async def test_bucket_is_shared_by_routes_and_credentials_of_the_same_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, repo, clock = await make_server(monkeypatch)
    legacy_counts = server._rejection_counters.snapshot()
    other_token = "test-only-rotated-telemetry"
    repo.rows.append(replace(row(other_token), client_id="brain-v42-mcp", families=["telemetry"]))
    await server._credential_verifier.notify()
    for index in range(21):
        path, handler, _ = ROUTES[index % 2]
        token = TOKEN if index % 2 else other_token
        response = await getattr(server, handler)(
            _request(
                path,
                headers={"Authorization": "Bearer " + token},
                payload=_stream(data=b'{"resourceLogs":[]}'),
            )
        )
        assert response.status == (200 if index < 20 else 429)
    assert server._receiver_refused == {"rate_limited": 1}
    assert server._rejection_counters.snapshot() == legacy_counts
    clock.advance(0.1)
    response = await server._handle_codex_logs(
        _request(
            "/v1/logs",
            headers={"Authorization": "Bearer " + TOKEN},
            payload=_stream(data=b'{"resourceLogs":[]}'),
        )
    )
    assert response.status == 200


@pytest.mark.parametrize(
    ("headers", "body", "status", "reason"),
    [
        ({"Content-Type": "text/plain"}, b"", 415, "unsupported_representation"),
        ({"Content-Length": "99999999"}, b"", 413, "payload_too_large"),
        ({}, b"invalid JSON", 400, "invalid_payload"),
    ],
)
async def test_body_refusals_keep_legacy_counters_and_log_once(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], body: bytes, status: int, reason: str
) -> None:
    server, _, _ = await make_server(monkeypatch)
    with capture_logs() as logs:
        response = await server._handle_codex_logs(
            _request(
                "/v1/logs",
                headers={"Authorization": "Bearer " + TOKEN, **headers},
                payload=_stream(data=body),
            )
        )
    assert response.status == status
    assert server._receiver_refused == {reason: 1}
    assert server._rejection_counters.snapshot()["codex_logs"] == {str(status): 1}
    assert len([entry for entry in logs if entry["event"] == "metrics_receiver.refused"]) == 1


async def test_saturation_is_counted_separately_from_registry_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _, _ = await make_server(monkeypatch)
    while not server._codex_request_slots.locked():
        await server._codex_request_slots.acquire()
    with capture_logs() as logs:
        response = await server._handle_codex_logs(
            _request("/v1/logs", headers={"Authorization": "Bearer " + TOKEN})
        )
    assert response.status == 503
    assert server._receiver_refused == {"receiver_busy": 1}
    assert server._rejection_counters.snapshot()["codex_logs"] == {"503": 1}
    assert sum(entry["event"] == "metrics_receiver.refused" for entry in logs) == 1


async def test_public_read_routes_and_structural_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    server, _, _ = await make_server(monkeypatch)
    app = server._build_app()
    routes = {route.resource.canonical for route in app.router.routes()}
    assert {"/metrics", "/healthz", "/api/cockpit", *(r[0] for r in ROUTES)} <= routes
    health = await server._handle_healthz(make_mocked_request("GET", "/healthz"))
    assert json.loads(health.body)["ingest_receivers"] == "enabled"
    server._collector.get_metrics.return_value = {"embedding_service": {}}
    server._collector.collect_process_metrics = AsyncMock(return_value={"active_processes": 0})
    for probe in (
        "collect_db_stats",
        "collect_search_quality",
        "collect_dream_metrics",
        "collect_nightly_ops",
    ):
        setattr(server._collector, probe, AsyncMock(return_value={}))
    server._embedding_svc.healthcheck = AsyncMock(return_value=True)
    response = await server._handle_metrics(make_mocked_request("GET", "/metrics"))
    assert json.loads(response.body)["receiver_refused"] == {}
    await server._handle_codex_logs(_request("/v1/logs"))
    response = await server._handle_metrics(make_mocked_request("GET", "/metrics"))
    assert json.loads(response.body)["receiver_refused"] == {"missing_token": 1}
    server._cockpit = MagicMock(snapshot=AsyncMock(return_value={"ok": True}))
    assert (await server._handle_cockpit(make_mocked_request("GET", "/api/cockpit"))).status == 200


async def test_refusal_json_matches_watcher_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    from brain_v42.metrics.runtime import build_sidecar_structlog_processors

    server, _, _ = await make_server(monkeypatch)
    with capture_logs() as logs:
        await server._handle_codex_logs(_request("/v1/logs"))
    expected = json.loads(
        (Path(__file__).parent / "golden" / "metrics_receiver.refused.jsonl").read_text()
    )
    assert len(logs) == 1
    assert logs[0] == {
        **{key: value for key, value in expected.items() if key not in {"timestamp", "level"}},
        "log_level": "warning",
    }
    processors = build_sidecar_structlog_processors(MagicMock(), log_format="json")
    processors[0] = lambda _, __, event: {**event, "timestamp": "2026-10-06T00:00:00Z"}
    rendered: Any = {key: value for key, value in logs[0].items() if key != "log_level"}
    for processor in processors:
        rendered = processor(None, "warning", rendered)
    assert json.loads(rendered) == expected


async def test_bucket_keeps_other_principals_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    server, repo, _ = await make_server(monkeypatch)
    another_token = "test-only-independent-client"
    repo.rows.append(
        replace(row(another_token), client_id="workstation-otel", families=["telemetry"])
    )
    await server._credential_verifier.notify()
    for _ in range(20):
        assert server._receiver_bucket.allow("brain-v42-mcp")
    request = _request(
        "/v1/logs",
        headers={"Authorization": "Bearer " + another_token},
        payload=_stream(data=b'{"resourceLogs":[]}'),
    )
    assert (await server._handle_codex_logs(request)).status == 200


def test_auth_none_preserves_disabled_non_loopback_receivers() -> None:
    from brain_v42.metrics.server import NonLoopbackReceiversError

    server = MetricsServer(MagicMock(), MagicMock(), host="203.0.113.1", allow_non_loopback=True)
    routes = {route.resource.canonical for route in server._build_app().router.routes()}
    assert not any(path.startswith("/v1/") for path in routes)
    server = MetricsServer(
        MagicMock(), MagicMock(), host="203.0.113.1", nonloopback_posture="fail_closed"
    )
    with pytest.raises(NonLoopbackReceiversError):
        server._build_app()


async def test_authenticated_non_loopback_registration_requires_bind_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.metrics.server import NonLoopbackReceiversError

    server, _, _ = await make_server(monkeypatch)
    server._allow_non_loopback = False
    with pytest.raises(NonLoopbackReceiversError, match="METRICS_ALLOW_NON_LOOPBACK"):
        server._build_app()


@pytest.mark.parametrize("authorization", ["", "Basic test-only-value", "Bearer"])
async def test_malformed_authorization_never_reads_a_body(
    monkeypatch: pytest.MonkeyPatch, authorization: str
) -> None:
    server, _, _ = await make_server(monkeypatch)
    response = await server._handle_codex_logs(
        _request("/v1/logs", headers={"Authorization": authorization})
    )
    assert response.status == 401
    assert server._receiver_refused == {"missing_token": 1}


async def test_duplicate_authorization_is_not_an_ambiguous_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multidict import CIMultiDict

    server, _, _ = await make_server(monkeypatch)
    request = make_mocked_request(
        "POST",
        "/v1/logs",
        headers=CIMultiDict(
            [
                ("Authorization", "Bearer " + TOKEN),
                ("Authorization", "Bearer test-only-other-token"),
            ]
        ),
    )
    assert (await server._handle_codex_logs(request)).status == 401
    assert server._receiver_refused == {"missing_token": 1}
