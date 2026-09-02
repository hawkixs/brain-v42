"""HTTP boundary tests for the loopback-only Codex OTLP logs receiver."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import make_mocked_request
from structlog.testing import capture_logs

from brain_v42.metrics.codex_telemetry import (
    MAX_ATTRIBUTES_PER_RECORD,
    MAX_IN_FLIGHT_REQUESTS,
    MAX_JSON_CONTAINERS,
    MAX_JSON_DEPTH,
    MAX_LOG_RECORDS,
    MAX_REQUEST_BYTES,
    CodexConversationRegistry,
)
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.server import MetricsServer

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "codex_otlp_logs.json"
FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"


def _attribute(key: str, value: dict[str, object]) -> dict[str, object]:
    return {"key": key, "value": value}


def _record(
    *,
    event_name: str = "codex.user_prompt",
    timestamp: int = 1,
    extra_attributes: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    attributes = [
        _attribute("event.name", {"stringValue": event_name}),
        _attribute("conversation.id", {"stringValue": FAKE_UUID}),
        _attribute("model", {"stringValue": "gpt-5.4"}),
    ]
    attributes.extend(extra_attributes or [])
    return {"timeUnixNano": str(timestamp), "attributes": attributes}


def _payload(*records: dict[str, object], future: object | None = None) -> bytes:
    envelope: dict[str, object] = {"resourceLogs": [{"scopeLogs": [{"logRecords": list(records)}]}]}
    if future is not None:
        envelope["future"] = future
    return json.dumps(envelope, separators=(",", ":")).encode()


def _server(
    registry: CodexConversationRegistry | None = None,
    *,
    host: str = "127.0.0.1",
    collector: MetricsCollector | MagicMock | None = None,
) -> MetricsServer:
    return MetricsServer(
        collector or MagicMock(spec=MetricsCollector),
        MagicMock(),
        host=host,
        codex_registry=registry,
    )


def _routes(app: web.Application) -> set[tuple[str, str]]:
    return {(route.method, route.resource.canonical) for route in app.router.routes()}


async def _assert_rpc_status(response: Any, *, http_status: int, rpc_code: int) -> str:
    assert response.status == http_status
    assert response.content_type == "application/json"
    body = await response.text()
    status = json.loads(body)
    assert set(status) == {"code", "message", "details"}
    assert status["code"] == rpc_code
    assert isinstance(status["message"], str) and status["message"]
    assert status["details"] == []
    return body


def _loopback_transport(address: str) -> MagicMock:
    transport = MagicMock()
    transport.get_extra_info.return_value = (address, 4318)
    return transport


def _raw_deflate(payload: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(payload) + compressor.flush()


async def test_otlp_endpoint_accepts_json_and_returns_an_empty_json_object(
    aiohttp_client: Any,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    server = MetricsServer(
        MagicMock(),
        MagicMock(),
        host="127.0.0.1",
        codex_registry=registry,
    )
    client = await aiohttp_client(server._build_app())

    response = await client.post(
        "/v1/logs",
        data=FIXTURE_PATH.read_bytes(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )

    assert response.status == 200
    assert response.content_type == "application/json"
    assert await response.read() == b"{}"
    assert registry.snapshot()["active_convs"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        b'{"resourceLogs":[]}',
        _payload(_record(event_name="codex.future_event")),
    ],
    ids=["empty", "irrelevant"],
)
async def test_otlp_endpoint_treats_empty_and_irrelevant_batches_as_successful_noops(
    aiohttp_client: Any,
    payload: bytes,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        "/v1/logs",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    assert response.status == 200
    assert await response.read() == b"{}"
    assert registry.snapshot() == {
        "active_convs": 0,
        "ctx_tokens": 0,
        "activeConvs": [],
        "clients": [],
    }


@pytest.mark.parametrize("payload", [b"{", b"{}", b"[]"], ids=["json", "envelope", "root"])
async def test_otlp_endpoint_maps_malformed_payloads_to_static_bad_request(
    aiohttp_client: Any,
    payload: bytes,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        "/v1/logs",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    body = await _assert_rpc_status(response, http_status=400, rpc_code=3)
    assert "SENSITIVE_BAD_INPUT" not in body
    assert registry.snapshot()["active_convs"] == 0


async def test_otlp_endpoint_rejects_known_oversize_before_decoding(
    aiohttp_client: Any,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        "/v1/logs",
        data=b"SENSITIVE_OVERSIZE" + b" " * MAX_REQUEST_BYTES,
        headers={"Content-Type": "application/json"},
    )

    body = await _assert_rpc_status(response, http_status=413, rpc_code=8)
    assert "SENSITIVE_OVERSIZE" not in body
    assert registry.snapshot()["active_convs"] == 0


async def test_otlp_endpoint_rejects_chunked_body_when_stream_crosses_route_limit(
    aiohttp_client: Any,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    client = await aiohttp_client(_server(registry)._build_app())

    async def chunks() -> Any:  # noqa: ANN401
        yield b"x" * (MAX_REQUEST_BYTES // 2)
        yield b"y" * (MAX_REQUEST_BYTES // 2 + 1)

    response = await client.post(
        "/v1/logs",
        data=chunks(),
        headers={"Content-Type": "application/json"},
    )

    await _assert_rpc_status(response, http_status=413, rpc_code=8)
    assert registry.snapshot()["active_convs"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        _payload(*({} for _ in range(MAX_LOG_RECORDS + 1))),
        _payload(
            {
                "attributes": [
                    _attribute(f"future.{index}", {"stringValue": "x"})
                    for index in range(MAX_ATTRIBUTES_PER_RECORD + 1)
                ]
            }
        ),
        _payload(future=[{} for _ in range(MAX_JSON_CONTAINERS + 1)]),
    ],
    ids=["records", "attributes", "containers"],
)
async def test_otlp_endpoint_maps_decoder_capacity_limits_to_resource_exhausted(
    aiohttp_client: Any,
    payload: bytes,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        "/v1/logs",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    await _assert_rpc_status(response, http_status=413, rpc_code=8)
    assert registry.snapshot()["active_convs"] == 0


async def test_otlp_endpoint_maps_json_depth_limit_to_resource_exhausted(
    aiohttp_client: Any,
) -> None:
    nested: object = {}
    for _ in range(MAX_JSON_DEPTH + 1):
        nested = {"nested": nested}
    registry = CodexConversationRegistry(secret=b"x" * 32)
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        "/v1/logs",
        data=_payload(future=nested),
        headers={"Content-Type": "application/json"},
    )

    await _assert_rpc_status(response, http_status=413, rpc_code=8)
    assert registry.snapshot()["active_convs"] == 0


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/octet-stream", "application/json; profile=private"],
)
async def test_otlp_endpoint_rejects_unsupported_media_types(
    aiohttp_client: Any,
    content_type: str,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        "/v1/logs",
        data=b'{"resourceLogs":[]}',
        headers={"Content-Type": content_type},
    )

    await _assert_rpc_status(response, http_status=415, rpc_code=3)
    assert registry.snapshot()["active_convs"] == 0


@pytest.mark.parametrize("encoding", ["gzip", "identity, gzip"])
async def test_otlp_endpoint_rejects_non_identity_content_encoding(
    aiohttp_client: Any,
    encoding: str,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    client = await aiohttp_client(_server(registry)._build_app())
    encoded = gzip.compress(b'{"resourceLogs":[]}') if encoding == "gzip" else b"encoded"

    response = await client.post(
        "/v1/logs",
        data=encoded,
        headers={"Content-Type": "application/json", "Content-Encoding": encoding},
    )

    await _assert_rpc_status(response, http_status=415, rpc_code=3)
    assert registry.snapshot()["active_convs"] == 0


async def test_otlp_handler_rejects_non_identity_encoding_before_reading_the_body() -> None:
    server = _server()
    request = make_mocked_request(
        "POST",
        "/v1/logs",
        headers={"Content-Type": "application/json", "Content-Encoding": "br"},
        transport=_loopback_transport("127.0.0.1"),
    )

    response = await server._handle_codex_logs(request)

    assert response.status == 415
    assert json.loads(response.body)["code"] == 3


@pytest.mark.parametrize(
    "headers",
    [
        [
            ("Content-Type", "application/json"),
            ("Content-Type", "application/json"),
        ],
        [
            ("Content-Type", "application/json"),
            ("Content-Encoding", "identity"),
            ("Content-Encoding", "identity"),
        ],
    ],
    ids=["content-type", "content-encoding"],
)
async def test_otlp_handler_rejects_duplicate_representation_headers(
    headers: list[tuple[str, str]],
) -> None:
    server = _server()
    request = make_mocked_request(
        "POST",
        "/v1/logs",
        headers=headers,
        transport=_loopback_transport("127.0.0.1"),
    )

    response = await server._handle_codex_logs(request)

    assert response.status == 415
    assert json.loads(response.body)["code"] == 3


async def test_otlp_endpoint_accepts_explicit_identity_encoding(
    aiohttp_client: Any,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        "/v1/logs",
        data=b'{"resourceLogs":[]}',
        headers={"Content-Type": "application/json", "Content-Encoding": "identity"},
    )

    assert response.status == 200


@pytest.mark.parametrize("host", ["127.0.0.1", "127.42.0.9", "::1", "localhost"])
def test_otlp_route_is_registered_for_loopback_bind_hosts(host: str) -> None:
    app = _server(host=host)._build_app()

    assert ("POST", "/v1/logs") in _routes(app)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.8", "metrics.internal"])
def test_otlp_route_is_absent_for_non_loopback_bind_hosts(host: str) -> None:
    """The DEFAULT posture (`silent`, eac03668) — a named choice, not an oversight.
    See test_metrics_nonloopback_posture.py for the other two forms.
    """
    app = _server(host=host)._build_app()

    assert ("POST", "/v1/logs") not in _routes(app)


async def test_otlp_endpoint_rejects_non_loopback_tcp_peer_even_with_forwarded_spoof() -> None:
    server = _server()
    request = make_mocked_request(
        "POST",
        "/v1/logs",
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-For": "127.0.0.1",
            "Forwarded": "for=127.0.0.1",
        },
        transport=_loopback_transport("203.0.113.9"),
    )

    response = await server._handle_codex_logs(request)

    assert response.status == 403
    status = json.loads(response.body)
    assert status == {
        "code": 7,
        "message": "OTLP receiver requires a loopback peer",
        "details": [],
    }


async def test_otlp_endpoint_requires_a_numeric_loopback_tcp_peer() -> None:
    server = _server()
    request = make_mocked_request(
        "POST",
        "/v1/logs",
        headers={"Content-Type": "application/json"},
        transport=_loopback_transport("localhost"),
    )

    response = await server._handle_codex_logs(request)

    assert response.status == 403


async def test_otlp_endpoint_ignores_forwarding_headers_for_a_real_loopback_peer(
    aiohttp_client: Any,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        "/v1/logs",
        data=b'{"resourceLogs":[]}',
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-For": "203.0.113.9",
            "Forwarded": "for=203.0.113.9",
        },
    )

    assert response.status == 200


async def test_otlp_endpoint_returns_retryable_unavailable_without_waiting_when_saturated() -> None:
    server = _server()
    for _ in range(MAX_IN_FLIGHT_REQUESTS):
        await server._codex_request_slots.acquire()
    request = make_mocked_request(
        "POST",
        "/v1/logs",
        headers={"Content-Type": "application/json"},
        transport=_loopback_transport("127.0.0.1"),
    )

    try:
        response = await server._handle_codex_logs(request)
    finally:
        for _ in range(MAX_IN_FLIGHT_REQUESTS):
            server._codex_request_slots.release()

    assert response.status == 503
    assert response.headers["Retry-After"] == "1"
    assert json.loads(response.body) == {
        "code": 14,
        "message": "OTLP receiver is busy",
        "details": [],
    }


async def test_otlp_endpoint_rejects_a_real_fifth_request_while_four_bodies_are_blocked(
    aiohttp_client: Any,
) -> None:
    server = _server()
    client = await aiohttp_client(server._build_app())
    release = asyncio.Event()

    async def blocked_body() -> Any:  # noqa: ANN401
        yield b'{"resourceLogs":'
        await release.wait()
        yield b"[]}"

    pending = [
        asyncio.create_task(
            client.post(
                "/v1/logs",
                data=blocked_body(),
                headers={"Content-Type": "application/json"},
            )
        )
        for _ in range(MAX_IN_FLIGHT_REQUESTS)
    ]

    async def wait_until_saturated() -> None:
        while not server._codex_request_slots.locked():
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(wait_until_saturated(), timeout=2)
        response = await asyncio.wait_for(
            client.post(
                "/v1/logs",
                data=b'{"resourceLogs":[]}',
                headers={"Content-Type": "application/json"},
            ),
            timeout=2,
        )
        assert response.status == 503
        assert response.headers["Retry-After"] == "1"
    finally:
        release.set()

    responses = await asyncio.gather(*pending)
    assert [response.status for response in responses] == [200] * MAX_IN_FLIGHT_REQUESTS


async def test_otlp_endpoint_validates_the_full_batch_before_atomic_mutation(
    aiohttp_client: Any,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    invalid = _record(timestamp=2)
    invalid["attributes"].append(_attribute("event.name", {"stringValue": "codex.user_prompt"}))
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        "/v1/logs",
        data=_payload(_record(timestamp=1), invalid),
        headers={"Content-Type": "application/json"},
    )

    await _assert_rpc_status(response, http_status=400, rpc_code=3)
    assert registry.snapshot() == {
        "active_convs": 0,
        "ctx_tokens": 0,
        "activeConvs": [],
        "clients": [],
    }


async def test_otlp_endpoint_accepts_four_concurrent_atomic_ingests(
    aiohttp_client: Any,
) -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    client = await aiohttp_client(_server(registry)._build_app())

    responses = await asyncio.gather(
        *(
            client.post(
                "/v1/logs",
                data=_payload(_record(timestamp=index + 1)),
                headers={"Content-Type": "application/json"},
            )
            for index in range(MAX_IN_FLIGHT_REQUESTS)
        )
    )

    assert [response.status for response in responses] == [200] * MAX_IN_FLIGHT_REQUESTS
    assert registry.snapshot()["activeConvs"][0]["turns"] == MAX_IN_FLIGHT_REQUESTS


async def test_production_runner_rejects_gzip_and_preserves_identity_webhook() -> None:
    registry = CodexConversationRegistry(secret=b"x" * 32)
    ingestor = AsyncMock()
    ingestor.process_event = AsyncMock(return_value={"status": "ok", "created": 1})
    resolver = AsyncMock(return_value="brain_v42")
    server = MetricsServer(
        MagicMock(spec=MetricsCollector),
        MagicMock(),
        port=0,
        host="127.0.0.1",
        gitlab_ingestor=ingestor,
        project_key_resolver=resolver,
        webhook_secret="secret",
        codex_registry=registry,
    )

    await server.start()
    try:
        assert server._runner is not None
        site = next(iter(server._runner.sites))
        assert site._server is not None
        sockets = site._server.sockets
        assert sockets is not None
        port = sockets[0].getsockname()[1]

        async with ClientSession() as client:
            gzip_response = await client.post(
                f"http://127.0.0.1:{port}/v1/logs",
                data=b"not-a-valid-gzip-stream",
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
            )
            webhook_response = await client.post(
                f"http://127.0.0.1:{port}/gitlab/webhook",
                json={
                    "object_kind": "push",
                    "project": {"path_with_namespace": "hawkixs_project/brain_v42"},
                },
                headers={
                    "X-Gitlab-Token": "secret",
                    "X-Gitlab-Event-UUID": "identity-webhook-001",
                },
            )
            gzip_status = gzip_response.status
            webhook_status = webhook_response.status
            webhook_body = await webhook_response.json()

        assert gzip_status == 415
        assert registry.snapshot()["active_convs"] == 0
        assert webhook_status == 200
        assert webhook_body == {"status": "ok", "created": 1}
        ingestor.process_event.assert_awaited_once()
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
async def test_runner_preserves_compressed_webhook_ingestion(
    aiohttp_client: Any,
    aiohttp_server: Any,
    encoding: str,
    compress: Any,
) -> None:
    payload = {
        "object_kind": "push",
        "project": {"path_with_namespace": "hawkixs_project/brain_v42"},
    }
    ingestor = AsyncMock()
    ingestor.process_event = AsyncMock(return_value={"status": "ok", "created": 1})
    server = MetricsServer(
        MagicMock(spec=MetricsCollector),
        MagicMock(),
        host="127.0.0.1",
        gitlab_ingestor=ingestor,
        project_key_resolver=AsyncMock(return_value="brain_v42"),
        webhook_secret="secret",
    )
    test_server = await aiohttp_server(server._build_app(), auto_decompress=False)
    client = await aiohttp_client(test_server)

    response = await client.post(
        "/gitlab/webhook",
        data=compress(json.dumps(payload).encode()),
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": encoding,
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event-UUID": f"compressed-{encoding}-001",
        },
    )

    assert response.status == 200
    assert await response.json() == {"status": "ok", "created": 1}
    ingestor.process_event.assert_awaited_once_with(
        payload,
        f"compressed-{encoding}-001",
        "brain_v42",
    )


@pytest.mark.parametrize(
    ("encoding", "body"),
    [
        (None, b"x" * (1024**2)),
        ("gzip", gzip.compress(b"x" * (1024**2 + 1))),
    ],
    ids=["raw", "gzip-bomb"],
)
async def test_runner_rejects_bounded_webhook_bodies_before_ingestion(
    aiohttp_client: Any,
    aiohttp_server: Any,
    encoding: str | None,
    body: bytes,
) -> None:
    ingestor = AsyncMock()
    server = MetricsServer(
        MagicMock(spec=MetricsCollector),
        MagicMock(),
        host="127.0.0.1",
        gitlab_ingestor=ingestor,
        project_key_resolver=AsyncMock(return_value="brain_v42"),
        webhook_secret="secret",
    )
    test_server = await aiohttp_server(server._build_app(), auto_decompress=False)
    client = await aiohttp_client(test_server)
    headers = {
        "Content-Type": "application/json",
        "X-Gitlab-Token": "secret",
        "X-Gitlab-Event-UUID": "bounded-webhook-001",
    }
    if encoding is not None:
        headers["Content-Encoding"] = encoding

    response = await client.post("/gitlab/webhook", data=body, headers=headers)

    assert response.status == 413
    assert await response.json() == {"status": "payload_too_large"}
    ingestor.process_event.assert_not_awaited()


async def test_runner_rejects_invalid_compressed_webhook_without_logging_input(
    aiohttp_client: Any,
    aiohttp_server: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ingestor = AsyncMock()
    server = MetricsServer(
        MagicMock(spec=MetricsCollector),
        MagicMock(),
        host="127.0.0.1",
        gitlab_ingestor=ingestor,
        project_key_resolver=AsyncMock(return_value="brain_v42"),
        webhook_secret="secret",
    )
    test_server = await aiohttp_server(server._build_app(), auto_decompress=False)
    client = await aiohttp_client(test_server)

    response = await client.post(
        "/gitlab/webhook",
        data=b"SENSITIVE_INVALID_GZIP",
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event-UUID": "invalid-compressed-001",
        },
    )

    assert response.status == 400
    assert await response.json() == {"status": "invalid_request"}
    assert "SENSITIVE_INVALID_GZIP" not in caplog.text
    ingestor.process_event.assert_not_awaited()


async def test_runner_rejects_unauthenticated_compressed_bomb_before_decompression(
    aiohttp_client: Any,
    aiohttp_server: Any,
) -> None:
    ingestor = AsyncMock()
    server = MetricsServer(
        MagicMock(spec=MetricsCollector),
        MagicMock(),
        host="127.0.0.1",
        gitlab_ingestor=ingestor,
        project_key_resolver=AsyncMock(return_value="brain_v42"),
        webhook_secret="secret",
    )
    test_server = await aiohttp_server(server._build_app(), auto_decompress=False)
    client = await aiohttp_client(test_server)

    response = await client.post(
        "/gitlab/webhook",
        data=gzip.compress(b"x" * (1024**2 + 1)),
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "X-Gitlab-Event-UUID": "unauthenticated-bomb-001",
        },
    )

    assert response.status == 401
    ingestor.process_event.assert_not_awaited()


async def test_otlp_endpoint_never_logs_or_persists_sensitive_input(
    aiohttp_client: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_factory = MagicMock()
    settings = MagicMock(
        embedding_service_url="http://localhost:8003",
        embedding_dimension=1024,
    )
    with patch("brain_v42.metrics.collector.get_settings", return_value=settings):
        collector = MetricsCollector(engine=MagicMock(), session_factory=session_factory)
    registry = CodexConversationRegistry(secret=b"x" * 32)
    server = _server(registry, collector=collector)
    client = await aiohttp_client(server._build_app())
    recent_before = collector.get_recent_log()

    with patch.object(collector, "record_cost", wraps=collector.record_cost) as record_cost:
        with capture_logs() as structured_logs:
            response = await client.post(
                "/v1/logs",
                data=FIXTURE_PATH.read_bytes(),
                headers={
                    "Content-Type": "application/json",
                    "X-Private-Sentinel": "SENSITIVE_HEADER_SENTINEL",
                },
            )

    assert response.status == 200
    assert record_cost.call_count == 0
    assert session_factory.call_count == 0
    assert collector.get_recent_log() == recent_before
    observed = "\n".join(
        (
            json.dumps(registry.snapshot(), sort_keys=True),
            json.dumps(structured_logs, sort_keys=True, default=str),
            caplog.text,
            json.dumps(collector.get_recent_log(), sort_keys=True),
        )
    )
    for sentinel in (
        FAKE_UUID,
        "SENSITIVE_RESOURCE_SENTINEL",
        "SENSITIVE_BODY_SENTINEL",
        "SENSITIVE_EMAIL_SENTINEL",
        "SENSITIVE_ACCOUNT_SENTINEL",
        "SENSITIVE_PROMPT_SENTINEL",
        "SENSITIVE_MCP_SENTINEL",
        "SENSITIVE_HEADER_SENTINEL",
    ):
        assert sentinel not in observed


async def test_otlp_batch_is_projected_into_cockpit_without_disclosing_input(
    aiohttp_client: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    session.execute.return_value = MagicMock(
        one=MagicMock(return_value=(0, 0)),
        fetchall=MagicMock(return_value=[]),
    )
    session_factory = MagicMock(return_value=session)
    settings = MagicMock(
        embedding_service_url="http://localhost:8003",
        embedding_dimension=1024,
        brain_mcp_transport="http",
    )
    with patch("brain_v42.metrics.collector.get_settings", return_value=settings):
        collector = MetricsCollector(engine=MagicMock(), session_factory=session_factory)
    collector.record_cost(model="existing-cost", cost_usd=2.5)
    registry = CodexConversationRegistry(secret=b"x" * 32)
    server = _server(registry, collector=collector)
    client = await aiohttp_client(server._build_app())
    recent_before = collector.get_recent_log()

    with (
        patch.object(collector, "record_cost", wraps=collector.record_cost) as record_cost,
        patch(
            "brain_v42.metrics.cockpit.collect_memory_stats",
            new=AsyncMock(
                return_value={
                    "episodes": 0,
                    "episodic_mb": 0,
                    "semantic_chunks": 0,
                    "semantic_mb": 0,
                    "lastCompaction": None,
                    "freedLastCompaction": 0,
                    "vectorIndex": None,
                }
            ),
        ),
        patch("brain_v42.metrics.cockpit.get_settings", return_value=settings),
        capture_logs() as structured_logs,
    ):
        post_response = await client.post(
            "/v1/logs",
            data=FIXTURE_PATH.read_bytes(),
            headers={
                "Content-Type": "application/json",
                "X-Private-Sentinel": "SENSITIVE_HEADER_SENTINEL",
            },
        )
        get_response = await client.get("/api/cockpit")
        cockpit = await get_response.json()

    expected_digest = hmac.new(
        b"x" * 32,
        b"codex-conversation-id\0" + FAKE_UUID.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()[:32]
    assert post_response.status == 200
    assert get_response.status == 200
    assert cockpit["metrics"]["active_convs"] == 1
    assert cockpit["metrics"]["ctx_tokens"] == 1234
    assert cockpit["activeConvs"] == [
        {
            "id": f"codex-{expected_digest}",
            "topic": "[redacted]",
            "agent": "codex",
            "started": cockpit["activeConvs"][0]["started"],
            "turns": 1,
            "tokens": 1234,
            "model": "gpt-5.4",
            "cost": None,
        }
    ]
    assert cockpit["cost"]["today"] == pytest.approx(2.5)
    assert cockpit["cost"]["byModel"] == [{"m": "existing-cost", "v": 2.5, "pct": 1.0}]
    assert record_cost.call_count == 0
    assert collector.get_recent_log() == recent_before

    observed = "\n".join(
        (
            json.dumps(cockpit, sort_keys=True),
            json.dumps(structured_logs, sort_keys=True, default=str),
            caplog.text,
            json.dumps(collector.get_recent_log(), sort_keys=True),
            repr(session.execute.await_args_list),
        )
    )
    for sentinel in (
        FAKE_UUID,
        "SENSITIVE_RESOURCE_SENTINEL",
        "SENSITIVE_BODY_SENTINEL",
        "SENSITIVE_EMAIL_SENTINEL",
        "SENSITIVE_ACCOUNT_SENTINEL",
        "SENSITIVE_PROMPT_SENTINEL",
        "SENSITIVE_MCP_SENTINEL",
        "SENSITIVE_HEADER_SENTINEL",
    ):
        assert sentinel not in observed
