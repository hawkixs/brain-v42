"""PostgreSQL round trip for the connection set that survives idle eviction."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "POSTGRES_URL": os.environ["BRAIN_V42_TEST_DB_URL"]},
        timeout=120,
    )


@pytest_asyncio.fixture
async def operator(engine: AsyncEngine) -> AsyncIterator[tuple[AsyncConnection, UUID]]:
    """Roll back the owned rows so constraint checks leave no shared residue."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            key = f"integ-061-{uuid4().hex[:16]}"
            await connection.execute(
                sa.text(
                    "INSERT INTO project_contexts (project_key, name, description) "
                    "VALUES (:key, :key, 'migration 061')"
                ),
                {"key": key},
            )
            session_id = await connection.scalar(
                sa.text(
                    "INSERT INTO brain_sessions "
                    "(project_key, client_key, started_focus_revision) "
                    "VALUES (:key, 'integ-061', 0) RETURNING id"
                ),
                {"key": key},
            )
            assert isinstance(session_id, UUID)
            yield connection, session_id
        finally:
            await transaction.rollback()


async def _record(connection: AsyncConnection, session_id: UUID, transport: str | None) -> None:
    await connection.execute(
        sa.text(
            "INSERT INTO brain_session_connections (session_id, connection_id) "
            "VALUES (:session_id, :transport)"
        ),
        {"session_id": session_id, "transport": transport},
    )


async def test_columns_are_nonnullable_with_a_server_clock(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    sa.text(
                        "SELECT column_name, data_type, is_nullable, column_default, "
                        "character_maximum_length FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'brain_session_connections'"
                    )
                )
            )
            .mappings()
            .all()
        )
    columns = {row["column_name"]: row for row in rows}
    # 063 adds a nullable client_id above this revision; its own test pins that column.
    # The set stays closed: any other column appearing here is still a failure.
    later = {"client_id"}
    assert set(columns) - later == {"session_id", "connection_id", "first_seen_at"}
    assert all(row["is_nullable"] == "NO" for name, row in columns.items() if name not in later)
    assert columns["session_id"]["data_type"] == "uuid"
    assert columns["connection_id"]["data_type"] == "character varying"
    assert columns["connection_id"]["character_maximum_length"] == 64
    assert columns["first_seen_at"]["data_type"] == "timestamp with time zone"
    assert columns["first_seen_at"]["column_default"] == "clock_timestamp()"


@pytest.mark.parametrize("bad", ["", "   "])
async def test_check_refuses_blank_connections(
    operator: tuple[AsyncConnection, UUID], bad: str
) -> None:
    connection, session_id = operator
    with pytest.raises(IntegrityError, match="brain_session_connections_connection_nonblank"):
        async with connection.begin_nested():
            await _record(connection, session_id, bad)


async def test_check_is_fail_closed(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        definition = await connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'brain_session_connections_connection_nonblank'"
            )
        )
    assert "COALESCE" in definition
    assert "false" in definition


async def test_null_connection_is_refused(operator: tuple[AsyncConnection, UUID]) -> None:
    connection, session_id = operator
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await _record(connection, session_id, None)


async def test_primary_key_refuses_a_second_observation(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    await _record(connection, session_id, "first")
    with pytest.raises(IntegrityError, match="brain_session_connections_pkey"):
        async with connection.begin_nested():
            await _record(connection, session_id, "first")
    await _record(connection, session_id, "second")
    assert (
        await connection.scalar(
            sa.text("SELECT count(*) FROM brain_session_connections WHERE session_id = :id"),
            {"id": session_id},
        )
        == 2
    )


async def test_foreign_key_refuses_a_missing_session(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        with pytest.raises(IntegrityError, match="brain_session_connections_session_id_fkey"):
            async with connection.begin():
                await _record(connection, uuid4(), "missing")


async def test_session_deletion_cascades(operator: tuple[AsyncConnection, UUID]) -> None:
    connection, session_id = operator
    await _record(connection, session_id, "first")
    await connection.execute(
        sa.text("DELETE FROM brain_sessions WHERE id = :id"), {"id": session_id}
    )
    assert (
        await connection.scalar(
            sa.text("SELECT count(*) FROM brain_session_connections WHERE session_id = :id"),
            {"id": session_id},
        )
        == 0
    )


async def test_downgrade_drops_the_table_and_reupgrade_restores_it(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None]
) -> None:
    migration_downgrade_fence(downgraded_to="060")
    down = _run_alembic("downgrade", "060")
    assert down.returncode == 0, down.stderr
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.text("SELECT to_regclass('public.brain_session_connections')")
            )
            is None
        )
    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.text("SELECT to_regclass('public.brain_session_connections')")
            )
            == "brain_session_connections"
        )
