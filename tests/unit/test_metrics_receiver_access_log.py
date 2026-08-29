"""Un rejet du sidecar de métriques doit laisser une trace — sans rien réintroduire.

Ticket `d5e4bd73`. Les trois récepteurs POST refusent un pair non-loopback (403), une
représentation non supportée (415), un corps trop gros (413), la saturation (503), un
corps qui traîne (408) et un payload malformé (400). **Toutes ces défenses fonctionnent
et personne ne les voit fonctionner** : aucun rejet ne laissait de trace, donc on ne
pouvait ni savoir qu'on saturait, ni savoir qu'on avait saturé hier.

Le piège que ces tests épinglent autant que la fonctionnalité : ce composant hache les
identifiants bruts À LA RÉCEPTION, avec un secret par processus. Un access log naïf
réintroduirait exactement ce que le hachage retire. D'où
``test_the_access_log_carries_nothing_but_constants``, qui verrouille le jeu de champs
par égalité — pas par « ne contient pas », qui laisserait passer le champ suivant.
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

# Le pair non-loopback des tests 403 : une adresse TEST-NET-3 (RFC 5737), jamais routée.
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
        stream.feed_data(b"{")  # commence puis se tait : jamais de feed_eof
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


# --- Les six déclencheurs, un par code réellement atteignable -------------------------
# Six et non cinq : `415` est atteint par DEUX sites (encodage non-identity, media type
# non supporté) et n'apparaît dans aucune description du ticket. Mesuré, pas supposé.


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
    """Un rejet servi, une ligne émise — pour CHACUN des six codes, pas pour le seul 503."""
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
    """TÉMOIN NÉGATIF : sans lui, un log inconditionnel passerait tous les tests ci-dessus."""
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
    """Trois récepteurs partagent UN budget : sans ce champ, un 503 ne dit pas qui saturait."""
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
    """Le cœur du lot : l'égalité de jeu de clés, pas un « ne contient pas ».

    Un `assert "peer" not in line` laisserait passer le champ suivant. L'égalité fait
    échouer le test dès qu'un champ est AJOUTÉ, ce qui force la question à être reposée.
    """
    server = _server()
    # Un CANARI, pas un secret. La forme précédente nommait cette variable avec le
    # mot « secret » et lui donnait une valeur à l'allure de clé : `gitleaks` la
    # relevait en `generic-api-key`, entropie 3,913, et faisait rougir la CI. Faux
    # positif au sens strict — mais le corriger À LA SOURCE vaut mieux qu'un
    # `gitleaks:allow` ou une entrée d'allowlist, qui affaibliraient un contrôle
    # pour faire taire un rouge dû à notre propre formulation. « Canari » dit
    # d'ailleurs mieux ce que la valeur fait : elle est injectée pour qu'on vérifie
    # qu'elle ne ressort PAS. Ne pas reciter ici l'ancienne valeur — un commentaire
    # qui cite ce qu'il explique le réintroduit, ce qui est arrivé au premier essai.
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
    # Ni l'adresse du pair, ni un en-tête, ni le chemin brut ne doivent transparaître.
    rendered = repr(line)
    assert canary not in rendered
    assert _FOREIGN_PEER not in rendered
    assert "/v1/logs" not in rendered
    # Les trois valeurs restantes appartiennent à des ensembles CONSTANTS et clos.
    assert line["receiver"] in {"codex_logs", "claude_logs", "client_activity"}
    assert line["status"] in server_module._OTLP_ERROR_STATUSES
    assert line["reason"] == server_module._OTLP_ERROR_STATUSES[line["status"]][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "trigger"), _TRIGGERS, ids=[str(s) for s, _ in _TRIGGERS])
async def test_a_failing_access_log_can_never_break_the_rejection(
    monkeypatch: pytest.MonkeyPatch, status: int, trigger: Any
) -> None:
    """L'instrument ne devient pas la panne — et surtout pas sous saturation, qu'il mesure."""
    server = _server()

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("le journal est cassé")

    monkeypatch.setattr(server_module.logger, "warning", _explode, raising=True)

    response = await trigger(server)

    assert response.status == status
    assert json.loads(response.body)["code"] == server_module._OTLP_ERROR_STATUSES[status][0]


def test_no_declared_status_can_ship_without_an_access_log() -> None:
    """Garde STRUCTURELLE : un 7ᵉ code ajouté à la table sera journalisé sans y penser.

    Les six rejets passent tous par `_otlp_error`, seul constructeur de ces réponses.
    Journaliser LÀ rend la couverture indéfectible par construction, au lieu de la
    laisser dépendre de la vigilance du prochain site d'appel.
    """
    counters = server_module.ReceiverRejectionCounters()
    for status in server_module._OTLP_ERROR_STATUSES:
        with capture_logs() as records:
            response = server_module._otlp_error(status, receiver="codex_logs", counters=counters)
        assert response.status == status
        assert [r for r in records if r.get("event") == _ACCESS_LOG_EVENT], (
            f"{status} déclaré dans la table mais non journalisé"
        )
