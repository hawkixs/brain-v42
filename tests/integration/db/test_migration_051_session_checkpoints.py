"""Migration 051 — append-only is a property of the DATA, or it is a wish.

Everything this file proves is unprovable anywhere else. A unit test can show that
no code path updates a checkpoint; only the database can show that no code path
CAN. That distinction is the whole reason `SPEC-checkpoint.md` §3 asks for a
trigger instead of a convention, and it is the same reason 039 pins a function by
SHA256 rather than by trust.

The `ON DELETE RESTRICT` consequence is tested here too, and deliberately so: it
is the one behaviour an operator meets by surprise. A session carrying checkpoints
becomes INDELIBLE — deleting it needs its checkpoints gone first, and the trigger
forbids exactly that. Consistent with append-only, and not obvious until it bites.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.disposable_db import repository_head

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


async def _session_with_checkpoint(
    engine: AsyncEngine,
    *,
    seq: int = 1,
    progress: str = "read the spec",
    next_step: str = "write the table",
    blocker: str | None = None,
) -> tuple[str, str]:
    """A project, an open session, and one checkpoint on it."""
    project_key = f"integ-051-{uuid4().hex[:10]}"
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO project_contexts (project_key, name, description) "
                "VALUES (:project_key, :project_key, 'migration 051 scene')"
            ),
            {"project_key": project_key},
        )
        session_id = await conn.scalar(
            sa.text(
                "INSERT INTO brain_sessions "
                "(id, project_key, client_key, status, started_focus_revision) "
                "VALUES (gen_random_uuid(), :project_key, :client_key, 'open', 0) "
                "RETURNING id"
            ),
            {"project_key": project_key, "client_key": f"c-{uuid4().hex[:8]}"},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO brain_session_checkpoints "
                "(session_id, seq, progress, next_step, blocker) "
                "VALUES (:session_id, :seq, :progress, :next_step, :blocker)"
            ),
            {
                "session_id": session_id,
                "seq": seq,
                "progress": progress,
                "next_step": next_step,
                "blocker": blocker,
            },
        )
    return project_key, str(session_id)


async def _cleanup(engine: AsyncEngine, project_key: str) -> None:
    """Undo the scene — which the trigger makes deliberately awkward.

    The checkpoints cannot be deleted while the trigger is armed, so the fixture
    drops it for the length of one transaction rather than pretending the ledger is
    erasable. Doing this in the CLEANUP and never in a test keeps the guard true
    everywhere it is under test.
    """
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "ALTER TABLE brain_session_checkpoints "
                "DISABLE TRIGGER brain_session_checkpoints_append_only"
            )
        )
        await conn.execute(
            sa.text(
                "DELETE FROM brain_session_checkpoints WHERE session_id IN "
                "(SELECT id FROM brain_sessions WHERE project_key = :project_key)"
            ),
            {"project_key": project_key},
        )
        await conn.execute(
            sa.text(
                "ALTER TABLE brain_session_checkpoints "
                "ENABLE TRIGGER brain_session_checkpoints_append_only"
            )
        )
        await conn.execute(
            sa.text("DELETE FROM brain_sessions WHERE project_key = :project_key"),
            {"project_key": project_key},
        )
        await conn.execute(
            sa.text("DELETE FROM project_contexts WHERE project_key = :project_key"),
            {"project_key": project_key},
        )


async def test_an_update_is_refused_by_the_database_not_by_the_code(
    engine: AsyncEngine,
) -> None:
    """The point of the trigger: rewriting a judgment is impossible, not merely unimplemented."""
    project_key, session_id = await _session_with_checkpoint(engine)
    try:
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "UPDATE brain_session_checkpoints SET progress = 'rewritten' "
                        "WHERE session_id = :session_id"
                    ),
                    {"session_id": session_id},
                )

        async with engine.connect() as conn:
            kept = await conn.scalar(
                sa.text(
                    "SELECT progress FROM brain_session_checkpoints WHERE session_id = :session_id"
                ),
                {"session_id": session_id},
            )
        assert kept == "read the spec"
    finally:
        await _cleanup(engine, project_key)


async def test_a_delete_is_refused_and_names_the_session_and_seq(engine: AsyncEngine) -> None:
    """The message must let an operator find WHICH judgment was protected."""
    project_key, session_id = await _session_with_checkpoint(engine, seq=7)
    try:
        with pytest.raises(DBAPIError) as excinfo:
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text("DELETE FROM brain_session_checkpoints WHERE session_id = :session_id"),
                    {"session_id": session_id},
                )
        message = str(excinfo.value)
        assert "append-only" in message
        assert session_id in message
        assert "7" in message
    finally:
        await _cleanup(engine, project_key)


async def test_a_session_carrying_checkpoints_cannot_be_deleted(engine: AsyncEngine) -> None:
    """`ON DELETE RESTRICT`, and the surprise it buys.

    This is the cost the migration docstring writes down: append-only plus RESTRICT
    makes such a session INDELIBLE. It is tested so the property is discovered here
    and not by an operator at 3am.
    """
    project_key, session_id = await _session_with_checkpoint(engine)
    try:
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text("DELETE FROM brain_sessions WHERE id = :session_id"),
                    {"session_id": session_id},
                )
    finally:
        await _cleanup(engine, project_key)


async def test_the_same_seq_twice_is_refused_by_the_unique_key(engine: AsyncEngine) -> None:
    """The key IS the idempotence mechanism (SPEC §1.1); without it there is no replay."""
    project_key, session_id = await _session_with_checkpoint(engine, seq=3)
    try:
        with pytest.raises(IntegrityError, match="uq_brain_session_checkpoints_session_seq"):
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO brain_session_checkpoints "
                        "(session_id, seq, progress, next_step) "
                        "VALUES (:session_id, 3, 'other', 'other')"
                    ),
                    {"session_id": session_id},
                )
    finally:
        await _cleanup(engine, project_key)


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("progress", "   ", "brain_session_checkpoints_progress_nonempty"),
        ("next_step", "   ", "brain_session_checkpoints_next_step_nonempty"),
        ("blocker", "   ", "brain_session_checkpoints_blocker_nonempty"),
    ],
)
async def test_a_blank_judgment_is_refused_by_its_own_named_check(
    engine: AsyncEngine, column: str, value: str, constraint: str
) -> None:
    """Blank is not a short judgment, it is the absence of one — and the DB says so.

    Each CHECK is named separately so a failure names the FIELD, not merely the row.
    """
    project_key, session_id = await _session_with_checkpoint(engine)
    payload = {"progress": "p", "next_step": "n", "blocker": None, column: value}
    try:
        with pytest.raises(IntegrityError, match=constraint):
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO brain_session_checkpoints "
                        "(session_id, seq, progress, next_step, blocker) "
                        "VALUES (:session_id, 99, :progress, :next_step, :blocker)"
                    ),
                    {"session_id": session_id, **payload},
                )
    finally:
        await _cleanup(engine, project_key)


async def test_a_seq_below_one_is_refused(engine: AsyncEngine) -> None:
    project_key, session_id = await _session_with_checkpoint(engine)
    try:
        with pytest.raises(IntegrityError, match="brain_session_checkpoints_seq_positive"):
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO brain_session_checkpoints "
                        "(session_id, seq, progress, next_step) "
                        "VALUES (:session_id, 0, 'p', 'n')"
                    ),
                    {"session_id": session_id},
                )
    finally:
        await _cleanup(engine, project_key)


async def test_the_downgrade_refuses_to_destroy_judgment_and_names_its_sessions(
    engine: AsyncEngine,
    migration_downgrade_fence,
) -> None:
    """Fail-closed, 047-048-050's template: count AND NAME.

    A mute downgrade would succeed — no constraint opposes dropping the table, and
    the result looks perfectly healthy. What is gone is prose that exists in no
    other column and cannot be recomputed from anything. A loss with no symptom is
    exactly the loss a refusal has to name.
    """
    project_key, session_id = await _session_with_checkpoint(engine)
    migration_downgrade_fence("050")

    # Read from the environment, not from the conftest constant: that constant
    # is bound when the conftest is imported, which is before the session fixture
    # in this directory rebinds the variable to a disposable database.
    INTEGRATION_DB_URL = os.environ["BRAIN_V42_TEST_DB_URL"]

    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "050"],
            env={**os.environ, "POSTGRES_URL": INTEGRATION_DB_URL},
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode != 0, "the downgrade destroyed session judgment in silence"
        assert "cannot downgrade 051" in result.stderr
        assert session_id in result.stderr, "a count without the sessions leaves no move to make"
        assert "allow_checkpoint_downgrade=yes" in result.stderr

        async with engine.connect() as conn:
            head = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
            still_there = await conn.scalar(
                sa.text(
                    "SELECT count(*) FROM brain_session_checkpoints WHERE session_id = :session_id"
                ),
                {"session_id": session_id},
            )
        # DERIVED, not "051": a refused downgrade leaves the head where it was,
        # and where it was is wherever the chain currently ends. Written as a
        # literal this assertion started failing the day 052 landed, for a reason
        # that had nothing to do with what it tests.
        assert head == repository_head()
        assert still_there == 1
    finally:
        await _cleanup(engine, project_key)


async def test_the_named_opt_in_lets_a_deliberate_operator_through(
    engine: AsyncEngine,
    migration_downgrade_fence,
) -> None:
    """The refusal is a fence, not a wall: named, it opens, and the head returns to 050."""
    project_key, _ = await _session_with_checkpoint(engine)
    migration_downgrade_fence("050")

    # Read from the environment, not from the conftest constant: that constant
    # is bound when the conftest is imported, which is before the session fixture
    # in this directory rebinds the variable to a disposable database.
    INTEGRATION_DB_URL = os.environ["BRAIN_V42_TEST_DB_URL"]

    try:
        down = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-x",
                "allow_checkpoint_downgrade=yes",
                "downgrade",
                "050",
            ],
            env={**os.environ, "POSTGRES_URL": INTEGRATION_DB_URL},
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert down.returncode == 0, down.stderr

        async with engine.connect() as conn:
            head = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
            table_gone = await conn.scalar(
                sa.text("SELECT to_regclass('public.brain_session_checkpoints')")
            )
        assert head == "050"
        assert table_gone is None
    finally:
        # `head`, not "051". This bench shares its database: restoring to a
        # literal left every following test one revision behind, and the setup
        # guard reported it as residue from an interrupted run — a true message
        # pointing at the wrong cause.
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env={**os.environ, "POSTGRES_URL": INTEGRATION_DB_URL},
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        await _cleanup(engine, project_key)
