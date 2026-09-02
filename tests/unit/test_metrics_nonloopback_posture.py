"""The non-loopback bind posture is a NAMED CHOICE — no longer a silence.

Ticket `eac03668`: on a non-loopback bind, `_build_app()` registers 2 routes
instead of 5 and NO POST route — a refusal returned by the aiohttp router UPSTREAM
of the application code, which neither the access log nor the rejection counters
can see. Two tests ENSHRINED that fail-open while CLAUDE.md listed it as an open
hole. And the tension is real in both directions: `metrics_host` DELIBERATELY has
no loopback-only validator (2026-07-04, in anticipation of a docker gateway
revival), and failing closed would turn a setting documented as configurable into a
`brain-metrics` crash.

The arbitration belongs to the HUMAN. This module prepares BOTH forms behind a
setting whose DEFAULT CHANGES NOTHING — `silent` is the historical behaviour to the
byte:

* ``warn`` — the same missing routes, but ONE line at startup naming the sacrificed
  receivers and the host sacrificing them;
* ``fail_closed`` — the construction refuses, naming the setting that reopens it.
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
    """The DEFAULT changes nothing: routes absent, no line emitted.

    This is the behaviour the two historical tests have pinned since batch d5e4bd73
    — it stays pinned HERE as a NAMED posture, awaiting the operator's arbitration,
    instead of being enshrined by omission.
    """
    assert _settings().metrics_nonloopback_posture == "silent"

    with capture_logs() as records:
        app = _server("0.0.0.0")._build_app()

    assert all(("POST", path) not in _routes(app) for path in _RECEIVER_PATHS)
    assert _posture_lines(records) == []


def test_the_warn_posture_keeps_routes_absent_but_says_so() -> None:
    """Form (2): the same surface as today, but the sacrifice is STATED.

    The line names the missing receivers and the host: that is what an operator will
    read the day "the dashboard's brain half is empty forever" (a permanent 404 on
    the emitter's side) instead of deducing it.
    """
    with capture_logs() as records:
        app = _server("0.0.0.0", posture="warn")._build_app()

    assert all(("POST", path) not in _routes(app) for path in _RECEIVER_PATHS)
    lines = _posture_lines(records)
    assert len(lines) == 1
    assert lines[0]["host"] == "0.0.0.0"
    assert set(lines[0]["absent_routes"]) == set(_RECEIVER_PATHS)


def test_the_fail_closed_posture_refuses_to_build() -> None:
    """Form (1): the construction fails while naming the setting that reopens it —
    never an anonymous crash."""
    with pytest.raises(NonLoopbackReceiversError) as excinfo:
        _server("0.0.0.0", posture="fail_closed")._build_app()

    message = str(excinfo.value)
    assert "0.0.0.0" in message
    assert "METRICS_NONLOOPBACK_POSTURE" in message


@pytest.mark.parametrize("posture", ["silent", "warn", "fail_closed"])
def test_a_loopback_bind_ignores_the_posture(posture: str) -> None:
    """The posture governs ONLY the non-loopback bind: on loopback, the three routes
    are there and nothing speaks, whatever the value."""
    with capture_logs() as records:
        app = _server("127.0.0.1", posture=posture)._build_app()

    assert all(("POST", path) in _routes(app) for path in _RECEIVER_PATHS)
    assert _posture_lines(records) == []


def test_settings_reject_an_unknown_posture() -> None:
    with pytest.raises(Exception, match="metrics_nonloopback_posture|literal_error|Input should"):
        _settings(metrics_nonloopback_posture="closed")


def test_the_production_build_site_passes_the_posture() -> None:
    """The setting is useless if it does not reach the sole production construction
    site (`runtime.py`, through BrainGraphMetricsServer)."""
    source = (
        __import__("pathlib").Path(__file__).parents[2]
        / "src"
        / "brain_v42"
        / "metrics"
        / "runtime.py"
    ).read_text(encoding="utf-8")

    assert "nonloopback_posture=effective_settings.metrics_nonloopback_posture" in source
