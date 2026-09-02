"""Disposable databases built by the alembic chain — the shared bricks.

Extracted from `tests/integration/db/test_fresh_head_is_the_yardstick.py`, which
had them private. A second consumer now needs the same four gestures (the schema
family guard, which derives its reference from a fresh head rather than pinning
it), and two copies of "create, upgrade, drop" would drift the day one of them
learns something — a timeout, a lock, a cleanup order.

Measured on 2026-09-03: **0.93 s** end to end on this host — 0.05 s to create,
0.85 s for `alembic upgrade head` across 51 revisions, 0.04 s to drop. That number
is what makes a DERIVED reference affordable once per session instead of a pinned
literal that ages at every migration. Re-measure it rather than trusting this line
if the chain grows a slow revision.

These databases never touch `brain`: they carry a unique generated name and live
in the same server as the URL handed in, exactly like `brain_test` itself.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import asyncpg

PROJECT_ROOT = Path(__file__).parents[2]


def asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def swap_database(url: str, database: str) -> str:
    base, _, _old = url.rpartition("/")
    return f"{base}/{database}"


def run_sql(dsn: str, statements: list[str]) -> None:
    async def run() -> None:
        connection = await asyncpg.connect(dsn)
        try:
            for statement in statements:
                await connection.execute(statement)
        finally:
            await connection.close()

    asyncio.run(run())


def create_database(admin_url: str, database: str) -> None:
    run_sql(asyncpg_dsn(admin_url), [f'CREATE DATABASE "{database}"'])


def drop_database(admin_url: str, database: str) -> None:
    run_sql(asyncpg_dsn(admin_url), [f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'])


def alembic_upgrade_head(db_url: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "POSTGRES_URL": db_url},
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stderr}\n{result.stdout}")


@contextmanager
def fresh_head_database(admin_url: str, *, prefix: str = "brain_fresh") -> Iterator[str]:
    """A pristine database brought to head, destroyed on the way out.

    The drop runs in a `finally`: a database left behind holds a connection slot
    and, worse, would be picked up by a later `\\l` as if someone meant it.
    """
    database = f"{prefix}_{uuid.uuid4().hex[:12]}"
    create_database(admin_url, database)
    url = swap_database(admin_url, database)
    try:
        alembic_upgrade_head(url)
        yield url
    finally:
        drop_database(admin_url, database)
