"""Migration 065: programme progress (programmes/v1, tickets 8a063256 and 24c1f172).

The tables hold what the contract `docs/contracts/programmes_v1.md` publishes. The
database refuses the shapes the contract excludes, so a writer that skipped the
service's validation still cannot store a gate the consumers would choke on.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]
NOW = datetime(2026, 10, 13, 9, tzinfo=UTC)


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
async def project(engine: AsyncEngine) -> AsyncIterator[tuple[AsyncConnection, str]]:
    """One project inside a transaction that is always rolled back."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            key = f"integ-065-{uuid4().hex[:12]}"
            await connection.execute(
                sa.text(
                    "INSERT INTO project_contexts (project_key, name, description) "
                    "VALUES (:key, :key, 'migration 065')"
                ),
                {"key": key},
            )
            yield connection, key
        finally:
            await transaction.rollback()


async def _programme(connection: AsyncConnection, project: str, **overrides: Any) -> UUID:
    values: dict[str, Any] = {
        "key": f"p-{uuid4().hex[:8]}",
        "title": "Brain to red-base",
        "coordinator": project,
        "state": "active",
        "position": 1,
        "by": project,
    }
    values.update(overrides)
    programme_id = await connection.scalar(
        sa.text(
            "INSERT INTO programmes (key, title, coordinator_project, state, position, "
            "updated_by) VALUES (:key, :title, :coordinator, :state, :position, :by) "
            "RETURNING id"
        ),
        values,
    )
    assert isinstance(programme_id, UUID)
    return programme_id


async def _gate(
    connection: AsyncConnection, programme_id: UUID, owner: str, **overrides: Any
) -> UUID:
    values: dict[str, Any] = {
        "programme_id": programme_id,
        "key": "G1",
        "title": "Design",
        "owner": owner,
        "state": "todo",
        "depends_on": [],
        "weight": 1,
        "ask": None,
        "since": None,
        "done": None,
        "total": None,
        "unit": None,
        "note": None,
        "position": 1,
        "by": owner,
    }
    values.update(overrides)
    gate_id = await connection.scalar(
        sa.text(
            "INSERT INTO programme_gates (programme_id, key, title, owner_project, state, "
            "depends_on, weight, waiting_ask, waiting_since, progress_done, progress_total, "
            "progress_unit, note, position, updated_by) VALUES (:programme_id, :key, :title, "
            ":owner, :state, :depends_on, :weight, :ask, :since, :done, :total, :unit, :note, "
            ":position, :by) RETURNING id"
        ),
        values,
    )
    assert isinstance(gate_id, UUID)
    return gate_id


async def _refused(connection: AsyncConnection, statement: Any) -> None:
    savepoint = await connection.begin_nested()
    with pytest.raises((IntegrityError, DataError)):  # a CHECK, a key or a length
        await statement
    await savepoint.rollback()


async def test_upgrade_creates_both_tables(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        for table in ("programmes", "programme_gates"):
            assert (
                await connection.scalar(sa.text(f"SELECT to_regclass('public.{table}')")) == table
            )


async def test_a_valid_programme_and_gate_round_trip_with_server_times(
    project: tuple[AsyncConnection, str],
) -> None:
    connection, key = project
    programme_id = await _programme(connection, key)
    gate_id = await _gate(
        connection,
        programme_id,
        key,
        state="in_progress",
        weight=4,
        ask="validate the spec",
        since=NOW,
        done=6,
        total=15,
        unit="tasks",
        note="first deploy",
    )
    row = (
        await connection.execute(
            sa.text(
                "SELECT g.updated_at = now() AS fresh, p.last_change_gate "
                "FROM programme_gates g JOIN programmes p ON p.id = g.programme_id "
                "WHERE g.id = :id"
            ),
            {"id": gate_id},
        )
    ).one()
    assert row.fresh is True
    assert row.last_change_gate is None


@pytest.mark.parametrize(
    ("overrides"),
    [
        {"state": "finished"},
        {"key": "has space"},
        {"key": "x" * 65},
        {"title": "   "},
        {"title": "t" * 121},
        {"position": -1},
        # Review round 1 of #376: outer blanks, line breaks, identities.
        {"title": " padded"},
        {"title": "two" + chr(10) + "lines"},
        {"title": "bidi" + chr(0x202E) + "flip"},
        {"by": "Brain_V42"},
        {"by": ""},
        # Review round 2 of #376: Unicode spaces at the edges, not only U+0020.
        {"title": chr(0xA0) + "padded"},
        {"title": "padded" + chr(0x2003)},
        {"title": chr(0x3000) + "ideographic"},
    ],
)
async def test_programme_shapes_outside_the_contract_are_refused(
    project: tuple[AsyncConnection, str], overrides: dict[str, Any]
) -> None:
    connection, key = project
    await _refused(connection, _programme(connection, key, **overrides))


async def test_programme_last_change_is_all_or_nothing(
    project: tuple[AsyncConnection, str],
) -> None:
    connection, key = project
    programme_id = await _programme(connection, key)
    await _refused(
        connection,
        connection.execute(
            sa.text("UPDATE programmes SET last_change_gate = 'G1' WHERE id = :id"),
            {"id": programme_id},
        ),
    )
    await _refused(
        connection,
        connection.execute(
            sa.text(
                "UPDATE programmes SET last_change_gate = 'G1', last_change_from = 'todo', "
                "last_change_to = 'todo', last_change_at = now(), last_change_by = :key "
                "WHERE id = :id"
            ),
            {"id": programme_id, "key": key},
        ),
    )
    for gate, by in (("bad key", key), ("G" * 33, key), ("G1", "BAD_KEY")):
        await _refused(
            connection,
            connection.execute(
                sa.text(
                    "UPDATE programmes SET last_change_gate = :gate, last_change_from = 'todo', "
                    "last_change_to = 'done', last_change_at = now(), last_change_by = :by "
                    "WHERE id = :id"
                ),
                {"id": programme_id, "gate": gate, "by": by},
            ),
        )
    await connection.execute(
        sa.text(
            "UPDATE programmes SET last_change_gate = 'G1', last_change_from = 'todo', "
            "last_change_to = 'done', last_change_at = now(), last_change_by = :key "
            "WHERE id = :id"
        ),
        {"id": programme_id, "key": key},
    )


@pytest.mark.parametrize(
    ("overrides"),
    [
        {"state": "mostly_done"},
        {"key": "G 7"},
        {"key": "G" * 33},
        {"weight": 0},
        {"weight": 101},
        {"depends_on": [f"G{n}" for n in range(13)]},
        {"depends_on": ["G1"]},
        {"depends_on": ["G 2"]},
        {"ask": "validate", "since": None},
        {"ask": None, "since": NOW},
        {"ask": "x" * 201, "since": NOW},
        {"ask": "  ", "since": NOW},
        {"done": 3, "total": None, "unit": None},
        {"done": 16, "total": 15, "unit": "tasks"},
        {"done": -1, "total": 15, "unit": "tasks"},
        {"done": 0, "total": 0, "unit": "tasks"},
        {"done": 0, "total": 100001, "unit": "tasks"},
        {"done": 1, "total": 2, "unit": "u" * 25},
        {"note": ""},
        {"note": "n" * 201},
        # Review round 1 of #376: each dependency is checked on its own.
        {"depends_on": ["G2,G3"]},
        {"depends_on": [""]},
        {"depends_on": ["G2", ""]},
        {"depends_on": ["G1,"]},
        {"title": "padded "},
        {"note": "line" + chr(0x2028) + "separator"},
        {"note": "zero" + chr(0x200B) + "width"},
        {"ask": "tab" + chr(9) + "inside", "since": NOW},
        {"done": 1, "total": 2, "unit": " t"},
        {"by": "BAD_KEY"},
        # Review round 2 of #376: Unicode spaces at the edges, not only U+0020.
        {"note": "narrow" + chr(0x202F)},
        {"ask": chr(0x1680) + "ogham", "since": NOW},
        {"done": 1, "total": 2, "unit": "t" + chr(0x205F)},
        {"title": chr(0x2000) + "en quad"},
    ],
)
async def test_gate_shapes_outside_the_contract_are_refused(
    project: tuple[AsyncConnection, str], overrides: dict[str, Any]
) -> None:
    connection, key = project
    programme_id = await _programme(connection, key)
    await _refused(connection, _gate(connection, programme_id, key, **overrides))


async def test_valid_dependencies_and_clean_text_still_pass(
    project: tuple[AsyncConnection, str],
) -> None:
    connection, key = project
    programme_id = await _programme(connection, key, title="café — déjà vu")
    await _gate(connection, programme_id, key, key="G1")
    await _gate(connection, programme_id, key, key="G2")
    await _gate(connection, programme_id, key, key="G3", depends_on=["G1", "G2"], note="naïve 50 %")
    # A Unicode space inside a line is text, not padding.
    await _gate(connection, programme_id, key, key="G4", note="50" + chr(0xA0) + "%")


async def test_gate_keys_are_unique_per_programme_only(
    project: tuple[AsyncConnection, str],
) -> None:
    connection, key = project
    first = await _programme(connection, key)
    second = await _programme(connection, key)
    await _gate(connection, first, key, key="G1")
    await _gate(connection, second, key, key="G1")
    await _refused(connection, _gate(connection, first, key, key="G1"))


async def test_owner_and_coordinator_must_be_known_projects(
    project: tuple[AsyncConnection, str],
) -> None:
    connection, key = project
    unknown = f"nobody-{uuid4().hex[:8]}"
    await _refused(connection, _programme(connection, key, coordinator=unknown))
    programme_id = await _programme(connection, key)
    await _refused(connection, _gate(connection, programme_id, unknown))


async def test_programme_keys_are_unique(project: tuple[AsyncConnection, str]) -> None:
    connection, key = project
    await _programme(connection, key, key="brain-move")
    await _refused(connection, _programme(connection, key, key="brain-move"))


async def test_downgrade_drops_both_tables_and_upgrade_restores_them(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None]
) -> None:
    migration_downgrade_fence(downgraded_to="064")
    down = _run_alembic("downgrade", "064")
    assert down.returncode == 0, down.stderr
    async with engine.connect() as connection:
        for table in ("programmes", "programme_gates"):
            assert await connection.scalar(sa.text(f"SELECT to_regclass('public.{table}')")) is None
    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
    async with engine.connect() as connection:
        for table in ("programmes", "programme_gates"):
            assert (
                await connection.scalar(sa.text(f"SELECT to_regclass('public.{table}')")) == table
            )
