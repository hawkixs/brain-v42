"""Integration tests for Alembic migration 026.

Collapse process_metrics PRIMARY KEY from composite (agent_name, pid) to
bare (agent_name).  pid column MUST survive (back-compat for active_processes
= COUNT(DISTINCT pid) in collector_db.py).

Single deterministic round-trip: upgrade→head (026), downgrade -1 (→025),
re-upgrade→026.  Each step drives the migration itself so no test method
depends on another's side effects.

Also asserts the dedup logic: pre-seeding two rows for the same agent_name
with different pids and different updated_at values before the upgrade must
leave exactly one row (the latest updated_at) after the upgrade.

Requires:
    POSTGRES_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test
    BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


def _db_url() -> str:
    """Read at CALL time, never at import.

    A module-level capture binds during collection, before the session fixture in
    `tests/integration/db/conftest.py` points this directory at its own disposable
    database — and this module would keep downgrading the SHARED one. Ticket
    f7af0977 measured what that costs: 77 dropped columns per decay table per run.
    """
    return os.environ["BRAIN_V42_TEST_DB_URL"]


def _run_alembic(args: list[str]) -> None:
    """Run an alembic command against brain_test, raise on failure."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic"] + args,
        env={**os.environ, "POSTGRES_URL": _db_url()},
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
        engine = create_async_engine(_db_url())
        try:
            async with engine.connect() as conn:
                result = await conn.execute(sa.text(query))
                return result.fetchall()
        finally:
            await engine.dispose()

    # asyncio.run() spins up a fresh event loop each call — avoids
    # "no current event loop" errors after prior async tests close the loop.
    return asyncio.run(_run())


def _get_pk_columns() -> list[str]:
    """Return PK column names for process_metrics (sorted for stable comparison)."""
    rows = _inspect("""
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
           AND tc.table_name      = kcu.table_name
        WHERE tc.table_name    = 'process_metrics'
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
    """)
    return sorted(r[0] for r in rows)


def _get_column_names() -> list[str]:
    """Return all column names of process_metrics in ordinal order."""
    rows = _inspect("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name   = 'process_metrics'
          AND table_schema = 'public'
        ORDER BY ordinal_position
    """)
    return [r[0] for r in rows]


def _get_column_info(col_name: str) -> dict | None:  # type: ignore[type-arg]
    """Return nullable flag for a given column, or None if not found."""
    rows = _inspect(f"""
        SELECT is_nullable
        FROM information_schema.columns
        WHERE table_name   = 'process_metrics'
          AND table_schema = 'public'
          AND column_name  = '{col_name}'
    """)
    if not rows:
        return None
    return {"nullable": rows[0][0] == "YES"}


def _count_rows_for_agent(agent_name: str) -> int:
    """Return number of process_metrics rows with the given agent_name."""
    rows = _inspect(f"SELECT COUNT(*) FROM process_metrics WHERE agent_name = '{agent_name}'")
    return int(rows[0][0])


def _get_updated_at_for_agent(agent_name: str) -> str:
    """Return updated_at (as text) for the surviving row of agent_name."""
    rows = _inspect(
        f"SELECT updated_at::text FROM process_metrics WHERE agent_name = '{agent_name}'"
    )
    assert rows, f"No row found for agent_name='{agent_name}'"
    return rows[0][0]


def _get_alembic_version() -> str:
    """Return current alembic version_num from brain_test."""
    rows = _inspect("SELECT version_num FROM alembic_version")
    assert rows, "alembic_version table is empty"
    return rows[0][0]


def _seed_dedup_rows() -> None:
    """Seed two rows for the same agent_name with different pids / updated_at.

    The upgrade dedup must keep only the row with the later updated_at.
    We use a unique synthetic agent_name to avoid colliding with other tests.
    """

    async def _run() -> None:
        engine = create_async_engine(_db_url())
        try:
            async with engine.begin() as conn:
                # Pre-clean in case of a prior crashed run.
                await conn.execute(
                    sa.text("DELETE FROM process_metrics WHERE agent_name = '__dedup_test__'")
                )
                # Older row: pid=111001, updated_at = 1 hour ago
                await conn.execute(
                    sa.text(
                        "INSERT INTO process_metrics "
                        "(agent_name, pid, started_at, updated_at, "
                        " tool_stats, embedding_stats, memory_rss_bytes) "
                        "VALUES ('__dedup_test__', 111001, NOW(), "
                        "        NOW() - INTERVAL '1 hour', "
                        "        '{}', '{}', 0)"
                    )
                )
                # Newer row: pid=111002, updated_at = NOW() — this one must survive.
                await conn.execute(
                    sa.text(
                        "INSERT INTO process_metrics "
                        "(agent_name, pid, started_at, updated_at, "
                        " tool_stats, embedding_stats, memory_rss_bytes) "
                        "VALUES ('__dedup_test__', 111002, NOW(), NOW(), "
                        "        '{}', '{}', 0)"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _get_surviving_pid_for_agent(agent_name: str) -> int:
    """Return the pid of the single surviving row for agent_name."""
    rows = _inspect(f"SELECT pid FROM process_metrics WHERE agent_name = '{agent_name}'")
    assert len(rows) == 1, f"Expected exactly 1 row, got {len(rows)}"
    return int(rows[0][0])


def _delete_dedup_rows() -> None:
    """Remove the synthetic __dedup_test__ rows (works at any schema version)."""

    async def _run() -> None:
        engine = create_async_engine(_db_url())
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text("DELETE FROM process_metrics WHERE agent_name = '__dedup_test__'")
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


@pytest.fixture(autouse=True)
def _cleanup_dedup_rows():  # type: ignore[no-untyped-def]
    """Delete the synthetic __dedup_test__ rows after the test so they never leak
    into the shared brain_test DB.

    The cross_process contract tests compute active_processes =
    COUNT(DISTINCT pid) over a 60s window; a leftover row here inflates that
    count and fails them. This is a test-isolation regression guard.
    """
    yield
    _delete_dedup_rows()


@pytest.mark.integration
def test_migration_026_round_trip(migration_downgrade_fence: Callable[..., None]) -> None:
    """Single deterministic round-trip: upgrade→026, downgrade→025, re-upgrade→026.

    Assertions after upgrade to 026:
    (a) PK is exactly ['agent_name'] — bare single-column.
    (b) pid column STILL EXISTS and is nullable (back-compat for active_processes).
    (c) Dedup: two pre-seeded rows for the same agent_name collapse to one (latest
        updated_at survives).
    (d) alembic version_num == '026'.

    Assertions after downgrade to 025:
    - PK is back to ['agent_name', 'pid'] composite.

    Assertions after re-upgrade to 026:
    - PK is ['agent_name'] again.
    - alembic version_num == '026'.
    """
    # ── Step 0: ensure DB is at the 025 composite-PK baseline ────────────────
    # The session-scoped conftest fixture already upgraded brain_test to head
    # (026, bare agent_name PK). Downgrade to 025 so we can seed two rows with
    # the SAME agent_name (different pid) — only the composite PK permits that.
    # (`upgrade 025` would be a no-op here since 026 is already ahead.)
    migration_downgrade_fence("025")
    _run_alembic(
        [
            "-x",
            "allow_project_context_trigger_downgrade=yes",
            "-x",
            "allow_focus_history_downgrade=yes",
            "downgrade",
            "025",
        ]
    )

    # ── Pre-seed two rows for the same agent_name (dedup test) ───────────────
    _seed_dedup_rows()

    # Confirm two rows exist before upgrade.
    assert _count_rows_for_agent("__dedup_test__") == 2, (
        "Pre-condition: expected 2 seeded rows for '__dedup_test__'"
    )

    # ── Step 1: upgrade to revision 026 (NOT head — later migrations exist) ──
    # Pinning to 026 keeps this round-trip immune to future migrations (the
    # exact stale-head failure test_migration_025 had before being pinned).
    _run_alembic(["upgrade", "026"])

    # (a) PK is exactly ['agent_name']
    pk_cols = _get_pk_columns()
    assert pk_cols == ["agent_name"], f"After upgrade: expected PK ['agent_name'], got {pk_cols}"

    # Constraint name sanity — information_schema reports it
    constraint_rows = _inspect("""
        SELECT tc.constraint_name
        FROM information_schema.table_constraints tc
        WHERE tc.table_name    = 'process_metrics'
          AND tc.constraint_type = 'PRIMARY KEY'
    """)
    assert constraint_rows, "No PK constraint found after upgrade to 026"
    assert constraint_rows[0][0] == "process_metrics_pkey", (
        f"Expected PK constraint name 'process_metrics_pkey', got '{constraint_rows[0][0]}'"
    )

    # (b) pid column still exists and is nullable
    col_names = _get_column_names()
    assert "pid" in col_names, (
        f"After upgrade: pid column must still exist for back-compat, got columns: {col_names}"
    )
    pid_info = _get_column_info("pid")
    assert pid_info is not None, "pid column info not found after upgrade"
    assert pid_info["nullable"] is True, (
        f"After upgrade: pid should be nullable, got nullable={pid_info['nullable']}"
    )

    # (c) Dedup: exactly 1 row remains (the newer one, pid=111002)
    assert _count_rows_for_agent("__dedup_test__") == 1, (
        "After upgrade: dedup must leave exactly 1 row for '__dedup_test__'"
    )
    surviving_pid = _get_surviving_pid_for_agent("__dedup_test__")
    assert surviving_pid == 111002, (
        f"After upgrade: expected the newer row (pid=111002) to survive, got pid={surviving_pid}"
    )

    # (d) alembic version
    version = _get_alembic_version()
    assert version == "026", f"After upgrade: expected alembic head 026, got {version}"

    # ── Step 2: downgrade one step (026 → 025) ───────────────────────────────
    _run_alembic(["downgrade", "-1"])

    pk_cols = _get_pk_columns()
    assert pk_cols == ["agent_name", "pid"], (
        f"After downgrade: expected composite PK ['agent_name', 'pid'], got {pk_cols}"
    )

    # ── Step 3: re-upgrade to 026 to verify idempotency ──────────────────────
    _run_alembic(["upgrade", "026"])

    pk_cols = _get_pk_columns()
    assert pk_cols == ["agent_name"], f"After re-upgrade: expected PK ['agent_name'], got {pk_cols}"
    version = _get_alembic_version()
    assert version == "026", f"After re-upgrade: expected version 026, got {version}"

    # ── Step 4: restore head so the DB is not left at a stale revision ───────
    _run_alembic(["upgrade", "head"])
