"""The directory that downgrades migrations must not do it on a shared database.

Ticket `f7af0977`. The hermetic guard in
`tests/unit/test_migration_tests_do_not_capture_the_shared_url.py` refuses the
source shape that would silently re-attach a module to the shared database. This
one checks the LIVE binding instead: what the engine is actually connected to
while these tests run.

Both are needed. Source can be clean and the fixture still mis-wired; the fixture
can be right and one module still hold an import-time capture. Neither test
implies the other.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

#: Names that must never be what this directory downgrades.
FORBIDDEN_TARGETS = frozenset({"brain", "brain_test"})


@pytest.mark.asyncio
async def test_the_engine_is_bound_to_a_database_made_for_this_session(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        database = await connection.scalar(sa.text("SELECT current_database()"))

    assert database not in FORBIDDEN_TARGETS, (
        f"this directory is downgrading migrations against {database!r}. Every dropped "
        "column burns an attnum that PostgreSQL never returns: measured 2026-09-03, one "
        "run costs 77 per decay table, and the 1600 ceiling arrives in about 20 runs."
    )
    assert str(database).startswith("brain_migration_"), (
        f"expected the per-session disposable database, got {database!r}"
    )


@pytest.mark.asyncio
async def test_that_database_is_at_head_and_carries_the_real_schema(
    engine: AsyncEngine,
) -> None:
    """A disposable database that is EMPTY would make every test here vacuous."""
    async with engine.connect() as connection:
        revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        tables = await connection.scalar(
            sa.text("SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public'")
        )

    assert revision, "no alembic revision: the chain did not run on this database"
    assert int(tables or 0) > 25, f"only {tables} tables — this is not a database at head"


@pytest.mark.asyncio
async def test_the_alembic_subprocesses_reach_the_same_database(
    engine: AsyncEngine, migration_database_url: str
) -> None:
    """The rebind is the ONLY channel the subprocesses have; prove they share it.

    Every alembic call in this directory builds its environment from `os.environ`
    at call time. If the engine and that variable ever diverged, the tests would
    assert against one database while downgrading another — green, and measuring
    nothing.
    """
    import os

    assert os.environ["BRAIN_V42_TEST_DB_URL"] == migration_database_url
    assert str(engine.url).rsplit("/", 1)[-1] == migration_database_url.rsplit("/", 1)[-1]
