"""PostgreSQL round trip for the focus-history migration 050 (M-D).

The 048 template: what the revision lays down, what its downgrade refuses to
destroy, and what the named opt-in unlocks. Plus the two things 050 owns that no
earlier revision did — an append-only table, and a DEFERRED constraint trigger
that must abort at COMMIT rather than at the statement.

The trigger ships DISABLED, so the tests that exercise it arm it and put it back.
Leaking the armed state is survivable by construction: the schema guard accepts
this one trigger in `D` or `O`, never in `R`.

The downgrade tests take `migration_downgrade_fence` — not politeness, a
requirement: six files migrate this shared database and the fence both serializes
them and restores the head when a test leaves it behind.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[3]

_TRIGGER = "project_contexts_focus_history_required"
_OPT_IN = "allow_focus_history_downgrade"
_PROBE = "w44-focus-history-probe"
#: `pg_trigger.tgenabled` is Postgres's `"char"` type, which asyncpg hands back
#: as a single byte rather than a str. Comparing to "D" passes silently as
#: False, so the constant is bytes and stays bytes.
_DISABLED = b"D"


def _repo_head() -> str:
    """The head, READ from the chain rather than written here.

    These tests restore the database after downgrading it, and the revision they
    must restore to is whatever head currently is — not 050. Writing `"050"`
    made them leave the shared database one revision behind the day 051 landed:
    they PASSED and still poisoned the run, which is exactly the failure mode
    `migration_downgrade_fence` exists to catch and refuses to paper over.

    What stays literal is `"049"`: that is 050's parent, a fact about 050 rather
    than about the tree, and it does not move when a revision is added.
    """
    head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    assert head is not None, "migration chain has no single head"
    return head


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "POSTGRES_URL": os.environ["BRAIN_V42_TEST_DB_URL"]},
        capture_output=True,
        text=True,
        timeout=120,
    )


async def _drop_history_rows(connection: AsyncConnection, like: str) -> None:
    """Delete audit rows the only way the append-only trigger permits: by lifting it.

    Restored immediately, in the same transaction. The alternative — dropping and
    re-laying the table — costs two alembic subprocesses per test.
    """
    await connection.execute(sa.text("ALTER TABLE project_focus_history DISABLE TRIGGER USER"))
    await connection.execute(
        sa.text("DELETE FROM project_focus_history WHERE project_key LIKE :like"), {"like": like}
    )
    await connection.execute(sa.text("ALTER TABLE project_focus_history ENABLE TRIGGER USER"))


@pytest_asyncio.fixture
async def armed(engine: AsyncEngine) -> AsyncIterator[None]:
    """Arm the constraint trigger for one test, then put it back to shipped state."""
    async with engine.begin() as connection:
        await connection.execute(sa.text(f"ALTER TABLE project_contexts ENABLE TRIGGER {_TRIGGER}"))
    try:
        yield
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(f"ALTER TABLE project_contexts DISABLE TRIGGER {_TRIGGER}")
            )


@pytest_asyncio.fixture
async def context_row(engine: AsyncEngine) -> AsyncIterator[str]:
    """A throwaway context whose focus is already historicised at revision 3."""
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO project_contexts (project_key, name, description, "
                "current_focus, focus_revision) VALUES (:k, 'probe', 'probe', 'first prose', 3)"
            ),
            {"k": _PROBE},
        )
        await connection.execute(
            sa.text(
                "INSERT INTO project_focus_history "
                "(project_key, focus_revision, focus, actor, source) "
                "VALUES (:k, 3, 'first prose', 'w44', 'focus_tool')"
            ),
            {"k": _PROBE},
        )
    try:
        yield _PROBE
    finally:
        async with engine.begin() as connection:
            await _drop_history_rows(connection, f"{_PROBE}%")
            await connection.execute(
                sa.text("DELETE FROM project_contexts WHERE project_key = :k"), {"k": _PROBE}
            )


# ── What the revision lays down ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_constraint_trigger_ships_disabled_and_scoped_to_the_focus_column(
    engine: AsyncEngine,
) -> None:
    """Three properties in one reading, because all three are load-bearing.

    DISABLED: an armed trigger between the upgrade and the MCP restart would
    abort every `brain_session_end` that applies a focus. DEFERRED: the check
    must land at COMMIT, after the shared write path inserted its row in the same
    transaction. `OF current_focus`: without it, the plan-index repair's two
    UPDATEs — which name `plan_scan_paths` and `updated_at` — would fire it.
    """
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    sa.text(
                        "SELECT tgenabled, tgdeferrable, tginitdeferred, "
                        "pg_get_triggerdef(oid) AS definition "
                        "FROM pg_trigger WHERE tgname = :name"
                    ),
                    {"name": _TRIGGER},
                )
            )
            .mappings()
            .one()
        )

    assert row["tgenabled"] == _DISABLED
    assert row["tgdeferrable"] is True
    assert row["tginitdeferred"] is True
    assert "AFTER UPDATE OF current_focus" in row["definition"]


@pytest.mark.asyncio
async def test_the_history_table_is_append_only(engine: AsyncEngine, context_row: str) -> None:
    """A trail that can be edited is a convention, not an audit."""
    for statement in (
        "UPDATE project_focus_history SET focus = 'rewritten' WHERE project_key = :k",
        "DELETE FROM project_focus_history WHERE project_key = :k",
    ):
        with pytest.raises(sa.exc.DBAPIError, match="append-only"):
            async with engine.begin() as connection:
                await connection.execute(sa.text(statement), {"k": context_row})


# ── The exit criterion: the guard catches a writer that bypasses the path ────


@pytest.mark.asyncio
async def test_a_focus_update_without_its_history_row_aborts_at_commit(
    engine: AsyncEngine, context_row: str, armed: None
) -> None:
    """The honest criterion — and note WHERE it fails.

    The statement itself must succeed: a constraint trigger firing at statement
    time would forbid the shared write path from inserting the history row AFTER
    the focus write, which is the only order that can read the revision the
    trigger then checks.
    """
    with pytest.raises(sa.exc.DBAPIError, match="focus_history_row_missing"):
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE project_contexts SET current_focus = 'overwritten' "
                    "WHERE project_key = :k"
                ),
                {"k": context_row},
            )
            # Still inside the transaction: the statement passed, and that is
            # half the assertion. The refusal below happens on COMMIT.

    async with engine.connect() as connection:
        surviving = (
            await connection.execute(
                sa.text("SELECT current_focus FROM project_contexts WHERE project_key = :k"),
                {"k": context_row},
            )
        ).scalar_one()

    assert surviving == "first prose", "the aborted transaction left the focus untouched"


@pytest.mark.asyncio
async def test_a_focus_update_carrying_its_history_row_commits(
    engine: AsyncEngine, context_row: str, armed: None
) -> None:
    """The nominal witness. Without it, a trigger that refuses everything passes above."""
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "UPDATE project_contexts SET current_focus = 'second prose' WHERE project_key = :k"
            ),
            {"k": context_row},
        )
        new_revision = (
            await connection.execute(
                sa.text("SELECT focus_revision FROM project_contexts WHERE project_key = :k"),
                {"k": context_row},
            )
        ).scalar_one()
        await connection.execute(
            sa.text(
                "INSERT INTO project_focus_history "
                "(project_key, focus_revision, focus, actor, source) "
                "VALUES (:k, :rev, 'second prose', 'w44', 'generic_update')"
            ),
            {"k": context_row, "rev": new_revision},
        )

    assert new_revision == 4, "032's trigger still bumps the revision on a changed focus"


@pytest.mark.asyncio
async def test_an_update_that_never_names_the_focus_does_not_fire_the_trigger(
    engine: AsyncEngine, context_row: str, armed: None
) -> None:
    """The plan-index repair's shape, reproduced: `plan_scan_paths` + `updated_at`.

    This is review R1.4 executed rather than argued. If the `OF current_focus`
    clause were ever dropped, this test — not production — is where it shows.
    """
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "UPDATE project_contexts SET plan_scan_paths = ARRAY['docs/'], "
                "updated_at = now() WHERE project_key = :k"
            ),
            {"k": context_row},
        )


# ── The downgrade: fail-closed outside the seed, named opt-in ────────────────


@pytest.mark.asyncio
async def test_downgrade_is_refused_outside_the_seed_then_accepted_by_its_named_opt_in(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None]
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO project_focus_history "
                "(project_key, focus_revision, focus, actor, source) "
                "VALUES (:k, 1, 'prose', NULL, 'session_end')"
            ),
            {"k": f"{_PROBE}-downgrade"},
        )
    try:
        refused = _run_alembic("downgrade", "049")

        assert refused.returncode != 0
        assert "cannot downgrade 050" in refused.stderr
        assert f"{_PROBE}-downgrade" in refused.stderr, "the refusal NAMES what it would destroy"
        assert _OPT_IN in refused.stderr, "and names the gesture that lifts it"

        migration_downgrade_fence(downgraded_to="049")
        accepted = _run_alembic("-x", f"{_OPT_IN}=yes", "downgrade", "049")
        assert accepted.returncode == 0, accepted.stderr

        async with engine.connect() as connection:
            assert (
                await connection.execute(sa.text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == "049"
            assert (
                await connection.execute(
                    sa.text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name = 'project_focus_history'"
                    )
                )
            ).scalar_one() == 0
    finally:
        assert _run_alembic("upgrade", "head").returncode == 0

    async with engine.connect() as connection:
        assert (
            await connection.execute(sa.text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == _repo_head(), (
            "the restore must reach the HEAD, not the revision this file is about — "
            "a test that passes and leaves the database behind poisons the run"
        )
        assert (
            await connection.execute(
                sa.text("SELECT tgenabled FROM pg_trigger WHERE tgname = :n"), {"n": _TRIGGER}
            )
        ).scalar_one() == _DISABLED, (
            "a re-upgrade re-ships the trigger disabled, it inherits nothing"
        )


@pytest.mark.asyncio
async def test_the_seed_anchors_every_context_including_one_with_a_null_focus(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None]
) -> None:
    """§5.2's one interesting case, proved on a real database rather than a planner.

    The plan proposed a pure Python planner because it believed a non-empty seed
    could not be exercised in-session. It can: the named opt-in that same plan
    asks for is exactly what makes the round trip possible.
    """
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO project_contexts (project_key, name, description, "
                "current_focus, focus_revision) VALUES "
                "('w44-seed-null', 'p', 'p', NULL, 0), "
                "('w44-seed-prose', 'p', 'p', 'prose at seed time', 7)"
            )
        )
    try:
        migration_downgrade_fence(downgraded_to="049")
        assert _run_alembic("-x", f"{_OPT_IN}=yes", "downgrade", "049").returncode == 0
        assert _run_alembic("upgrade", "head").returncode == 0

        async with engine.connect() as connection:
            seeded = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT project_key, focus_revision, focus, source "
                            "FROM project_focus_history WHERE project_key LIKE 'w44-seed-%' "
                            "ORDER BY project_key"
                        )
                    )
                )
                .mappings()
                .all()
            )

        assert [dict(row) for row in seeded] == [
            {
                "project_key": "w44-seed-null",
                "focus_revision": 0,
                "focus": None,
                "source": "migration_seed",
            },
            {
                "project_key": "w44-seed-prose",
                "focus_revision": 7,
                "focus": "prose at seed time",
                "source": "migration_seed",
            },
        ]
    finally:
        async with engine.begin() as connection:
            await _drop_history_rows(connection, "w44-seed-%")
            await connection.execute(
                sa.text("DELETE FROM project_contexts WHERE project_key LIKE 'w44-seed-%'")
            )
