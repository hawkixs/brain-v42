"""Frontière HTTP des deux récepteurs d'activité du sidecar.

``POST /v1/client-activity`` reçoit les observations poussées par le processus
MCP ; ``POST /v1/logs/claude`` reçoit l'OTLP de Claude Code. Deux routes
distinctes plutôt qu'un récepteur qui devine le schéma : deviner obligerait à
sonder les attributs d'une charge pas encore validée.

Chaque propriété de durcissement de ``/v1/logs`` est réépinglée **route par
route** : loopback-only, corps borné, saturation, rejet fail-closed,
``Content-Type`` et encodage stricts. Un décorateur partagé ne prouve rien tant
qu'on n'a pas vu la route neuve refuser d'elle-même.
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
    """Un lot dont le premier élément est valide et le second ne l'est pas."""
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
    """Un corps déjà en mémoire, dont on peut observer s'il a été lu ou non."""
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
    """``declared_length=None`` simule un corps chunké, sans ``Content-Length``."""
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
    """Contrôle positif du test d'absence qui suit."""
    assert ("POST", path) in _routes(_server(_registry(), host=host)._build_app())


@pytest.mark.parametrize("path", _RECEIVER_PATHS)
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.8", "metrics.internal"])
def test_receiver_route_is_absent_on_a_non_loopback_bind(host: str, path: str) -> None:
    """Posture PAR DÉFAUT (`silent`, eac03668) — un choix nommé, pas un oubli.

    Ce test épingle le comportement historique en ATTENDANT l'arbitrage
    opérateur, il ne le consacre plus par omission : les deux autres postures
    (warn, fail_closed) existent et sont testées par
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
    """Le cas NOMINAL mesuré : aucun client ne déclare sa session aujourd'hui."""
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
    """L'UUID brut traverse la socket loopback ; il ne doit jamais en ressortir.

    L'assertion d'absence a son contrôle positif dans la même charge : la ligne
    existe bien, donc le vert ne vient pas d'un registre resté vide.
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
    """Une session Claude ne doit pas gonfler « Codex activity » avant la bascule.

    Contrôle positif de l'absence : ``clients`` est peuplé dans la même charge.
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
    """Contrôle positif : les tests de chemin nominal passent par un vrai pair loopback."""
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
    """Les récepteurs neufs partagent le budget de requêtes en vol du sidecar."""
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
    """Contrôle positif du rejet d'encodage : ``identity`` reste accepté."""
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
    """Le lot porte un élément valide puis un invalide : rien ne doit rester.

    Contrôle positif : les tests de chemin nominal envoient le même premier
    élément, seul, et produisent bien une ligne.
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
    """Contrôle positif du 413 : la borne du fil est bien MAX_OBSERVATION_BYTES.

    Sans lui, un récepteur qui refuserait tout corps de plus de cent octets
    passerait le test de rejet au vert. La borne est bien plus serrée que celle
    de l'OTLP : elle doit être prouvée pour elle-même.
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
    """La borne serrée s'applique même à un corps que l'OTLP accepterait.

    ``MAX_OBSERVATION_BYTES`` (16 KiB) est loin sous ``MAX_REQUEST_BYTES``
    (256 KiB). Ce test épingle le **statut** de bout en bout, et rien de plus :
    mesuré, il reste vert sur un récepteur privé de toute borne, parce que
    ``decode_observations`` refuse le même corps par lui-même. Le garde-fou HTTP
    a ses propres témoins, juste en dessous.
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
    """Le seul témoin possible de la borne HTTP : le corps n'est pas bufferisé.

    Mesuré : retirer les deux bornes de ``server.py`` laisse tous les 413 verts,
    parce que ``decode_observations`` (16 KiB) et ``_load_json`` (256 KiB)
    refusent le même corps une couche plus bas. Le statut ne distingue donc rien.
    Ce que le récepteur HTTP apporte, et lui seul, c'est de refuser **avant** de
    lire — c'est cette propriété qu'on épingle ici.

    Le cas ``/v1/client-activity`` déclare une longueur qui tient largement sous
    ``MAX_REQUEST_BYTES`` : un récepteur qui se contenterait du garde-fou OTLP
    partagé lirait ce corps-là intégralement.
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
    """Sans ``Content-Length``, seule la borne de flux protège la mémoire.

    Un corps chunké ne déclare aucune longueur : le contrôle d'en-tête ne peut
    pas se déclencher, et la lecture doit s'arrêter d'elle-même. Le corps est
    volontairement plus gros que ce que la lecture bornée consommera, pour qu'il
    reste des octets non lus à observer.
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
    """Contrôle positif des deux ``not stream.at_eof()`` ci-dessus.

    Sans lui, un récepteur qui refuserait tout sans jamais rien lire les
    passerait au vert. Ici le corps est accepté, donc lu jusqu'au bout — le
    harnais sait bien distinguer les deux cas.
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
