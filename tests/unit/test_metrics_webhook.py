"""Tests for MetricsServer webhook endpoint — POST /gitlab/webhook."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.server import MetricsServer

_MOCK_SETTINGS = MagicMock(
    embedding_service_url="http://localhost:8003",
    embedding_dimension=1024,
)


@pytest.fixture(autouse=True)
def _patch_settings() -> Any:  # noqa: ANN401
    with patch("brain_v42.metrics.collector.get_settings", return_value=_MOCK_SETTINGS):
        yield


@pytest.fixture
def collector() -> MetricsCollector:
    return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())


@pytest.fixture
def mock_embedding_svc() -> MagicMock:
    svc = MagicMock()
    svc.healthcheck = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def mock_gitlab_ingestor() -> AsyncMock:
    ingestor = AsyncMock()
    ingestor.process_event = AsyncMock(return_value={"status": "ok", "created": 1})
    return ingestor


@pytest.fixture
def mock_project_key_resolver() -> AsyncMock:
    resolver = AsyncMock(return_value="brain_v42")
    return resolver


# ── Token validation ─────────────────────────────────────────────────────────


async def test_webhook_rejects_without_token(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
    mock_gitlab_ingestor: AsyncMock,
    mock_project_key_resolver: AsyncMock,
) -> None:
    """POST /gitlab/webhook without X-Gitlab-Token returns 401 when secret is set."""
    server = MetricsServer(
        collector,
        mock_embedding_svc,
        port=0,
        host="127.0.0.1",
        gitlab_ingestor=mock_gitlab_ingestor,
        project_key_resolver=mock_project_key_resolver,
        webhook_secret="my-secret-token",
    )
    client = await aiohttp_client(server._build_app())

    resp = await client.post(
        "/gitlab/webhook",
        json={"object_kind": "push"},
        headers={"X-Gitlab-Event-UUID": "abc-123"},
    )
    assert resp.status == 401
    text = await resp.text()
    assert "Invalid token" in text


async def test_webhook_rejects_wrong_token(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
    mock_gitlab_ingestor: AsyncMock,
    mock_project_key_resolver: AsyncMock,
) -> None:
    """POST /gitlab/webhook with wrong X-Gitlab-Token returns 401."""
    server = MetricsServer(
        collector,
        mock_embedding_svc,
        port=0,
        host="127.0.0.1",
        gitlab_ingestor=mock_gitlab_ingestor,
        project_key_resolver=mock_project_key_resolver,
        webhook_secret="my-secret-token",
    )
    client = await aiohttp_client(server._build_app())

    resp = await client.post(
        "/gitlab/webhook",
        json={"object_kind": "push"},
        headers={
            "X-Gitlab-Token": "wrong-token",
            "X-Gitlab-Event-UUID": "abc-123",
        },
    )
    assert resp.status == 401


async def test_webhook_accepts_valid_token(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
    mock_gitlab_ingestor: AsyncMock,
    mock_project_key_resolver: AsyncMock,
) -> None:
    """POST /gitlab/webhook with correct token returns 200."""
    server = MetricsServer(
        collector,
        mock_embedding_svc,
        port=0,
        host="127.0.0.1",
        gitlab_ingestor=mock_gitlab_ingestor,
        project_key_resolver=mock_project_key_resolver,
        webhook_secret="my-secret-token",
    )
    client = await aiohttp_client(server._build_app())

    resp = await client.post(
        "/gitlab/webhook",
        json={
            "object_kind": "push",
            "project": {"path_with_namespace": "hawkixs_project/brain_v42"},
        },
        headers={
            "X-Gitlab-Token": "my-secret-token",
            "X-Gitlab-Event-UUID": "evt-uuid-001",
        },
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    mock_gitlab_ingestor.process_event.assert_awaited_once()


# ── No secret configured (fail-closed) ──────────────────────────────────────


async def test_webhook_rejects_when_no_secret_configured(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
    mock_gitlab_ingestor: AsyncMock,
    mock_project_key_resolver: AsyncMock,
) -> None:
    """When webhook_secret is empty, the endpoint fails CLOSED (401) and never
    ingests an unauthenticated event — no valid token can be presented, so
    serving the webhook open would let anyone inject GitLab events."""
    server = MetricsServer(
        collector,
        mock_embedding_svc,
        port=0,
        host="127.0.0.1",
        gitlab_ingestor=mock_gitlab_ingestor,
        project_key_resolver=mock_project_key_resolver,
        webhook_secret="",
    )
    client = await aiohttp_client(server._build_app())

    resp = await client.post(
        "/gitlab/webhook",
        json={
            "object_kind": "push",
            "project": {"path_with_namespace": "hawkixs_project/brain_v42"},
        },
        headers={"X-Gitlab-Event-UUID": "evt-uuid-002"},
    )
    assert resp.status == 401
    mock_gitlab_ingestor.process_event.assert_not_awaited()


# ── Missing event UUID ──────────────────────────────────────────────────────


async def test_webhook_rejects_missing_event_uuid(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
    mock_gitlab_ingestor: AsyncMock,
    mock_project_key_resolver: AsyncMock,
) -> None:
    """POST /gitlab/webhook without X-Gitlab-Event-UUID returns 400 (authenticated)."""
    server = MetricsServer(
        collector,
        mock_embedding_svc,
        port=0,
        host="127.0.0.1",
        gitlab_ingestor=mock_gitlab_ingestor,
        project_key_resolver=mock_project_key_resolver,
        webhook_secret="s",
    )
    client = await aiohttp_client(server._build_app())

    resp = await client.post(
        "/gitlab/webhook",
        json={"object_kind": "push"},
        headers={"X-Gitlab-Token": "s"},
    )
    assert resp.status == 400
    text = await resp.text()
    assert "Missing X-Gitlab-Event-UUID" in text


# ── Unknown project ─────────────────────────────────────────────────────────


async def test_webhook_returns_unknown_project(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
    mock_gitlab_ingestor: AsyncMock,
) -> None:
    """When resolver returns None, response indicates unknown_project."""
    resolver = AsyncMock(return_value=None)
    server = MetricsServer(
        collector,
        mock_embedding_svc,
        port=0,
        host="127.0.0.1",
        gitlab_ingestor=mock_gitlab_ingestor,
        project_key_resolver=resolver,
        webhook_secret="s",
    )
    client = await aiohttp_client(server._build_app())

    resp = await client.post(
        "/gitlab/webhook",
        json={
            "object_kind": "push",
            "project": {"path_with_namespace": "unknown/project"},
        },
        headers={"X-Gitlab-Token": "s", "X-Gitlab-Event-UUID": "evt-uuid-003"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "unknown_project"
    assert data["path"] == "unknown/project"


# ── No ingestor → no route ──────────────────────────────────────────────────


async def test_webhook_route_not_registered_without_ingestor(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
) -> None:
    """When no gitlab_ingestor is provided, POST /gitlab/webhook returns 404."""
    server = MetricsServer(
        collector,
        mock_embedding_svc,
        port=0,
        host="127.0.0.1",
    )
    client = await aiohttp_client(server._build_app())

    resp = await client.post(
        "/gitlab/webhook",
        json={"object_kind": "push"},
    )
    assert resp.status == 404


# ── Calls ingestor with correct args ────────────────────────────────────────


async def test_webhook_passes_correct_args_to_ingestor(
    aiohttp_client: Any,
    collector: MetricsCollector,
    mock_embedding_svc: MagicMock,
    mock_gitlab_ingestor: AsyncMock,
    mock_project_key_resolver: AsyncMock,
) -> None:
    """Verify that process_event is called with payload, event_uuid, project_key."""
    server = MetricsServer(
        collector,
        mock_embedding_svc,
        port=0,
        host="127.0.0.1",
        gitlab_ingestor=mock_gitlab_ingestor,
        project_key_resolver=mock_project_key_resolver,
        webhook_secret="s",
    )
    client = await aiohttp_client(server._build_app())

    payload = {
        "object_kind": "push",
        "project": {"path_with_namespace": "hawkixs_project/brain_v42"},
        "ref": "refs/heads/main",
    }

    await client.post(
        "/gitlab/webhook",
        json=payload,
        headers={"X-Gitlab-Token": "s", "X-Gitlab-Event-UUID": "evt-uuid-004"},
    )

    mock_gitlab_ingestor.process_event.assert_awaited_once_with(
        payload, "evt-uuid-004", "brain_v42"
    )
