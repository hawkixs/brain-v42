"""A metrics-sidecar rejection must leave a trace — without reintroducing anything.

Ticket `d5e4bd73`. The three POST receivers refuse a non-loopback peer (403), an
unsupported representation (415), an oversize body (413), saturation (503), a body that
drags (408) and a malformed payload (400). **All these defences work and nobody sees
them work**: no rejection left a trace, so one could neither know one was saturating,
nor know one had saturated yesterday.

The trap these tests pin as much as the functionality: this component hashes the raw
identifiers ON RECEPTION, with a per-process secret. A naive access log would
reintroduce exactly what the hashing removes. Hence
``test_the_access_log_carries_nothing_but_constants``, which locks the field set by
equality — not by "does not contain", which would let the next field through.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import streams
from aiohttp.test_utils import make_mocked_request
from structlog.testing import capture_logs

from brain_v42.metrics import server as server_module
from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.codex_telemetry import MAX_IN_FLIGHT_REQUESTS, MAX_REQUEST_BYTES
from brain_v42.metrics.server import MetricsServer

_DEADLINE_FOR_TESTS = 0.2
_ACCESS_LOG_EVENT = "metrics_server.receiver_rejected"

# The non-loopback peer of the 403 tests: a TEST-NET-3 address (RFC 5737), never routed.
_FOREIGN_PEER = "203.0.113.9"


def _server() -> MetricsServer:
    return MetricsServer(
        MagicMock(),
        MagicMock(),
        host="127.0.0.1",
        codex_registry=ClientActivityRegistry(secret=b"x" * 32),
    )


def _transport(address: str = "127.0.0.1") -> MagicMock:
    transport = MagicMock()
    transport.get_extra_info.return_value = (address, 4318)
    return transport


def _stream(*, data: bytes | None = None, stall: bool = False) -> streams.StreamReader:
    stream = streams.StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    if stall:
        stream.feed_data(b"{")  # starts then goes quiet: never a feed_eof
        return stream
    stream.feed_data(data or b"")
    stream.feed_eof()
    return stream


def _request(path: str, **kwargs: Any) -> Any:
    headers = {"Content-Type": "application/json"}
    headers.update(kwargs.pop("headers", {}))
    return make_mocked_request(
        "POST",
        path,
        headers=headers,
        transport=kwargs.pop("transport", None) or _transport(),
        **kwargs,
    )


def _rejections(server: MetricsServer, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("event") == _ACCESS_LOG_EVENT]


# --- The six triggers, one per genuinely reachable code -------------------------------
# Six and not five: `415` is reached by TWO sites (non-identity encoding, unsupported
# media type) and appears in no description in the ticket. Measured, not assumed.


async def _trigger_403(server: MetricsServer) -> Any:
    return await server._handle_codex_logs(
        _request("/v1/logs", transport=_transport(_FOREIGN_PEER))
    )


async def _trigger_415(server: MetricsServer) -> Any:
    return await server._handle_codex_logs(
        _request("/v1/logs", headers={"Content-Encoding": "gzip"})
    )


async def _trigger_413(server: MetricsServer) -> Any:
    return await server._handle_codex_logs(
        _request("/v1/logs", headers={"Content-Length": str(MAX_REQUEST_BYTES + 1)})
    )


async def _trigger_503(server: MetricsServer) -> Any:
    for _ in range(MAX_IN_FLIGHT_REQUESTS):
        await server._codex_request_slots.acquire()
    try:
        return await server._handle_codex_logs(_request("/v1/logs"))
    finally:
        for _ in range(MAX_IN_FLIGHT_REQUESTS):
            server._codex_request_slots.release()


async def _trigger_408(server: MetricsServer) -> Any:
    return await asyncio.wait_for(
        server._handle_codex_logs(_request("/v1/logs", payload=_stream(stall=True))),
        timeout=3,
    )


async def _trigger_400(server: MetricsServer) -> Any:
    body = b"definitivement pas du JSON"
    return await server._handle_codex_logs(
        _request(
            "/v1/logs",
            headers={"Content-Length": str(len(body))},
            payload=_stream(data=body),
        )
    )


_TRIGGERS = [
    (403, _trigger_403),
    (415, _trigger_415),
    (413, _trigger_413),
    (503, _trigger_503),
    (408, _trigger_408),
    (400, _trigger_400),
]


@pytest.fixture(autouse=True)
def _short_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        server_module, "_OTLP_BODY_READ_TIMEOUT_SECONDS", _DEADLINE_FOR_TESTS, raising=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "trigger"), _TRIGGERS, ids=[str(s) for s, _ in _TRIGGERS])
async def test_every_rejection_code_emits_exactly_one_access_log_line(
    status: int, trigger: Any
) -> None:
    """One rejection served, one line emitted — for EACH of the six codes, not for the 503 alone."""
    server = _server()

    with capture_logs() as records:
        response = await trigger(server)

    assert response.status == status
    lines = _rejections(server, records)
    assert len(lines) == 1, f"{status}: {len(lines)} ligne(s) au lieu d'une"
    assert lines[0]["status"] == status
    assert lines[0]["reason"] == server_module._OTLP_ERROR_STATUSES[status][1]


@pytest.mark.asyncio
async def test_an_accepted_request_emits_no_access_log_line() -> None:
    """NEGATIVE WITNESS: without it, an unconditional log would pass every test above."""
    server = _server()
    body = json.dumps({"resourceLogs": []}, separators=(",", ":")).encode()

    with capture_logs() as records:
        response = await server._handle_codex_logs(
            _request(
                "/v1/logs",
                headers={"Content-Length": str(len(body))},
                payload=_stream(data=body),
            )
        )

    assert response.status == 200
    assert _rejections(server, records) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "expected_receiver", "path"),
    [
        ("_handle_codex_logs", "codex_logs", "/v1/logs"),
        ("_handle_claude_logs", "claude_logs", "/v1/logs/claude"),
        ("_handle_client_activity", "client_activity", "/v1/client-activity"),
    ],
)
async def test_the_access_log_names_which_receiver_rejected(
    handler_name: str, expected_receiver: str, path: str
) -> None:
    """Three receivers share ONE budget: without this field, a 503 does not say who was saturating."""
    server = _server()
    handler = getattr(server, handler_name)

    with capture_logs() as records:
        response = await handler(_request(path, transport=_transport(_FOREIGN_PEER)))

    assert response.status == 403
    lines = _rejections(server, records)
    assert len(lines) == 1
    assert lines[0]["receiver"] == expected_receiver


@pytest.mark.asyncio
async def test_the_access_log_carries_nothing_but_constants() -> None:
    """The heart of the batch: key-set equality, not a "does not contain".

    An `assert "peer" not in line` would let the next field through. Equality fails the
    test as soon as a field is ADDED, which forces the question to be asked again.
    """
    server = _server()
    # A CANARY, not a secret. The previous form named this variable with the word
    # "secret" and gave it a key-shaped value: `gitleaks` flagged it as
    # `generic-api-key`, entropy 3.913, and reddened CI. A false positive in the
    # strict sense — but fixing it AT THE SOURCE beats a `gitleaks:allow` or an
    # allowlist entry, which would weaken a control to silence a red caused by our
    # own phrasing. "Canary" also says better what the value does: it is injected so
    # that we can check it does NOT come back out. Do not re-quote the old value here
    # — a comment that quotes what it explains reintroduces it, which happened on the
    # first attempt.
    canary = "canary-must-not-leak"

    with capture_logs() as records:
        await server._handle_codex_logs(
            _request(
                "/v1/logs",
                headers={"traceparent": canary, "User-Agent": canary},
                transport=_transport(_FOREIGN_PEER),
            )
        )

    line = _rejections(server, records)[0]
    assert set(line) == {"event", "log_level", "receiver", "status", "reason"}
    # Neither the peer address, nor a header, nor the raw path must show through.
    rendered = repr(line)
    assert canary not in rendered
    assert _FOREIGN_PEER not in rendered
    assert "/v1/logs" not in rendered
    # The three remaining values belong to CONSTANT, closed sets.
    assert line["receiver"] in {"codex_logs", "claude_logs", "client_activity"}
    assert line["status"] in server_module._OTLP_ERROR_STATUSES
    assert line["reason"] == server_module._OTLP_ERROR_STATUSES[line["status"]][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "trigger"), _TRIGGERS, ids=[str(s) for s, _ in _TRIGGERS])
async def test_a_failing_access_log_can_never_break_the_rejection(
    monkeypatch: pytest.MonkeyPatch, status: int, trigger: Any
) -> None:
    """The instrument does not become the failure — least of all under saturation, which it measures."""
    server = _server()

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("le journal est cassé")

    monkeypatch.setattr(server_module.logger, "warning", _explode, raising=True)

    response = await trigger(server)

    assert response.status == status
    assert json.loads(response.body)["code"] == server_module._OTLP_ERROR_STATUSES[status][0]


def test_no_declared_status_can_ship_without_an_access_log() -> None:
    """A STRUCTURAL guard: a 7th code added to the table will be logged without a thought.

    The six rejections all go through `_otlp_error`, the sole constructor of these
    responses. Logging THERE makes the coverage unfailing by construction, instead of
    leaving it to depend on the next call site's vigilance.
    """
    counters = server_module.ReceiverRejectionCounters()
    for status in server_module._OTLP_ERROR_STATUSES:
        with capture_logs() as records:
            response = server_module._otlp_error(status, receiver="codex_logs", counters=counters)
        assert response.status == status
        assert [r for r in records if r.get("event") == _ACCESS_LOG_EVENT], (
            f"{status} déclaré dans la table mais non journalisé"
        )
