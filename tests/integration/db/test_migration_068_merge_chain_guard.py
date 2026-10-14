"""Exercise commit-time merge invariants on a private migrated database."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from brain_v42.db.tables import adrs, decisions, features, learnings, runbooks, snippets
from brain_v42.maintenance.merge_chain_repair import repair
from brain_v42.repositories.pg_consolidation_log import PgConsolidationLogRepo
from brain_v42.services.consolidation import ConsolidationJob
from tests.integration.db._merge_chain_helpers import legacy_chain

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]
TABLES = (learnings, decisions, snippets, runbooks, adrs, features)


@pytest.fixture(params=TABLES, ids=lambda table: table.name)
def table(request: pytest.FixtureRequest) -> sa.Table:
    return request.param


def _fields(table: sa.Table, entity_id: UUID, project: str, number: int) -> dict[str, Any]:
    fields: dict[str, Any] = {"id": entity_id, "project_key": project}
    if table is learnings:
        fields.update(topic="Guard fixture", insight="Guard fixture")
    elif table is decisions:
        fields.update(title="Guard fixture", description="Fixture", reasoning="Fixture")
    elif table is snippets:
        fields.update(title="Guard fixture", intention="Fixture", code="pass", language="python")
    elif table is runbooks:
        fields.update(title=str(entity_id), description="Fixture", trigger="Fixture")
    elif table is adrs:
        fields.update(
            number=number,
            title="Guard fixture",
            context="Fixture",
            decision="Fixture",
            consequences="Fixture",
        )
    else:
        fields.update(name="Guard fixture", description="Fixture")
    return fields


@pytest_asyncio.fixture
async def row_ids(
    private_head_engine: AsyncEngine, table: sa.Table
) -> AsyncIterator[tuple[UUID, UUID, UUID]]:
    """Clean up only owned rows so repair inventory cannot inherit another case."""
    ids = (uuid4(), uuid4(), uuid4())
    project = f"test-merge-guard-{uuid4().hex[:12]}"
    async with private_head_engine.begin() as connection:
        for number, entity_id in enumerate(ids, start=1):
            await connection.execute(
                table.insert().values(**_fields(table, entity_id, project, number))
            )
    try:
        yield ids
    finally:
        async with private_head_engine.begin() as connection:
            await connection.execute(
                table.update().where(table.c.id.in_(ids)).values(merged_into=None)
            )
            await connection.execute(table.delete().where(table.c.id.in_(ids)))


async def _point(
    connection: AsyncConnection, table: sa.Table, source: UUID, target: UUID | None
) -> None:
    await connection.execute(
        sa.text(f"UPDATE public.{table.name} SET merged_into = :target WHERE id = :source"),
        {"source": source, "target": target},
    )


async def _pointers(
    engine: AsyncEngine, table: sa.Table, ids: tuple[UUID, UUID, UUID]
) -> dict[UUID, UUID | None]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.select(table.c.id, table.c.merged_into).where(table.c.id.in_(ids))
        )
        return dict(rows.tuples().all())


async def test_update_to_merged_target_is_refused_at_commit(
    private_head_engine: AsyncEngine, table: sa.Table, row_ids: tuple[UUID, UUID, UUID]
) -> None:
    child, source, survivor = row_ids
    async with private_head_engine.begin() as connection:
        await _point(connection, table, source, survivor)
    async with private_head_engine.connect() as connection:
        transaction = await connection.begin()
        await _point(connection, table, child, source)
        with pytest.raises(sa.exc.DBAPIError, match="merge_chain_refused") as error:
            await transaction.commit()
        assert f"merge_chain_refused: {table.name} {child} {source}" in str(error.value.orig)
        assert error.value.orig.sqlstate == "23514"
    assert await _pointers(private_head_engine, table, row_ids) == {
        child: None,
        source: survivor,
        survivor: None,
    }


async def test_merging_a_row_with_a_child_is_refused_at_commit(
    private_head_engine: AsyncEngine, table: sa.Table, row_ids: tuple[UUID, UUID, UUID]
) -> None:
    child, source, survivor = row_ids
    async with private_head_engine.begin() as connection:
        await _point(connection, table, child, source)
    async with private_head_engine.connect() as connection:
        transaction = await connection.begin()
        await _point(connection, table, source, survivor)
        with pytest.raises(sa.exc.DBAPIError, match="merge_chain_refused") as error:
            await transaction.commit()
        assert f"merge_chain_refused: {table.name} {child} {source}" in str(error.value.orig)
    assert await _pointers(private_head_engine, table, row_ids) == {
        child: source,
        source: None,
        survivor: None,
    }


async def test_source_first_merge_and_child_repoint_commit_together(
    private_head_engine: AsyncEngine, table: sa.Table, row_ids: tuple[UUID, UUID, UUID]
) -> None:
    child, source, survivor = row_ids
    async with private_head_engine.begin() as connection:
        await _point(connection, table, child, source)
    async with private_head_engine.begin() as connection:
        await _point(connection, table, source, survivor)
        await _point(connection, table, child, survivor)
    assert await _pointers(private_head_engine, table, row_ids) == {
        child: survivor,
        source: survivor,
        survivor: None,
    }


async def test_insert_pointing_to_merged_target_is_refused_at_commit(
    private_head_engine: AsyncEngine, table: sa.Table, row_ids: tuple[UUID, UUID, UUID]
) -> None:
    child, source, survivor = row_ids
    async with private_head_engine.begin() as connection:
        await _point(connection, table, source, survivor)
    async with private_head_engine.connect() as connection:
        transaction = await connection.begin()
        # Reuse an owned id so even an unexpectedly accepted INSERT is cleaned up.
        project = await connection.scalar(sa.select(table.c.project_key).where(table.c.id == child))
        assert isinstance(project, str)
        fields = _fields(table, child, project, 1)
        await connection.execute(table.delete().where(table.c.id == child))
        await connection.execute(table.insert().values(**{**fields, "merged_into": source}))
        with pytest.raises(sa.exc.DBAPIError, match="merge_chain_refused"):
            await transaction.commit()
    assert await _pointers(private_head_engine, table, row_ids) == {
        child: None,
        source: survivor,
        survivor: None,
    }


async def test_unrelated_update_does_not_validate_a_legacy_chain(
    private_head_engine: AsyncEngine, table: sa.Table, row_ids: tuple[UUID, UUID, UUID]
) -> None:
    await legacy_chain(private_head_engine, table, row_ids)
    before = await _pointers(private_head_engine, table, row_ids)
    label = "topic" if table is learnings else "name" if table is features else "title"
    async with private_head_engine.begin() as connection:
        await connection.execute(
            sa.text(f"UPDATE public.{table.name} SET {label} = 'Updated fixture' WHERE id = :id"),
            {"id": row_ids[0]},
        )
    assert await _pointers(private_head_engine, table, row_ids) == before


async def test_deferred_check_reads_the_last_pointer_value(
    private_head_engine: AsyncEngine, table: sa.Table, row_ids: tuple[UUID, UUID, UUID]
) -> None:
    child, source, survivor = row_ids
    async with private_head_engine.begin() as connection:
        await _point(connection, table, source, survivor)
    async with private_head_engine.begin() as connection:
        await _point(connection, table, child, source)
        await _point(connection, table, child, survivor)
    assert await _pointers(private_head_engine, table, row_ids) == {
        child: survivor,
        source: survivor,
        survivor: None,
    }


async def test_clearing_a_pointer_before_commit_needs_no_check(
    private_head_engine: AsyncEngine, table: sa.Table, row_ids: tuple[UUID, UUID, UUID]
) -> None:
    child, source, survivor = row_ids
    async with private_head_engine.begin() as connection:
        await _point(connection, table, source, survivor)
    async with private_head_engine.begin() as connection:
        await _point(connection, table, child, source)
        await _point(connection, table, child, None)
    assert await _pointers(private_head_engine, table, row_ids) == {
        child: None,
        source: survivor,
        survivor: None,
    }


@pytest.mark.parametrize("table", TABLES[:-1], ids=lambda table: table.name)
async def test_consolidation_merge_with_one_child_commits(
    private_head_engine: AsyncEngine, table: sa.Table, row_ids: tuple[UUID, UUID, UUID]
) -> None:
    child, source, survivor = row_ids
    async with private_head_engine.begin() as connection:
        await _point(connection, table, child, source)
    factory = async_sessionmaker(private_head_engine, expire_on_commit=False)
    job = ConsolidationJob(factory, PgConsolidationLogRepo(factory))
    await job.merge(table.name[:-1], source, survivor)
    assert await _pointers(private_head_engine, table, row_ids) == {
        child: survivor,
        source: survivor,
        survivor: None,
    }


async def test_chain_repair_commits_under_the_guard(
    private_head_engine: AsyncEngine, table: sa.Table, row_ids: tuple[UUID, UUID, UUID]
) -> None:
    await legacy_chain(private_head_engine, table, row_ids)
    factory = async_sessionmaker(private_head_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        result = await repair(session, expected={table.name: 1})
        assert result.changed_count == 1
    child, source, survivor = row_ids
    assert await _pointers(private_head_engine, table, row_ids) == {
        child: survivor,
        source: survivor,
        survivor: None,
    }


def _alembic_result(url: str, action: str, revision: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), action, revision],
        cwd=ROOT,
        env={**os.environ, "POSTGRES_URL": url},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _alembic(url: str, action: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), action, revision],
        cwd=ROOT,
        env={**os.environ, "POSTGRES_URL": url},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, "disposable database migration failed"


async def _wait_for_blocker(
    observer: AsyncConnection, task: asyncio.Task[None], waiter: int, holder: int
) -> None:
    """Observe a real row-lock wait rather than assume concurrency from a sleep."""
    async with asyncio.timeout(5):
        while True:
            blockers = await observer.scalar(
                sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter}
            )
            if holder in blockers:
                return
            assert not task.done(), "guard allowed the conflicting writer to finish without waiting"
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("first", ["source", "child"])
async def test_concurrent_chain_writers_serialize_in_both_orders(
    private_head_engine: AsyncEngine,
    table: sa.Table,
    row_ids: tuple[UUID, UUID, UUID],
    first: str,
) -> None:
    """The target share lock must cover both a pending update and a later update."""
    child, source, survivor = row_ids
    async with (
        private_head_engine.connect() as holder,
        private_head_engine.connect() as waiter,
        private_head_engine.connect() as observer,
    ):
        holder_pid = await holder.scalar(sa.text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(sa.text("SELECT pg_backend_pid()"))
        assert isinstance(holder_pid, int) and isinstance(waiter_pid, int)
        await holder.execute(sa.text("SET LOCAL statement_timeout = '10s'"))
        await waiter.execute(sa.text("SET LOCAL statement_timeout = '10s'"))
        if first == "source":
            await _point(holder, table, source, survivor)
            await _point(waiter, table, child, source)
        else:
            await _point(holder, table, child, source)
            # Run the deferred check while keeping its target lock until commit.
            await holder.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))

        async def finish_waiter() -> None:
            if first == "child":
                await _point(waiter, table, source, survivor)
            await waiter.commit()

        task = asyncio.create_task(finish_waiter())
        try:
            await _wait_for_blocker(observer, task, waiter_pid, holder_pid)
            await holder.commit()
            with pytest.raises(sa.exc.DBAPIError, match="merge_chain_refused"):
                await asyncio.wait_for(task, timeout=10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await holder.rollback()
            await waiter.rollback()
    assert await _pointers(private_head_engine, table, row_ids) == {
        child: source if first == "child" else None,
        source: survivor if first == "source" else None,
        survivor: None,
    }


async def test_concurrent_cycle_fails_without_hanging(
    private_head_engine: AsyncEngine, table: sa.Table, row_ids: tuple[UUID, UUID, UUID]
) -> None:
    child, source, _survivor = row_ids
    async with (
        private_head_engine.connect() as left,
        private_head_engine.connect() as right,
    ):
        await left.execute(sa.text("SET LOCAL statement_timeout = '8s'"))
        await right.execute(sa.text("SET LOCAL statement_timeout = '8s'"))
        await _point(left, table, child, source)
        await _point(right, table, source, child)
        results = await asyncio.wait_for(
            asyncio.gather(left.commit(), right.commit(), return_exceptions=True), timeout=10
        )
        errors = [result for result in results if isinstance(result, sa.exc.DBAPIError)]
        assert errors
        assert all(error.orig.sqlstate in {"40P01", "23514"} for error in errors)
    pointers = await _pointers(private_head_engine, table, row_ids)
    assert not (pointers[child] == source and pointers[source] == child)


async def _guard_indexes(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND indexname = ANY(:names) ORDER BY indexname"
            ),
            {"names": [f"idx_{table.name}_merged_into" for table in TABLES]},
        )
        return list(rows.scalars().all())


async def test_merge_pointer_index_is_partial(
    private_head_engine: AsyncEngine, table: sa.Table
) -> None:
    async with private_head_engine.connect() as connection:
        definition = await connection.scalar(
            sa.text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"
            ),
            {"name": f"idx_{table.name}_merged_into"},
        )
    assert definition is not None
    assert "(merged_into) WHERE (merged_into IS NOT NULL)" in definition


async def _guard_triggers(engine: AsyncEngine) -> list[tuple[str, bool, bool]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT c.relname, t.tgdeferrable, t.tginitdeferred "
                "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE t.tgfoid = to_regprocedure('public.refuse_merge_chain()') "
                "ORDER BY c.relname"
            )
        )
        return list(rows.tuples().all())


async def test_upgrade_refuses_while_a_legacy_chain_remains(
    private_head_engine: AsyncEngine,
    _private_module_head: str,
    table: sa.Table,
    row_ids: tuple[UUID, UUID, UUID],
) -> None:
    """The repair must run before 068: the upgrade itself enforces that order."""
    if table not in (learnings, features):
        pytest.skip("two tables are enough to prove the upgrade-time count")
    child, middle, survivor = row_ids
    await asyncio.to_thread(_alembic, _private_module_head, "downgrade", "067")
    try:
        async with private_head_engine.begin() as connection:
            await _point(connection, table, middle, survivor)
            await _point(connection, table, child, middle)
        refused = await asyncio.to_thread(_alembic_result, _private_module_head, "upgrade", "head")
        assert refused.returncode != 0
        assert "merge_chains_present" in refused.stderr
        assert await _guard_triggers(private_head_engine) == []
    finally:
        async with private_head_engine.begin() as connection:
            await _point(connection, table, child, None)
            await _point(connection, table, middle, None)
        await asyncio.to_thread(_alembic, _private_module_head, "upgrade", "head")
    assert len(await _guard_triggers(private_head_engine)) == len(TABLES)


async def test_upgrade_downgrade_upgrade_restores_six_deferred_triggers(
    private_head_engine: AsyncEngine, _private_module_head: str
) -> None:
    expected = sorted((table.name, True, True) for table in TABLES)
    expected_indexes = sorted(f"idx_{table.name}_merged_into" for table in TABLES)
    assert await _guard_triggers(private_head_engine) == expected
    assert await _guard_indexes(private_head_engine) == expected_indexes
    try:
        await asyncio.to_thread(_alembic, _private_module_head, "downgrade", "067")
        assert await _guard_triggers(private_head_engine) == []
        assert await _guard_indexes(private_head_engine) == []
        async with private_head_engine.connect() as connection:
            assert (
                await connection.scalar(
                    sa.text("SELECT to_regprocedure('public.refuse_merge_chain()')")
                )
                is None
            )
    finally:
        await asyncio.to_thread(_alembic, _private_module_head, "upgrade", "head")
    assert await _guard_triggers(private_head_engine) == expected
    assert await _guard_indexes(private_head_engine) == expected_indexes
