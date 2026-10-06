"""Migration 063: client credential registry, admin elevations, schema compat."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.integration.disposable_db import asyncpg_dsn

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]

TABLES = ("brain_client_credentials", "brain_admin_elevations", "brain_schema_compat")
DIGEST = "ab" * 32


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
    """Roll back the owned rows so constraint checks leave no residue."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            key = f"integ-063-{uuid4().hex[:16]}"
            await connection.execute(
                sa.text(
                    "INSERT INTO project_contexts (project_key, name, description) "
                    "VALUES (:key, :key, 'migration 063')"
                ),
                {"key": key},
            )
            session_id = await connection.scalar(
                sa.text(
                    "INSERT INTO brain_sessions "
                    "(project_key, client_key, started_focus_revision) "
                    "VALUES (:key, 'integ-063', 0) RETURNING id"
                ),
                {"key": key},
            )
            assert isinstance(session_id, UUID)
            yield connection, session_id
        finally:
            await transaction.rollback()


async def _credential(connection: AsyncConnection, **overrides: object) -> None:
    values: dict[str, object] = {
        "client_id": "auto-discord",
        "digest": bytes.fromhex(DIGEST),
        "families": ["read"],
        "created_by": "operator",
        "transition": False,
        "expires_at": None,
        "revoked_at": None,
        "revoked_reason": None,
    }
    values.update(overrides)
    await connection.execute(
        sa.text(
            "INSERT INTO brain_client_credentials (client_id, token_sha256, families, "
            "created_by, transition, expires_at, revoked_at, revoked_reason) "
            "VALUES (:client_id, :digest, :families, :created_by, :transition, "
            ":expires_at, :revoked_at, :revoked_reason)"
        ),
        values,
    )


async def _elevation(
    connection: AsyncConnection, session_id: UUID, *, hours: int, connection_ids: list[str]
) -> None:
    await connection.execute(
        sa.text(
            "INSERT INTO brain_admin_elevations (session_id, connection_ids, granted_at, "
            "expires_at, granted_by, reason) VALUES (:session_id, :connection_ids, now(), "
            "now() + make_interval(hours => :hours), 'operator', 'maintenance window')"
        ),
        {"session_id": session_id, "connection_ids": connection_ids, "hours": hours},
    )


async def test_upgrade_creates_the_three_tables_and_the_trigger(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        for table in TABLES:
            assert (
                await connection.scalar(sa.text(f"SELECT to_regclass('public.{table}')")) == table
            )
        trigger = await connection.execute(
            sa.text(
                "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                "WHERE tgrelid = 'public.brain_client_credentials'::regclass AND NOT tgisinternal"
            )
        )
        rows = trigger.all()
        assert await connection.scalar(sa.text("SELECT count(*) FROM brain_schema_compat")) == 0
    assert len(rows) == 1
    definition = rows[0][1]
    assert "AFTER INSERT OR DELETE OR UPDATE" in definition
    assert "FOR EACH ROW" in definition
    assert "brain_client_credentials_notify()" in definition


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"families": ["read", "admin"]}, "brain_client_credentials_families_valid"),
        ({"families": ["admin"]}, "brain_client_credentials_families_valid"),
        ({"families": []}, "brain_client_credentials_families_valid"),
        ({"client_id": "Bad_Id"}, "brain_client_credentials_client_id_format"),
        ({"client_id": "-leading"}, "brain_client_credentials_client_id_format"),
        ({"client_id": "a" * 65}, "brain_client_credentials_client_id_format"),
        ({"digest": bytes(31)}, "brain_client_credentials_token_sha256_length"),
        ({"transition": True}, "brain_client_credentials_transition_expires"),
        (
            {"revoked_at": datetime(2026, 1, 1, tzinfo=UTC)},
            "brain_client_credentials_revocation_pair",
        ),
        ({"revoked_reason": "leaked"}, "brain_client_credentials_revocation_pair"),
        ({"created_by": ""}, "brain_client_credentials_created_by_nonblank"),
    ],
)
async def test_credential_checks_refuse_their_bad_row(
    operator: tuple[AsyncConnection, UUID], overrides: dict[str, object], constraint: str
) -> None:
    connection, _ = operator
    with pytest.raises(IntegrityError, match=constraint):
        async with connection.begin_nested():
            await _credential(connection, **overrides)


async def test_a_transition_credential_with_an_expiry_and_a_full_revocation_are_stored(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, _ = operator
    await _credential(
        connection,
        transition=True,
        expires_at=await connection.scalar(sa.text("SELECT now() + interval '1 day'")),
        families=["read", "write", "delivery", "telemetry"],
        revoked_at=await connection.scalar(sa.text("SELECT now()")),
        revoked_reason="rotated",
    )


async def test_token_digest_is_unique(operator: tuple[AsyncConnection, UUID]) -> None:
    connection, _ = operator
    await _credential(connection)
    with pytest.raises(IntegrityError, match="brain_client_credentials_token_sha256_key"):
        async with connection.begin_nested():
            await _credential(connection, client_id="other")


@pytest.mark.parametrize(
    ("hours", "connection_ids", "constraint"),
    [
        (5, ["conn-1"], "brain_admin_elevations_window"),
        (0, ["conn-1"], "brain_admin_elevations_window"),
        (1, [], "brain_admin_elevations_connection_ids_nonempty"),
    ],
)
async def test_elevation_checks_refuse_their_bad_row(
    operator: tuple[AsyncConnection, UUID], hours: int, connection_ids: list[str], constraint: str
) -> None:
    connection, session_id = operator
    with pytest.raises(IntegrityError, match=constraint):
        async with connection.begin_nested():
            await _elevation(connection, session_id, hours=hours, connection_ids=connection_ids)


async def test_a_four_hour_elevation_is_the_longest_stored(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    await _elevation(connection, session_id, hours=4, connection_ids=["conn-1"])


async def test_a_blank_elevation_reason_is_refused(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    with pytest.raises(IntegrityError, match="brain_admin_elevations_reason_nonblank"):
        async with connection.begin_nested():
            await connection.execute(
                sa.text(
                    "INSERT INTO brain_admin_elevations (session_id, connection_ids, "
                    "expires_at, granted_by, reason) "
                    "VALUES (:session_id, ARRAY['c'], now() + interval '1 hour', 'op', '  ')"
                ),
                {"session_id": session_id},
            )


async def test_elevation_requires_an_existing_session_and_follows_its_deletion(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    with pytest.raises(IntegrityError, match="brain_admin_elevations_session_id_fkey"):
        async with connection.begin_nested():
            await _elevation(connection, uuid4(), hours=1, connection_ids=["c"])
    await _elevation(connection, session_id, hours=1, connection_ids=["c"])
    await connection.execute(
        sa.text("DELETE FROM brain_sessions WHERE id = :id"), {"id": session_id}
    )
    assert await connection.scalar(sa.text("SELECT count(*) FROM brain_admin_elevations")) == 0


async def test_the_notify_payload_is_the_row_id_and_never_the_digest(
    engine: AsyncEngine,
) -> None:
    dsn = asyncpg_dsn(os.environ["BRAIN_V42_TEST_DB_URL"])
    listener = await asyncpg.connect(dsn)
    received: asyncio.Queue[str] = asyncio.Queue()
    await listener.add_listener(
        "brain_client_credentials", lambda _c, _pid, _channel, payload: received.put_nowait(payload)
    )
    try:
        async with engine.begin() as connection:
            row_id = await connection.scalar(
                sa.text(
                    "INSERT INTO brain_client_credentials "
                    "(client_id, token_sha256, families, created_by) "
                    "VALUES ('notify-probe', decode(:digest, 'hex'), ARRAY['read'], 'test') "
                    "RETURNING id"
                ),
                {"digest": DIGEST},
            )
        assert isinstance(row_id, UUID)
        assert await asyncio.wait_for(received.get(), timeout=5) == str(row_id)
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM brain_client_credentials WHERE id = :id"), {"id": row_id}
            )
        assert await asyncio.wait_for(received.get(), timeout=5) == str(row_id)
    finally:
        await listener.close()


async def test_downgrade_drops_the_tables_and_function_then_reupgrade_restores_them(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None]
) -> None:
    migration_downgrade_fence(downgraded_to="062")
    down = _run_alembic("downgrade", "062")
    assert down.returncode == 0, down.stderr
    async with engine.connect() as connection:
        for table in TABLES:
            assert await connection.scalar(sa.text(f"SELECT to_regclass('public.{table}')")) is None
        assert (
            await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_proc WHERE proname = 'brain_client_credentials_notify'"
                )
            )
            == 0
        )
    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
    async with engine.connect() as connection:
        for table in TABLES:
            assert (
                await connection.scalar(sa.text(f"SELECT to_regclass('public.{table}')")) == table
            )
