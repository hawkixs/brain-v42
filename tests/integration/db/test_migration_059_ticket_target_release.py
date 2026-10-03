"""PostgreSQL round trip for migration 059: tickets.target_release."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from brain_v42.models.ticket import TARGET_RELEASE_PATTERN

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]
_OPT_IN = "allow_target_release_downgrade"


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


async def _ticket(engine: AsyncEngine, target_release: str | None = None) -> str:
    async with engine.begin() as connection:
        result = await connection.execute(
            sa.text(
                "INSERT INTO tickets (kind, title, body, from_project, to_project, target_release) "
                "VALUES ('request', 'integ-059', 'b', 'brain-v42', 'brain-v42', :r) RETURNING id"
            ),
            {"r": target_release},
        )
        return str(result.scalar_one())


async def _drop(engine: AsyncEngine, ticket_id: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(sa.text("DELETE FROM tickets WHERE id = :i"), {"i": ticket_id})


@pytest.mark.asyncio
async def test_column_is_nullable_without_default(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text(
                    "SELECT is_nullable, column_default, data_type FROM information_schema.columns "
                    "WHERE table_name = 'tickets' AND column_name = 'target_release'"
                )
            )
        ).one()
    assert (row.is_nullable, row.column_default, row.data_type) == ("YES", None, "text")
    ticket = await _ticket(engine)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                sa.text("SELECT target_release FROM tickets WHERE id = :i"), {"i": ticket}
            )
        assert value is None
    finally:
        await _drop(engine, ticket)


@pytest.mark.parametrize("bad", ["v0.6.3", "0.6", "0.6.3-rc1", " 0.6.3", "0.6.3 ", ""])
@pytest.mark.asyncio
async def test_check_refuses_invalid_values(engine: AsyncEngine, bad: str) -> None:
    with pytest.raises(IntegrityError, match="tickets_target_release_valid"):
        await _ticket(engine, bad)


@pytest.mark.asyncio
async def test_check_accepts_multi_digit_components(engine: AsyncEngine) -> None:
    ticket = await _ticket(engine, "0.10.12")
    await _drop(engine, ticket)


@pytest.mark.asyncio
async def test_check_and_python_pattern_are_identical(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        definition = await connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'tickets_target_release_valid'"
            )
        )
    assert f"'{TARGET_RELEASE_PATTERN}'" in definition


@pytest.mark.asyncio
async def test_partial_index_covers_planned_rows_only(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        indexdef = await connection.scalar(
            sa.text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_tickets_to_project_target_release'"
            )
        )
    assert "(to_project, target_release)" in indexdef
    assert "WHERE (target_release IS NOT NULL)" in indexdef


@pytest.mark.asyncio
async def test_downgrade_refuses_plans(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None]
) -> None:
    ticket = await _ticket(engine, "0.6.3")
    try:
        migration_downgrade_fence(downgraded_to="059")
        refused = _run_alembic("downgrade", "058")
        assert refused.returncode != 0
        assert _OPT_IN in refused.stderr + refused.stdout
        async with engine.connect() as connection:
            kept = await connection.scalar(
                sa.text("SELECT target_release FROM tickets WHERE id = :i"), {"i": ticket}
            )
        assert kept == "0.6.3"
    finally:
        await _drop(engine, ticket)


@pytest.mark.asyncio
async def test_opt_in_downgrades_and_head_returns(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None]
) -> None:
    ticket = await _ticket(engine, "0.6.3")
    try:
        migration_downgrade_fence(downgraded_to="058")
        down = _run_alembic("-x", f"{_OPT_IN}=yes", "downgrade", "058")
        assert down.returncode == 0, down.stderr
        up = _run_alembic("upgrade", "head")
        assert up.returncode == 0, up.stderr
        async with engine.connect() as connection:
            value = await connection.scalar(
                sa.text("SELECT target_release FROM tickets WHERE id = :i"), {"i": ticket}
            )
        assert value is None
    finally:
        await _drop(engine, ticket)
