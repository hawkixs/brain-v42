"""Integration tests for the app_lifecycle context manager (Task 2.2, H4/H5).

These tests prove that the single ``app_lifecycle`` async context manager is the
SOLE owner of the brain background-task / engine / neo4j lifecycle:

- on enter: the metrics flusher starts and writes a ``process_metrics`` row for
  this PID (requires ``metrics_enabled=True``);
- on exit: ``dispose_engine()`` runs, clearing the module-level engine singleton.

Requires a live PostgreSQL on :5433 (skipped automatically when unreachable via
the session-scoped check in conftest.py). The metrics flusher writes to the
``process_metrics`` table, so ``metrics_enabled`` MUST be True for these tests.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Generator
from typing import Any

import pytest
import sqlalchemy as sa

import brain_v42.db.engine as engine_module
from brain_v42.config import Settings, get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def metrics_settings(monkeypatch: pytest.MonkeyPatch) -> Generator[Settings, None, None]:
    """Build a Settings with metrics enabled and decay/graph disabled.

    Clears the cached singleton so ``build_services()`` / ``app_lifecycle`` see
    the metrics-enabled settings. Decay is disabled to keep the test focused on
    the metrics flusher (decay needs additional live state and a 300s interval);
    graph is disabled so the test does not require a live Neo4j.
    """
    from tests.integration.conftest import INTEGRATION_DB_URL

    monkeypatch.setenv("POSTGRES_URL", INTEGRATION_DB_URL)
    monkeypatch.setenv("METRICS_ENABLED", "true")
    monkeypatch.setenv("DECAY_ENABLED", "false")
    monkeypatch.setenv("GRAPH_ENABLED", "false")
    # The pair, never half of it: a dev machine's .env carries
    # GRAPH_LEDGER_WRITE_ENABLED=true, and Settings rightly refuses a ledger armed
    # without a graph. Forcing GRAPH_ENABLED alone left the bench in a
    # ValidationError at setup — the contract is right, the bench honours it.
    monkeypatch.setenv("GRAPH_LEDGER_WRITE_ENABLED", "false")
    get_settings.cache_clear()
    settings = get_settings()
    yield settings
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_app_lifecycle_starts_flushers_and_disposes_engine(
    metrics_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app_lifecycle starts the metrics flusher on enter, disposes engine on exit.

    1. Patch the metrics flush interval down so a ``process_metrics`` row appears
       quickly (the production default is 30s).
    2. Inside ``async with app_lifecycle(...)``: poll for a ``process_metrics``
       row for ``os.getpid()`` (up to ~3s).
    3. After exit: assert the module engine singleton is None (dispose ran).
    """
    # Shorten the flush interval so the loop writes a row within the test window.
    import brain_v42.metrics.flusher as flusher_module

    original_init = flusher_module.MetricsFlusher.__init__

    def _fast_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("flush_interval", 0.2)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(flusher_module.MetricsFlusher, "__init__", _fast_init)

    from brain_v42.mcp.server import app_lifecycle, build_services

    services = build_services()
    metrics_collector = services["metrics_collector"]

    pid = os.getpid()
    engine_for_probe = engine_module.get_engine()

    async def _row_exists() -> bool:
        async with engine_for_probe.connect() as conn:
            result = await conn.execute(
                sa.text("SELECT 1 FROM process_metrics WHERE pid = :pid"),
                {"pid": pid},
            )
            return result.first() is not None

    try:
        async with app_lifecycle(metrics_settings, services, metrics_collector):
            assert engine_module._engine is not None

            found = False
            for _ in range(30):  # ~3s budget
                if await _row_exists():
                    found = True
                    break
                await asyncio.sleep(0.1)
            assert found, "metrics flusher did not write a process_metrics row"

        # After exit the engine singleton must be disposed/cleared.
        assert engine_module._engine is None, "dispose_engine() did not run on exit"
    finally:
        # Defensive cleanup: remove the process_metrics row using the engine reference
        # captured BEFORE app_lifecycle (engine_for_probe). Do NOT call get_engine()
        # here — app_lifecycle already disposed the singleton, and calling get_engine()
        # would silently re-create it, making the dispose assertion vacuous.
        async with engine_for_probe.begin() as conn:
            await conn.execute(
                sa.text("DELETE FROM process_metrics WHERE pid = :pid"),
                {"pid": pid},
            )
        await engine_for_probe.dispose()
