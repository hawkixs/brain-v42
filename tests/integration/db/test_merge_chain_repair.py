"""Prove pointer, audit and graph changes share one disposable transaction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from brain_v42.db.tables import (
    adrs,
    brain_entities,
    consolidation_log,
    decisions,
    entity_relations,
    graph_outbox,
    learnings,
    runbooks,
    snippets,
)
from brain_v42.maintenance.merge_chain_repair import (
    MAX_DEPTH,
    MergeChainRepairError,
    inventory,
    repair,
)
from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

TABLES = (learnings, decisions, snippets, runbooks, adrs)


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as connection, connection.begin():
        async with AsyncSession(bind=connection, join_transaction_mode="create_savepoint") as value:
            async with value.begin():
                # The disposable database is shared by the whole session, and the
                # inventory is global by design: merges another test committed
                # would shift every count. Clear them inside this rolled-back
                # transaction so each test starts from an empty inventory.
                for table in TABLES:
                    await value.execute(
                        table.update()
                        .where(table.c.merged_into.is_not(None))
                        .values(merged_into=None)
                    )
                yield value
        # Never leave chain fixtures behind for another test in the disposable DB.
        await connection.rollback()


async def _rows(
    session: AsyncSession,
    pointers: list[int | UUID | None],
    *,
    table: sa.Table = learnings,
    projects: list[str | None] | None = None,
) -> list[UUID]:
    ids = [uuid4() for _ in pointers]
    project = f"test-merge-repair-{uuid4().hex[:12]}"
    for number, entity_id in enumerate(ids):
        fields: dict[str, Any] = {"id": entity_id, "project_key": project}
        if projects is not None:
            fields["project_key"] = projects[number]
        if table is learnings:
            fields.update(topic="Repair fixture", insight="Repair fixture")
        elif table is decisions:
            fields.update(title="Repair fixture", description="Fixture", reasoning="Fixture")
        elif table is snippets:
            fields.update(
                title="Repair fixture", intention="Fixture", code="pass", language="python"
            )
        elif table is runbooks:
            fields.update(title=str(entity_id), description="Fixture", trigger="Fixture")
        else:
            fields.update(
                number=number + 1,
                title="Fixture",
                context="Fixture",
                decision="Fixture",
                consequences="Fixture",
            )
        await session.execute(table.insert().values(**fields))
    for entity_id, pointer in zip(ids, pointers, strict=True):
        target = ids[pointer] if isinstance(pointer, int) else pointer
        if target is not None:
            await session.execute(
                table.update().where(table.c.id == entity_id).values(merged_into=target)
            )
    return ids


async def _pointers(
    session: AsyncSession, table: sa.Table, ids: list[UUID]
) -> dict[UUID, UUID | None]:
    result = await session.execute(
        sa.select(table.c.id, table.c.merged_into).where(table.c.id.in_(ids))
    )
    return dict(result.tuples().all())


async def _counts(session: AsyncSession) -> tuple[int, int, int]:
    counts = []
    for table in (consolidation_log, entity_relations, graph_outbox):
        counts.append(await session.scalar(sa.select(sa.func.count()).select_from(table)))
    return tuple(counts)


@pytest.mark.parametrize("table", TABLES, ids=lambda table: table.name)
async def test_flattens_chain_and_sibling_with_audit_and_outbox(
    session: AsyncSession, table: sa.Table
) -> None:
    a, b, c, root, d = ids = await _rows(session, [1, 2, 3, None, 2], table=table)
    before = await inventory(session)
    report = next(value for value in before.tables if value.table == table.name)
    assert report.merged_count == 4
    assert report.chained_count == 3
    assert dict(report.depth_histogram) == {1: 1, 2: 2, 3: 1}
    assert {row.id for row in report.in_project_chains} == {a, b, d}

    result = await repair(session, expected=3)

    assert result.changed_count == 3
    assert await _pointers(session, table, ids) == {a: root, b: root, c: root, root: None, d: root}
    logs = (
        (
            await session.execute(
                sa.select(consolidation_log).where(consolidation_log.c.source_id.in_([a, b, d]))
            )
        )
        .mappings()
        .all()
    )
    assert {(row["source_id"], row["target_id"], row["action"]) for row in logs} == {
        (value, root, "repointed") for value in (a, b, d)
    }
    assert len(logs) == 3
    assert {row["entity_type"] for row in logs} == {table.name[:-1] if table is not adrs else "adr"}
    source = brain_entities.alias("source")
    target = brain_entities.alias("target")
    relations = (
        await session.execute(
            sa.select(
                source.c.source_uuid,
                target.c.source_uuid,
                entity_relations.c.lifecycle,
                entity_relations.c.deleted_at,
                graph_outbox.c.operation,
            )
            .select_from(
                entity_relations.join(source, source.c.id == entity_relations.c.source_entity_id)
                .join(target, target.c.id == entity_relations.c.target_entity_id)
                .join(
                    graph_outbox,
                    sa.and_(
                        graph_outbox.c.relation_id == entity_relations.c.id,
                        graph_outbox.c.aggregate_revision == entity_relations.c.revision,
                    ),
                )
            )
            .where(
                source.c.source_uuid.in_([a, b, d]),
                entity_relations.c.relation_type == "MERGED_INTO",
            )
        )
    ).all()
    assert {(row[0], row[1], row[2], row[4]) for row in relations} == {
        *((value, root, "active", "upsert_relation") for value in (a, b, d)),
        *((value, old, "deleted", "delete_relation") for value, old in ((a, b), (b, c), (d, c))),
    }
    assert len(relations) == 6
    assert all((row[3] is not None) == (row[2] == "deleted") for row in relations)


async def test_cross_project_chain_is_reported_and_unchanged(session: AsyncSession) -> None:
    ids = await _rows(
        session, [1, 2, None], projects=["test-repair-a", "test-repair-b", "test-repair-b"]
    )
    before = await _pointers(session, learnings, ids)
    result = await repair(session, expected=0)
    report = next(value for value in result.after.tables if value.table == "learnings")
    assert report.chained_count == 1
    assert len(report.cross_project_chains) == 1
    assert report.cross_project_chains[0].id == ids[0]
    assert report.cross_project_chains[0].root == ids[2]
    assert report.transits_other_project == 1
    assert await _pointers(session, learnings, ids) == before
    assert result.changed_count == 0


@pytest.mark.parametrize("table", TABLES, ids=lambda table: table.name)
async def test_chain_returning_to_its_project_is_reported_and_repaired(
    session: AsyncSession, table: sa.Table
) -> None:
    a, b, root = ids = await _rows(
        session,
        [1, 2, None],
        table=table,
        projects=["test-repair-a", "test-repair-b", "test-repair-a"],
    )
    result = await repair(session, expected=1)
    before = next(value for value in result.before.tables if value.table == table.name)
    assert before.transits_other_project == 2
    assert {pointer.id for pointer in before.in_project_chains} == {a}
    assert await _pointers(session, table, ids) == {a: root, b: root, root: None}
    after = next(value for value in result.after.tables if value.table == table.name)
    assert after.transits_other_project == 1
    assert result.changed_count == 1


async def test_lock_timeout_is_restored_after_table_lock(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brain_v42.maintenance import merge_chain_repair as module

    await _rows(session, [1, 2, None])
    previous = await session.scalar(sa.text("SHOW lock_timeout"))
    measured = []

    async def inspect_inventory(value: AsyncSession) -> Any:
        measured.append(await value.scalar(sa.text("SHOW lock_timeout")))
        return await inventory(value)

    monkeypatch.setattr(module, "inventory", inspect_inventory)
    await repair(session, expected=1, lock_timeout_ms=100)
    assert measured == [previous, previous]
    assert await session.scalar(sa.text("SHOW lock_timeout")) == previous


async def test_cli_dry_run_refuses_writes_in_postgresql(
    migration_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from brain_v42.config import Settings
    from brain_v42.db import engine as db_engine
    from brain_v42.scripts import repair_merge_chains as cli

    settings = Settings(postgres_url=migration_database_url, _env_file=None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(db_engine, "get_settings", lambda: settings)
    monkeypatch.setattr(db_engine, "_engine", None)
    monkeypatch.setattr(db_engine, "_session_factory", None)
    monkeypatch.setattr(db_engine, "_profile", "maintenance")
    refused = []

    async def probe_inventory(value: AsyncSession) -> Any:
        assert await value.scalar(sa.text("SHOW transaction_read_only")) == "on"
        with pytest.raises(sa.exc.DBAPIError) as error:
            async with value.begin_nested():
                await value.execute(learnings.update().where(sa.false()).values(topic="Forbidden"))
        refused.append(getattr(error.value.orig, "sqlstate", None))
        return await inventory(value)

    monkeypatch.setattr(cli, "inventory", probe_inventory)
    assert await cli.run_from_args(cli.parse_args([])) == 0
    assert refused == ["25006"]
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


async def test_lock_timeout_refuses_within_bound_and_writes_nothing(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    ids = await _rows(session, [1, 2, None])
    before = await _pointers(session, learnings, ids)
    counts = await _counts(session)
    async with engine.connect() as blocker, blocker.begin():
        await blocker.execute(sa.text("LOCK TABLE learnings IN ROW EXCLUSIVE MODE"))
        started = monotonic()
        with pytest.raises(MergeChainRepairError, match="^lock_timeout$"):
            await asyncio.wait_for(repair(session, expected=1, lock_timeout_ms=100), timeout=2)
        assert monotonic() - started < 2

    assert await _pointers(session, learnings, ids) == before
    assert await _counts(session) == counts
    assert (await repair(session, expected=1)).changed_count == 1


@pytest.mark.parametrize(
    ("pointers", "expected", "reason"),
    [([1, 0], 3, "cycle"), ([], 4, "expected_count_mismatch")],
)
async def test_refusal_writes_nothing(
    session: AsyncSession, pointers: list[int | UUID | None], expected: int, reason: str
) -> None:
    ids = await _rows(session, [1, 2, 3, None, 2])
    ids += await _rows(session, pointers)
    before = await _pointers(session, learnings, ids)
    counts = await _counts(session)
    with pytest.raises(MergeChainRepairError, match=reason):
        await repair(session, expected=expected)
    assert await _pointers(session, learnings, ids) == before
    assert await _counts(session) == counts


async def test_second_repair_changes_nothing(session: AsyncSession) -> None:
    await _rows(session, [1, 2, None])
    assert (await repair(session, expected=1)).changed_count == 1
    assert (await inventory(session)).in_project_chained_count == 0
    counts = await _counts(session)
    assert (await repair(session, expected=0)).changed_count == 0
    assert await _counts(session) == counts


async def test_inventory_refuses_depth_beyond_bound(session: AsyncSession) -> None:
    await _rows(session, [*range(1, MAX_DEPTH + 2), None])
    counts = await _counts(session)
    with pytest.raises(MergeChainRepairError, match="depth_limit"):
        await inventory(session)
    assert await _counts(session) == counts


async def test_depth_at_bound_can_be_repaired(session: AsyncSession) -> None:
    await _rows(session, [*range(1, MAX_DEPTH + 1), None])
    assert (await repair(session, expected=MAX_DEPTH - 1)).changed_count == MAX_DEPTH - 1


async def test_repair_uses_registry_trigger_without_private_ledger_calls(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    ids = await _rows(session, [1, 2, None])
    resolve = AsyncMock(side_effect=RuntimeError("private resolution must not run"))
    stage = AsyncMock(side_effect=RuntimeError("private staging must not run"))
    monkeypatch.setattr(PgGraphLedgerRepo, "_resolve_uuid_endpoints", resolve)
    monkeypatch.setattr(
        PgGraphLedgerRepo,
        "_stage_relation_in_session",
        stage,
    )
    assert (await repair(session, expected=1)).changed_count == 1
    assert await _pointers(session, learnings, ids) == {
        ids[0]: ids[2],
        ids[1]: ids[2],
        ids[2]: None,
    }
    resolve.assert_not_awaited()
    stage.assert_not_awaited()


async def test_final_inventory_refusal_rolls_back_all_changes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from brain_v42.maintenance import merge_chain_repair as module

    ids = await _rows(session, [1, 2, None])
    before = await inventory(session)
    pointers = await _pointers(session, learnings, ids)
    counts = await _counts(session)
    monkeypatch.setattr(module, "inventory", AsyncMock(return_value=before))
    with pytest.raises(MergeChainRepairError, match="chains_remain"):
        await repair(session, expected=1)
    assert await _pointers(session, learnings, ids) == pointers
    assert await _counts(session) == counts
