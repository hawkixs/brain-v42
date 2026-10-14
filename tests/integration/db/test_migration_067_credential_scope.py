"""Prove that a downgrade cannot silently widen credential authority."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]
OPT_IN = "allow_credential_scope_downgrade"


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={**os.environ, "POSTGRES_URL": os.environ["BRAIN_V42_TEST_DB_URL"]},
    )


async def test_scope_column_is_nullable_without_default(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text(
                    "SELECT is_nullable, column_default, udt_name FROM information_schema.columns "
                    "WHERE table_name = 'brain_client_credentials' AND column_name = 'allowed_projects'"
                )
            )
        ).one()
        assert tuple(row) == ("YES", None, "_text")


async def test_scope_check_needs_no_schema_owned_function(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                sa.text("SELECT to_regprocedure('public.brain_credential_projects_valid(text[])')")
            )
            is None
        )


@pytest.mark.parametrize("opt_in", [False, True])
async def test_scoped_downgrade_requires_explicit_opt_in(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None], opt_in: bool
) -> None:
    async with engine.begin() as connection:
        identifier = await connection.scalar(
            sa.text(
                "INSERT INTO brain_client_credentials (client_id, token_sha256, families, created_by, allowed_projects) "
                "VALUES ('scope-migration', :digest, ARRAY['write'], 'operator', ARRAY['brain-v42']) RETURNING id"
            ),
            {"digest": uuid4().bytes + uuid4().bytes},
        )
    try:
        migration_downgrade_fence(downgraded_to="066")
        args = ("-x", f"{OPT_IN}=yes") if opt_in else ()
        result = _run_alembic(*args, "downgrade", "066")
        if opt_in:
            assert result.returncode == 0, result.stderr
            up = _run_alembic("upgrade", "head")
            assert up.returncode == 0, up.stderr
        else:
            assert result.returncode != 0
            assert OPT_IN in result.stderr + result.stdout
        async with engine.connect() as connection:
            scope = await connection.scalar(
                sa.text("SELECT allowed_projects FROM brain_client_credentials WHERE id = :id"),
                {"id": identifier},
            )
        assert scope == (None if opt_in else ["brain-v42"])
    finally:
        up = _run_alembic("upgrade", "head")
        assert up.returncode == 0, up.stderr
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM brain_client_credentials WHERE id = :id"), {"id": identifier}
            )


async def test_null_scope_downgrade_and_upgrade_preserve_existing_credentials(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None]
) -> None:
    async with engine.begin() as connection:
        identifier = await connection.scalar(
            sa.text(
                "INSERT INTO brain_client_credentials (client_id, token_sha256, families, created_by) "
                "VALUES ('scope-unrestricted', :digest, ARRAY['read'], 'operator') RETURNING id"
            ),
            {"digest": uuid4().bytes + uuid4().bytes},
        )
    try:
        migration_downgrade_fence(downgraded_to="066")
        down = _run_alembic("downgrade", "066")
        assert down.returncode == 0, down.stderr
        up = _run_alembic("upgrade", "head")
        assert up.returncode == 0, up.stderr
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text("SELECT allowed_projects FROM brain_client_credentials WHERE id = :id"),
                    {"id": identifier},
                )
                is None
            )
    finally:
        up = _run_alembic("upgrade", "head")
        assert up.returncode == 0, up.stderr
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM brain_client_credentials WHERE id = :id"), {"id": identifier}
            )
