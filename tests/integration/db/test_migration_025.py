"""Integration tests for Alembic migration 025.

Single deterministic round-trip: upgrade→head, assert, downgrade -1, assert,
re-upgrade→head, assert. Each step drives the migration itself so no test
method depends on another's side effects (safe under reordering / xdist).

Requires:
    POSTGRES_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test
    BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
_DB_URL = os.environ.get(
    "BRAIN_V42_TEST_DB_URL",
    "postgresql+asyncpg://brain:brain@localhost:5433/brain_test",
)


def _run_alembic(args: list[str]) -> None:
    """Run an alembic command against brain_test, raise on failure."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic"] + args,
        env={**os.environ, "POSTGRES_URL": _DB_URL},
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stderr}\n{result.stdout}")


def _inspect(query: str) -> list[tuple]:  # type: ignore[type-arg]
    """Run a sync inspection query via asyncpg through asyncio."""

    async def _run() -> list[tuple]:  # type: ignore[type-arg]
        engine = create_async_engine(_DB_URL)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(sa.text(query))
                return result.fetchall()
        finally:
            await engine.dispose()

    # asyncio.run() spins up a fresh event loop each call. We do NOT reuse
    # get_event_loop(), which raises "no current event loop" once an earlier
    # async test in the same session has run and closed the default loop.
    return asyncio.run(_run())


def _get_pk_columns() -> list[str]:
    """Return PK column names for process_metrics (sorted for stable comparison)."""
    rows = _inspect("""
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
           AND tc.table_name = kcu.table_name
        WHERE tc.table_name = 'process_metrics'
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
    """)
    return sorted(r[0] for r in rows)


def _get_column_names() -> list[str]:
    """Return all column names of process_metrics."""
    rows = _inspect("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'process_metrics'
          AND table_schema = 'public'
        ORDER BY ordinal_position
    """)
    return [r[0] for r in rows]


def _get_column_info(col_name: str) -> dict | None:  # type: ignore[type-arg]
    """Return nullable and column_default for a given column, or None if not found."""
    rows = _inspect(f"""
        SELECT is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = 'process_metrics'
          AND table_schema = 'public'
          AND column_name = '{col_name}'
    """)
    if not rows:
        return None
    is_nullable, col_default = rows[0]
    return {"nullable": is_nullable == "YES", "default": col_default}


def _get_alembic_version() -> str:
    """Return current alembic version_num from brain_test."""
    rows = _inspect("SELECT version_num FROM alembic_version")
    assert rows, "alembic_version table is empty"
    return rows[0][0]


@pytest.mark.integration
def test_migration_025_round_trip() -> None:
    """Single deterministic round-trip: upgrade→025, downgrade→024, re-upgrade→025.

    The test is scoped to revision 025 specifically (not head) so that later
    migrations (e.g. 026 which collapses the PK back to bare agent_name) do not
    invalidate the assertions made here about the 025 end-state.

    Each migration step is driven within this test so assertions always reflect
    the state produced by the immediately preceding step — no dependency on other
    test methods or their execution order.
    """
    # ── Step 0: ensure we start at a deterministic baseline ──────────────────
    # Downgrade all the way to just before 025 so the test is self-contained
    # regardless of what state a previous run left the DB in.
    try:
        _run_alembic(["-x", "allow_project_context_trigger_downgrade=yes", "downgrade", "024"])
    except RuntimeError:
        # DB may already be at or below 024; try to reach it from scratch
        _run_alembic(["upgrade", "024"])

    # ── Step 1: upgrade to revision 025 (not head) ───────────────────────────
    _run_alembic(["upgrade", "025"])

    pk_cols = _get_pk_columns()
    assert pk_cols == ["agent_name", "pid"], (
        f"After upgrade to 025: expected PK ['agent_name', 'pid'], got {pk_cols}"
    )
    assert pk_cols != ["pid"], "After upgrade to 025: pid alone must NOT be the sole PK"

    col_info = _get_column_info("agent_name")
    assert col_info is not None, "After upgrade to 025: agent_name column not found"
    assert col_info["nullable"] is False, (
        f"After upgrade to 025: agent_name should be NOT NULL, got nullable={col_info['nullable']}"
    )
    assert col_info["default"] is not None and "unknown" in col_info["default"], (
        f"After upgrade to 025: agent_name should have server_default 'unknown', "
        f"got default={col_info['default']}"
    )

    version = _get_alembic_version()
    assert version == "025", f"After upgrade: expected version 025, got {version}"

    # ── Step 2: downgrade one step (025→024) ─────────────────────────────────
    _run_alembic(["downgrade", "-1"])

    pk_cols = _get_pk_columns()
    assert pk_cols == ["pid"], f"After downgrade: expected PK ['pid'], got {pk_cols}"
    col_names = _get_column_names()
    assert "agent_name" not in col_names, (
        f"After downgrade: agent_name column should be gone, found in {col_names}"
    )

    version = _get_alembic_version()
    assert version == "024", f"After downgrade: expected version 024, got {version}"

    # ── Step 3: re-upgrade to 025 to verify idempotency ──────────────────────
    _run_alembic(["upgrade", "025"])

    pk_cols = _get_pk_columns()
    assert pk_cols == ["agent_name", "pid"], (
        f"After re-upgrade to 025: expected PK ['agent_name', 'pid'], got {pk_cols}"
    )
    version = _get_alembic_version()
    assert version == "025", f"After re-upgrade: expected version 025, got {version}"

    # ── Step 4: restore head so the DB is not left at a stale revision ───────
    _run_alembic(["upgrade", "head"])
