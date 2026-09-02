"""HTTP boundary of the sidecar's two activity receivers.

``POST /v1/client-activity`` receives the observations pushed by the MCP process;
``POST /v1/logs/claude`` receives Claude Code's OTLP. Two distinct routes rather
than one receiver guessing the schema: guessing would require probing the
attributes of a payload that has not been validated yet.

Every hardening property of ``/v1/logs`` is re-pinned **route by route**:
loopback-only, bounded body, saturation, fail-closed rejection, strict
``Content-Type`` and encoding. A shared decorator proves nothing until the new
route has been seen refusing on its own.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request

from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.client_observation import MAX_OBSERVATION_BYTES
from brain_v42.metrics.codex_telemetry import MAX_IN_FLIGHT_REQUESTS, MAX_REQUEST_BYTES
from brain_v42.metrics.server import MetricsServer

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"
JSON_HEADERS = {"Content-Type": "application/json"}
CLIENT_ACTIVITY_PATH = "/v1/client-activity"
CLAUDE_LOGS_PATH = "/v1/logs/claude"

_RECEIVER_PATHS = [CLIENT_ACTIVITY_PATH, CLAUDE_LOGS_PATH]
_RECEIVERS = [
    pytest.param(CLIENT_ACTIVITY_PATH, "_handle_client_activity", id="client-activity"),
    pytest.param(CLAUDE_LOGS_PATH, "_handle_claude_logs", id="claude-logs"),
]
_BOUNDED_RECEIVERS = [
    pytest.param(
        CLIENT_ACTIVITY_PATH,
        "_handle_client_activity",
        MAX_OBSERVATION_BYTES,
        id="client-activity",
    ),
    pytest.param(CLAUDE_LOGS_PATH, "_handle_claude_logs", MAX_REQUEST_BYTES, id="claude-logs"),
]


def _observations(items: list[dict[str, object]]) -> bytes:
    return json.dumps({"observations": items}, separators=(",", ":")).encode()


def _claude_record(
    *,
    session_id: str = FAKE_UUID,
    event_name: str = "user_prompt",
) -> dict[str, object]:
    return {
        "timeUnixNano": "1",
        "attributes": [
            {"key": "event.name", "value": {"stringValue": event_name}},
            {"key": "session.id", "value": {"stringValue": session_id}},
        ],
    }


def _claude_logs(*records: dict[str, object]) -> bytes:
    return json.dumps(
        {"resourceLogs": [{"scopeLogs": [{"logRecords": list(records)}]}]},
        separators=(",", ":"),
    ).encode()


def _valid_body(path: str) -> bytes:
    if path == CLIENT_ACTIVITY_PATH:
        return _observations([{"actor": "brain-v42", "calls": 1}])
    return _claude_logs(_claude_record())


def _partially_invalid_body(path: str) -> bytes:
    """A batch whose first element is valid and whose second is not."""
    if path == CLIENT_ACTIVITY_PATH:
        return _observations(
            [{"actor": "brain-v42", "calls": 1}, {"actor": "brain-v42", "calls": "2"}]
        )
    return _claude_logs(_claude_record(), _claude_record(session_id="not-a-uuid"))


def _oversize_body(path: str) -> bytes:
    limit = MAX_OBSERVATION_BYTES if path == CLIENT_ACTIVITY_PATH else MAX_REQUEST_BYTES
    return b"SENSITIVE_OVERSIZE" + b" " * limit


def _registry() -> ClientActivityRegistry:
    return ClientActivityRegistry(secret=b"\x03" * 32)


def _server(registry: ClientActivityRegistry, *, host: str = "127.0.0.1") -> MetricsServer:
    return MetricsServer(MagicMock(), MagicMock(), host=host, codex_registry=registry)


def _routes(app: web.Application) -> set[tuple[str, str]]:
    return {(route.method, route.resource.canonical) for route in app.router.routes()}


def _loopback_transport(address: str) -> MagicMock:
    transport = MagicMock()
    transport.get_extra_info.return_value = (address, 4318)
    return transport


def _fed_stream(*chunks: bytes) -> streams.StreamReader:
    """A body already in memory, whose read-or-not state can be observed."""
    stream = streams.StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    for chunk in chunks:
        stream.feed_data(chunk)
    stream.feed_eof()
    return stream


def _request_with_body(
    path: str,
    stream: streams.StreamReader,
    *,
    declared_length: int | None,
) -> Any:
    """``declared_length=None`` simulates a chunked body, with no ``Content-Length``."""
    headers = {"Content-Type": "application/json"}
    if declared_length is not None:
        headers["Content-Length"] = str(declared_length)
    return make_mocked_request(
        "POST",
        path,
        headers=headers,
        transport=_loopback_transport("127.0.0.1"),
        payload=stream,
    )


async def _rpc_status(response: Any, *, http_status: int, rpc_code: int) -> str:
    assert response.status == http_status
    assert response.content_type == "application/json"
    body = await response.text()
    status = json.loads(body)
    assert set(status) == {"code", "message", "details"}
    assert status["code"] == rpc_code
    assert isinstance(status["message"], str) and status["message"]
    assert status["details"] == []
    return body


@pytest.mark.parametrize("path", _RECEIVER_PATHS)
@pytest.mark.parametrize("host", ["127.0.0.1", "127.42.0.9", "::1", "localhost"])
def test_receiver_route_is_registered_on_a_loopback_bind(host: str, path: str) -> None:
    """Positive control for the absence test that follows."""
    assert ("POST", path) in _routes(_server(_registry(), host=host)._build_app())


@pytest.mark.parametrize("path", _RECEIVER_PATHS)
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.8", "metrics.internal"])
def test_receiver_route_is_absent_on_a_non_loopback_bind(host: str, path: str) -> None:
    """The DEFAULT posture (`silent`, eac03668) — a named choice, not an oversight.

    This test pins the historical behaviour WHILE AWAITING the operator's
    arbitration, it no longer enshrines it by omission: the other two postures
    (warn, fail_closed) exist and are tested by
    test_metrics_nonloopback_posture.py.
    """
    assert ("POST", path) not in _routes(_server(_registry(), host=host)._build_app())


async def test_client_activity_applies_the_observation(aiohttp_client: Any) -> None:
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        CLIENT_ACTIVITY_PATH,
        data=_observations([{"actor": "brain-v42", "session": FAKE_UUID, "calls": 2}]),
        headers=JSON_HEADERS,
    )

    assert response.status == 200
    rows = registry.snapshot()["clients"]
    assert len(rows) == 1
    assert rows[0]["actor"] == "brain-v42"
    assert rows[0]["brain_calls"] == 2


async def test_client_activity_without_a_session_yields_a_residual_row(
    aiohttp_client: Any,
) -> None:
    """The measured NOMINAL case: no client declares its session today."""
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        CLIENT_ACTIVITY_PATH,
        data=_observations([{"actor": "codex", "calls": 3}]),
        headers=JSON_HEADERS,
    )

    assert response.status == 200
    rows = registry.snapshot()["clients"]
    assert len(rows) == 1
    assert rows[0]["id"] == "unattributed:codex"
    assert rows[0]["kind"] == "unattributed"
    assert rows[0]["brain_calls"] == 3


async def test_client_activity_hashes_the_session_at_reception(aiohttp_client: Any) -> None:
    """The raw UUID crosses the loopback socket; it must never come back out.

    The absence assertion has its positive control in the same payload: the row
    does exist, so the green does not come from a registry left empty.
    """
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        CLIENT_ACTIVITY_PATH,
        data=_observations([{"actor": "brain-v42", "session": FAKE_UUID, "calls": 1}]),
        headers=JSON_HEADERS,
    )

    assert response.status == 200
    snapshot = registry.snapshot()
    assert snapshot["clients"][0]["brain_calls"] == 1
    assert FAKE_UUID not in json.dumps(snapshot)


async def test_claude_logs_route_feeds_the_registry(aiohttp_client: Any) -> None:
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        CLAUDE_LOGS_PATH,
        data=_claude_logs(_claude_record()),
        headers=JSON_HEADERS,
    )

    assert response.status == 200
    assert await response.read() == b"{}"
    rows = registry.snapshot()["clients"]
    assert len(rows) == 1
    assert rows[0]["agent"] == "claude"
    assert rows[0]["turns"] == 1


async def test_claude_logs_route_leaves_the_legacy_codex_list_empty(
    aiohttp_client: Any,
) -> None:
    """A Claude session must not inflate "Codex activity" before the switch-over.

    Positive control for the absence: ``clients`` is populated in the same payload.
    """
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        CLAUDE_LOGS_PATH,
        data=_claude_logs(_claude_record()),
        headers=JSON_HEADERS,
    )

    assert response.status == 200
    snapshot = registry.snapshot()
    assert len(snapshot["clients"]) == 1
    assert snapshot["activeConvs"] == []
    assert snapshot["active_convs"] == 0


@pytest.mark.parametrize(("path", "handler"), _RECEIVERS)
async def test_receiver_rejects_a_non_loopback_peer_despite_forwarding_headers(
    path: str,
    handler: str,
) -> None:
    """Positive control: the nominal-path tests go through a real loopback peer."""
    server = _server(_registry())
    request = make_mocked_request(
        "POST",
        path,
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-For": "127.0.0.1",
            "Forwarded": "for=127.0.0.1",
        },
        transport=_loopback_transport("203.0.113.9"),
    )

    response = await getattr(server, handler)(request)

    assert response.status == 403
    status = json.loads(response.body)
    assert status["code"] == 7
    assert status["details"] == []


@pytest.mark.parametrize(("path", "handler"), _RECEIVERS)
async def test_receiver_requires_a_numeric_loopback_peer(path: str, handler: str) -> None:
    server = _server(_registry())
    request = make_mocked_request(
        "POST",
        path,
        headers=JSON_HEADERS,
        transport=_loopback_transport("localhost"),
    )

    response = await getattr(server, handler)(request)

    assert response.status == 403


@pytest.mark.parametrize(("path", "handler"), _RECEIVERS)
async def test_receiver_returns_retryable_unavailable_without_waiting_when_saturated(
    path: str,
    handler: str,
) -> None:
    """The new receivers share the sidecar's in-flight request budget."""
    server = _server(_registry())
    for _ in range(MAX_IN_FLIGHT_REQUESTS):
        await server._codex_request_slots.acquire()
    request = make_mocked_request(
        "POST",
        path,
        headers=JSON_HEADERS,
        transport=_loopback_transport("127.0.0.1"),
    )

    try:
        response = await getattr(server, handler)(request)
    finally:
        for _ in range(MAX_IN_FLIGHT_REQUESTS):
            server._codex_request_slots.release()

    assert response.status == 503
    assert response.headers["Retry-After"] == "1"
    assert json.loads(response.body)["code"] == 14


@pytest.mark.parametrize("path", _RECEIVER_PATHS)
@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/octet-stream", "application/json; profile=private"],
)
async def test_receiver_rejects_unsupported_media_types(
    aiohttp_client: Any,
    path: str,
    content_type: str,
) -> None:
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        path,
        data=_valid_body(path),
        headers={"Content-Type": content_type},
    )

    await _rpc_status(response, http_status=415, rpc_code=3)
    assert registry.snapshot()["clients"] == []


@pytest.mark.parametrize("path", _RECEIVER_PATHS)
@pytest.mark.parametrize("encoding", ["gzip", "identity, gzip"])
async def test_receiver_rejects_a_non_identity_content_encoding(
    aiohttp_client: Any,
    path: str,
    encoding: str,
) -> None:
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())
    body = gzip.compress(_valid_body(path)) if encoding == "gzip" else b"encoded"

    response = await client.post(
        path,
        data=body,
        headers={"Content-Type": "application/json", "Content-Encoding": encoding},
    )

    await _rpc_status(response, http_status=415, rpc_code=3)
    assert registry.snapshot()["clients"] == []


@pytest.mark.parametrize("path", _RECEIVER_PATHS)
async def test_receiver_accepts_an_explicit_identity_encoding(
    aiohttp_client: Any,
    path: str,
) -> None:
    """Positive control for the encoding rejection: ``identity`` stays accepted."""
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        path,
        data=_valid_body(path),
        headers={"Content-Type": "application/json", "Content-Encoding": "identity"},
    )

    assert response.status == 200
    assert len(registry.snapshot()["clients"]) == 1


@pytest.mark.parametrize(("path", "handler"), _RECEIVERS)
@pytest.mark.parametrize(
    "headers",
    [
        [("Content-Type", "application/json"), ("Content-Type", "application/json")],
        [
            ("Content-Type", "application/json"),
            ("Content-Encoding", "identity"),
            ("Content-Encoding", "identity"),
        ],
    ],
    ids=["content-type", "content-encoding"],
)
async def test_receiver_rejects_duplicate_representation_headers(
    path: str,
    handler: str,
    headers: list[tuple[str, str]],
) -> None:
    server = _server(_registry())
    request = make_mocked_request(
        "POST",
        path,
        headers=headers,
        transport=_loopback_transport("127.0.0.1"),
    )

    response = await getattr(server, handler)(request)

    assert response.status == 415
    assert json.loads(response.body)["code"] == 3


@pytest.mark.parametrize("path", _RECEIVER_PATHS)
@pytest.mark.parametrize(
    "body",
    [b"{", b"[]", b"SENSITIVE_BAD_INPUT"],
    ids=["truncated", "root-array", "text"],
)
async def test_receiver_maps_a_malformed_body_to_a_static_bad_request(
    aiohttp_client: Any,
    path: str,
    body: bytes,
) -> None:
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(path, data=body, headers=JSON_HEADERS)

    rendered = await _rpc_status(response, http_status=400, rpc_code=3)
    assert "SENSITIVE_BAD_INPUT" not in rendered
    assert registry.snapshot()["clients"] == []


@pytest.mark.parametrize("path", _RECEIVER_PATHS)
async def test_receiver_validates_the_whole_batch_before_mutating_the_registry(
    aiohttp_client: Any,
    path: str,
) -> None:
    """The batch carries a valid element then an invalid one: nothing must remain.

    Positive control: the nominal-path tests send that same first element, alone,
    and do produce a row.
    """
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        path,
        data=_partially_invalid_body(path),
        headers=JSON_HEADERS,
    )

    await _rpc_status(response, http_status=400, rpc_code=3)
    assert registry.snapshot()["clients"] == []


@pytest.mark.parametrize("path", _RECEIVER_PATHS)
async def test_receiver_rejects_an_oversize_body_without_echoing_it(
    aiohttp_client: Any,
    path: str,
) -> None:
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(path, data=_oversize_body(path), headers=JSON_HEADERS)

    rendered = await _rpc_status(response, http_status=413, rpc_code=8)
    assert "SENSITIVE_OVERSIZE" not in rendered
    assert registry.snapshot()["clients"] == []


async def test_client_activity_accepts_a_body_exactly_at_its_own_size_limit(
    aiohttp_client: Any,
) -> None:
    """Positive control for the 413: the wire bound really is MAX_OBSERVATION_BYTES.

    Without it, a receiver refusing any body over a hundred bytes would pass the
    rejection test green. The bound is far tighter than OTLP's: it must be proved
    on its own terms.
    """
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())
    core = _observations([{"actor": "brain-v42", "calls": 1}])
    body = core + b" " * (MAX_OBSERVATION_BYTES - len(core))
    assert len(body) == MAX_OBSERVATION_BYTES

    response = await client.post(CLIENT_ACTIVITY_PATH, data=body, headers=JSON_HEADERS)

    assert response.status == 200
    assert registry.snapshot()["clients"][0]["brain_calls"] == 1


async def test_client_activity_rejects_a_body_one_byte_over_the_otlp_limit_too(
    aiohttp_client: Any,
) -> None:
    """The tight bound applies even to a body OTLP would accept.

    ``MAX_OBSERVATION_BYTES`` (16 KiB) is far below ``MAX_REQUEST_BYTES``
    (256 KiB). This test pins the end-to-end **status**, and nothing more:
    measured, it stays green on a receiver stripped of every bound, because
    ``decode_observations`` refuses the same body on its own. The HTTP guardrail
    has its own witnesses, just below.
    """
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())
    core = _observations([{"actor": "brain-v42", "calls": 1}])
    body = core + b" " * (MAX_OBSERVATION_BYTES + 1 - len(core))
    assert MAX_OBSERVATION_BYTES < len(body) < MAX_REQUEST_BYTES

    response = await client.post(CLIENT_ACTIVITY_PATH, data=body, headers=JSON_HEADERS)

    await _rpc_status(response, http_status=413, rpc_code=8)
    assert registry.snapshot()["clients"] == []


@pytest.mark.parametrize(("path", "handler", "max_bytes"), _BOUNDED_RECEIVERS)
async def test_receiver_refuses_a_declared_oversize_length_without_reading_the_body(
    path: str,
    handler: str,
    max_bytes: int,
) -> None:
    """The only possible witness of the HTTP bound: the body is not buffered.

    Measured: removing ``server.py``'s two bounds leaves every 413 green, because
    ``decode_observations`` (16 KiB) and ``_load_json`` (256 KiB) refuse the same
    body one layer below. The status therefore distinguishes nothing. What the HTTP
    receiver brings, and it alone, is refusing **before** reading — that is the
    property pinned here.

    The ``/v1/client-activity`` case declares a length that sits well under
    ``MAX_REQUEST_BYTES``: a receiver settling for the shared OTLP guardrail would
    read that body in full.
    """
    server = _server(_registry())
    stream = _fed_stream(b"x" * (max_bytes + 1))
    request = _request_with_body(path, stream, declared_length=max_bytes + 1)

    response = await getattr(server, handler)(request)

    assert response.status == 413
    assert json.loads(response.body)["code"] == 8
    assert not stream.at_eof()
    assert server._codex_registry.snapshot()["clients"] == []


@pytest.mark.parametrize(("path", "handler", "max_bytes"), _BOUNDED_RECEIVERS)
async def test_receiver_stops_reading_a_chunked_body_that_crosses_its_limit(
    path: str,
    handler: str,
    max_bytes: int,
) -> None:
    """Without ``Content-Length``, only the stream bound protects memory.

    A chunked body declares no length: the header check cannot fire, and the read
    must stop by itself. The body is deliberately larger than the bounded read will
    consume, so that unread bytes remain to observe.
    """
    server = _server(_registry())
    chunk = 2**16
    chunks = (b"x" * chunk,) * (max_bytes // chunk + 4)
    stream = _fed_stream(*chunks)
    request = _request_with_body(path, stream, declared_length=None)

    response = await getattr(server, handler)(request)

    assert response.status == 413
    assert json.loads(response.body)["code"] == 8
    assert not stream.at_eof()
    assert server._codex_registry.snapshot()["clients"] == []


@pytest.mark.parametrize(("path", "handler", "max_bytes"), _BOUNDED_RECEIVERS)
async def test_receiver_reads_a_body_that_sits_exactly_on_its_limit(
    path: str,
    handler: str,
    max_bytes: int,
) -> None:
    """Positive control for the two ``not stream.at_eof()`` above.

    Without it, a receiver refusing everything without ever reading anything would
    pass them green. Here the body is accepted, hence read to the end — the harness
    does distinguish the two cases.
    """
    server = _server(_registry())
    core = _valid_body(path)
    body = core + b" " * (max_bytes - len(core))
    assert len(body) == max_bytes
    stream = _fed_stream(body)
    request = _request_with_body(path, stream, declared_length=max_bytes)

    response = await getattr(server, handler)(request)

    assert response.status == 200
    assert stream.at_eof()
    assert len(server._codex_registry.snapshot()["clients"]) == 1
