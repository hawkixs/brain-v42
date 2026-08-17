"""PostgreSQL round trip for project-context timestamp migration 039."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[3]


def _repo_head() -> str:
    """Head of the migration chain, read from the repo rather than written here.

    These assertions run after `alembic upgrade head`, so the revision they
    expect is whatever head currently is — not a constant. A literal rots at
    the next migration: `039` outlived its truth the day `040` landed and left
    this test red in CI while the code it guards was correct.
    """
    head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    assert head is not None, "migration chain has no single head"
    return head


def _run_alembic(*args: str) -> None:
    database_url = os.environ["BRAIN_V42_TEST_DB_URL"]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "POSTGRES_URL": database_url},
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"alembic_failed:{' '.join(args)}:{result.stderr}")


def _run_alembic_result(*args: str) -> subprocess.CompletedProcess[str]:
    database_url = os.environ["BRAIN_V42_TEST_DB_URL"]
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "POSTGRES_URL": database_url},
        capture_output=True,
        text=True,
        timeout=60,
    )


async def _contract_state(engine: AsyncEngine) -> tuple[str, str | None, str | None]:
    async with engine.connect() as connection:
        revision = str(await connection.scalar(sa.text("SELECT version_num FROM alembic_version")))
        dedicated = await connection.scalar(
            sa.text("SELECT to_regprocedure('public.set_project_context_updated_at()')::text")
        )
        trigger_function = await connection.scalar(
            sa.text(
                """
                SELECT function_record.proname
                FROM pg_catalog.pg_trigger AS trigger_record
                JOIN pg_catalog.pg_proc AS function_record
                  ON function_record.oid = trigger_record.tgfoid
                WHERE trigger_record.tgrelid = 'public.project_contexts'::regclass
                  AND trigger_record.tgname = 'trg_project_contexts_updated'
                """
            )
        )
    return revision, dedicated, trigger_function


@pytest.mark.asyncio
async def test_migration_039_downgrade_and_reupgrade(engine: AsyncEngine) -> None:
    assert await _contract_state(engine) == (
        _repo_head(),
        "set_project_context_updated_at()",
        "set_project_context_updated_at",
    )
    try:
        _run_alembic(
            "-x",
            "allow_project_context_trigger_downgrade=yes",
            "downgrade",
            "038",
        )
        assert await _contract_state(engine) == ("038", None, "update_updated_at")
    finally:
        _run_alembic("upgrade", "head")
    assert await _contract_state(engine) == (
        _repo_head(),
        "set_project_context_updated_at()",
        "set_project_context_updated_at",
    )


@pytest.mark.asyncio
async def test_migration_039_downgrade_without_opt_in_is_atomic(engine: AsyncEngine) -> None:
    expected = await _contract_state(engine)
    result = _run_alembic_result("downgrade", "038")
    try:
        assert result.returncode != 0
        assert await _contract_state(engine) == expected
    finally:
        if (await _contract_state(engine))[0] != _repo_head():
            _run_alembic("upgrade", "head")


@pytest.mark.asyncio
async def test_migration_039_upgrade_rejects_trigger_drift_atomically(
    engine: AsyncEngine,
) -> None:
    _run_alembic(
        "-x",
        "allow_project_context_trigger_downgrade=yes",
        "downgrade",
        "038",
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DROP TRIGGER trg_project_contexts_updated ON public.project_contexts")
            )
            await connection.execute(
                sa.text(
                    "CREATE TRIGGER trg_project_contexts_updated "
                    "BEFORE UPDATE OF updated_at ON public.project_contexts "
                    "FOR EACH ROW EXECUTE FUNCTION public.update_updated_at()"
                )
            )
        result = _run_alembic_result("upgrade", "head")
        assert result.returncode != 0
        assert await _contract_state(engine) == ("038", None, "update_updated_at")
    finally:
        if (await _contract_state(engine))[0] == "038":
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text("DROP TRIGGER trg_project_contexts_updated ON public.project_contexts")
                )
                await connection.execute(
                    sa.text(
                        "CREATE TRIGGER trg_project_contexts_updated "
                        "BEFORE UPDATE ON public.project_contexts FOR EACH ROW "
                        "EXECUTE FUNCTION public.update_updated_at()"
                    )
                )
        _run_alembic("upgrade", "head")
