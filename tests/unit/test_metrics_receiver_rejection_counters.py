"""Every refusal served by the sidecar advances a counter — and its zero MEANS something.

Ticket `d5e4bd73`, track (b), complementing the shipped access log: the log tells
the story, the counter totals, and it is exposed in the same place as the other
metrics (GET /metrics). The ticket's lesson governs the shape: "a counter at zero
on a source that counts nothing is indistinguishable from a real zero". The
structure is therefore ALWAYS exposed, the three receivers present from startup — a
zero then reads as "the instrument is armed and has seen nothing", never as "nobody
is counting".
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import streams
from aiohttp.test_utils import make_mocked_request

from brain_v42.metrics import server as server_module
from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.codex_telemetry import MAX_IN_FLIGHT_REQUESTS, MAX_REQUEST_BYTES
from brain_v42.metrics.server import MetricsServer, ReceiverRejectionCounters

_DEADLINE_FOR_TESTS = 0.2
_FOREIGN_PEER = "203.0.113.9"

ALL_RECEIVERS = frozenset({"codex_logs", "claude_logs", "client_activity"})


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
        stream.feed_data(b"{")
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


def test_the_structure_is_armed_before_any_rejection() -> None:
    """The meaningful zero: the three receivers are exposed from construction time,
    each empty — the structure's presence proves the instrument counts, its content
    says what it has seen."""
    snapshot = _server()._rejection_counters.snapshot()

    assert set(snapshot) == ALL_RECEIVERS
    assert all(by_status == {} for by_status in snapshot.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "trigger"), _TRIGGERS, ids=[str(s) for s, _ in _TRIGGERS])
async def test_every_rejection_code_increments_its_counter(status: int, trigger: Any) -> None:
    server = _server()

    response = await trigger(server)

    assert response.status == status
    snapshot = server._rejection_counters.snapshot()
    assert snapshot["codex_logs"] == {str(status): 1}
    assert snapshot["claude_logs"] == {}
    assert snapshot["client_activity"] == {}


@pytest.mark.asyncio
async def test_an_accepted_request_increments_nothing() -> None:
    """NEGATIVE WITNESS: an unconditional increment would pass all the others."""
    server = _server()
    body = json.dumps({"resourceLogs": []}, separators=(",", ":")).encode()

    response = await server._handle_codex_logs(
        _request(
            "/v1/logs",
            headers={"Content-Length": str(len(body))},
            payload=_stream(data=body),
        )
    )

    assert response.status == 200
    assert all(by_status == {} for by_status in server._rejection_counters.snapshot().values())


@pytest.mark.asyncio
async def test_a_provoked_413_is_visible_in_the_metrics_payload() -> None:
    """The mandate's end-to-end check: a 413 provoked on /v1/logs appears in
    GET /metrics — in the same place as the rest."""
    server = _server()
    collector = server._collector
    collector.get_metrics.return_value = {"embedding_service": {}}
    collector.collect_process_metrics = AsyncMock(return_value={"active_processes": 0})
    # The rest of the handler aggregates other sources; they are mute here — only
    # the rejection-counter path is under test.
    for probe in (
        "collect_db_stats",
        "collect_search_quality",
        "collect_dream_metrics",
        "collect_nightly_ops",
    ):
        setattr(collector, probe, AsyncMock(return_value={}))
    server._embedding_svc.healthcheck = AsyncMock(return_value=True)

    rejected = await _trigger_413(server)
    assert rejected.status == 413

    response = await server._handle_metrics(make_mocked_request("GET", "/metrics"))
    payload = json.loads(response.body)

    assert payload["receiver_rejections"]["codex_logs"] == {"413": 1}
    # The two mute receivers stay PRESENT: their zero is meaningful.
    assert payload["receiver_rejections"]["claude_logs"] == {}
    assert payload["receiver_rejections"]["client_activity"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "trigger"), _TRIGGERS, ids=[str(s) for s, _ in _TRIGGERS])
async def test_a_failing_counter_can_never_break_the_rejection(
    monkeypatch: pytest.MonkeyPatch, status: int, trigger: Any
) -> None:
    """Same promise as the access log: the instrument is never the failure."""
    server = _server()

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("le compteur est cassé")

    monkeypatch.setattr(server._rejection_counters, "increment", _explode, raising=True)

    response = await trigger(server)

    assert response.status == status


def test_no_declared_status_can_ship_uncounted() -> None:
    """A STRUCTURAL guard, twin of the access log's: `_otlp_error` requires
    `counters` as a keyword — a 7th code cannot be constructed without being counted,
    the coverage holds by construction."""
    counters = ReceiverRejectionCounters()

    for status in server_module._OTLP_ERROR_STATUSES:
        response = server_module._otlp_error(status, receiver="codex_logs", counters=counters)
        assert response.status == status

    assert counters.snapshot()["codex_logs"] == {
        str(status): 1 for status in server_module._OTLP_ERROR_STATUSES
    }
