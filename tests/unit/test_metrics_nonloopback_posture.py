"""La posture du bind non-loopback est un CHOIX NOMMÉ — plus un silence.

Ticket `eac03668` : sur bind non-loopback, `_build_app()` enregistre 2 routes
au lieu de 5 et AUCUNE route POST — un refus rendu par le routeur aiohttp EN
AMONT du code applicatif, que ni l'access log ni les compteurs de refus ne
peuvent voir. Deux tests CONSACRAIENT ce fail-open pendant que CLAUDE.md le
listait comme trou ouvert. Et la tension est réelle dans les deux sens :
`metrics_host` n'a DÉLIBÉRÉMENT aucun validator loopback-only (2026-07-04, en
prévision d'un revival gateway docker), et échouer fermé transformerait un
réglage documenté comme configurable en crash de `brain-metrics`.

L'arbitrage appartient à l'HUMAIN. Ce module prépare les DEUX formes derrière
un réglage dont le DÉFAUT NE CHANGE RIEN — `silent` est le comportement
historique à l'octet près :

* ``warn`` — mêmes routes absentes, mais UNE ligne au démarrage qui nomme les
  receveurs sacrifiés et l'hôte qui les sacrifie ;
* ``fail_closed`` — la construction refuse, en nommant le réglage qui rouvre.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from brain_v42.config import Settings
from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.server import MetricsServer, NonLoopbackReceiversError

_RECEIVER_PATHS = ("/v1/logs", "/v1/logs/claude", "/v1/client-activity")
_POSTURE_EVENT = "metrics_server.receivers_disabled_non_loopback"


def _settings(**overrides: object) -> Settings:
    return Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
        _env_file=None,  # type: ignore[call-arg]
        **overrides,
    )


def _server(host: str, posture: str | None = None) -> MetricsServer:
    kwargs: dict[str, Any] = {}
    if posture is not None:
        kwargs["nonloopback_posture"] = posture
    return MetricsServer(
        MagicMock(),
        MagicMock(),
        host=host,
        codex_registry=ClientActivityRegistry(secret=b"x" * 32),
        **kwargs,
    )


def _routes(app: Any) -> set[tuple[str, str]]:
    return {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None
    }


def _posture_lines(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("event") == _POSTURE_EVENT]


def test_the_default_posture_is_silent_and_byte_for_byte_historical() -> None:
    """Le DÉFAUT ne change rien : routes absentes, aucune ligne émise.

    C'est le comportement que les deux tests historiques épinglent depuis le
    lot d5e4bd73 — il reste épinglé ICI comme une posture NOMMÉE, en attendant
    l'arbitrage opérateur, au lieu d'être consacré par omission.
    """
    assert _settings().metrics_nonloopback_posture == "silent"

    with capture_logs() as records:
        app = _server("0.0.0.0")._build_app()

    assert all(("POST", path) not in _routes(app) for path in _RECEIVER_PATHS)
    assert _posture_lines(records) == []


def test_the_warn_posture_keeps_routes_absent_but_says_so() -> None:
    """Forme (2) : même surface qu'aujourd'hui, mais le sacrifice se DIT.

    La ligne nomme les receveurs absents et l'hôte : c'est ce qu'un opérateur
    lira le jour où « la moitié brain du panneau est vide pour toujours »
    (404 permanent côté émetteur) au lieu de le déduire.
    """
    with capture_logs() as records:
        app = _server("0.0.0.0", posture="warn")._build_app()

    assert all(("POST", path) not in _routes(app) for path in _RECEIVER_PATHS)
    lines = _posture_lines(records)
    assert len(lines) == 1
    assert lines[0]["host"] == "0.0.0.0"
    assert set(lines[0]["absent_routes"]) == set(_RECEIVER_PATHS)


def test_the_fail_closed_posture_refuses_to_build() -> None:
    """Forme (1) : la construction échoue en nommant le réglage qui rouvre —
    jamais un crash anonyme."""
    with pytest.raises(NonLoopbackReceiversError) as excinfo:
        _server("0.0.0.0", posture="fail_closed")._build_app()

    message = str(excinfo.value)
    assert "0.0.0.0" in message
    assert "METRICS_NONLOOPBACK_POSTURE" in message


@pytest.mark.parametrize("posture", ["silent", "warn", "fail_closed"])
def test_a_loopback_bind_ignores_the_posture(posture: str) -> None:
    """La posture ne gouverne QUE le bind non-loopback : en loopback, les
    trois routes sont là et rien ne parle, quelle que soit la valeur."""
    with capture_logs() as records:
        app = _server("127.0.0.1", posture=posture)._build_app()

    assert all(("POST", path) in _routes(app) for path in _RECEIVER_PATHS)
    assert _posture_lines(records) == []


def test_settings_reject_an_unknown_posture() -> None:
    with pytest.raises(Exception, match="metrics_nonloopback_posture|literal_error|Input should"):
        _settings(metrics_nonloopback_posture="closed")


def test_the_production_build_site_passes_the_posture() -> None:
    """Le réglage ne sert à rien s'il n'atteint pas l'unique site de
    construction de production (`runtime.py`, via BrainGraphMetricsServer)."""
    source = (
        __import__("pathlib").Path(__file__).parents[2]
        / "src"
        / "brain_v42"
        / "metrics"
        / "runtime.py"
    ).read_text(encoding="utf-8")

    assert "nonloopback_posture=effective_settings.metrics_nonloopback_posture" in source
