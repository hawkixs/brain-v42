"""Migration 048 — the database can say BY WHICH KEY a row was attributed.

Without this rail, 048 would be an intention. Three things are only provable here:
the CHECK refuses an invented mode, the partial index really exists, and above all
the downgrade REFUSES to destroy what distinguishes a proof from a deduction.

That last point is the only one that really matters. The downgrade loses no ledger
row: the artifacts keep their session, and a downgraded database looks perfectly
healthy. What it has lost — "this attribution was GUESSED, not proved" — is
invisible in the remaining data. That is exactly the class of loss a refusal must
NAME, because nobody will notice it.
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


async def _session_with_one_artifact(engine: AsyncEngine, *, mode: str | None) -> tuple[str, str]:
    """A project, a session, a ledger artifact carrying this mode."""
    project_key = f"integ-048-{uuid4().hex[:10]}"
    knowledge_id = str(uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO project_contexts (project_key, name, description) "
                "VALUES (:project_key, :project_key, 'migration 048 scene')"
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
                "INSERT INTO brain_session_artifacts "
                "(knowledge_id, session_id, knowledge_type, attribution_mode) "
                "VALUES (CAST(:knowledge_id AS uuid), :session_id, 'learning', :mode)"
            ),
            {"knowledge_id": knowledge_id, "session_id": session_id, "mode": mode},
        )
    return project_key, knowledge_id


async def _cleanup(engine: AsyncEngine, project_key: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "DELETE FROM brain_session_artifacts WHERE session_id IN "
                "(SELECT id FROM brain_sessions WHERE project_key = :project_key)"
            ),
            {"project_key": project_key},
        )
        await conn.execute(
            sa.text("DELETE FROM brain_sessions WHERE project_key = :project_key"),
            {"project_key": project_key},
        )
        await conn.execute(
            sa.text("DELETE FROM project_contexts WHERE project_key = :project_key"),
            {"project_key": project_key},
        )


async def test_the_check_accepts_the_four_modes_and_nothing_else(engine: AsyncEngine) -> None:
    """Four named modes, and an invented fifth fails at INSERT.

    The CHECK is what makes a typo fail here rather than in production, six months
    later, on a row nobody will be able to read back.
    """
    for mode in ("explicit", "derived_deposit", "derived_connection", "derived_window", None):
        project_key, _ = await _session_with_one_artifact(engine, mode=mode)
        await _cleanup(engine, project_key)

    with pytest.raises(IntegrityError, match="attribution_mode_valid"):
        project_key, _ = await _session_with_one_artifact(engine, mode="derived_vibes")
    # The INSERT failed: nothing to clean up, the transaction died with it.


async def test_the_deduced_mode_is_the_only_one_with_its_own_index(engine: AsyncEngine) -> None:
    """Undoing a guess must be a QUERY, not a scan.

    The other three modes are never searched in bulk: `explicit` is the normal case,
    and the two derived ones are read row by row. Indexing all four would cost four
    indexes for a single real question.
    """
    async with engine.connect() as conn:
        indexes = set(
            (
                await conn.execute(
                    sa.text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = 'brain_session_artifacts'"
                    )
                )
            )
            .scalars()
            .all()
        )
        predicate = await conn.scalar(
            sa.text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'idx_brain_session_artifacts_derived_window'"
            )
        )

    assert "idx_brain_session_artifacts_derived_window" in indexes
    assert predicate is not None and "derived_window" in predicate
    assert "WHERE" in predicate, "un index PLEIN coûterait pour trois modes qu'on ne cherche pas"


async def test_the_downgrade_refuses_to_erase_what_marks_a_guess_as_a_guess(
    engine: AsyncEngine,
    migration_downgrade_fence,
) -> None:
    """Fail-closed, 047's template: count AND NAME.

    A mute downgrade would succeed here — no constraint opposes it, the column
    disappears and everything looks healthy. That is precisely why an explicit
    refusal is needed: the loss has no symptom.
    """
    project_key, knowledge_id = await _session_with_one_artifact(engine, mode="derived_window")
    migration_downgrade_fence("047")

    from tests.integration.conftest import INTEGRATION_DB_URL

    async with engine.connect() as conn:
        head_before = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))

    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "047"],
            env={**os.environ, "POSTGRES_URL": INTEGRATION_DB_URL},
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode != 0, "le downgrade a effacé une déduction en silence"
        assert "cannot downgrade 048" in result.stderr
        # NAME, not merely count: a message saying "1 row" without saying which
        # leaves the operator with no possible move.
        assert knowledge_id in result.stderr
        assert "allow_attribution_mode_downgrade=yes" in result.stderr

        async with engine.connect() as conn:
            head = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
        # "Intact" = WHERE IT WAS, not a literal: the multi-step descent runs in a
        # transaction — since 049, 048's refusal also rolls back the 049→048 step,
        # and a literal "048" would have reddened at every new head for an unrelated
        # reason.
        assert head == head_before, "le refus doit laisser la tête intacte"
    finally:
        await _cleanup(engine, project_key)


async def test_the_named_opt_in_lets_a_deliberate_operator_through(
    engine: AsyncEngine,
    migration_downgrade_fence,
) -> None:
    """The refusal is a guard, not a walled-up door — and the opt-in is NAMED.

    A generic flag gets copied from one migration to the next without anyone
    re-reading what it authorises; this one means only one thing and applies only to
    048.
    """
    project_key, _ = await _session_with_one_artifact(engine, mode="derived_window")
    migration_downgrade_fence("047")

    from tests.integration.conftest import INTEGRATION_DB_URL

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-x",
                "allow_attribution_mode_downgrade=yes",
                "downgrade",
                "047",
            ],
            env={**os.environ, "POSTGRES_URL": INTEGRATION_DB_URL},
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

        async with engine.connect() as conn:
            head = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
            columns = set(
                (
                    await conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'brain_session_artifacts'"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert head == "047"
        assert "attribution_mode" not in columns
        # The ledger row, for its part, is STILL there: this downgrade loses no
        # attribution, it loses the trace of what distinguished it from a proof.
        async with engine.connect() as conn:
            surviving = await conn.scalar(
                sa.text(
                    "SELECT count(*) FROM brain_session_artifacts a "
                    "JOIN brain_sessions s ON s.id = a.session_id "
                    "WHERE s.project_key = :project_key"
                ),
                {"project_key": project_key},
            )
        assert surviving == 1
    finally:
        # GO BACK UP OURSELVES. The conftest's safety net exists for the case where
        # a red assertion short-circuits this block, not to stand in for a
        # restoration: a test that passes and leaves the database behind poisons
        # every following one, and the net says so explicitly.
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env={**os.environ, "POSTGRES_URL": INTEGRATION_DB_URL},
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        await _cleanup(engine, project_key)
