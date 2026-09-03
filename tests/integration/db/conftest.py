"""This directory runs on its own database, created and destroyed per session.

Ticket `f7af0977`. The tests here downgrade and re-upgrade their database, and a
downgrade that drops a column NEVER gives its `attnum` back: the `pg_attribute`
row survives with `attisdropped`, and `VACUUM FULL` does not reclaim it. Against
a SHARED `brain_test` that made the database's life finite, and the number is not
comfortable — measured 2026-09-03, ONE run of `tests/integration/db` burns **77
dropped columns per decay table**. PostgreSQL's hard ceiling is 1600 columns, so
the shared database survived roughly **20 runs**, and on 2026-09-02 it reached it:
`ALTER TABLE snippets ADD COLUMN` failed with `TooManyColumnsError`, `alembic
upgrade head` could not complete, and 162 tests failed for a reason none of them
was about.

Recreating `brain_test` by hand fixes the symptom and restarts the clock. This
moves the clock somewhere it cannot hurt: the whole directory binds to a database
built by the alembic chain for this session and dropped after it, so the burn is
thrown away with it.

The retargeting works by rebinding `BRAIN_V42_TEST_DB_URL` before the engine is
built. A module that read that variable at IMPORT time would bypass it silently —
imports happen during collection, before any fixture runs — which is why
`tests/unit/test_migration_tests_do_not_capture_the_shared_url.py` refuses that
shape in source, with no database of its own.

`run_migrations` from the parent conftest still runs against the real shared
database, on purpose: it is what applies the schema family guard, and an
`alembic upgrade head` on a database already at head costs nothing and burns no
column.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.conftest import _get_integration_db_url_or_skip
from tests.integration.disposable_db import fresh_head_database


@pytest.fixture(scope="session")
def migration_database_url() -> Iterator[str]:
    """A database at head for this session, dropped after it. No rebinding here.

    Built once because it costs 0.93 s and the tests here leave it at head; the
    burn they inflict is thrown away with the database rather than accumulated in
    a shared one.
    """
    # The parent's resolver, not the raw variable: it SKIPS both when the variable
    # is unset and when it is set to something rejected (a production database, a
    # malformed URL). Reading the environment directly turned a clean skip into an
    # error on a rejected value, which the loud-skip banner exists to prevent.
    shared_url = _get_integration_db_url_or_skip()

    with fresh_head_database(shared_url, prefix="brain_migration") as disposable_url:
        yield disposable_url


@pytest.fixture(autouse=True)
def _point_the_environment_at_the_disposable_database(
    migration_database_url: str,
) -> Iterator[None]:
    """Rebind for the duration of ONE test in this directory, and no longer.

    The rebind is the only channel the alembic subprocesses have: each builds its
    environment from `os.environ` at call time, so none of them needs to know this
    fixture exists.

    Function-scoped ON PURPOSE. A session-scoped rebind was the first shape tried
    and it LEAKED: `tests/integration/db` sorts before `tests/integration/mcp`, so
    the variable was still rebound when the parent's shared engine was first
    built, and eight `mcp` tests ran against a database that was dropped at the
    end of the session. Measured: 8 failed, 433 passed. Narrow is not a style
    preference here.
    """
    shared_url = os.environ.get("BRAIN_V42_TEST_DB_URL")
    os.environ["BRAIN_V42_TEST_DB_URL"] = migration_database_url
    try:
        yield
    finally:
        if shared_url is None:
            del os.environ["BRAIN_V42_TEST_DB_URL"]
        else:
            os.environ["BRAIN_V42_TEST_DB_URL"] = shared_url


@pytest_asyncio.fixture(scope="session")
async def engine(migration_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Overrides the shared-database engine, for this directory only.

    Deliberately NOT depending on `run_migrations`: this database comes out of
    `alembic upgrade head` already, and depending on the parent's fixture would
    order it before the rebind above.
    """
    disposable_engine = create_async_engine(migration_database_url, poolclass=NullPool, echo=False)
    yield disposable_engine
    await disposable_engine.dispose()
