"""The metrics /healthz says whether PostgreSQL answers, not a constant "ok".

Measured on red-base 2026-10-12 (ticket abbfabfd): /healthz answered ok during a
PostgreSQL outage, so the compose healthcheck and red-agent's health field for the
metrics container meant nothing.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer

from brain_v42.config import Settings
from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.runtime import build_metrics_runtime
from brain_v42.metrics.server import MetricsServer

SECRET = "password=hunter2-must-never-be-served"


def _server(**kwargs: Any) -> MetricsServer:
    return MetricsServer(
        MagicMock(),
        MagicMock(),
        host="127.0.0.1",
        codex_registry=ClientActivityRegistry(secret=b"x" * 32),
        **kwargs,
    )


async def _healthz(server: MetricsServer) -> tuple[int, dict[str, Any]]:
    async with TestClient(TestServer(server._build_app())) as client:
        response = await client.get("/healthz")
        return response.status, dict(await response.json())


async def test_healthz_is_ok_when_postgresql_answers() -> None:
    probe = AsyncMock()
    status, body = await _healthz(_server(database_probe=probe))
    assert status == 200
    assert body["status"] == "ok"
    assert body["database"] == "reachable"
    probe.assert_awaited_once()


async def test_healthz_is_unhealthy_when_postgresql_fails() -> None:
    probe = AsyncMock(side_effect=OSError(SECRET))
    status, body = await _healthz(_server(database_probe=probe))
    assert status == 503
    assert body["status"] == "degraded"
    assert body["database"] == "unreachable"
    assert SECRET not in str(body)


async def test_healthz_does_not_hang_on_a_stalled_postgresql() -> None:
    async def stalled() -> None:
        await asyncio.sleep(30)

    server = _server(database_probe=stalled, database_probe_timeout_seconds=0.05)
    status, body = await asyncio.wait_for(_healthz(server), timeout=5)
    assert status == 503
    assert body["database"] == "unreachable"


async def test_healthz_answers_within_its_timeout_even_if_cancellation_is_slow() -> None:
    """Codex review of #368: asyncio.wait_for waits for the cancelled probe to clean
    up, and an asyncpg connection torn down during an outage can take seconds, past
    the compose healthcheck's 3 s client timeout. The answer must not wait for it."""
    finished = asyncio.Event()

    async def slow_to_cancel() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(1.0)
            finished.set()
            raise

    server = _server(database_probe=slow_to_cancel, database_probe_timeout_seconds=0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()
    status, body = await _healthz(server)
    elapsed = loop.time() - started

    assert (status, body["database"]) == (503, "unreachable")
    assert elapsed < 0.8
    await asyncio.wait_for(finished.wait(), timeout=5)


async def test_healthz_keeps_reporting_the_receivers() -> None:
    status, body = await _healthz(_server(database_probe=AsyncMock(side_effect=OSError())))
    assert body["ingest_receivers"] == "enabled"


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()

    @asynccontextmanager
    async def connect(self):  # type: ignore[no-untyped-def]
        yield self.connection


async def test_the_production_server_probes_its_own_engine() -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
        metrics_legacy_automation_enabled=False,
        graph_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    engine = _Engine()
    runtime = build_metrics_runtime(settings=settings, engine=engine)  # type: ignore[arg-type]
    server = runtime._resources.server_factory(None, None)  # type: ignore[misc]

    status, body = await _healthz(server)

    assert (status, body["database"]) == (200, "reachable")
    assert engine.connection.statements == ["SELECT 1"]
