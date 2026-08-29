"""Chaque refus servi par le sidecar avance un compteur — et son zéro SIGNIFIE.

Ticket `d5e4bd73`, piste (b), en complément de l'access log livré : le journal
raconte, le compteur totalise, et il est exposé au même endroit que les autres
métriques (GET /metrics). La leçon du ticket gouverne la forme : « un compteur
à zéro sur une source qui ne compte rien est indistinguable d'un vrai zéro ».
La structure est donc TOUJOURS exposée, les trois récepteurs présents dès le
démarrage — un zéro se lit alors « l'instrument est armé et n'a rien vu »,
jamais « personne ne compte ».
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
    """Le zéro signifiant : les trois récepteurs sont exposés dès la
    construction, chacun vide — la présence de la structure prouve que
    l'instrument compte, son contenu dit ce qu'il a vu."""
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
    """TÉMOIN NÉGATIF : un incrément inconditionnel passerait tous les autres."""
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
    """La vérification du mandat, de bout en bout : un 413 provoqué sur
    /v1/logs apparaît dans GET /metrics — au même endroit que le reste."""
    server = _server()
    collector = server._collector
    collector.get_metrics.return_value = {"embedding_service": {}}
    collector.collect_process_metrics = AsyncMock(return_value={"active_processes": 0})
    # Le reste du handler agrège d'autres sources ; elles sont muettes ici —
    # seul le chemin des compteurs de refus est sous test.
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
    # Les deux récepteurs muets restent PRÉSENTS : leur zéro est signifiant.
    assert payload["receiver_rejections"]["claude_logs"] == {}
    assert payload["receiver_rejections"]["client_activity"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "trigger"), _TRIGGERS, ids=[str(s) for s, _ in _TRIGGERS])
async def test_a_failing_counter_can_never_break_the_rejection(
    monkeypatch: pytest.MonkeyPatch, status: int, trigger: Any
) -> None:
    """Même promesse que l'access log : l'instrument n'est jamais la panne."""
    server = _server()

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("le compteur est cassé")

    monkeypatch.setattr(server._rejection_counters, "increment", _explode, raising=True)

    response = await trigger(server)

    assert response.status == status


def test_no_declared_status_can_ship_uncounted() -> None:
    """Garde STRUCTURELLE, jumelle de celle de l'access log : `_otlp_error`
    exige `counters` par mot-clé — un 7ᵉ code ne peut pas être construit sans
    être compté, la couverture tient par construction."""
    counters = ReceiverRejectionCounters()

    for status in server_module._OTLP_ERROR_STATUSES:
        response = server_module._otlp_error(status, receiver="codex_logs", counters=counters)
        assert response.status == status

    assert counters.snapshot()["codex_logs"] == {
        str(status): 1 for status in server_module._OTLP_ERROR_STATUSES
    }
