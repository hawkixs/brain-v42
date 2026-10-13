from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from brain_v42.db.focus_slots import cas_slot_body, record_slot_history
from brain_v42.db.tables import (
    brain_entities,
    brain_session_artifacts,
    brain_session_checkpoints,
    brain_session_connections,
    brain_sessions,
    decisions,
    focus_slots,
    graph_outbox,
    project_contexts,
)
from brain_v42.repositories.pg_trace_retention import PgTraceRetention

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
OLD = timedelta(days=60)


@pytest.fixture
async def factory(private_head_engine):
    factory = async_sessionmaker(private_head_engine, expire_on_commit=False)
    # Earlier tests' rows stay private and outside this test's retention window. Only
    # rows that already carry the timestamp move: an open session must keep a NULL
    # ended_at (brain_sessions_terminal_state_valid) and a pending outbox row must stay
    # undelivered.
    async with factory.begin() as session:
        await session.execute(
            graph_outbox.update()
            .where(graph_outbox.c.delivered_at.is_not(None))
            .values(delivered_at=sa.func.now())
        )
        await session.execute(
            brain_sessions.update()
            .where(brain_sessions.c.nature == "agent", brain_sessions.c.ended_at.is_not(None))
            .values(ended_at=sa.func.now())
        )
    return factory


async def _project(factory):
    key = f"trace-retention-{uuid4().hex[:12]}"
    async with factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=key,
                name="Retention",
                description="Retention fixture",
                current_focus="focus",
            )
        )
    return key


async def _session(
    factory, project, status="closed_inactive", *, nature="agent", old=True, client=None
):
    at = datetime.now(UTC) - (OLD if old else timedelta(days=1))
    values = {
        "project_key": project,
        "client_key": client or uuid4().hex,
        "status": status,
        "nature": nature,
        "started_focus": "focus",
        "started_focus_revision": 0,
        "started_at": at,
        "last_heartbeat_at": at,
        "ended_at": at if status != "open" else None,
    }
    if status == "abandoned":
        values["abandonment_reason"] = "retention fixture"
    elif status == "ended":
        values.update(
            summary="finished", next_focus="next", focus_outcome="applied", focus_at_end="next"
        )
    async with factory.begin() as session:
        return await session.scalar(
            brain_sessions.insert().values(**values).returning(brain_sessions.c.id)
        )


async def _purge(factory, **kwargs):
    defaults = {"older_than": timedelta(days=30), "batch_size": 5000, "max_batches": 100}
    return await PgTraceRetention(factory).purge_agent_traces(**(defaults | kwargs))


async def test_old_trace_deleted_and_connections_cascade(factory):
    project = await _project(factory)
    trace = await _session(factory, project)
    async with factory.begin() as session:
        await session.execute(
            brain_session_connections.insert().values(
                session_id=trace, connection_id="trace-connection"
            )
        )
    report = await _purge(factory, dry_run=False)
    async with factory() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(brain_sessions)
                .where(brain_sessions.c.id == trace)
            )
            == 0
        )
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(brain_session_connections)
                .where(brain_session_connections.c.session_id == trace)
            )
            == 0
        )
    assert report.deleted == 1


async def test_ledger_checkpoint_and_relay_child_protect_trace(factory):
    project = await _project(factory)
    ledger = await _session(factory, project)
    checkpoint = await _session(factory, project)
    parent = await _session(factory, project)
    history = await _session(factory, project)
    async with factory.begin() as session:
        knowledge = await session.scalar(
            decisions.insert()
            .values(project_key=project, title="captured", description="body", reasoning="fixture")
            .returning(decisions.c.id)
        )
        await session.execute(
            brain_session_artifacts.insert().values(
                session_id=ledger, knowledge_id=knowledge, knowledge_type="decision"
            )
        )
        await session.execute(
            brain_session_checkpoints.insert().values(
                session_id=checkpoint, seq=1, progress="progress", next_step="next"
            )
        )
        slot = await session.scalar(
            focus_slots.insert()
            .values(project_key=project, title="slot", body="body")
            .returning(focus_slots.c.id)
        )
        await record_slot_history(
            session, slot_id=slot, revision=0, body="body", source="slot_open"
        )
        moved = await cas_slot_body(session, slot_id=slot, expected=0, body="historical")
        assert moved is not None
        await record_slot_history(
            session,
            slot_id=slot,
            revision=1,
            body="historical",
            source="session_end",
            session_id=history,
        )
        child = await session.scalar(
            brain_sessions.insert()
            .values(
                project_key=project,
                client_key=uuid4().hex,
                status="abandoned",
                nature="operator",
                started_focus="focus",
                started_focus_revision=0,
                ended_at=datetime.now(UTC),
                abandonment_reason="fixture",
                slot_id=slot,
                relayed_from_session_id=parent,
            )
            .returning(brain_sessions.c.id)
        )
    assert child
    report = await _purge(factory, dry_run=False)
    assert report.deleted == 0
    async with factory() as session:
        present = set(
            await session.scalars(
                sa.select(brain_sessions.c.id).where(
                    brain_sessions.c.id.in_([ledger, checkpoint, history, parent])
                )
            )
        )
    assert present == {ledger, checkpoint, history, parent}


async def test_open_ended_operator_and_recent_sessions_are_kept(factory):
    project = await _project(factory)
    traces = [
        await _session(factory, project, "open", old=True),
        await _session(factory, project, "ended", old=True),
        await _session(factory, project, "abandoned", nature="operator", old=True),
        await _session(factory, project, old=False),
    ]
    report = await _purge(factory, dry_run=False)
    assert report.deleted == 0
    async with factory() as session:
        assert set(
            await session.scalars(
                sa.select(brain_sessions.c.id).where(brain_sessions.c.id.in_(traces))
            )
        ) == set(traces)


async def test_dry_run_matches_execute_and_batches_are_bounded(factory):
    project = await _project(factory)
    traces = [await _session(factory, project) for _ in range(3)]
    preview = await _purge(factory, dry_run=True)
    assert preview.candidates == 3 and preview.deleted == 0
    first = await _purge(factory, dry_run=False, batch_size=1, max_batches=1)
    assert first.deleted == 1
    async with factory() as session:
        remaining_after_first = set(
            await session.scalars(
                sa.select(brain_sessions.c.id).where(brain_sessions.c.id.in_(traces))
            )
        )
    assert len(remaining_after_first) == 2
    remainder = await _purge(factory, dry_run=False)
    assert remainder.deleted == 2
    async with factory() as session:
        assert not set(
            await session.scalars(
                sa.select(brain_sessions.c.id).where(brain_sessions.c.id.in_(traces))
            )
        )


async def test_old_abandoned_agent_trace_is_deleted(factory):
    project = await _project(factory)
    trace = await _session(factory, project, "abandoned")
    report = await _purge(factory, dry_run=False)
    assert report.deleted == 1
    async with factory() as session:
        assert (
            await session.scalar(sa.select(brain_sessions.c.id).where(brain_sessions.c.id == trace))
            is None
        )


async def test_outbox_deletes_only_old_delivered_rows(factory):
    await _project(factory)
    async with factory.begin() as session:
        entity = await session.scalar(
            brain_entities.insert()
            .values(entity_type="retention_test", entity_key=uuid4().hex, scope_kind="global")
            .returning(brain_entities.c.id)
        )
        old = datetime.now(UTC) - OLD
        recent = datetime.now(UTC)
        ids = []
        for delivered_at, operation, attempts in [
            (old, "upsert_entity", 1),
            (recent, "upsert_entity", 1),
            (None, "upsert_entity", 0),
            (None, "upsert_entity", 20),
        ]:
            ids.append(
                await session.scalar(
                    graph_outbox.insert()
                    .values(
                        entity_id=entity,
                        aggregate_revision=len(ids) + 1,
                        operation=operation,
                        attempt_count=attempts,
                        delivered_at=delivered_at,
                    )
                    .returning(graph_outbox.c.id)
                )
            )
    repo = PgTraceRetention(factory)
    preview = await repo.purge_delivered_outbox(
        older_than=timedelta(days=30), batch_size=10, max_batches=10, dry_run=True
    )
    assert preview.candidates == 1 and preview.deleted == 0
    report = await repo.purge_delivered_outbox(
        older_than=timedelta(days=30), batch_size=10, max_batches=10, dry_run=False
    )
    assert report.deleted == 1
    async with factory() as session:
        remaining = set(
            await session.scalars(sa.select(graph_outbox.c.id).where(graph_outbox.c.id.in_(ids)))
        )
    assert remaining == set(ids[1:])
