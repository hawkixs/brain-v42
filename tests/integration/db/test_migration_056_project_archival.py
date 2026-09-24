"""PostgreSQL round trip for the project-archival migration 056.

What the revision lays down, what its downgrade refuses to destroy, what the
named opt-in unlocks — and the operator rule the whole ticket rests on, proved
against a real database rather than asserted in a docstring: archiving and
unarchiving a project leaves the knowledge count of every type strictly
unchanged.

The downgrade tests take `migration_downgrade_fence`: several files migrate this
database, and the fence both serializes them and restores the head when a test
leaves it behind.
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
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[3]

_OPT_IN = "allow_archival_downgrade"
_PROBE = "integ-056-archival-probe"

#: One row per decay-tracked knowledge type, with only the columns PostgreSQL
#: insists on. The point is the COUNT surviving, not the content.
_SEEDS: dict[str, str] = {
    "learnings": "INSERT INTO learnings (topic, insight, project_key) VALUES ('t', 'i', :k)",
    "decisions": (
        "INSERT INTO decisions (title, description, reasoning, project_key) "
        "VALUES ('t', 'd', 'r', :k)"
    ),
    "snippets": (
        "INSERT INTO snippets (title, intention, code, language, project_key) "
        "VALUES ('t', 'i', 'c', 'python', :k)"
    ),
    "runbooks": (
        "INSERT INTO runbooks (title, description, project_key, trigger) VALUES ('t', 'd', :k, 'g')"
    ),
    "adrs": (
        "INSERT INTO adrs (number, title, context, decision, consequences, project_key) "
        "VALUES (90561, 't', 'c', 'd', 'q', :k)"
    ),
    "indexed_plans": (
        "INSERT INTO indexed_plans (file_path, title, plan_type, project_key, content_hash) "
        "VALUES ('/tmp/integ-056/a-plan.md', 't', 'plan', :k, 'h')"
    ),
}


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        # The disposable database this session built, never the one POSTGRES_URL
        # happens to name: a downgrade aimed at production would be the worst
        # possible way to learn that.
        env={**os.environ, "POSTGRES_URL": os.environ["BRAIN_V42_TEST_DB_URL"]},
        timeout=120,
    )


def test_056_follows_055_and_is_an_ancestor_of_the_head() -> None:
    """056 is no longer necessarily the repository head — 057 legitimately
    followed it (ticket 4fac067a) — but this file's fixtures and downgrade
    fence still need 056 to sit, unbroken, between 055 and whatever the head
    now is. Assert exactly that, not the literal head this file's premise
    used to be able to assume.

    Bumping the head without noticing this file is what the fence prevents:
    this test now proves the chain, so a future revision bump (058, ...)
    keeps passing without another edit here.
    """
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))

    assert script.get_revision("056").down_revision == "055"

    head = script.get_current_head()
    ancestors = {revision.revision for revision in script.walk_revisions(base="base", head=head)}
    assert "056" in ancestors, f"056 must be an ancestor of the head ({head})"


@pytest_asyncio.fixture
async def seeded_project(engine: AsyncEngine) -> AsyncIterator[str]:
    """A throwaway project carrying one artifact of every knowledge type."""
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO project_contexts (project_key, name, description) "
                "VALUES (:k, 'probe', 'probe')"
            ),
            {"k": _PROBE},
        )
        for statement in _SEEDS.values():
            await connection.execute(sa.text(statement), {"k": _PROBE})
    try:
        yield _PROBE
    finally:
        async with engine.begin() as connection:
            for table in _SEEDS:
                await connection.execute(
                    sa.text(f"DELETE FROM {table} WHERE project_key = :k"),  # noqa: S608
                    {"k": _PROBE},
                )
            await connection.execute(
                sa.text("DELETE FROM project_contexts WHERE project_key = :k"), {"k": _PROBE}
            )


async def _counts(engine: AsyncEngine, project_key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with engine.connect() as connection:
        for table in _SEEDS:
            result = await connection.execute(
                sa.text(f"SELECT count(*) FROM {table} WHERE project_key = :k"),  # noqa: S608
                {"k": project_key},
            )
            counts[table] = result.scalar_one()
    return counts


class TestTheMarkerLandsOnTheTable:
    @pytest.mark.asyncio
    async def test_both_columns_exist_and_are_nullable(self, engine: AsyncEngine) -> None:
        async with engine.connect() as connection:
            result = await connection.execute(
                sa.text(
                    "SELECT column_name, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'project_contexts' "
                    "AND column_name IN ('archived_at', 'archived_reason')"
                )
            )
            rows = {r.column_name: r for r in result}

        assert set(rows) == {"archived_at", "archived_reason"}
        for row in rows.values():
            assert row.is_nullable == "YES"
            assert row.column_default is None

    @pytest.mark.asyncio
    async def test_every_existing_project_stays_active(self, engine: AsyncEngine) -> None:
        """No backfill: `archived_at IS NULL` IS active, so the migration
        rewrites no row and nothing disappears from a view on upgrade."""
        async with engine.connect() as connection:
            archived = await connection.execute(
                sa.text("SELECT count(*) FROM project_contexts WHERE archived_at IS NOT NULL")
            )

        assert archived.scalar_one() == 0

    @pytest.mark.asyncio
    async def test_a_reason_without_a_date_is_refused(self, engine: AsyncEngine) -> None:
        """A reason describing no archival is a half-written state."""
        with pytest.raises(Exception, match="ck_project_contexts_archived_reason_needs_a_date"):
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "INSERT INTO project_contexts (project_key, name, description, "
                        "archived_reason) VALUES ('integ-056-halfwritten', 'n', 'd', 'why')"
                    )
                )

    @pytest.mark.asyncio
    async def test_the_partial_index_serves_the_default_view_filter(
        self, engine: AsyncEngine
    ) -> None:
        async with engine.connect() as connection:
            result = await connection.execute(
                sa.text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'idx_project_contexts_archived'"
                )
            )
            indexdef = result.scalar_one()

        assert "archived_at IS NOT NULL" in indexdef
        assert "project_key" in indexdef


class TestArchivingDeletesNothing:
    @pytest.mark.asyncio
    async def test_a_round_trip_leaves_every_type_count_untouched(
        self, engine: AsyncEngine, seeded_project: str
    ) -> None:
        """The operator rule, proved type by type against PostgreSQL.

        Not a global total: a global count would balance a type losing a row
        against another gaining one, and read green.
        """
        before = await _counts(engine, seeded_project)
        assert all(count == 1 for count in before.values()), before

        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE project_contexts SET archived_at = NOW(), "
                    "archived_reason = 'probe' WHERE project_key = :k"
                ),
                {"k": seeded_project},
            )
        archived = await _counts(engine, seeded_project)

        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE project_contexts SET archived_at = NULL, "
                    "archived_reason = NULL WHERE project_key = :k"
                ),
                {"k": seeded_project},
            )
        after = await _counts(engine, seeded_project)

        assert archived == before
        assert after == before

    @pytest.mark.asyncio
    async def test_the_project_row_itself_survives(
        self, engine: AsyncEngine, seeded_project: str
    ) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("UPDATE project_contexts SET archived_at = NOW() WHERE project_key = :k"),
                {"k": seeded_project},
            )
        async with engine.connect() as connection:
            result = await connection.execute(
                sa.text("SELECT count(*) FROM project_contexts WHERE project_key = :k"),
                {"k": seeded_project},
            )

        assert result.scalar_one() == 1


class TestTheDowngradeIsFailClosed:
    @pytest.mark.asyncio
    async def test_it_refuses_while_a_project_is_archived(
        self,
        engine: AsyncEngine,
        seeded_project: str,
        migration_downgrade_fence: Callable[..., None],
    ) -> None:
        """Dropping the columns un-archives every project at once, silently."""
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE project_contexts SET archived_at = NOW(), "
                    "archived_reason = 'probe' WHERE project_key = :k"
                ),
                {"k": seeded_project},
            )

        migration_downgrade_fence(downgraded_to="056")
        refused = _run_alembic("downgrade", "055")

        assert refused.returncode != 0
        assert "archived" in (refused.stderr + refused.stdout)

        async with engine.connect() as connection:
            still_there = await connection.execute(
                sa.text("SELECT archived_reason FROM project_contexts WHERE project_key = :k"),
                {"k": seeded_project},
            )
        assert still_there.scalar_one() == "probe"

    @pytest.mark.asyncio
    async def test_the_named_opt_in_unlocks_it_and_the_head_comes_back(
        self,
        engine: AsyncEngine,
        seeded_project: str,
        migration_downgrade_fence: Callable[..., None],
    ) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("UPDATE project_contexts SET archived_at = NOW() WHERE project_key = :k"),
                {"k": seeded_project},
            )

        migration_downgrade_fence(downgraded_to="055")
        accepted = _run_alembic("-x", f"{_OPT_IN}=yes", "downgrade", "055")
        assert accepted.returncode == 0, accepted.stderr

        async with engine.connect() as connection:
            result = await connection.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'project_contexts' AND column_name = 'archived_at'"
                )
            )
            assert result.scalar_one() == 0

        restored = _run_alembic("upgrade", "056")
        assert restored.returncode == 0, restored.stderr
