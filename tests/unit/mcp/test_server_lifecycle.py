"""Lifecycle tests for MCP server: parent-death signal, signal handlers, engine disposal.

Regression coverage for the zombie-process leak that saturated PostgreSQL after 13 days
when stdio MCP children outlived their Claude Code parent (no SIGTERM, no stdin EOF).
"""

from __future__ import annotations

import asyncio
import ctypes
import inspect
import signal
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_setup_parent_death_signal_callable() -> None:
    from brain_v42.mcp.server import _setup_parent_death_signal

    assert callable(_setup_parent_death_signal)


@pytest.mark.skipif(sys.platform != "linux", reason="prctl PR_SET_PDEATHSIG is Linux-only")
def test_setup_parent_death_signal_sets_sigterm() -> None:
    """Kernel must send SIGTERM to the MCP child when the Claude Code parent dies."""
    from brain_v42.mcp.server import _setup_parent_death_signal

    _setup_parent_death_signal()
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    out = ctypes.c_int(0)
    PR_GET_PDEATHSIG = 2
    ret = libc.prctl(PR_GET_PDEATHSIG, ctypes.byref(out), 0, 0, 0)
    assert ret == 0
    assert out.value == signal.SIGTERM


def test_install_signal_handlers_callable() -> None:
    from brain_v42.mcp.server import _install_signal_handlers

    assert callable(_install_signal_handlers)


def test_install_signal_handlers_sets_event_on_sigterm() -> None:
    """SIGTERM must trigger the shutdown event so the main loop unblocks gracefully."""
    from brain_v42.mcp.server import _install_signal_handlers

    async def _run() -> bool:
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        _install_signal_handlers(loop, event)
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(event.wait(), timeout=1.0)
        return event.is_set()

    assert asyncio.run(_run()) is True


def test_dispose_engine_called_in_shutdown_path() -> None:
    """SQLAlchemy async engine must be disposed on shutdown — root cause of the PG leak."""
    import brain_v42.mcp.server as server_mod

    src = inspect.getsource(server_mod)
    assert "dispose_engine" in src, "dispose_engine must be imported"
    assert "cleanup.push_async_callback(dispose_engine)" in src


@pytest.mark.asyncio
async def test_graph_outbox_projector_follows_server_lifecycle(monkeypatch) -> None:
    """The canonical projector starts after wiring and drains before DB shutdown."""
    import brain_v42.mcp.server as server_mod

    projector = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    ledger = SimpleNamespace(assert_schema_ready=AsyncMock())
    plan_indexer = SimpleNamespace(index_all_projects=AsyncMock(return_value={}))
    services = {
        "access_logger": SimpleNamespace(start=AsyncMock(), stop=AsyncMock()),
        "plan_indexer": plan_indexer,
        "graph_outbox_projector": projector,
        "graph_ledger_repo": ledger,
        "neo4j_driver": None,
    }
    settings = SimpleNamespace(
        metrics_enabled=False, decay_enabled=False, otel_tracing_enabled=False
    )
    ensure_projection_schema = AsyncMock()
    monkeypatch.setattr(
        server_mod,
        "ensure_graph_projection_schema",
        ensure_projection_schema,
        raising=False,
    )
    monkeypatch.setattr(server_mod, "close_neo4j_driver", AsyncMock())
    monkeypatch.setattr(server_mod, "dispose_engine", AsyncMock())

    async with server_mod.app_lifecycle(settings, services, metrics_collector=None):
        ledger.assert_schema_ready.assert_awaited_once()
        ensure_projection_schema.assert_awaited_once_with(services["neo4j_driver"])
        projector.start.assert_awaited_once()
        projector.stop.assert_not_awaited()

    projector.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_failure_unwinds_every_started_resource(monkeypatch) -> None:
    """A late startup failure must not leak earlier background resources."""
    import brain_v42.mcp.server as server_mod
    from brain_v42.metrics import flusher as flusher_mod
    from brain_v42.metrics import timeseries_flusher as timeseries_mod

    projector = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    ledger = SimpleNamespace(assert_schema_ready=AsyncMock())
    metrics_flusher = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    timeseries_flusher = SimpleNamespace(
        start=AsyncMock(side_effect=RuntimeError("startup failed")),
        stop=AsyncMock(),
    )
    services = {
        "access_logger": SimpleNamespace(start=AsyncMock(), stop=AsyncMock()),
        "plan_indexer": SimpleNamespace(index_all_projects=AsyncMock(return_value={})),
        "graph_outbox_projector": projector,
        "graph_ledger_repo": ledger,
        "neo4j_driver": object(),
    }
    settings = SimpleNamespace(
        metrics_enabled=True, decay_enabled=False, otel_tracing_enabled=False
    )
    close_driver = AsyncMock()
    dispose_engine = AsyncMock()
    monkeypatch.setattr(server_mod, "get_session_factory", lambda: object())
    monkeypatch.setattr(server_mod, "ensure_graph_projection_schema", AsyncMock())
    monkeypatch.setattr(server_mod, "close_neo4j_driver", close_driver)
    monkeypatch.setattr(server_mod, "dispose_engine", dispose_engine)
    monkeypatch.setattr(
        flusher_mod,
        "MetricsFlusher",
        lambda **_kwargs: metrics_flusher,
    )
    monkeypatch.setattr(
        timeseries_mod,
        "TimeseriesFlusher",
        lambda **_kwargs: timeseries_flusher,
    )

    with pytest.raises(RuntimeError, match="startup failed"):
        async with server_mod.app_lifecycle(settings, services, metrics_collector=None):
            pytest.fail("startup failure must happen before yielding")

    metrics_flusher.stop.assert_awaited_once()
    timeseries_flusher.stop.assert_awaited_once()
    projector.stop.assert_awaited_once()
    close_driver.assert_awaited_once_with(services["neo4j_driver"])
    dispose_engine.assert_awaited_once()
