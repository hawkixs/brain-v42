"""Migrations never inherit a short budget from the database they run against.

A future ``ALTER ROLE`` / ``ALTER DATABASE ... SET statement_timeout`` aimed at the
application must not turn a schema migration into a timeout. Settings sent at
connection time win over those defaults, which is what ``alembic/env.py`` relies
on; this proves it on a private database, never on ``brain`` or ``brain_test``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.disposable_db import asyncpg_dsn, run_sql

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]


def _run_alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "POSTGRES_URL": url},
        timeout=120,
    )


def test_migrations_ignore_a_one_millisecond_database_statement_timeout(
    _private_module_head: str,
) -> None:
    database = _private_module_head.rsplit("/", 1)[1]
    assert database.startswith("brain_migration_module_"), "refusing a non-private database"
    dsn = asyncpg_dsn(_private_module_head)

    run_sql(dsn, [f"ALTER DATABASE \"{database}\" SET statement_timeout = '1ms'"])
    try:
        down = _run_alembic(_private_module_head, "downgrade", "060")
        assert down.returncode == 0, down.stderr
        up = _run_alembic(_private_module_head, "upgrade", "head")
        assert up.returncode == 0, up.stderr
    finally:
        run_sql(dsn, [f'ALTER DATABASE "{database}" RESET statement_timeout'])
