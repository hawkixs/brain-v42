"""Deleting knowledge that a session captured is refused, in every delete branch.

The capture ledger (`brain_session_artifacts`) has no foreign key to the knowledge
tables, so nothing but the repositories stops a delete from leaving a dangling id
behind a closed session. These tests run against PostgreSQL because the guarantee
is a row lock: capture takes `FOR KEY SHARE`, the guarded delete takes `FOR UPDATE`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    adrs,
    brain_session_artifacts,
    brain_sessions,
    decisions,
    indexed_plans,
    learnings,
    project_contexts,
    runbooks,
    snippets,
)
from brain_v42.models.brain_session import KnowledgeCapturedError
from brain_v42.repositories.pg_adr import PgADRRepo
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.repositories.pg_runbook import PgRunbookRepo
from brain_v42.repositories.pg_snippet import PgSnippetRepo

pytestmark = pytest.mark.integration


def _values(kind: str, project_key: str) -> dict[str, Any]:
    if kind == "decision":
        return {"title": "t", "description": "d", "reasoning": "r"}
    if kind == "learning":
        return {"topic": "t", "insight": "i"}
    if kind == "snippet":
        return {"title": "t", "intention": "i", "code": "c", "language": "python"}
    if kind == "runbook":
        return {"title": "t", "description": "d", "trigger": "x"}
    if kind == "adr":
        return {"number": 1, "title": "t", "context": "c", "decision": "d", "consequences": "q"}
    return {
        "file_path": f"/plans/{uuid4().hex}.md",
        "title": "t",
        "plan_type": "plan",
        "content_hash": "h",
    }


_TABLES = {
    "decision": decisions,
    "learning": learnings,
    "snippet": snippets,
    "runbook": runbooks,
    "adr": adrs,
    "indexed_plan": indexed_plans,
}


async def _delete_with(
    kind: str,
    factory: async_sessionmaker[AsyncSession],
    row_id: UUID,
    project_key: str | None,
) -> bool:
    if kind == "indexed_plan":
        async with factory() as session:
            if project_key is None:
                return await PgIndexedPlanRepo(session).delete(row_id)
            return await PgIndexedPlanRepo(session).delete(row_id, project_key=project_key)
    repos = {
        "decision": PgDecisionRepo,
        "learning": PgLearningRepo,
        "snippet": PgSnippetRepo,
        "runbook": PgRunbookRepo,
        "adr": PgADRRepo,
    }
    repo: Any = repos[kind](factory)
    if project_key is None:
        return await repo.delete(row_id)
    return await repo.delete(row_id, project_key=project_key)


@pytest_asyncio.fixture
async def project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    project_key = f"integ-capdel-{uuid4().hex[:10]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Captured delete guard",
                description="Isolated fixture",
                current_focus="initial focus",
            )
        )
    try:
        yield project_key
    finally:
        async with session_factory.begin() as session:
            owned = sa.select(brain_sessions.c.id).where(
                brain_sessions.c.project_key == project_key
            )
            await session.execute(
                brain_session_artifacts.delete().where(
                    brain_session_artifacts.c.session_id.in_(owned)
                )
            )
            await session.execute(
                brain_sessions.delete().where(brain_sessions.c.project_key == project_key)
            )
            for table in _TABLES.values():
                await session.execute(table.delete().where(table.c.project_key == project_key))
            await session.execute(
                project_contexts.delete().where(project_contexts.c.project_key == project_key)
            )


async def _insert_row(
    factory: async_sessionmaker[AsyncSession], kind: str, project_key: str
) -> UUID:
    row_id = uuid4()
    async with factory.begin() as session:
        await session.execute(
            _TABLES[kind]
            .insert()
            .values(id=row_id, project_key=project_key, **_values(kind, project_key))
        )
    return row_id


async def _open_session(factory: async_sessionmaker[AsyncSession], project_key: str) -> UUID:
    started = await PgBrainSessionRepo(factory).start(project_key, f"capdel-{uuid4().hex[:8]}")
    return UUID(str(started.session.id))


async def _capture(
    factory: async_sessionmaker[AsyncSession],
    session_id: UUID,
    row_id: UUID,
    knowledge_type: str,
) -> None:
    async with factory.begin() as session:
        await session.execute(
            brain_session_artifacts.insert().values(
                knowledge_id=row_id, session_id=session_id, knowledge_type=knowledge_type
            )
        )


async def _exists(factory: async_sessionmaker[AsyncSession], kind: str, row_id: UUID) -> bool:
    table = _TABLES[kind]
    async with factory() as session:
        found = await session.execute(sa.select(table.c.id).where(table.c.id == row_id))
        return found.scalar_one_or_none() is not None


KINDS = list(_TABLES)
SCOPES = pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "project_key"])


@pytest.mark.parametrize("kind", KINDS)
@SCOPES
async def test_captured_knowledge_is_refused_and_kept(
    session_factory: async_sessionmaker[AsyncSession], project: str, kind: str, scoped: bool
) -> None:
    row_id = await _insert_row(session_factory, kind, project)
    session_id = await _open_session(session_factory, project)
    await _capture(session_factory, session_id, row_id, kind)

    with pytest.raises(KnowledgeCapturedError) as refused:
        await _delete_with(kind, session_factory, row_id, project if scoped else None)

    assert refused.value.knowledge_id == row_id
    assert refused.value.session_id == session_id
    assert await _exists(session_factory, kind, row_id)


@pytest.mark.parametrize("kind", KINDS)
@SCOPES
async def test_uncaptured_knowledge_is_still_deleted(
    session_factory: async_sessionmaker[AsyncSession], project: str, kind: str, scoped: bool
) -> None:
    row_id = await _insert_row(session_factory, kind, project)

    deleted = await _delete_with(kind, session_factory, row_id, project if scoped else None)

    assert deleted is True
    assert not await _exists(session_factory, kind, row_id)


@pytest.mark.parametrize("kind", KINDS)
@SCOPES
async def test_absent_knowledge_still_reports_not_found(
    session_factory: async_sessionmaker[AsyncSession], project: str, kind: str, scoped: bool
) -> None:
    assert not await _delete_with(kind, session_factory, uuid4(), project if scoped else None)


@pytest.mark.parametrize("kind", KINDS)
async def test_legacy_typed_artifact_refuses_whatever_the_table(
    session_factory: async_sessionmaker[AsyncSession], project: str, kind: str
) -> None:
    row_id = await _insert_row(session_factory, kind, project)
    session_id = await _open_session(session_factory, project)
    await _capture(session_factory, session_id, row_id, "legacy")

    with pytest.raises(KnowledgeCapturedError):
        await _delete_with(kind, session_factory, row_id, None)

    assert await _exists(session_factory, kind, row_id)


@SCOPES
async def test_refused_decision_delete_leaves_superseded_by_untouched(
    session_factory: async_sessionmaker[AsyncSession], project: str, scoped: bool
) -> None:
    captured_id = await _insert_row(session_factory, "decision", project)
    superseded_id = uuid4()
    async with session_factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=superseded_id,
                project_key=project,
                status="superseded",
                superseded_by=captured_id,
                **_values("decision", project),
            )
        )
    await _capture(
        session_factory, await _open_session(session_factory, project), captured_id, "decision"
    )

    with pytest.raises(KnowledgeCapturedError):
        await _delete_with("decision", session_factory, captured_id, project if scoped else None)

    async with session_factory() as session:
        row = (
            (await session.execute(sa.select(decisions).where(decisions.c.id == superseded_id)))
            .mappings()
            .one()
        )
    assert row["superseded_by"] == captured_id
    assert row["status"] == "superseded"


async def test_delete_waits_for_an_in_flight_capture_then_refuses(
    session_factory: async_sessionmaker[AsyncSession], project: str
) -> None:
    """Capture holds `FOR KEY SHARE` on the row: the delete must queue behind it."""
    row_id = await _insert_row(session_factory, "decision", project)
    session_id = await _open_session(session_factory, project)

    async with session_factory() as capturing:
        async with capturing.begin():
            await capturing.execute(
                sa.select(decisions.c.id)
                .where(decisions.c.id == row_id)
                .with_for_update(read=True, key_share=True)
            )
            await capturing.execute(
                brain_session_artifacts.insert().values(
                    knowledge_id=row_id, session_id=session_id, knowledge_type="decision"
                )
            )
            deleting = asyncio.create_task(
                _delete_with("decision", session_factory, row_id, project)
            )
            await asyncio.sleep(0.5)
            assert not deleting.done(), "the delete must wait for the capture to commit"

        with pytest.raises(KnowledgeCapturedError):
            await asyncio.wait_for(deleting, timeout=10)

    assert await _exists(session_factory, "decision", row_id)


# ── Lock order: a scoped delete and a multi-decision capture must not deadlock ──
#
# Ticket d85b4f66. A.superseded_by = B. A scoped delete of B needs B and A; a
# capture of {A, B} needs both as well. Locking them in opposite orders made
# PostgreSQL abort one of the two with a raw deadlock error. Both paths now lock
# the full set in ascending id order, in one statement.


async def _lock_waiters(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int(
            (
                await session.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        )


async def _until(condition: Any, what: str) -> None:
    async with asyncio.timeout(10):
        while not await condition():
            await asyncio.sleep(0.02)


async def _insert_referenced_pair(
    factory: async_sessionmaker[AsyncSession], project_key: str, referrer_id: UUID, target_id: UUID
) -> None:
    """A (the referrer) points at B (the target); B is touched last so a heap scan
    meets A first, the order in which the unfixed capture locked them."""
    async with factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=target_id, project_key=project_key, **_values("decision", project_key)
            )
        )
        await session.execute(
            decisions.insert().values(
                id=referrer_id,
                project_key=project_key,
                status="superseded",
                superseded_by=target_id,
                **_values("decision", project_key),
            )
        )
        await session.execute(
            decisions.update().where(decisions.c.id == target_id).values(title="touched")
        )


@pytest_asyncio.fixture
async def heap_order_scans(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Make the unordered capture lock in heap order, whatever the planner thinks.

    Only the unfixed code depends on it (an index scan would hand it id order), so
    this is what keeps the B<A case a reproduction of the deadlock and not of luck.
    """
    async with session_factory() as session:
        database = await session.scalar(sa.text("SELECT quote_ident(current_database())"))
    async with session_factory() as session:
        conn = await session.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
        await conn.execute(
            sa.text(f"ALTER DATABASE {database} SET enable_indexscan = off")  # nosec B608
        )
        await conn.execute(
            sa.text(f"ALTER DATABASE {database} SET enable_bitmapscan = off")  # nosec B608
        )
    try:
        yield
    finally:
        async with session_factory() as session:
            conn = await session.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
            await conn.execute(sa.text(f"ALTER DATABASE {database} RESET enable_indexscan"))  # nosec B608
            await conn.execute(sa.text(f"ALTER DATABASE {database} RESET enable_bitmapscan"))  # nosec B608


@pytest.mark.usefixtures("heap_order_scans")
@pytest.mark.parametrize("referrer_sorts_first", [True, False], ids=["A<B", "B<A"])
async def test_scoped_delete_and_capture_of_the_same_pair_do_not_deadlock(
    session_factory: async_sessionmaker[AsyncSession], project: str, referrer_sorts_first: bool
) -> None:
    low, high = sorted((uuid4(), uuid4()), key=str)
    referrer_id, target_id = (low, high) if referrer_sorts_first else (high, low)
    sessions = PgBrainSessionRepo(session_factory)
    client_key = f"deadlock-{uuid4().hex[:8]}"
    started = await sessions.start(project, client_key)
    await _insert_referenced_pair(session_factory, project, referrer_id, target_id)

    async with session_factory() as gate:
        async with gate.begin():
            # NO KEY UPDATE blocks the delete's FOR UPDATE on A but not the
            # capture's FOR KEY SHARE: it parks the delete while it holds B.
            await gate.execute(
                sa.select(decisions.c.id)
                .where(decisions.c.id == referrer_id)
                .with_for_update(key_share=True)
            )
            deleting = asyncio.create_task(
                _delete_with("decision", session_factory, target_id, project)
            )
            await _until(lambda: _lock_waiters_at_least(session_factory, 1), "delete parked")
            capturing = asyncio.create_task(
                sessions.capture(started.session.id, client_key, [referrer_id, target_id])
            )
            await _until(
                lambda: _settled_or_parked(capturing, session_factory), "capture parked or done"
            )

    outcomes = await asyncio.wait_for(
        asyncio.gather(deleting, capturing, return_exceptions=True), timeout=15
    )

    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert not any("deadlock" in str(f).lower() for f in failures), failures
    deleted_ok = outcomes[0] is True
    captured_ok = not isinstance(outcomes[1], BaseException)
    # Whichever commits first wins; the other one is refused cleanly.
    assert deleted_ok != captured_ok, outcomes
    if deleted_ok:
        assert not await _exists(session_factory, "decision", target_id)
    else:
        assert isinstance(outcomes[0], KnowledgeCapturedError), outcomes
        assert await _exists(session_factory, "decision", target_id)


async def _lock_waiters_at_least(factory: async_sessionmaker[AsyncSession], count: int) -> bool:
    return await _lock_waiters(factory) >= count


async def _settled_or_parked(
    task: asyncio.Task[Any], factory: async_sessionmaker[AsyncSession]
) -> bool:
    return task.done() or await _lock_waiters(factory) >= 2


@SCOPES
async def test_delete_adopts_a_referrer_committed_while_it_waited_for_the_lock(
    session_factory: async_sessionmaker[AsyncSession], project: str, scoped: bool
) -> None:
    """The referrer is read by the locking statement, so one committed after that
    snapshot is invisible to it: the delete must notice it, lock it and clear it."""
    target_id = await _insert_row(session_factory, "decision", project)
    referrer_id = uuid4()

    async with session_factory() as writer:
        async with writer.begin():
            # The foreign key check key-shares the target: the delete queues behind it.
            await writer.execute(
                decisions.insert().values(
                    id=referrer_id,
                    project_key=project,
                    status="superseded",
                    superseded_by=target_id,
                    **_values("decision", project),
                )
            )
            deleting = asyncio.create_task(
                _delete_with("decision", session_factory, target_id, project if scoped else None)
            )
            await _until(lambda: _lock_waiters_at_least(session_factory, 1), "delete parked")

    assert await asyncio.wait_for(deleting, timeout=10) is True
    async with session_factory() as session:
        row = (
            (await session.execute(sa.select(decisions).where(decisions.c.id == referrer_id)))
            .mappings()
            .one()
        )
    assert row["superseded_by"] is None
    assert row["status"] == "active"
    assert not await _exists(session_factory, "decision", target_id)


async def test_scoped_delete_of_another_projects_decision_takes_no_lock_on_it(
    session_factory: async_sessionmaker[AsyncSession], project: str
) -> None:
    """A refused delete must not queue behind, or block, rows of a project it cannot touch."""
    foreign_id = uuid4()
    foreign_project = f"{project}-other"
    async with session_factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=foreign_id, project_key=foreign_project, **_values("decision", project)
            )
        )
    try:
        async with session_factory() as holder:
            async with holder.begin():
                await holder.execute(
                    sa.select(decisions.c.id).where(decisions.c.id == foreign_id).with_for_update()
                )
                deleted = await asyncio.wait_for(
                    _delete_with("decision", session_factory, foreign_id, project), timeout=5
                )
        assert deleted is False
        assert await _exists(session_factory, "decision", foreign_id)
    finally:
        async with session_factory.begin() as session:
            await session.execute(decisions.delete().where(decisions.c.id == foreign_id))
