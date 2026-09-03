"""Migration 047 — the database stops requiring a capture receipt to close.

The scene's third rail. The repository and the Pydantic model let through a closure
whose ledger the SERVER had filled; here we prove the database does too. Without
this rail, 047 would be an intention: the `brain_sessions_terminal_state_valid`
CHECK refused the row, and the user was left with a session they could no longer
close.

What REMAINS refused is the point: a blank reason. Giving a reason is an act, and
the server cannot perform it in the user's place.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_ENDED = sa.text(
    """
    INSERT INTO brain_sessions (
        id, project_key, client_key, status, started_focus, started_focus_revision,
        captured_knowledge_ids, nothing_to_capture_reason, summary, next_focus,
        ended_at, end_expected_focus_revision, focus_outcome, focus_at_end,
        focus_revision_at_end
    ) VALUES (
        gen_random_uuid(), :project_key, :client_key, 'ended', 'old', 7,
        CAST(:captured AS uuid[]), :reason, 'reviewed design', 'implement tools',
        now(), 7, 'applied', 'implement tools', 8
    )
    """
)


async def _project(engine: AsyncEngine) -> str:
    project_key = f"integ-047-{uuid4().hex[:10]}"
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO project_contexts (project_key, name, description) "
                "VALUES (:project_key, :project_key, 'migration 047 scene')"
            ),
            {"project_key": project_key},
        )
    return project_key


async def _close(
    engine: AsyncEngine,
    project_key: str,
    *,
    client_key: str,
    captured: list[str],
    reason: str | None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            _ENDED,
            {
                "project_key": project_key,
                "client_key": client_key,
                "captured": captured,
                "reason": reason,
            },
        )


async def test_an_ended_session_may_hold_a_derived_ledger_and_a_reason(
    engine: AsyncEngine,
) -> None:
    """THE case that motivates the revision: the server attributed, the user says
    "nothing durable". Before 047, that session was unclosable."""
    project_key = await _project(engine)
    await _close(
        engine,
        project_key,
        client_key="derived",
        captured=[str(uuid4())],
        reason="no durable new knowledge",
    )


async def test_an_ended_session_may_hold_neither_a_ledger_nor_a_reason(
    engine: AsyncEngine,
) -> None:
    """The gate is no longer diligence: having produced nothing is an outcome."""
    project_key = await _project(engine)
    await _close(engine, project_key, client_key="quiet", captured=[], reason=None)


async def test_a_blank_reason_is_still_refused_by_the_database(engine: AsyncEngine) -> None:
    """What 047 does NOT relax, and the one thing the server cannot produce."""
    project_key = await _project(engine)
    with pytest.raises(IntegrityError, match="brain_sessions_terminal_state_valid"):
        await _close(engine, project_key, client_key="blank", captured=[], reason="   ")


async def test_the_downgrade_refuses_to_destroy_a_closure_it_cannot_restore(
    engine: AsyncEngine,
    migration_downgrade_fence,
) -> None:
    """Fail-closed, 037's template: the downgrade NAMES what it would destroy.

    The two shapes 047 makes legal are illegal without it. A mute downgrade would
    fail anyway — the constraint would refuse it — but with a constraint message,
    without saying WHO is at fault. This one says so, and counts.
    """
    project_key = await _project(engine)
    await _close(
        engine,
        project_key,
        client_key="derived",
        captured=[str(uuid4())],
        reason="no durable new knowledge",
    )
    migration_downgrade_fence("046")

    # Read from the environment, not from the conftest constant: that constant
    # is bound when the conftest is imported, which is before the session fixture
    # in this directory rebinds the variable to a disposable database.
    INTEGRATION_DB_URL = os.environ["BRAIN_V42_TEST_DB_URL"]

    async with engine.connect() as conn:
        head_before = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            "allow_focus_history_downgrade=yes",
            "downgrade",
            "046",
        ],
        env={**os.environ, "POSTGRES_URL": INTEGRATION_DB_URL},
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0, "le downgrade a détruit une fermeture dérivée en silence"
    assert "cannot downgrade 047" in result.stderr
    assert "ended session(s) hold a capture outcome" in result.stderr

    async with engine.connect() as conn:
        head = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
    # Compared to the head MEASURED before the attempt, never to a literal. The
    # previous form pinned `"047"` and became false at every subsequent revision —
    # the database is then higher than 047, and a `downgrade 046` traverses the
    # intermediate revisions. This is the class of defect this repository has already
    # paid for with hard-coded Alembic heads.
    assert head == head_before, "le refus doit laisser la tête intacte"
