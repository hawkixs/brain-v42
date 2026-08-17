"""Les trois récepteurs du sidecar doivent borner la LECTURE du corps, pas seulement sa taille.

Mesuré le 2026-08-10 (ticket 5fa2771e). ``_read_bounded_otlp_body`` n'avait aucune
échéance, et le sémaphore des 4 requêtes en vol est acquis AUTOUR de la lecture. Quatre
connexions loopback qui annoncent un corps chunké, envoient un octet puis se taisent
suffisaient donc à verrouiller les trois récepteurs — mesuré encore verrouillé après 3,2 s,
et sans aucun mécanisme de sortie : c'est « à vie », pas « quelques secondes ».

aiohttp 3.14.3 n'offre aucune garde amont : ``RequestHandler`` expose keepalive_timeout,
lingering_time, read_bufsize, max_line_size, et rien sur la lecture du corps. Le correctif
applicatif est le seul possible.

La forme est celle du shim d'embedding (``services/embedding_shim/shim_app.py:167``), qui
a exactement cette garde depuis sa livraison : une échéance TOTALE posée à l'extérieur de
la boucle, jamais par morceau — un émetteur qui envoie un octet toutes les quatre secondes
passerait indéfiniment sous une garde par morceau.
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
    """Un corps chunké qui commence puis se tait : un octet, jamais de ``feed_eof``."""
    stream = streams.StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    stream.feed_data(b"{")
    return stream


def _request(path: str, stream: streams.StreamReader) -> Any:
    """Sans ``Content-Length`` : c'est un corps chunké, la seule forme qui puisse figer."""
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
    """Les TROIS récepteurs, parce qu'ils partagent le même budget de requêtes en vol.

    RED avant correctif : le handler ne rend jamais la main, ``asyncio.wait_for``
    l'annule et le test part en TimeoutError avant même le premier assert.
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
    """Le test qui vaut le ticket : l'ingestion des trois routes doit REPRENDRE.

    Sans échéance, quatre corps figés verrouillaient le sémaphore jusqu'au
    redémarrage de brain-metrics — mesuré : POST propre → 503 + Retry-After, et le
    registre restait vide.
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
    # Contrôle positif : sans saturation constatée, les assertions suivantes
    # seraient vraies pour rien.
    while not server._codex_request_slots.locked():
        await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(*storm), timeout=3)

    assert server._codex_request_slots.locked() is False, (
        "le budget de requêtes en vol n'est pas rendu : les trois récepteurs restent morts"
    )
    # Filet plus fin que locked() — il attrape une libération PARTIELLE. Attribut
    # privé assumé : le contrat est locked(), ceci est la ceinture.
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
    """La sonde ANTI-TAUTOLOGIE : un corps lent mais qui avance doit aboutir.

    Sans elle, on rendrait le test précédent vert en refusant tout corps sans
    Content-Length, ou en posant un budget quasi nul — et la garde ne garderait plus
    rien d'utile, elle casserait l'usage légitime.
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
    """La valeur livrée, épinglée — même forme que les limites du shim d'embedding.

    Elle est lue DANS LE CORPS de la fonction et jamais en valeur par défaut d'argument :
    une valeur par défaut est liée au moment du ``def``, donc un monkeypatch resterait
    sans effet et les tests ci-dessus dureraient cinq secondes en croyant mesurer la garde.
    """
    assert server_module._OTLP_BODY_READ_TIMEOUT_SECONDS == 5.0


def test_the_timeout_status_is_declared_before_it_is_used() -> None:
    """``_otlp_error(408)`` lève KeyError tant que 408 n'est pas déclaré.

    Sans cette entrée, le RED des tests ci-dessus serait rouge pour la MAUVAISE raison
    (KeyError → 500) et on croirait l'échéance cassée.
    """
    assert 408 in server_module._OTLP_ERROR_STATUSES
