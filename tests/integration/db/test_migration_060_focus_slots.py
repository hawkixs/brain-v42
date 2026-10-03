"""PostgreSQL contract for migration 060: focus slots, slot history, binding, 16314b31.

Slot rows can never be deleted (append-only history, no delete path), so nothing
here writes them onto this session's shared head: the in-process tests use the
module-private `private_head_engine`, and every scenario that moves the head runs
on a database built by `fresh_head_database`. The file still contains "downgrade",
so it is declared in PRIVATE_HEAD_DOWNGRADING_FILES (it needs no downgrade fence).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[3]
_OPT_IN = "allow_focus_slots_downgrade"


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "POSTGRES_URL": url},
        timeout=180,
    )


@pytest.fixture
def private_head() -> Iterator[str]:
    with fresh_head_database(
        os.environ["BRAIN_V42_TEST_DB_URL"], prefix="brain_migration_060"
    ) as url:
        yield url


async def _project(conn: AsyncConnection) -> str:
    key = f"slots-{uuid4().hex[:10]}"
    await conn.execute(
        sa.text(
            "INSERT INTO project_contexts (project_key, name, description) "
            "VALUES (:k, :k, 'migration 060')"
        ),
        {"k": key},
    )
    return key


async def _slot(conn: AsyncConnection, project_key: str, **columns: object) -> UUID:
    """Insert a slot AND its revision's history row, as the writer module will."""
    values = {"title": f"slot {uuid4().hex[:6]}", "body": "body", **columns}
    names = ", ".join(values)
    marks = ", ".join(f":{name}" for name in values)
    slot_id = await conn.scalar(
        sa.text(
            f"INSERT INTO focus_slots (project_key, {names}) VALUES (:project_key, {marks}) "
            "RETURNING id"
        ),
        {"project_key": project_key, **values},
    )
    await conn.execute(
        sa.text(
            "INSERT INTO focus_slot_history (slot_id, revision, body, source) "
            "SELECT id, revision, body, 'slot_open' FROM focus_slots WHERE id = :s"
        ),
        {"s": slot_id},
    )
    assert isinstance(slot_id, UUID)
    return slot_id


async def _session(conn: AsyncConnection, project_key: str, **columns: object) -> UUID:
    values = {"client_key": f"k-{uuid4().hex[:8]}", "started_focus_revision": 0, **columns}
    names = ", ".join(values)
    marks = ", ".join(f":{name}" for name in values)
    session_id = await conn.scalar(
        sa.text(
            f"INSERT INTO brain_sessions (project_key, {names}) "
            f"VALUES (:project_key, {marks}) RETURNING id"
        ),
        {"project_key": project_key, **values},
    )
    assert isinstance(session_id, UUID)
    return session_id


async def _focus_tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(
            (
                await conn.execute(
                    sa.text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "AND tablename LIKE 'focus_slot%'"
                    )
                )
            ).scalars()
        )


@pytest.mark.asyncio
async def test_the_three_tables_and_their_guards_exist_enabled(
    private_head_engine: AsyncEngine,
) -> None:
    async with private_head_engine.connect() as conn:
        triggers = dict(
            (
                await conn.execute(
                    sa.text(
                        "SELECT tgname, tgenabled::text FROM pg_trigger WHERE NOT tgisinternal "
                        "AND tgrelid::regclass::text LIKE 'focus_slot%'"
                    )
                )
            ).all()
        )
    assert await _focus_tables(private_head_engine) == {
        "focus_slots",
        "focus_slot_anchors",
        "focus_slot_history",
    }
    assert triggers == {
        "focus_slots_history_required": "O",
        "focus_slot_history_append_only_trigger": "O",
        "focus_slot_anchors_append_only_trigger": "O",
    }


@pytest.mark.asyncio
async def test_closed_inactive_with_a_null_nature_is_refused_16314b31(
    private_head_engine: AsyncEngine,
) -> None:
    async with private_head_engine.begin() as conn:
        project = await _project(conn)
    with pytest.raises(IntegrityError, match="brain_sessions_terminal_state_valid"):
        async with private_head_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO brain_sessions (project_key, client_key, "
                    "started_focus_revision, status, ended_at) "
                    "VALUES (:p, 'null-nature', 0, 'closed_inactive', now())"
                ),
                {"p": project},
            )
    async with private_head_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO brain_sessions (project_key, client_key, started_focus_revision, "
                "status, nature, ended_at) VALUES (:p, 'trace', 0, 'closed_inactive', "
                "'agent', now())"
            ),
            {"p": project},
        )


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        ({"title": "   "}, "focus_slots_title_nonblank"),
        ({"body": " "}, "focus_slots_body_valid"),
        ({"body": "x" * 4001}, "focus_slots_body_valid"),
        ({"revision": -1}, "focus_slots_revision_valid"),
        ({"close_reason": "explicit", "close_note": "n"}, "focus_slots_close_valid"),
    ],
)
@pytest.mark.asyncio
async def test_every_focus_slots_check_refuses_bad_values(
    private_head_engine: AsyncEngine, columns: dict[str, object], constraint: str
) -> None:
    async with private_head_engine.begin() as conn:
        project = await _project(conn)
    with pytest.raises(IntegrityError, match=constraint):
        async with private_head_engine.begin() as conn:
            await _slot(conn, project, **columns)


@pytest.mark.asyncio
async def test_close_checks_refuse_half_written_closes(private_head_engine: AsyncEngine) -> None:
    async with private_head_engine.begin() as conn:
        project = await _project(conn)
        slot_id = await _slot(conn, project)
    for assignment in (
        "closed_at = now()",
        "closed_at = now(), close_reason = 'explicit'",
        "closed_at = now(), close_reason = 'explicit', close_note = '  '",
        "closed_at = now(), close_reason = 'receipt:not-a-uuid'",
        f"closed_at = now(), close_reason = 'receipt:{UUID(int=1)}', close_note = 'n'",
        "close_reason = 'explicit', close_note = 'n'",
    ):
        with pytest.raises(IntegrityError, match="focus_slots_close_valid"):
            async with private_head_engine.begin() as conn:
                await conn.execute(
                    sa.text(f"UPDATE focus_slots SET {assignment} WHERE id = :i"),
                    {"i": slot_id},
                )


@pytest.mark.asyncio
async def test_the_body_bound_counts_characters_not_bytes(private_head_engine: AsyncEngine) -> None:
    """4,000 characters is 8,000 bytes here: the limit must count the former."""
    async with private_head_engine.begin() as conn:
        project = await _project(conn)
        slot_id = await _slot(conn, project, body="é" * 4000)
        sizes = (
            await conn.execute(
                sa.text(
                    "SELECT char_length(body), octet_length(body) FROM focus_slots WHERE id = :i"
                ),
                {"i": slot_id},
            )
        ).one()
    assert tuple(sizes) == (4000, 8000)
    with pytest.raises(IntegrityError, match="focus_slots_body_valid"):
        async with private_head_engine.begin() as conn:
            await _slot(conn, project, title="one too many", body="é" * 4001)


@pytest.mark.parametrize(
    ("anchor", "refusal"),
    [
        ("kind = 'ticket', ticket_id = NULL", "focus_slot_anchors_shape"),
        ("kind = 'lot', target_release = NULL", "focus_slot_anchors_shape"),
        ("kind = 'lot', target_release = '0.6'", "focus_slot_anchors_shape"),
        ("kind = 'lot', target_release = 'v0.6.4'", "focus_slot_anchors_shape"),
        ("kind = 'pr', repository_id = 7, pr_number = NULL", "focus_slot_anchors_shape"),
        ("kind = 'pr', repository_id = NULL, pr_number = 3", "focus_slot_anchors_shape"),
        ("kind = 'pr', repository_id = 7, pr_number = 0", "focus_slot_anchors_shape"),
        (
            "kind = 'pr', repository_id = 7, pr_number = 3, target_release = '0.6.4'",
            "focus_slot_anchors_shape",
        ),
        ("kind = 'other', target_release = '0.6.4'", "focus_slot_anchors_shape"),
        ("kind = NULL, target_release = '0.6.4'", "null value"),
    ],
)
@pytest.mark.asyncio
async def test_anchor_shape_refuses_nulls_and_mixed_kinds(
    private_head_engine: AsyncEngine, anchor: str, refusal: str
) -> None:
    async with private_head_engine.begin() as conn:
        project = await _project(conn)
        slot_id = await _slot(conn, project)
    pairs = [part.split(" = ") for part in anchor.split(", ")]
    names = ", ".join(name for name, _ in pairs)
    values = ", ".join(value for _, value in pairs)
    # `kind = NULL` trips NOT NULL before the CHECK: the only case with another message.
    with pytest.raises(IntegrityError, match=refusal):
        async with private_head_engine.begin() as conn:
            await conn.execute(
                sa.text(f"INSERT INTO focus_slot_anchors (slot_id, {names}) VALUES (:s, {values})"),
                {"s": slot_id},
            )


@pytest.mark.parametrize(
    ("source", "with_session", "constraint"),
    [
        # An unknown source breaks both CHECKs (session_valid is false for it too);
        # PostgreSQL reports the first by name, so source_valid is never the one named.
        ("other", False, "focus_slot_history_session_valid"),
        (None, False, "null value"),
        ("session_end", False, "focus_slot_history_session_valid"),
        ("session_relay", False, "focus_slot_history_session_valid"),
        ("slot_open", True, "focus_slot_history_session_valid"),
        ("slot_close", True, "focus_slot_history_session_valid"),
    ],
)
@pytest.mark.asyncio
async def test_history_checks_refuse_nulls_and_mismatched_sessions(
    private_head_engine: AsyncEngine, source: str | None, with_session: bool, constraint: str
) -> None:
    async with private_head_engine.begin() as conn:
        project = await _project(conn)
        slot_id = await _slot(conn, project)
        session_id = await _session(conn, project) if with_session else None
    with pytest.raises(IntegrityError, match=constraint):
        async with private_head_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO focus_slot_history (slot_id, revision, body, source, session_id) "
                    "VALUES (:s, 99, 'b', :src, :sid)"
                ),
                {"s": slot_id, "src": source, "sid": session_id},
            )


@pytest.mark.asyncio
async def test_brain_sessions_slot_checks_refuse_traces_and_slotless_relays(
    private_head_engine: AsyncEngine,
) -> None:
    async with private_head_engine.begin() as conn:
        project = await _project(conn)
        slot_id = await _slot(conn, project)
        donor = await _session(conn, project)
    with pytest.raises(IntegrityError, match="brain_sessions_slot_operator_only"):
        async with private_head_engine.begin() as conn:
            await _session(conn, project, nature="agent", slot_id=slot_id)
    with pytest.raises(IntegrityError, match="brain_sessions_relay_requires_slot"):
        async with private_head_engine.begin() as conn:
            await _session(conn, project, relayed_from_session_id=donor)
    with pytest.raises(IntegrityError, match="brain_sessions_slot_operator_only"):
        async with private_head_engine.begin() as conn:
            await _session(
                conn, project, nature="agent", slot_id=slot_id, relayed_from_session_id=donor
            )
    async with private_head_engine.begin() as conn:  # nature IS NULL is an operator row: accepted
        await _session(conn, project, slot_id=slot_id)


@pytest.mark.asyncio
async def test_one_open_session_per_slot_and_one_relay_per_session(
    private_head_engine: AsyncEngine,
) -> None:
    async with private_head_engine.begin() as conn:
        project = await _project(conn)
        slot_id = await _slot(conn, project)
        first = await _session(conn, project, slot_id=slot_id)
    with pytest.raises(IntegrityError, match="uq_brain_sessions_open_slot"):
        async with private_head_engine.begin() as conn:
            await _session(conn, project, slot_id=slot_id)
    async with private_head_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE brain_sessions SET status = 'abandoned', abandonment_reason = 'r', "
                "ended_at = now() WHERE id = :i"
            ),
            {"i": first},
        )
        await _session(conn, project, slot_id=slot_id, relayed_from_session_id=first)
    with pytest.raises(IntegrityError, match="uq_brain_sessions_relayed_from"):
        async with private_head_engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO brain_sessions (project_key, client_key, "
                    "started_focus_revision, slot_id, relayed_from_session_id, status, "
                    "abandonment_reason, ended_at) VALUES (:p, :k, 0, :s, :f, 'abandoned', "
                    "'r', now())"
                ),
                {"p": project, "k": f"k-{uuid4().hex[:8]}", "s": slot_id, "f": first},
            )


@pytest.mark.asyncio
async def test_one_open_slot_per_title(private_head_engine: AsyncEngine) -> None:
    async with private_head_engine.begin() as conn:
        project = await _project(conn)
        await _slot(conn, project, title="same")
    with pytest.raises(IntegrityError, match="uq_focus_slots_open_title"):
        async with private_head_engine.begin() as conn:
            await _slot(conn, project, title="same")


@pytest.mark.asyncio
async def test_a_slot_revision_without_its_history_row_fails_at_commit(
    private_head_engine: AsyncEngine,
) -> None:
    async with private_head_engine.begin() as conn:
        project = await _project(conn)
        slot_id = await _slot(conn, project)
    with pytest.raises(DBAPIError, match="focus_slot_history_row_missing"):
        async with private_head_engine.begin() as conn:
            await conn.execute(
                sa.text("INSERT INTO focus_slots (project_key, title, body) VALUES (:p, 'x', 'b')"),
                {"p": project},
            )
    with pytest.raises(DBAPIError, match="focus_slot_history_row_missing"):
        async with private_head_engine.begin() as conn:
            await conn.execute(
                sa.text("UPDATE focus_slots SET revision = revision + 1 WHERE id = :i"),
                {"i": slot_id},
            )


@pytest.mark.asyncio
async def test_history_and_anchors_are_append_only(private_head_engine: AsyncEngine) -> None:
    async with private_head_engine.begin() as conn:
        project = await _project(conn)
        slot_id = await _slot(conn, project)
        await conn.execute(
            sa.text(
                "INSERT INTO focus_slot_anchors (slot_id, kind, target_release) "
                "VALUES (:s, 'lot', '0.6.4')"
            ),
            {"s": slot_id},
        )
    for statement, message in (
        ("UPDATE focus_slot_history SET body = 'x' WHERE slot_id = :s", "append-only"),
        ("DELETE FROM focus_slot_history WHERE slot_id = :s", "append-only"),
        (
            "UPDATE focus_slot_anchors SET target_release = '0.6.5' WHERE slot_id = :s",
            "written once",
        ),
        ("DELETE FROM focus_slot_anchors WHERE slot_id = :s", "written once"),
    ):
        with pytest.raises(DBAPIError, match=message):
            async with private_head_engine.begin() as conn:
                await conn.execute(sa.text(statement), {"s": slot_id})


def test_upgrade_precount_aborts_on_a_null_nature_closed_inactive_row(private_head: str) -> None:
    """Session-run (subprocess): alembic runs as a child process."""
    engine = create_async_engine(private_head, poolclass=NullPool)

    async def run(statement: str) -> None:
        async with engine.begin() as conn:
            await conn.execute(sa.text(statement))

    down = _alembic(private_head, "downgrade", "059")
    assert down.returncode == 0, down.stderr
    asyncio.run(
        run(
            "INSERT INTO project_contexts (project_key, name, description) "
            "VALUES ('slots-precount', 'p', 'p')"
        )
    )
    asyncio.run(
        run(
            "INSERT INTO brain_sessions (project_key, client_key, started_focus_revision, "
            "status, ended_at) VALUES ('slots-precount', 'legacy', 0, 'closed_inactive', now())"
        )
    )
    refused = _alembic(private_head, "upgrade", "head")
    assert refused.returncode != 0
    assert "16314b31" in refused.stderr + refused.stdout
    asyncio.run(run("DELETE FROM brain_sessions WHERE client_key = 'legacy'"))
    accepted = _alembic(private_head, "upgrade", "head")
    assert accepted.returncode == 0, accepted.stderr
    asyncio.run(engine.dispose())


def test_downgrade_refuses_with_slots_and_passes_with_the_named_opt_in(private_head: str) -> None:
    """Session-run (subprocess): alembic runs as a child process."""
    engine = create_async_engine(private_head, poolclass=NullPool)

    async def seed() -> None:
        async with engine.begin() as conn:
            project = await _project(conn)
            await _slot(conn, project)

    asyncio.run(seed())
    refused = _alembic(private_head, "downgrade", "059")
    assert refused.returncode != 0
    assert _OPT_IN in refused.stderr + refused.stdout
    assert asyncio.run(_focus_tables(engine)) == {
        "focus_slots",
        "focus_slot_anchors",
        "focus_slot_history",
    }
    down = _alembic(private_head, "-x", f"{_OPT_IN}=yes", "downgrade", "059")
    assert down.returncode == 0, down.stderr
    assert asyncio.run(_focus_tables(engine)) == set()
    up = _alembic(private_head, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    asyncio.run(engine.dispose())
