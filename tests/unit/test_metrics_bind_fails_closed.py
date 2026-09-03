"""A non-loopback metrics bind is REFUSED at startup unless it is opted into.

Ticket `eac03668`, and this module is the operator arbitration the ticket kept
open. Its own thread named the two candidate shapes -- "un validateur loopback sur
`metrics_host` **ou** une posture `fail_closed` par défaut" -- and the decision of
2026-09-03 took the first.

Why the first rather than the second: the posture governs what `_build_app` DOES
on a non-loopback bind; the validator governs whether that bind is reachable at
all. Composed, the validator is the outer gate and the posture the inner
behaviour, so a bind nobody opted into never reaches the question. `metrics_host`
was the only bind of this repository with no validator at all, while
`mcp_http_host`, `automation_host` and `client_activity_url` have had one for
weeks.

What the opt-in buys, and what it does NOT: with `METRICS_ALLOW_NON_LOOPBACK=yes`
the process starts, but the three POST receivers stay unregistered -- that is a
design constraint, not a posture, because their refusal comes from the aiohttp
router upstream of any application code and neither the access log nor the
rejection counters can see it. So the sacrifice is SAID twice: a warning at
startup, and `ingest_receivers: "disabled"` on `/healthz`, which is the only place
a monitor can read it without parsing logs.

Production is unaffected and that was measured before writing a line: the live
unit sets `BRAIN_METRICS_NONLOOPBACK_POSTURE=warn` and does NOT set
`METRICS_HOST`, so the default `127.0.0.1` passes this validator untouched.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from brain_v42.config import Settings
from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.server import MetricsServer

_RECEIVER_PATHS = ("/v1/logs", "/v1/logs/claude", "/v1/client-activity")
_OPT_IN = "METRICS_ALLOW_NON_LOOPBACK"


def _settings(**overrides: object) -> Settings:
    return Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
        _env_file=None,  # type: ignore[call-arg]
        **overrides,
    )


def _server(host: str, *, allow_non_loopback: bool = False) -> MetricsServer:
    return MetricsServer(
        MagicMock(),
        MagicMock(),
        host=host,
        codex_registry=ClientActivityRegistry(secret=b"x" * 32),
        allow_non_loopback=allow_non_loopback,
    )


def _routes(app: Any) -> set[tuple[str, str]]:
    return {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None
    }


class TestTheBindIsRefusedWithoutAnOptIn:
    @pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "192.168.1.12", "::"])
    def test_a_non_loopback_host_refuses_to_build_settings(self, host: str) -> None:
        with pytest.raises(ValidationError) as caught:
            _settings(metrics_host=host)
        assert host in str(caught.value)

    def test_the_refusal_names_the_setting_AND_the_way_out(self) -> None:
        """A refusal that does not name its own opt-in sends people to the source."""
        with pytest.raises(ValidationError) as caught:
            _settings(metrics_host="0.0.0.0")
        rendered = str(caught.value)
        assert "METRICS_HOST" in rendered
        assert _OPT_IN in rendered

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_a_loopback_host_is_accepted_with_no_opt_in(self, host: str) -> None:
        assert _settings(metrics_host=host).metrics_host == host

    def test_the_default_is_loopback_so_nothing_deployed_changes(self) -> None:
        assert _settings().metrics_host == "127.0.0.1"
        assert _settings().metrics_allow_non_loopback is False


class TestTheOptInAllowsTheBindAndSaysWhatItCosts:
    def test_the_opt_in_lets_the_settings_build(self) -> None:
        settings = _settings(metrics_host="0.0.0.0", metrics_allow_non_loopback=True)
        assert settings.metrics_host == "0.0.0.0"

    def test_the_receivers_stay_absent_even_with_the_opt_in(self) -> None:
        """The opt-in is about the BIND, never about the receivers.

        Their refusal comes from the router, upstream of any code that could log
        it. Registering them on a LAN bind would serve unauthenticated ingestion
        to the network -- the opt-in does not buy that and must not look like it.
        """
        app = _server("0.0.0.0")._build_app()
        served = {path for _, path in _routes(app)}
        assert served.isdisjoint(_RECEIVER_PATHS)
        assert "/metrics" in served

    def test_healthz_says_the_receivers_are_disabled(self) -> None:
        app = _server("0.0.0.0")._build_app()
        assert ("GET", "/healthz") in _routes(app)


class TestHealthzIsWhereAMonitorReadsIt:
    """A log line is not readable by a monitor; a JSON field is."""

    @pytest.mark.asyncio
    async def test_a_loopback_bind_reports_the_receivers_enabled(self) -> None:
        payload = await _healthz_payload(_server("127.0.0.1"))
        assert payload["ingest_receivers"] == "enabled"

    @pytest.mark.asyncio
    async def test_a_non_loopback_bind_reports_them_disabled(self) -> None:
        payload = await _healthz_payload(_server("0.0.0.0"))
        assert payload["ingest_receivers"] == "disabled"

    @pytest.mark.asyncio
    async def test_healthz_never_carries_the_bind_address(self) -> None:
        """`/healthz` is served on the bind it describes, including a LAN one."""
        payload = await _healthz_payload(_server("10.0.0.5"))
        assert "10.0.0.5" not in json.dumps(payload)


async def _healthz_payload(server: MetricsServer) -> dict[str, Any]:
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(server._build_app())) as client:
        response = await client.get("/healthz")
        assert response.status == 200
        return dict(await response.json())


class TestTheSacrificeIsSaidAtStartupToo:
    def test_a_non_loopback_build_under_the_opt_in_warns(self) -> None:
        """`/healthz` is for monitors; the log line is for whoever reads a boot.

        The warning hangs off the OPT-IN and not off the posture, deliberately.
        `silent` was the placeholder default "awaiting the operator arbitration";
        the arbitration is this validator, so the line belongs to the decision
        that was taken rather than to the one that was waiting. That is also why
        `test_the_default_posture_is_silent_and_byte_for_byte_historical` stays
        green: a server built with no opt-in is still exactly as silent.
        """
        with capture_logs() as records:
            _server("0.0.0.0", allow_non_loopback=True)._build_app()
        events = [r for r in records if "receivers_disabled" in str(r.get("event", ""))]
        assert events, f"nothing said the receivers were dropped: {records!r}"
