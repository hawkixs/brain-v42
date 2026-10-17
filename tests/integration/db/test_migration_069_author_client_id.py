"""Round-trip 069 on private empty and populated databases without losing authors."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]
TABLES = ("decisions", "learnings", "snippets", "runbooks", "adrs", "focus_slots")
INSERTS = {
    "decisions": "(title, description, reasoning) VALUES ('Choice', 'Context', 'Reason')",
    "learnings": "(topic, insight) VALUES ('Lesson', 'Insight')",
    "snippets": "(title, intention, code, language) VALUES ('Parse', 'Intent', 'pass', 'python')",
    "runbooks": "(title, description, project_key, trigger) VALUES ('Deploy', 'Description', 'author-fixture', 'release')",
    "adrs": "(number, title, context, decision, consequences, project_key) VALUES (1, 'Choice', 'Context', 'Choice', 'Effects', 'author-fixture')",
    "focus_slots": "(project_key, title, body) VALUES ('author-fixture', 'Topic', 'Work')",
}


def alembic(url, *args):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "POSTGRES_URL": url},
    )


@pytest.fixture
def private_head():
    with fresh_head_database(
        os.environ["BRAIN_V42_TEST_DB_URL"], prefix="brain_migration_069"
    ) as url:
        yield url


async def seed(connection):
    await connection.execute(
        sa.text(
            "INSERT INTO project_contexts (project_key, name, description) VALUES ('author-fixture', 'Authors', 'Fixture')"
        )
    )
    for table, insert in INSERTS.items():
        await connection.execute(sa.text(f"INSERT INTO {table} {insert}"))
    await connection.execute(
        sa.text(
            "INSERT INTO focus_slot_history (slot_id, revision, body, source) SELECT id, revision, body, 'slot_open' FROM focus_slots"
        )
    )


@pytest.mark.parametrize("populated", [False, True])
async def test_empty_and_historical_rows_survive_upgrade_and_downgrade(private_head, populated):
    down = alembic(private_head, "downgrade", "068")
    assert down.returncode == 0, down.stderr
    engine = create_async_engine(private_head, poolclass=NullPool)
    try:
        if populated:
            async with engine.begin() as connection:
                await seed(connection)
        up = alembic(private_head, "upgrade", "069")
        assert up.returncode == 0, up.stderr
        async with engine.connect() as connection:
            for table in TABLES:
                row = (
                    await connection.execute(
                        sa.text(
                            "SELECT is_nullable, column_default, udt_name FROM information_schema.columns WHERE table_name=:table AND column_name='author_client_id'"
                        ),
                        {"table": table},
                    )
                ).one()
                assert tuple(row) == ("YES", None, "text")
                assert (
                    await connection.scalar(
                        sa.text(f"SELECT count(*) FROM {table} WHERE author_client_id IS NOT NULL")
                    )
                    == 0
                )
                assert await connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) == int(
                    populated
                )
        down = alembic(private_head, "downgrade", "068")
        assert down.returncode == 0, down.stderr
        up = alembic(private_head, "upgrade", "069")
        assert up.returncode == 0, up.stderr
    finally:
        await engine.dispose()


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("opt_in", [False, True])
async def test_each_table_blocks_data_loss_unless_explicitly_allowed(private_head, table, opt_in):
    engine = create_async_engine(private_head, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await seed(connection)
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(f"UPDATE {table} SET author_client_id='credential-writer'")
            )
        args = ("-x", "allow_author_client_id_downgrade=yes") if opt_in else ()
        result = alembic(private_head, *args, "downgrade", "068")
        if opt_in:
            assert result.returncode == 0, result.stderr
            up = alembic(private_head, "upgrade", "069")
            assert up.returncode == 0, up.stderr
        else:
            assert result.returncode != 0
            assert "allow_author_client_id_downgrade" in result.stderr
        async with engine.connect() as connection:
            assert await connection.scalar(sa.text(f"SELECT author_client_id FROM {table}")) == (
                None if opt_in else "credential-writer"
            )
            assert (
                await connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "069"
            )
            for name in TABLES:
                assert await connection.scalar(sa.text(f"SELECT count(*) FROM {name}")) == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("author", ["", "Uppercase", "bad_id", "a" * 65, "bad\n"])
async def test_author_grammar_matches_credentials(private_head_engine, table, author):
    # Each fixture is rolled back, including slot history, on the module-private database.
    async with private_head_engine.connect() as connection:
        await seed(connection)
        with pytest.raises(sa.exc.IntegrityError, match=f"{table}_author_client_id_format"):
            await connection.execute(
                sa.text(f"UPDATE {table} SET author_client_id=:author"), {"author": author}
            )


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("author", [None, "a", "client-a.9", "a" * 64])
async def test_author_grammar_accepts_history_and_credential_boundaries(
    private_head_engine, table, author
):
    async with private_head_engine.connect() as connection:
        await seed(connection)
        await connection.execute(
            sa.text(f"UPDATE {table} SET author_client_id=:author"), {"author": author}
        )
        assert await connection.scalar(sa.text(f"SELECT author_client_id FROM {table}")) == author
