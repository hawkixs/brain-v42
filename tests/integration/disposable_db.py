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
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import asyncpg
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

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


def repository_head() -> str:
    """The single head declared under `alembic/versions/`, DERIVED not retyped.

    Migration benches downgrade the shared database and restore it afterwards.
    Both gestures were written with the head of the day as a literal, which works
    exactly until the next migration lands: the 051 bench asserted `head == "051"`
    and restored to `051`, so the day 052 arrived it failed on the assertion AND
    left the database one revision behind for every test that followed.

    A downgrade TARGET stays a literal — `050` is the predecessor of 051 and will
    not become anything else. What must be derived is the head a bench returns to,
    and the head it expects to still find after a REFUSED downgrade.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini"))).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one alembic head, found {heads}")
    return heads[0]


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


async def replay_attestation(url: str, asset: Path) -> dict[str, dict[str, Any]]:
    """Replay an attestation asset READ-ONLY, return its failures alone.

    `SET TRANSACTION READ ONLY` then rollback: a contract that wrote would no
    longer be a contract, and a disposable database owes its cleanliness to the
    alembic chain alone -- not to the fact that nobody looked.

    Lives here rather than in one of its two callers. The yardstick had it
    private; the ACL mutation module imported it across test modules, which works
    and reads like an accident. Two copies would drift the day one of them learns
    something -- a timeout, a different isolation level, a second receipt shape.

    An EMPTY mapping means every check passed. That is the whole return contract:
    callers assert `== {}` for a clean receipt and index by check id otherwise.
    """
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(sa.text("SET TRANSACTION READ ONLY"))
                raw = await connection.scalar(sa.text(asset.read_text(encoding="utf-8")))
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()

    receipt = json.loads(str(raw))
    return {check["id"]: check for check in receipt["checks"] if check["status"] != "pass"}
