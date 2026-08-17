"""Safety guards for scripts that write directly to the Neo4j projection."""

from __future__ import annotations

import sys

import pytest
from scripts import init_graph, reconcile_graph


def _enable_canonical_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain:brain@localhost:5433/brain")
    monkeypatch.setenv("GRAPH_ENABLED", "true")
    monkeypatch.setenv("GRAPH_LEDGER_WRITE_ENABLED", "true")
    monkeypatch.setenv("NEO4J_URL", "bolt://localhost:7687")


@pytest.mark.asyncio
async def test_init_graph_refuses_direct_writes_when_canonical_ledger_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_canonical_ledger(monkeypatch)

    async def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("init_graph must stop before opening PostgreSQL")

    monkeypatch.setattr(init_graph.asyncpg, "connect", forbidden_connect)

    result = await init_graph.main()

    assert result == 2


@pytest.mark.asyncio
async def test_reconcile_graph_fix_refuses_direct_writes_when_canonical_ledger_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_canonical_ledger(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["reconcile_graph.py", "--fix"])

    async def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reconcile_graph --fix must stop before opening PostgreSQL")

    monkeypatch.setattr(reconcile_graph.asyncpg, "connect", forbidden_connect)

    result = await reconcile_graph.main()

    assert result == 2


@pytest.mark.asyncio
async def test_reconcile_graph_report_remains_available_when_canonical_ledger_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_canonical_ledger(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["reconcile_graph.py"])

    class ReadModeReached(RuntimeError):
        pass

    async def stop_after_guard(*_args: object, **_kwargs: object) -> None:
        raise ReadModeReached

    monkeypatch.setattr(reconcile_graph.asyncpg, "connect", stop_after_guard)

    with pytest.raises(ReadModeReached):
        await reconcile_graph.main()
