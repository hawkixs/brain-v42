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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from brain_v42.repositories.pg_client_credentials import PgClientCredentialRepo
from tests.integration.disposable_db import asyncpg_dsn

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]

TABLES = (
    "brain_client_credentials",
    "brain_admin_elevations",
    "brain_schema_compat",
    "brain_credential_audit",
)
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
    connection: AsyncConnection,
    session_id: UUID,
    *,
    hours: int,
    connection_ids: list[str],
    client_ids: list[str | None] | None = None,
    via: str = "cli",
    requested_by: str | None = None,
    reason: str = "maintenance window",
) -> None:
    paired = client_ids if client_ids is not None else ["auto-discord"] * len(connection_ids)
    await connection.execute(
        sa.text(
            "INSERT INTO brain_admin_elevations (session_id, connection_ids, "
            "connection_client_ids, granted_at, expires_at, granted_by, reason, via, "
            "requested_by_client_id) VALUES (:session_id, :connection_ids, :client_ids, now(), "
            "now() + make_interval(hours => :hours), 'operator', :reason, :via, :requested_by)"
        ),
        {
            "session_id": session_id,
            "connection_ids": connection_ids,
            "client_ids": paired,
            "hours": hours,
            "reason": reason,
            "via": via,
            "requested_by": requested_by,
        },
    )


async def test_upgrade_creates_the_four_tables_and_the_trigger(engine: AsyncEngine) -> None:
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
    assert len(rows) == 2
    for _name, definition in rows:
        assert "FOR EACH ROW" in definition
        assert "brain_client_credentials_notify()" in definition
    definitions = " | ".join(definition for _name, definition in rows)
    assert "AFTER INSERT OR DELETE" in definitions
    assert "AFTER UPDATE" in definitions


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"families": ["read", "admin"]}, "brain_client_credentials_families_valid"),
        ({"families": ["admin"]}, "brain_client_credentials_families_valid"),
        ({"families": []}, "brain_client_credentials_families_valid"),
        ({"families": ["elevate", "read"]}, "brain_client_credentials_elevate_exclusive"),
        ({"families": ["telemetry", "elevate"]}, "brain_client_credentials_elevate_exclusive"),
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


@pytest.mark.parametrize("families", [["elevate"], ["telemetry"], ["read", "telemetry"]])
async def test_elevate_and_telemetry_are_storable_families(
    operator: tuple[AsyncConnection, UUID], families: list[str]
) -> None:
    connection, _ = operator
    await _credential(connection, families=families)


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


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        (
            {"connection_ids": ["c1", "c2"], "client_ids": ["auto-discord"]},
            "brain_admin_elevations_connection_pairs",
        ),
        (
            {"connection_ids": ["c1"], "client_ids": ["a", "b"]},
            "brain_admin_elevations_connection_pairs",
        ),
        (
            {"connection_ids": ["c1"], "client_ids": [None]},
            "brain_admin_elevations_connection_pairs",
        ),
        ({"via": "api"}, "brain_admin_elevations_via_valid"),
        ({"via": "hook"}, "brain_admin_elevations_hook_requester"),
        (
            {"via": "cli", "requested_by": "workstation-elevate"},
            "brain_admin_elevations_cli_no_requester",
        ),
        ({"reason": "x" * 201}, "brain_admin_elevations_reason_length"),
    ],
)
async def test_elevation_attribution_checks_refuse_their_bad_row(
    operator: tuple[AsyncConnection, UUID], overrides: dict[str, object], constraint: str
) -> None:
    connection, session_id = operator
    params: dict[str, object] = {"hours": 1, "connection_ids": ["c1"]}
    params.update(overrides)
    with pytest.raises(IntegrityError, match=constraint):
        async with connection.begin_nested():
            await _elevation(connection, session_id, **params)  # type: ignore[arg-type]


async def test_a_hook_elevation_with_its_requester_and_a_200_char_reason_is_stored(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    await _elevation(
        connection,
        session_id,
        hours=1,
        connection_ids=["c1", "c2"],
        client_ids=["workstation-claude", "red-rail"],
        via="hook",
        requested_by="workstation-elevate",
        reason="x" * 200,
    )
    audited = await connection.scalar(
        sa.text("SELECT expiry_audited_at FROM brain_admin_elevations WHERE session_id = :id"),
        {"id": session_id},
    )
    assert audited is None


_INSERT_CONNECTION = sa.text(
    "INSERT INTO brain_session_connections (session_id, connection_id, client_id) "
    "VALUES (:session_id, :connection_id, :client_id)"
)


async def _set_opener(connection: AsyncConnection, session_id: UUID, client_id: str | None) -> None:
    await connection.execute(
        sa.text("UPDATE brain_sessions SET opener_client_id = :client WHERE id = :id"),
        {"client": client_id, "id": session_id},
    )


async def test_the_connection_client_id_is_nullable_and_format_checked(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    insert = _INSERT_CONNECTION
    await _set_opener(connection, session_id, "workstation-claude")
    for connection_id, client_id in (("historical", None), ("attributed", "workstation-claude")):
        await connection.execute(
            insert,
            {"session_id": session_id, "connection_id": connection_id, "client_id": client_id},
        )
    with pytest.raises(IntegrityError, match="brain_session_connections_client_id_format"):
        async with connection.begin_nested():
            await connection.execute(
                insert,
                {"session_id": session_id, "connection_id": "bad", "client_id": "Bad_Id"},
            )


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
                    "connection_client_ids, expires_at, granted_by, reason) "
                    "VALUES (:session_id, ARRAY['c'], ARRAY['auto-discord'], "
                    "now() + interval '1 hour', 'op', '  ')"
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


async def test_a_last_used_stamp_does_not_notify_but_a_revocation_does(
    engine: AsyncEngine,
) -> None:
    dsn = asyncpg_dsn(os.environ["BRAIN_V42_TEST_DB_URL"])
    listener = await asyncpg.connect(dsn)
    received: asyncio.Queue[str] = asyncio.Queue()
    await listener.add_listener(
        "brain_client_credentials", lambda _c, _pid, _channel, payload: received.put_nowait(payload)
    )
    repo = PgClientCredentialRepo()
    try:
        async with engine.begin() as connection:
            row_id = await connection.scalar(
                sa.text(
                    "INSERT INTO brain_client_credentials "
                    "(client_id, token_sha256, families, created_by) "
                    "VALUES ('notify-quiet', decode(:digest, 'hex'), ARRAY['read'], 'test') "
                    "RETURNING id"
                ),
                {"digest": DIGEST},
            )
        assert isinstance(row_id, UUID)
        assert await asyncio.wait_for(received.get(), timeout=5) == str(row_id)

        now = datetime.now(UTC)
        async with AsyncSession(engine) as owned, owned.begin():
            assert await repo.touch_last_used([row_id], now, session=owned) == 1
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(received.get(), timeout=1)

        async with AsyncSession(engine) as owned, owned.begin():
            await repo.revoke(row_id, "rotated", now, author="operator", session=owned)
        assert await asyncio.wait_for(received.get(), timeout=5) == str(row_id)
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM brain_client_credentials WHERE id = :id"), {"id": row_id}
            )
    finally:
        await listener.close()


async def test_the_opener_is_nullable_and_format_checked(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    assert (
        await connection.scalar(
            sa.text("SELECT opener_client_id FROM brain_sessions WHERE id = :id"),
            {"id": session_id},
        )
        is None
    )
    with pytest.raises(IntegrityError, match="brain_sessions_opener_client_id_format"):
        async with connection.begin_nested():
            await _set_opener(connection, session_id, "Bad_Id")
    await _set_opener(connection, session_id, "workstation-claude")


async def test_the_trigger_refuses_an_attributed_connection_that_is_not_the_openers(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    await _set_opener(connection, session_id, "workstation-claude")
    await connection.execute(
        _INSERT_CONNECTION,
        {"session_id": session_id, "connection_id": "own", "client_id": "workstation-claude"},
    )
    await connection.execute(
        _INSERT_CONNECTION,
        {"session_id": session_id, "connection_id": "historical", "client_id": None},
    )
    with pytest.raises(IntegrityError, match="foreign client") as foreign:
        async with connection.begin_nested():
            await connection.execute(
                _INSERT_CONNECTION,
                {"session_id": session_id, "connection_id": "secret-conn", "client_id": "red-rail"},
            )
    assert "secret-conn" not in str(foreign.value.orig)
    with pytest.raises(IntegrityError, match="foreign client"):
        async with connection.begin_nested():
            await connection.execute(
                sa.text(
                    "UPDATE brain_session_connections SET client_id = 'red-rail' "
                    "WHERE session_id = :id AND connection_id = 'historical'"
                ),
                {"id": session_id},
            )
    assert (
        await connection.scalar(
            sa.text("SELECT count(*) FROM brain_session_connections WHERE session_id = :id"),
            {"id": session_id},
        )
        == 2
    )


async def test_the_trigger_refuses_an_attributed_connection_on_an_unowned_operator_session(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    with pytest.raises(IntegrityError, match="foreign client"):
        async with connection.begin_nested():
            await connection.execute(
                _INSERT_CONNECTION,
                {
                    "session_id": session_id,
                    "connection_id": "conn",
                    "client_id": "workstation-claude",
                },
            )


async def _new_session(connection: AsyncConnection, nature: str | None) -> UUID:
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
            "INSERT INTO brain_sessions (project_key, client_key, started_focus_revision, "
            "nature, connection_id) VALUES (:key, 'integ-063-other', 0, :nature, :connection) "
            "RETURNING id"
        ),
        {"key": key, "nature": nature, "connection": "c" if nature == "agent" else None},
    )
    assert isinstance(session_id, UUID)
    return session_id


async def test_the_trigger_leaves_an_agent_trace_alone(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, _ = operator
    trace = await _new_session(connection, "agent")
    await connection.execute(
        _INSERT_CONNECTION, {"session_id": trace, "connection_id": "c", "client_id": "red-rail"}
    )


async def test_the_trigger_locks_an_operator_nature_session_like_an_unnamed_one(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, _ = operator
    named = await _new_session(connection, "operator")
    attach = {"session_id": named, "connection_id": "c", "client_id": "workstation-claude"}
    with pytest.raises(IntegrityError, match="foreign client"):
        async with connection.begin_nested():
            await connection.execute(_INSERT_CONNECTION, attach)
    await _set_opener(connection, named, "workstation-claude")
    await connection.execute(_INSERT_CONNECTION, attach)
    with pytest.raises(IntegrityError, match="foreign client"):
        async with connection.begin_nested():
            await connection.execute(
                _INSERT_CONNECTION, {**attach, "connection_id": "d", "client_id": "red-rail"}
            )


async def test_a_set_opener_can_never_change_or_clear(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    await _set_opener(connection, session_id, "workstation-claude")
    await _set_opener(connection, session_id, "workstation-claude")
    for other in ("red-rail", None):
        with pytest.raises(IntegrityError, match="opener"):
            async with connection.begin_nested():
                await _set_opener(connection, session_id, other)
    assert (
        await connection.scalar(
            sa.text("SELECT opener_client_id FROM brain_sessions WHERE id = :id"),
            {"id": session_id},
        )
        == "workstation-claude"
    )


async def _has_opener_and_trigger(connection: AsyncConnection) -> bool:
    column = await connection.scalar(
        sa.text(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'brain_sessions' AND column_name = 'opener_client_id'"
        )
    )
    trigger = await connection.scalar(
        sa.text(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = "
            "'public.brain_session_connections'::regclass AND NOT tgisinternal"
        )
    )
    function = await connection.scalar(
        sa.text(
            "SELECT count(*) FROM pg_proc WHERE proname = 'brain_session_connections_owner_check'"
        )
    )
    immutability = await connection.scalar(
        sa.text(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'public.brain_sessions'::regclass "
            "AND tgname = 'brain_sessions_opener_immutable'"
        )
    )
    functions = await connection.scalar(
        sa.text("SELECT count(*) FROM pg_proc WHERE proname = 'brain_sessions_opener_immutable'")
    )
    assert (column, trigger, function, immutability, functions) in {
        (0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1),
    }
    return bool(column)


async def _has_connection_client_id(connection: AsyncConnection) -> bool:
    return bool(
        await connection.scalar(
            sa.text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'brain_session_connections' AND column_name = 'client_id'"
            )
        )
    )


async def test_downgrade_drops_the_tables_and_function_then_reupgrade_restores_them(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None]
) -> None:
    migration_downgrade_fence(downgraded_to="062")
    down = _run_alembic("downgrade", "062")
    assert down.returncode == 0, down.stderr
    async with engine.connect() as connection:
        for table in TABLES:
            assert await connection.scalar(sa.text(f"SELECT to_regclass('public.{table}')")) is None
        assert await _has_connection_client_id(connection) is False
        assert await _has_opener_and_trigger(connection) is False
        assert (
            await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_proc WHERE proname = 'brain_client_credentials_notify'"
                )
            )
            == 0
        )
        assert (
            await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_proc WHERE proname = 'brain_credential_audit_notify'"
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
        assert await _has_connection_client_id(connection) is True
        assert await _has_opener_and_trigger(connection) is True


async def test_an_elevation_defaults_to_no_exclusion_and_refuses_a_negative_count(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    await _elevation(connection, session_id, hours=1, connection_ids=["c"])
    row = (
        await connection.execute(
            sa.text(
                "SELECT excluded_client_ids, excluded_connection_count "
                "FROM brain_admin_elevations WHERE session_id = :id"
            ),
            {"id": session_id},
        )
    ).one()
    assert (row.excluded_client_ids, row.excluded_connection_count) == ([], 0)
    with pytest.raises(IntegrityError, match="brain_admin_elevations_excluded_count"):
        async with connection.begin_nested():
            await connection.execute(
                sa.text(
                    "INSERT INTO brain_admin_elevations (session_id, connection_ids, "
                    "connection_client_ids, expires_at, granted_by, reason, "
                    "excluded_connection_count) VALUES (:id, ARRAY['c'], ARRAY['auto-discord'], "
                    "now() + interval '1 hour', 'op', 'why', -1)"
                ),
                {"id": session_id},
            )


async def _audit(
    connection: AsyncConnection,
    *,
    event: str = "credentials.issued",
    elevation_id: UUID | None = None,
    payload: str = "{}",
) -> None:
    await connection.execute(
        sa.text(
            "INSERT INTO brain_credential_audit (event, elevation_id, payload) "
            "VALUES (:event, :elevation_id, CAST(:payload AS jsonb))"
        ),
        {"event": event, "elevation_id": elevation_id, "payload": payload},
    )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"event": "credentials.deleted"}, "brain_credential_audit_event_valid"),
        ({"event": "credentials.elevated"}, "brain_credential_audit_elevation_pair"),
        (
            {"event": "credentials.issued", "elevation_id": uuid4()},
            "brain_credential_audit_elevation_pair",
        ),
        ({"payload": "[]"}, "brain_credential_audit_payload_object"),
    ],
)
async def test_audit_checks_refuse_their_bad_row(
    operator: tuple[AsyncConnection, UUID], overrides: dict[str, object], constraint: str
) -> None:
    connection, _ = operator
    with pytest.raises(IntegrityError, match=constraint):
        async with connection.begin_nested():
            await _audit(connection, **overrides)  # type: ignore[arg-type]


async def test_an_elevation_event_is_stored_once_per_elevation(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, _ = operator
    elevation_id = uuid4()
    await _audit(connection, event="credentials.elevation_expired", elevation_id=elevation_id)
    await _audit(connection, event="credentials.elevated", elevation_id=elevation_id)
    with pytest.raises(IntegrityError, match="uq_brain_credential_audit_event_elevation"):
        async with connection.begin_nested():
            await _audit(
                connection, event="credentials.elevation_expired", elevation_id=elevation_id
            )
    await _audit(connection)
    await _audit(connection)


async def test_an_audit_row_starts_unemitted_and_survives_its_elevation(
    operator: tuple[AsyncConnection, UUID],
) -> None:
    connection, session_id = operator
    await _elevation(connection, session_id, hours=1, connection_ids=["c"])
    elevation_id = await connection.scalar(
        sa.text("SELECT id FROM brain_admin_elevations WHERE session_id = :id"),
        {"id": session_id},
    )
    await _audit(connection, event="credentials.elevated", elevation_id=elevation_id)
    await connection.execute(
        sa.text("DELETE FROM brain_sessions WHERE id = :id"), {"id": session_id}
    )
    row = (
        await connection.execute(
            sa.text(
                "SELECT emitted_at, created_at FROM brain_credential_audit WHERE elevation_id = :id"
            ),
            {"id": elevation_id},
        )
    ).one()
    assert row.emitted_at is None
    assert row.created_at is not None


async def test_the_audit_notify_payload_is_the_row_id_only(engine: AsyncEngine) -> None:
    dsn = asyncpg_dsn(os.environ["BRAIN_V42_TEST_DB_URL"])
    listener = await asyncpg.connect(dsn)
    received: asyncio.Queue[str] = asyncio.Queue()
    await listener.add_listener(
        "brain_credential_audit", lambda _c, _pid, _channel, payload: received.put_nowait(payload)
    )
    try:
        async with engine.begin() as connection:
            row_id = await connection.scalar(
                sa.text(
                    "INSERT INTO brain_credential_audit (event, payload) "
                    "VALUES ('credentials.issued', '{\"secret\": \"never-notified\"}') RETURNING id"
                )
            )
        assert await asyncio.wait_for(received.get(), timeout=5) == str(row_id)
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM brain_credential_audit WHERE id = :id"), {"id": row_id}
            )
    finally:
        await listener.close()


async def test_unemitted_audit_rows_have_a_partial_index(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        definition = await connection.scalar(
            sa.text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'idx_brain_credential_audit_unemitted'"
            )
        )
    assert definition is not None
    assert "WHERE (emitted_at IS NULL)" in definition
