"""Tests for the shared GitLab webhook endpoint."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import brain_v42.automation.webhook as webhook_module
from brain_v42.automation.ownership import OwnershipLostError
from brain_v42.automation.webhook import GitLabWebhookEndpoint
from brain_v42.metrics.server import MetricsServer


def test_webhook_endpoint_is_importable() -> None:
    """The automation bounded context exposes its webhook endpoint."""
    from brain_v42.automation.webhook import GitLabWebhookEndpoint

    assert GitLabWebhookEndpoint is not None


@pytest.fixture
def ingestor() -> AsyncMock:
    processor = AsyncMock()
    processor.process_event = AsyncMock(return_value={"status": "ok", "created": 1})
    return processor


@pytest.fixture
def resolver() -> AsyncMock:
    return AsyncMock(return_value="brain-v42")


async def _client_for(
    aiohttp_client: Any,
    *,
    ingestor: AsyncMock,
    resolver: AsyncMock,
    secret: str,
) -> Any:
    endpoint = GitLabWebhookEndpoint(ingestor, resolver, secret)
    app = web.Application()
    app.router.add_post("/gitlab/webhook", endpoint.handle)
    return await aiohttp_client(app)


async def test_empty_secret_fails_closed_with_exact_message(
    aiohttp_client: Any,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    client = await _client_for(
        aiohttp_client,
        ingestor=ingestor,
        resolver=resolver,
        secret="",
    )

    response = await client.post(
        "/gitlab/webhook",
        json={"object_kind": "push"},
        headers={"X-Gitlab-Event-UUID": "event-1"},
    )

    assert response.status == 401
    assert await response.text() == "Webhook authentication not configured"
    resolver.assert_not_awaited()
    ingestor.process_event.assert_not_awaited()


async def test_wrong_token_is_rejected_with_exact_message(
    aiohttp_client: Any,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    client = await _client_for(
        aiohttp_client,
        ingestor=ingestor,
        resolver=resolver,
        secret="expected-token",
    )

    response = await client.post(
        "/gitlab/webhook",
        json={"object_kind": "push"},
        headers={
            "X-Gitlab-Token": "wrong-token",
            "X-Gitlab-Event-UUID": "event-2",
        },
    )

    assert response.status == 401
    assert await response.text() == "Invalid token"
    resolver.assert_not_awaited()
    ingestor.process_event.assert_not_awaited()


def test_webhook_token_matcher_uses_constant_time_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comparisons: list[tuple[bytes, bytes]] = []

    def recording_compare_digest(supplied: bytes, expected: bytes) -> bool:
        comparisons.append((supplied, expected))
        return supplied == expected

    matcher = webhook_module.webhook_token_matches
    monkeypatch.setattr(webhook_module.hmac, "compare_digest", recording_compare_digest)
    raw_token = b"jeton-\xff"
    decoded_token = raw_token.decode("utf-8", errors="surrogateescape")

    assert matcher(decoded_token, decoded_token) is True
    assert comparisons == [(raw_token, raw_token)]


async def test_shared_endpoint_uses_constant_time_token_matcher(
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    token_matches = MagicMock(return_value=False)
    monkeypatch.setattr(
        webhook_module,
        "webhook_token_matches",
        token_matches,
    )
    client = await _client_for(
        aiohttp_client,
        ingestor=ingestor,
        resolver=resolver,
        secret="expected-token",
    )

    response = await client.post(
        "/gitlab/webhook",
        json={"object_kind": "push"},
        headers={
            "X-Gitlab-Token": "wrong-token",
            "X-Gitlab-Event-UUID": "event-constant-time",
        },
    )

    assert response.status == 401
    assert await response.text() == "Invalid token"
    token_matches.assert_called_once_with("wrong-token", "expected-token")
    resolver.assert_not_awaited()
    ingestor.process_event.assert_not_awaited()


async def test_missing_event_uuid_is_rejected_with_exact_message(
    aiohttp_client: Any,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    client = await _client_for(
        aiohttp_client,
        ingestor=ingestor,
        resolver=resolver,
        secret="secret",
    )

    response = await client.post(
        "/gitlab/webhook",
        json={"object_kind": "push"},
        headers={"X-Gitlab-Token": "secret"},
    )

    assert response.status == 400
    assert await response.text() == "Missing X-Gitlab-Event-UUID"
    resolver.assert_not_awaited()
    ingestor.process_event.assert_not_awaited()


async def test_unknown_project_returns_project_path(
    aiohttp_client: Any,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    resolver.return_value = None
    client = await _client_for(
        aiohttp_client,
        ingestor=ingestor,
        resolver=resolver,
        secret="secret",
    )
    payload = {
        "object_kind": "push",
        "project": {"path_with_namespace": "unknown/project"},
    }

    response = await client.post(
        "/gitlab/webhook",
        json=payload,
        headers={
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event-UUID": "event-3",
        },
    )

    assert response.status == 200
    assert await response.json() == {
        "status": "unknown_project",
        "path": "unknown/project",
    }
    resolver.assert_awaited_once_with("unknown/project")
    ingestor.process_event.assert_not_awaited()


async def test_known_project_processes_exact_event_and_returns_result(
    aiohttp_client: Any,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    client = await _client_for(
        aiohttp_client,
        ingestor=ingestor,
        resolver=resolver,
        secret="secret",
    )
    payload = {
        "object_kind": "push",
        "project": {"path_with_namespace": "hawkixs_project/brain_v42"},
        "ref": "refs/heads/main",
    }

    response = await client.post(
        "/gitlab/webhook",
        json=payload,
        headers={
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event-UUID": "event-4",
        },
    )

    assert response.status == 200
    assert await response.json() == {"status": "ok", "created": 1}
    resolver.assert_awaited_once_with("hawkixs_project/brain_v42")
    ingestor.process_event.assert_awaited_once_with(payload, "event-4", "brain-v42")


async def test_metrics_legacy_route_delegates_to_shared_endpoint(
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    shared_handle = AsyncMock(
        return_value=web.Response(status=418, text="shared-endpoint-sentinel")
    )
    monkeypatch.setattr(GitLabWebhookEndpoint, "handle", shared_handle)
    server = MetricsServer(
        MagicMock(),
        MagicMock(),
        port=0,
        host="127.0.0.1",
        gitlab_ingestor=ingestor,
        project_key_resolver=resolver,
        webhook_secret="secret",
    )
    client = await aiohttp_client(server._build_app())

    response = await client.post(
        "/gitlab/webhook",
        json={"object_kind": "push"},
        headers={
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event-UUID": "event-legacy",
        },
    )

    assert response.status == 418
    assert await response.text() == "shared-endpoint-sentinel"
    shared_handle.assert_awaited_once()


async def test_metrics_legacy_route_uses_shared_constant_time_token_matcher(
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    token_matches = MagicMock(return_value=False)
    monkeypatch.setattr(
        webhook_module,
        "webhook_token_matches",
        token_matches,
    )
    server = MetricsServer(
        MagicMock(),
        MagicMock(),
        port=0,
        host="127.0.0.1",
        gitlab_ingestor=ingestor,
        project_key_resolver=resolver,
        webhook_secret="secret",
    )
    client = await aiohttp_client(server._build_app())

    response = await client.post(
        "/gitlab/webhook",
        json={"object_kind": "push"},
        headers={
            "X-Gitlab-Token": "wrong-token",
            "X-Gitlab-Event-UUID": "event-legacy-constant-time",
        },
    )

    assert response.status == 401
    assert await response.text() == "Invalid token"
    token_matches.assert_called_once_with("wrong-token", "secret")
    resolver.assert_not_awaited()
    ingestor.process_event.assert_not_awaited()


async def test_metrics_legacy_rejects_before_reading_or_decompressing_body(
    monkeypatch: pytest.MonkeyPatch,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    token_matches = MagicMock(return_value=False)
    monkeypatch.setattr(webhook_module, "webhook_token_matches", token_matches)
    decompress_body = MagicMock()
    monkeypatch.setattr(
        webhook_module,
        "_decompress_webhook_body",
        decompress_body,
    )
    server = MetricsServer(
        MagicMock(),
        MagicMock(),
        port=0,
        host="127.0.0.1",
        gitlab_ingestor=ingestor,
        project_key_resolver=resolver,
        webhook_secret="secret",
    )
    request = MagicMock()
    request.headers = {"X-Gitlab-Token": "wrong-token"}
    request.read = AsyncMock()
    request.json = AsyncMock()

    response = await server._handle_webhook(request)

    assert response.status == 401
    assert response.text == "Invalid token"
    token_matches.assert_called_once_with("wrong-token", "secret")
    request.read.assert_not_awaited()
    request.json.assert_not_awaited()
    decompress_body.assert_not_called()
    resolver.assert_not_awaited()
    ingestor.process_event.assert_not_awaited()


async def test_authenticated_ownership_loss_during_resolution_returns_exact_503(
    aiohttp_client: Any,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    resolver.side_effect = OwnershipLostError("lease lost")
    client = await _client_for(
        aiohttp_client,
        ingestor=ingestor,
        resolver=resolver,
        secret="secret",
    )

    response = await client.post(
        "/gitlab/webhook",
        json={"project": {"path_with_namespace": "hawkixs/brain_v42"}},
        headers={
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event-UUID": "event-lost-resolution",
        },
    )

    assert response.status == 503
    assert await response.json() == {"status": "ownership_lost"}
    ingestor.process_event.assert_not_awaited()


async def test_authenticated_ownership_loss_at_mutation_gate_returns_exact_503(
    aiohttp_client: Any,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    ingestor.process_event.side_effect = OwnershipLostError("lease lost")
    client = await _client_for(
        aiohttp_client,
        ingestor=ingestor,
        resolver=resolver,
        secret="secret",
    )

    response = await client.post(
        "/gitlab/webhook",
        json={"project": {"path_with_namespace": "hawkixs/brain_v42"}},
        headers={
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event-UUID": "event-lost-mutation",
        },
    )

    assert response.status == 503
    assert await response.json() == {"status": "ownership_lost"}


async def test_authentication_keeps_priority_over_ownership_loss(
    aiohttp_client: Any,
    ingestor: AsyncMock,
    resolver: AsyncMock,
) -> None:
    resolver.side_effect = OwnershipLostError("must remain hidden")
    ingestor.process_event.side_effect = OwnershipLostError("must remain hidden")
    client = await _client_for(
        aiohttp_client,
        ingestor=ingestor,
        resolver=resolver,
        secret="secret",
    )

    response = await client.post(
        "/gitlab/webhook",
        json={"project": {"path_with_namespace": "hawkixs/brain_v42"}},
        headers={
            "X-Gitlab-Token": "wrong",
            "X-Gitlab-Event-UUID": "event-unauthenticated-loss",
        },
    )

    assert response.status == 401
    assert await response.text() == "Invalid token"
    resolver.assert_not_awaited()
    ingestor.process_event.assert_not_awaited()
