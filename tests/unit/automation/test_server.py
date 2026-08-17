"""Tests for the dedicated automation HTTP server."""

from __future__ import annotations

import asyncio
import gzip
import json
import socket
import zlib
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientSession
from aiohttp.http_parser import DeflateBuffer
from aiohttp.web_request import BaseRequest

import brain_v42.automation.webhook as webhook_module
from brain_v42.automation.server import AutomationServer
from brain_v42.automation.webhook import GitLabWebhookEndpoint


@pytest.fixture
def ingestor() -> AsyncMock:
    processor = AsyncMock()
    processor.process_event = AsyncMock(return_value={"status": "ok"})
    return processor


@pytest.fixture
def resolver() -> AsyncMock:
    return AsyncMock(return_value="brain-v42")


@pytest.fixture
def endpoint(ingestor: AsyncMock, resolver: AsyncMock) -> GitLabWebhookEndpoint:
    return GitLabWebhookEndpoint(ingestor, resolver, "secret")


def _raw_deflate(body: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(body) + compressor.flush()


def _bound_port(server: AutomationServer) -> int:
    runner = server._runner
    assert runner is not None
    site = next(iter(runner.sites))
    site_server = site._server
    assert site_server is not None
    sockets = site_server.sockets
    assert sockets is not None
    return int(sockets[0].getsockname()[1])


async def test_health_route_returns_success(
    aiohttp_client: Any,
    endpoint: GitLabWebhookEndpoint,
) -> None:
    server = AutomationServer(endpoint, port=0)
    client = await aiohttp_client(server._build_app())

    response = await client.get("/health")

    assert response.status == 200


async def test_webhook_route_uses_shared_endpoint(
    aiohttp_client: Any,
    endpoint: GitLabWebhookEndpoint,
    ingestor: AsyncMock,
) -> None:
    server = AutomationServer(endpoint, port=0)
    client = await aiohttp_client(server._build_app())
    payload = {
        "object_kind": "push",
        "project": {"path_with_namespace": "hawkixs_project/brain_v42"},
    }

    response = await client.post(
        "/gitlab/webhook",
        json=payload,
        headers={
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event-UUID": "event-5",
        },
    )

    assert response.status == 200
    ingestor.process_event.assert_awaited_once_with(payload, "event-5", "brain-v42")


@pytest.mark.parametrize("path", ["/metrics", "/api/cockpit"])
async def test_metrics_surfaces_are_absent(
    aiohttp_client: Any,
    endpoint: GitLabWebhookEndpoint,
    path: str,
) -> None:
    server = AutomationServer(endpoint, port=0)
    client = await aiohttp_client(server._build_app())

    response = await client.get(path)

    assert response.status == 404


def test_route_table_contains_only_explicit_get_and_post(
    endpoint: GitLabWebhookEndpoint,
) -> None:
    routes = {
        (route.method, route.resource.canonical)
        for route in AutomationServer(endpoint)._build_app().router.routes()
    }

    assert routes == {
        ("GET", "/health"),
        ("POST", "/gitlab/webhook"),
    }


async def test_start_and_stop_are_idempotent_on_ephemeral_port(
    endpoint: GitLabWebhookEndpoint,
) -> None:
    server = AutomationServer(endpoint, port=0)

    await server.start()
    runner = server._runner
    assert runner is not None
    site = next(iter(runner.sites))
    site_server = site._server
    assert site_server is not None
    bound_port = site_server.sockets[0].getsockname()[1]

    await server.start()
    assert server._runner is runner
    async with ClientSession() as client:
        response = await client.get(f"http://127.0.0.1:{bound_port}/health")
        assert response.status == 200

    await server.stop()
    assert server._runner is None
    await server.stop()


def test_default_shutdown_timeout_leaves_cleanup_margin(
    endpoint: GitLabWebhookEndpoint,
) -> None:
    server = AutomationServer(endpoint, port=0)

    assert server._shutdown_timeout == 10.0
    assert 2 * server._shutdown_timeout < 30.0


async def test_stop_bounds_an_in_flight_webhook_request(
    endpoint: GitLabWebhookEndpoint,
    ingestor: AsyncMock,
) -> None:
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    async def block_request(*_args: object) -> dict[str, str]:
        request_started.set()
        await release_request.wait()
        return {"status": "ok"}

    ingestor.process_event.side_effect = block_request
    server = AutomationServer(endpoint, port=0, shutdown_timeout=0.01)
    request_task: asyncio.Task[Any] | None = None

    await server.start()
    runner = server._runner
    assert runner is not None
    site = next(iter(runner.sites))
    site_server = site._server
    assert site_server is not None
    bound_port = site_server.sockets[0].getsockname()[1]

    try:
        async with ClientSession() as client:
            request_task = asyncio.create_task(
                client.post(
                    f"http://127.0.0.1:{bound_port}/gitlab/webhook",
                    json={
                        "object_kind": "push",
                        "project": {"path_with_namespace": "hawkixs_project/brain_v42"},
                    },
                    headers={
                        "X-Gitlab-Token": "secret",
                        "X-Gitlab-Event-UUID": "event-in-flight",
                    },
                )
            )
            await asyncio.wait_for(request_started.wait(), timeout=1.0)

            await asyncio.wait_for(server.stop(), timeout=0.5)

            await asyncio.wait_for(
                asyncio.gather(request_task, return_exceptions=True),
                timeout=0.5,
            )
            assert request_task.done()
    finally:
        release_request.set()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        await server.stop()


async def test_bind_failure_cleans_partial_runner(
    endpoint: GitLabWebhookEndpoint,
) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    server = AutomationServer(endpoint, host="127.0.0.1", port=port)

    try:
        with pytest.raises(OSError):
            await server.start()
        assert server._runner is None
    finally:
        blocker.close()
        await server.stop()


async def test_start_disables_aiohttp_request_decompression(
    endpoint: GitLabWebhookEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = MagicMock()
    runner.setup = AsyncMock()
    site = MagicMock()
    site.start = AsyncMock()
    app_runner = MagicMock(return_value=runner)
    monkeypatch.setattr("brain_v42.automation.server.web.AppRunner", app_runner)
    monkeypatch.setattr("brain_v42.automation.server.web.TCPSite", MagicMock(return_value=site))
    server = AutomationServer(endpoint, port=0)

    await server.start()

    app_runner.assert_called_once()
    assert app_runner.call_args.kwargs == {
        "auto_decompress": False,
        "shutdown_timeout": 10.0,
    }


async def test_unauthenticated_compressed_body_is_not_decompressed(
    endpoint: GitLabWebhookEndpoint,
    ingestor: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decompression_calls: list[int] = []
    request_read_calls: list[BaseRequest] = []
    original_feed_data = DeflateBuffer.feed_data
    original_request_read = BaseRequest.read

    def recording_feed_data(self: DeflateBuffer, chunk: bytes, size: int) -> None:
        decompression_calls.append(len(chunk))
        original_feed_data(self, chunk, size)

    async def recording_request_read(self: BaseRequest) -> bytes:
        request_read_calls.append(self)
        return await original_request_read(self)

    application_decompress = MagicMock(wraps=webhook_module._decompress_webhook_body)
    monkeypatch.setattr(DeflateBuffer, "feed_data", recording_feed_data)
    monkeypatch.setattr(BaseRequest, "read", recording_request_read)
    monkeypatch.setattr(
        webhook_module,
        "_decompress_webhook_body",
        application_decompress,
    )
    server = AutomationServer(endpoint, port=0)

    await server.start()
    try:
        async with ClientSession() as client:
            response = await client.post(
                f"http://127.0.0.1:{_bound_port(server)}/gitlab/webhook",
                data=gzip.compress(b"x" * (1024**2 + 1)),
                headers={
                    "Content-Encoding": "gzip",
                    "X-Gitlab-Token": "wrong-token",
                    "X-Gitlab-Event-UUID": "unauthenticated-bomb",
                },
            )

            assert response.status == 401
            assert await response.text() == "Invalid token"
        assert decompression_calls == []
        assert request_read_calls == []
        application_decompress.assert_not_called()
        ingestor.process_event.assert_not_awaited()
    finally:
        await server.stop()


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
        ("deflate", _raw_deflate),
    ],
    ids=["gzip", "zlib-deflate", "raw-deflate"],
)
async def test_dedicated_server_preserves_compressed_webhook_ingestion(
    endpoint: GitLabWebhookEndpoint,
    ingestor: AsyncMock,
    encoding: str,
    compress: Callable[[bytes], bytes],
) -> None:
    payload = {
        "object_kind": "push",
        "project": {"path_with_namespace": "hawkixs_project/brain_v42"},
    }
    server = AutomationServer(endpoint, port=0)

    await server.start()
    try:
        async with ClientSession() as client:
            response = await client.post(
                f"http://127.0.0.1:{_bound_port(server)}/gitlab/webhook",
                data=compress(json.dumps(payload).encode()),
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": encoding,
                    "X-Gitlab-Token": "secret",
                    "X-Gitlab-Event-UUID": f"compressed-{encoding}",
                },
            )

            assert response.status == 200
            assert await response.json() == {"status": "ok"}
        ingestor.process_event.assert_awaited_once_with(
            payload,
            f"compressed-{encoding}",
            "brain-v42",
        )
    finally:
        await server.stop()


@pytest.mark.parametrize(
    ("encoding", "body"),
    [
        (None, b"x" * (1024**2)),
        ("gzip", gzip.compress(b"x" * (1024**2 + 1))),
    ],
    ids=["raw", "gzip-bomb"],
)
async def test_dedicated_server_rejects_bounded_webhook_bodies(
    endpoint: GitLabWebhookEndpoint,
    ingestor: AsyncMock,
    encoding: str | None,
    body: bytes,
) -> None:
    headers = {
        "Content-Type": "application/json",
        "X-Gitlab-Token": "secret",
        "X-Gitlab-Event-UUID": "bounded-webhook",
    }
    if encoding is not None:
        headers["Content-Encoding"] = encoding
    server = AutomationServer(endpoint, port=0)

    await server.start()
    try:
        async with ClientSession() as client:
            response = await client.post(
                f"http://127.0.0.1:{_bound_port(server)}/gitlab/webhook",
                data=body,
                headers=headers,
            )

            assert response.status == 413
            assert await response.json() == {"status": "payload_too_large"}
        ingestor.process_event.assert_not_awaited()
    finally:
        await server.stop()


async def test_dedicated_server_rejects_malformed_compressed_webhook(
    endpoint: GitLabWebhookEndpoint,
    ingestor: AsyncMock,
) -> None:
    server = AutomationServer(endpoint, port=0)

    await server.start()
    try:
        async with ClientSession() as client:
            response = await client.post(
                f"http://127.0.0.1:{_bound_port(server)}/gitlab/webhook",
                data=b"SENSITIVE_INVALID_GZIP",
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                    "X-Gitlab-Token": "secret",
                    "X-Gitlab-Event-UUID": "malformed-webhook",
                },
            )

            assert response.status == 400
            assert await response.json() == {"status": "invalid_request"}
        ingestor.process_event.assert_not_awaited()
    finally:
        await server.stop()
