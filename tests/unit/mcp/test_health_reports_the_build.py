"""Contract: `/health` names the build that answers.

A release is only worth something if one can ask a live server which release it
is. The probe already existed; it only said it was alive, never WHO was alive.
These tests require the two facts that identify an artifact: the package's
installed version, and the schema revision that package ships — measured, never
copied.

Everything runs in process: the engine is a double, no PostgreSQL required.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from brain_v42 import release
from brain_v42.mcp import server

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakePool:
    def size(self) -> int:
        return 20

    def checkedout(self) -> int:
        return 0


class _FakeConnection:
    async def execute(self, _statement: Any) -> None:
        return None


class _FakeEngine:
    def __init__(self, *, broken: bool = False) -> None:
        self.pool = _FakePool()
        self._broken = broken

    def connect(self) -> Any:
        @asynccontextmanager
        async def _connection() -> AsyncIterator[_FakeConnection]:
            if self._broken:
                raise RuntimeError("pool wedged")
            yield _FakeConnection()

        return _connection()


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/health", "headers": []})


def _install_engine(monkeypatch: pytest.MonkeyPatch, engine: _FakeEngine) -> None:
    monkeypatch.setattr("brain_v42.db.engine.get_engine", lambda: engine)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


async def test_health_names_the_installed_version_and_the_shipped_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_engine(monkeypatch, _FakeEngine())

    response = await server.health_check(_request())
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["version"] == release.package_version()
    assert body["alembic_head"] == release.shipped_alembic_head()


async def test_health_keeps_its_liveness_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watchdog and the integration test still read status and pool."""
    _install_engine(monkeypatch, _FakeEngine())

    body = json.loads((await server.health_check(_request())).body)

    assert body["status"] == "ok"
    assert body["pool"] == {"size": 20, "checked_out": 0}


async def test_degraded_health_still_names_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """The build identity does not depend on the database: that is where we want it."""
    _install_engine(monkeypatch, _FakeEngine(broken=True))

    response = await server.health_check(_request())
    body = json.loads(response.body)

    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["version"] == release.package_version()
    assert body["alembic_head"] == release.shipped_alembic_head()


async def test_health_does_not_measure_the_head_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe has a 10 s budget and its failure RESTARTS the server.

    Scanning the 44 revision files on every call would put a disk access on this
    path. The measurement is memoized: two probes, a single read.
    """
    _install_engine(monkeypatch, _FakeEngine())
    release.shipped_alembic_head.cache_clear()

    before = release.shipped_alembic_head.cache_info().misses
    await server.health_check(_request())
    await server.health_check(_request())

    assert release.shipped_alembic_head.cache_info().misses - before == 1


# ---------------------------------------------------------------------------
# Startup log
# ---------------------------------------------------------------------------


def _settings() -> Any:
    return SimpleNamespace(
        brain_mcp_transport="http",
        brain_code_mode=False,
        brain_mcp_profile="compact",
        metrics_enabled=True,
        decay_enabled=True,
    )


def test_startup_log_names_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RecordingLogger()
    monkeypatch.setattr(server, "logger", recorder)

    server.log_server_starting(_settings())

    assert len(recorder.events) == 1
    event, fields = recorder.events[0]
    assert event == "brain_v42.server.starting"
    assert fields["version"] == release.package_version()
    assert fields["alembic_head"] == release.shipped_alembic_head()


def test_startup_log_keeps_the_fields_it_already_had(monkeypatch: pytest.MonkeyPatch) -> None:
    """The extraction must lose nothing: these four keys already existed."""
    recorder = _RecordingLogger()
    monkeypatch.setattr(server, "logger", recorder)

    server.log_server_starting(_settings())

    _event, fields = recorder.events[0]
    assert fields["transport"] == "http"
    assert fields["tool_profile"] == "compact"
    assert fields["metrics"] == "flusher"
    assert fields["decay"] == "enabled"
