"""PostgreSQL round trip for migration 057 — `search_log.embedding_model`.

Ticket 4fac067a (judging the codestral trial), operator decision `1669d429`.
What the revision lays down, and that its plain downgrade drops exactly the one
column it added — proved against a real database rather than asserted in a
docstring.

Unlike 049/056, this downgrade carries no named opt-in: `search_log` already
expires every row after 30 days (`MetricsFlusher`), so nothing this column could
attribute survives a migration window regardless. See the revision's own
docstring for that argument in full.

The downgrade test takes `migration_downgrade_fence`: several files migrate this
database, and the fence both serializes them and restores the head when a test
leaves it behind.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[3]


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


def test_057_is_the_head_and_follows_056() -> None:
    """Bumping the head without noticing this file is what the fence prevents."""
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    head = script.get_current_head()

    assert head == "057"
    assert script.get_revision("057").down_revision == "056"


class TestTheColumnLandsOnTheTable:
    @pytest.mark.asyncio
    async def test_the_column_exists_is_nullable_and_has_no_default(
        self, engine: AsyncEngine
    ) -> None:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.text(
                        "SELECT data_type, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_name = 'search_log' AND column_name = 'embedding_model'"
                    )
                )
            ).one_or_none()

        assert row is not None, "search_log.embedding_model is missing"
        data_type, is_nullable, column_default = row
        assert data_type == "text"
        assert is_nullable == "YES"
        assert column_default is None

    @pytest.mark.asyncio
    async def test_a_row_written_without_the_column_stays_null(self, engine: AsyncEngine) -> None:
        """No default: an INSERT that does not name the column stores NULL.

        (NULL also covers searches no embedding model served — FTS fallback and
        an unresolved project_group — which the writer passes explicitly. The
        no-BACKFILL property, a row that existed before 057, is proved in
        TestTheDowngradeDropsExactlyThatColumn, where a row can exist at 056.)
        """
        async with engine.begin() as connection:
            row_id = (
                await connection.execute(
                    sa.text(
                        "INSERT INTO search_log (tool_name, result_count, latency_ms) "
                        "VALUES ('brain_search', 0, 1.0) RETURNING id"
                    )
                )
            ).scalar_one()
        try:
            async with engine.connect() as connection:
                value = (
                    await connection.execute(
                        sa.text("SELECT embedding_model FROM search_log WHERE id = :id"),
                        {"id": row_id},
                    )
                ).scalar_one()
            assert value is None
        finally:
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text("DELETE FROM search_log WHERE id = :id"), {"id": row_id}
                )

    @pytest.mark.asyncio
    async def test_a_row_can_name_its_model(self, engine: AsyncEngine) -> None:
        """The positive case: a search logged under a configured model stores it."""
        async with engine.begin() as connection:
            row_id = (
                await connection.execute(
                    sa.text(
                        "INSERT INTO search_log "
                        "(tool_name, result_count, latency_ms, embedding_model) "
                        "VALUES ('brain_search', 2, 12.5, 'codestral-trial') RETURNING id"
                    )
                )
            ).scalar_one()
        try:
            async with engine.connect() as connection:
                value = (
                    await connection.execute(
                        sa.text("SELECT embedding_model FROM search_log WHERE id = :id"),
                        {"id": row_id},
                    )
                ).scalar_one()
            assert value == "codestral-trial"
        finally:
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text("DELETE FROM search_log WHERE id = :id"), {"id": row_id}
                )


class TestTheDowngradeDropsExactlyThatColumn:
    @pytest.mark.asyncio
    async def test_downgrade_removes_the_column_and_upgrade_restores_it(
        self,
        engine: AsyncEngine,
        migration_downgrade_fence: Callable[..., None],
    ) -> None:
        migration_downgrade_fence(downgraded_to="056")
        downgraded = _run_alembic("downgrade", "056")
        assert downgraded.returncode == 0, downgraded.stderr

        async with engine.connect() as connection:
            result = await connection.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'search_log' AND column_name = 'embedding_model'"
                )
            )
            assert result.scalar_one() == 0

        restored = _run_alembic("upgrade", "head")
        assert restored.returncode == 0, restored.stderr

        async with engine.connect() as connection:
            result = await connection.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'search_log' AND column_name = 'embedding_model'"
                )
            )
            assert result.scalar_one() == 1

    @pytest.mark.asyncio
    async def test_a_row_that_existed_before_057_is_not_backfilled(
        self,
        engine: AsyncEngine,
        migration_downgrade_fence: Callable[..., None],
    ) -> None:
        """PR #199 review (codex, minor 2): a backfill slipped into upgrade() would
        pass every test that inserts AFTER 057. Only a row written at 056 and
        carried through the upgrade can prove that NULL means "written before 057".
        """
        migration_downgrade_fence(downgraded_to="056")
        downgraded = _run_alembic("downgrade", "056")
        assert downgraded.returncode == 0, downgraded.stderr

        async with engine.begin() as connection:
            row_id = (
                await connection.execute(
                    sa.text(
                        "INSERT INTO search_log (tool_name, result_count, latency_ms) "
                        "VALUES ('brain_search', 1, 3.0) RETURNING id"
                    )
                )
            ).scalar_one()
        try:
            restored = _run_alembic("upgrade", "head")
            assert restored.returncode == 0, restored.stderr

            async with engine.connect() as connection:
                value = (
                    await connection.execute(
                        sa.text("SELECT embedding_model FROM search_log WHERE id = :id"),
                        {"id": row_id},
                    )
                ).scalar_one()
            assert value is None
        finally:
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text("DELETE FROM search_log WHERE id = :id"), {"id": row_id}
                )
