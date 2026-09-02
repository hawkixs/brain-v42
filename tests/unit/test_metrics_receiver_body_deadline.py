"""The sidecar's three receivers must bound the body READ, not only its size.

Measured on 2026-08-10 (ticket 5fa2771e). ``_read_bounded_otlp_body`` had no deadline,
and the 4-in-flight-request semaphore is acquired AROUND the read. Four loopback
connections announcing a chunked body, sending one byte then going quiet were therefore
enough to lock the three receivers — measured still locked after 3.2 s, and with no exit
mechanism at all: that is "for life", not "a few seconds".

aiohttp 3.14.3 offers no upstream guard: ``RequestHandler`` exposes keepalive_timeout,
lingering_time, read_bufsize, max_line_size, and nothing about the body read. The
application-level fix is the only one possible.

The shape is the embedding shim's (``services/embedding_shim/shim_app.py:167``), which
has had exactly this guard since it shipped: a TOTAL deadline set outside the loop, never
per chunk — a sender emitting one byte every four seconds would pass indefinitely under a
per-chunk guard.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import streams
from aiohttp.test_utils import make_mocked_request

from brain_v42.metrics import server as server_module
from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.client_observation import MAX_OBSERVATION_BYTES
from brain_v42.metrics.codex_telemetry import MAX_IN_FLIGHT_REQUESTS, MAX_REQUEST_BYTES
from brain_v42.metrics.server import MetricsServer

_DEADLINE_FOR_TESTS = 0.2


def _server(registry: ClientActivityRegistry) -> MetricsServer:
    return MetricsServer(MagicMock(), MagicMock(), host="127.0.0.1", codex_registry=registry)


def _loopback_transport() -> MagicMock:
    transport = MagicMock()
    transport.get_extra_info.return_value = ("127.0.0.1", 4318)
    return transport


def _stalled_stream() -> streams.StreamReader:
    """A chunked body that starts then goes quiet: one byte, never a ``feed_eof``."""
    stream = streams.StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    stream.feed_data(b"{")
    return stream


def _request(path: str, stream: streams.StreamReader) -> Any:
    """Without ``Content-Length``: it is a chunked body, the only shape that can freeze."""
    return make_mocked_request(
        "POST",
        path,
        headers={"Content-Type": "application/json"},
        transport=_loopback_transport(),
        payload=stream,
    )


_RECEIVERS = [
    ("/v1/logs", "_handle_codex_logs", MAX_REQUEST_BYTES),
    ("/v1/logs/claude", "_handle_claude_logs", MAX_REQUEST_BYTES),
    ("/v1/client-activity", "_handle_client_activity", MAX_OBSERVATION_BYTES),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "handler_name", "_max_bytes"), _RECEIVERS)
async def test_a_stalled_body_is_abandoned_by_every_receiver(
    monkeypatch: pytest.MonkeyPatch, path: str, handler_name: str, _max_bytes: int
) -> None:
    """The THREE receivers, because they share the same in-flight request budget.

    RED before the fix: the handler never returns, ``asyncio.wait_for`` cancels it and
    the test goes to TimeoutError before even the first assert.
    """
    monkeypatch.setattr(
        server_module, "_OTLP_BODY_READ_TIMEOUT_SECONDS", _DEADLINE_FOR_TESTS, raising=True
    )
    server = _server(ClientActivityRegistry(secret=b"x" * 32))
    handler = getattr(server, handler_name)

    response = await asyncio.wait_for(handler(_request(path, _stalled_stream())), timeout=3)

    assert response.status == 408
    body = json.loads(response.text or "{}")
    assert body["code"] == 4
    assert "timed out" in body["message"]


@pytest.mark.asyncio
async def test_a_storm_of_stalled_bodies_releases_the_shared_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test that is worth the ticket: the three routes' ingestion must RESUME.

    Without a deadline, four frozen bodies locked the semaphore until brain-metrics was
    restarted — measured: clean POST → 503 + Retry-After, and the registry stayed empty.
    """
    monkeypatch.setattr(
        server_module, "_OTLP_BODY_READ_TIMEOUT_SECONDS", _DEADLINE_FOR_TESTS, raising=True
    )
    registry = ClientActivityRegistry(secret=b"x" * 32)
    server = _server(registry)

    storm = [
        asyncio.create_task(server._handle_codex_logs(_request("/v1/logs", _stalled_stream())))
        for _ in range(MAX_IN_FLIGHT_REQUESTS)
    ]
    # Positive control: with no observed saturation, the assertions below would be
    # true for nothing.
    while not server._codex_request_slots.locked():
        await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(*storm), timeout=3)

    assert server._codex_request_slots.locked() is False, (
        "le budget de requêtes en vol n'est pas rendu : les trois récepteurs restent morts"
    )
    # A finer net than locked() — it catches a PARTIAL release. A private attribute
    # by choice: the contract is locked(), this is the belt.
    assert server._codex_request_slots._value == MAX_IN_FLIGHT_REQUESTS

    observation = json.dumps(
        {"observations": [{"actor": "brain-v42", "calls": 1, "session": "s"}]}
    ).encode()
    clean = streams.StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    clean.feed_data(observation)
    clean.feed_eof()

    after = await asyncio.wait_for(
        server._handle_client_activity(_request("/v1/client-activity", clean)), timeout=3
    )
    assert after.status == 200, "l'ingestion n'a pas repris après la tempête"


@pytest.mark.asyncio
async def test_a_slow_but_progressing_body_is_not_cut_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ANTI-TAUTOLOGY probe: a slow but progressing body must succeed.

    Without it, the previous test could be made green by refusing every body with no
    Content-Length, or by setting a near-zero budget — and the guard would no longer
    guard anything useful, it would break the legitimate use.
    """
    monkeypatch.setattr(
        server_module, "_OTLP_BODY_READ_TIMEOUT_SECONDS", _DEADLINE_FOR_TESTS, raising=True
    )
    registry = ClientActivityRegistry(secret=b"x" * 32)
    server = _server(registry)

    payload = json.dumps(
        {"observations": [{"actor": "brain-v42", "calls": 1, "session": "s"}]}
    ).encode()
    half = len(payload) // 2
    stream = streams.StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    stream.feed_data(payload[:half])

    async def _feed_the_rest() -> None:
        await asyncio.sleep(_DEADLINE_FOR_TESTS / 4)
        stream.feed_data(payload[half:])
        stream.feed_eof()

    feeder = asyncio.create_task(_feed_the_rest())
    try:
        response = await asyncio.wait_for(
            server._handle_client_activity(_request("/v1/client-activity", stream)), timeout=3
        )
    finally:
        await feeder

    assert response.status == 200, "un corps lent mais qui progresse a été coupé à tort"


def test_the_body_read_deadline_is_five_seconds() -> None:
    """The shipped value, pinned — same shape as the embedding shim's limits.

    It is read INSIDE THE BODY of the function and never as an argument default: a
    default is bound at ``def`` time, so a monkeypatch would have no effect and the
    tests above would take five seconds while believing they measured the guard.
    """
    assert server_module._OTLP_BODY_READ_TIMEOUT_SECONDS == 5.0


def test_the_timeout_status_is_declared_before_it_is_used() -> None:
    """``_otlp_error(408)`` raises KeyError as long as 408 is not declared.

    Without this entry, the RED of the tests above would be red for the WRONG reason
    (KeyError → 500) and one would believe the deadline broken.
    """
    assert 408 in server_module._OTLP_ERROR_STATUSES
