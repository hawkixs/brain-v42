"""Real PostgreSQL failure-first proofs for SEC1b promotion authorization."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import adrs, dream_promotions, dream_runs, learnings, runbooks
from brain_v42.models.adr import ADRCreate
from brain_v42.models.runbook import RunbookCreate, RunbookStep
from brain_v42.repositories.pg_adr import PgADRRepo, SourceLearningNotFound
from brain_v42.repositories.pg_runbook import PgRunbookRepo

pytestmark = pytest.mark.integration
ADMIN_SCOPE = object()


def promotion_data(kind: str, project_key: str, token: str) -> ADRCreate | RunbookCreate:
    if kind == "adr":
        return ADRCreate(
            title=f"SEC1b ADR {token}",
            context="Context",
            decision="Decision",
            consequences="Consequences",
            project_key=project_key,
        )
    return RunbookCreate(
        title=f"SEC1b runbook {token}",
        description="Description",
        project_key=project_key,
        trigger="Trigger",
        steps=[RunbookStep(order=1, title="Step")],
    )


async def promote(
    kind: str,
    session_factory: Any,
    *,
    data: ADRCreate | RunbookCreate,
    source_id: UUID,
    scope: str | object,
    dream_run_id: int | None = None,
) -> Any:
    if kind == "adr":
        assert isinstance(data, ADRCreate)
        repo = PgADRRepo(session_factory)
        args = (data, None, source_id, True, dream_run_id)
    else:
        assert isinstance(data, RunbookCreate)
        repo = PgRunbookRepo(session_factory)
        args = (data, None, source_id, dream_run_id)
    if scope is ADMIN_SCOPE:
        return await repo.create_with_promotion(*args)
    return await repo.create_with_promotion(*args, project_key=scope)


async def insert_learning(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_key: str | None,
    token: str,
) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic=f"SEC1b promotion {token}",
                        insight="Scoped source",
                        project_key=project_key,
                        source_type="experience",
                        confidence="high",
                        tags=["sec1b:test"],
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()


async def cleanup_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    source_ids: list[UUID],
    token: str,
    dream_run_ids: list[int] | None = None,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            if source_ids:
                await session.execute(
                    dream_promotions.delete().where(
                        dream_promotions.c.source_learning_id.in_(source_ids)
                    )
                )
            await session.execute(adrs.delete().where(adrs.c.title.contains(token)))
            await session.execute(runbooks.delete().where(runbooks.c.title.contains(token)))
            if source_ids:
                await session.execute(learnings.delete().where(learnings.c.id.in_(source_ids)))
            if dream_run_ids:
                await session.execute(dream_runs.delete().where(dream_runs.c.id.in_(dream_run_ids)))


async def assert_no_target_or_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    source_id: UUID,
    token: str,
) -> None:
    async with session_factory() as session:
        adr_count = (
            await session.execute(
                sa.select(sa.func.count()).select_from(adrs).where(adrs.c.title.contains(token))
            )
        ).scalar_one()
        runbook_count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(runbooks)
                .where(runbooks.c.title.contains(token))
            )
        ).scalar_one()
        audit_count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(dream_promotions)
                .where(dream_promotions.c.source_learning_id == source_id)
            )
        ).scalar_one()
    assert (adr_count, runbook_count, audit_count) == (0, 0, 0)


async def wait_until_backend_is_locked(
    session_factory: async_sessionmaker[AsyncSession], backend_pid: int
) -> None:
    async with asyncio.timeout(5):
        while True:
            async with session_factory() as observer:
                wait_event_type = (
                    await observer.execute(
                        sa.text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": backend_pid},
                    )
                ).scalar_one_or_none()
            if wait_event_type == "Lock":
                return
            await asyncio.sleep(0.01)


class SourceLockProbeSession:
    def __init__(
        self,
        session: AsyncSession,
        attempted: asyncio.Event,
        backend_pid: list[int],
    ) -> None:
        self._session = session
        self._attempted = attempted
        self._backend_pid = backend_pid

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(statement)
        if "FROM learnings" in sql and "FOR UPDATE" in sql:
            pid = await self._session.execute(sa.text("SELECT pg_backend_pid()"))
            self._backend_pid.append(pid.scalar_one())
            self._attempted.set()
        return await self._session.execute(statement, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


def source_probe_factory(
    session_factory: async_sessionmaker[AsyncSession],
    attempted: asyncio.Event,
    backend_pid: list[int],
) -> Any:
    @asynccontextmanager
    async def factory() -> Any:
        async with session_factory() as session:
            yield SourceLockProbeSession(session, attempted, backend_pid)

    return factory


class RunbookRaceSession:
    """Coordinate the exact target/source lock inversion rejected in the prior attempt."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        role: str,
        admin_target_inserted: asyncio.Event,
        release_admin: asyncio.Event,
        scoped_wait_attempted: asyncio.Event,
        scoped_backend_pid: list[int],
    ) -> None:
        self._session = session
        self._role = role
        self._admin_target_inserted = admin_target_inserted
        self._release_admin = release_admin
        self._scoped_wait_attempted = scoped_wait_attempted
        self._scoped_backend_pid = scoped_backend_pid

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(statement)
        if self._role == "scoped" and not self._scoped_wait_attempted.is_set():
            waits_on_target_gate = "pg_advisory_xact_lock" in sql
            waits_on_target_insert = sql.lstrip().startswith("INSERT INTO runbooks")
            if waits_on_target_gate or waits_on_target_insert:
                pid = await self._session.execute(sa.text("SELECT pg_backend_pid()"))
                self._scoped_backend_pid.append(pid.scalar_one())
                self._scoped_wait_attempted.set()

        result = await self._session.execute(statement, *args, **kwargs)
        if self._role == "admin" and sql.lstrip().startswith("INSERT INTO runbooks"):
            self._admin_target_inserted.set()
            await self._release_admin.wait()
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


def runbook_race_factory(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    role: str,
    admin_target_inserted: asyncio.Event,
    release_admin: asyncio.Event,
    scoped_wait_attempted: asyncio.Event,
    scoped_backend_pid: list[int],
) -> Any:
    @asynccontextmanager
    async def factory() -> Any:
        async with session_factory() as session:
            yield RunbookRaceSession(
                session,
                role=role,
                admin_target_inserted=admin_target_inserted,
                release_admin=release_admin,
                scoped_wait_attempted=scoped_wait_attempted,
                scoped_backend_pid=scoped_backend_pid,
            )

    return factory


async def wait_for_event_or_task(event: asyncio.Event, task: asyncio.Task[Any]) -> None:
    event_task = asyncio.create_task(event.wait())
    try:
        done, _pending = await asyncio.wait(
            {event_task, task}, timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            await task
        assert event_task in done, "expected PostgreSQL lock attempt never happened"
    finally:
        if not event_task.done():
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)


def exception_chain_names(error: BaseException) -> set[str]:
    names: set[str] = set()
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        names.add(type(current).__name__)
        for linked in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return names


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["adr", "runbook"])
async def test_scoped_promotion_accepts_owned_and_refuses_foreign_null_missing(
    session_factory: async_sessionmaker[AsyncSession], kind: str
) -> None:
    token = uuid4().hex
    owned_project = f"integ-sec1b-prom-owned-{token[:8]}"
    foreign_project = f"integ-sec1b-prom-foreign-{token[:8]}"
    source_ids: list[UUID] = []
    try:
        owned_id = await insert_learning(
            session_factory, project_key=owned_project, token=f"owned-{token}"
        )
        source_ids.append(owned_id)
        foreign_id = await insert_learning(
            session_factory, project_key=foreign_project, token=f"foreign-{token}"
        )
        source_ids.append(foreign_id)
        null_id = await insert_learning(session_factory, project_key=None, token=f"null-{token}")
        source_ids.append(null_id)

        target = await promote(
            kind,
            session_factory,
            data=promotion_data(kind, owned_project, f"owned-{token}"),
            source_id=owned_id,
            scope=owned_project,
        )
        async with session_factory() as session:
            metadata = (
                await session.execute(
                    sa.select(learnings.c.metadata).where(learnings.c.id == owned_id)
                )
            ).scalar_one()
            audit = (
                (
                    await session.execute(
                        sa.select(dream_promotions).where(
                            dream_promotions.c.source_learning_id == owned_id
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert metadata["target_entity_id"] == str(target.id)
        assert audit["target_type"] == kind

        for label, source_id in (
            ("foreign", foreign_id),
            ("null", null_id),
            ("missing", uuid4()),
        ):
            denied_token = f"{label}-{token}"
            with pytest.raises(SourceLearningNotFound) as raised:
                await promote(
                    kind,
                    session_factory,
                    data=promotion_data(kind, owned_project, denied_token),
                    source_id=source_id,
                    scope=owned_project,
                )
            assert str(raised.value) == "source learning not found"
            await assert_no_target_or_audit(
                session_factory, source_id=source_id, token=denied_token
            )

        async with session_factory() as session:
            unchanged = (
                (
                    await session.execute(
                        sa.select(learnings.c.id, learnings.c.metadata).where(
                            learnings.c.id.in_([foreign_id, null_id])
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert {row["id"]: row["metadata"] for row in unchanged} == {
            foreign_id: {},
            null_id: {},
        }
    finally:
        await cleanup_rows(session_factory, source_ids=source_ids, token=token)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["adr", "runbook"])
async def test_scoped_promotion_rechecks_owner_after_concurrent_change(
    session_factory: async_sessionmaker[AsyncSession], kind: str
) -> None:
    token = uuid4().hex
    owned_project = f"integ-sec1b-prom-race-owned-{token[:8]}"
    moved_project = f"integ-sec1b-prom-race-moved-{token[:8]}"
    source_ids: list[UUID] = []
    lock_attempted = asyncio.Event()
    backend_pid: list[int] = []
    task: asyncio.Task[Any] | None = None
    try:
        source_id = await insert_learning(
            session_factory, project_key=owned_project, token=f"owner-{token}"
        )
        source_ids.append(source_id)
        async with session_factory() as owner_session:
            await owner_session.execute(
                sa.select(learnings.c.id).where(learnings.c.id == source_id).with_for_update()
            )
            await owner_session.execute(
                learnings.update()
                .where(learnings.c.id == source_id)
                .values(project_key=moved_project)
            )
            task = asyncio.create_task(
                promote(
                    kind,
                    source_probe_factory(session_factory, lock_attempted, backend_pid),
                    data=promotion_data(kind, owned_project, f"owner-{token}"),
                    source_id=source_id,
                    scope=owned_project,
                )
            )
            await wait_for_event_or_task(lock_attempted, task)
            assert len(backend_pid) == 1
            await wait_until_backend_is_locked(session_factory, backend_pid[0])
            assert not task.done()
            await owner_session.commit()

        with pytest.raises(SourceLearningNotFound, match="^source learning not found$"):
            await asyncio.wait_for(task, timeout=5)
        await assert_no_target_or_audit(
            session_factory, source_id=source_id, token=f"owner-{token}"
        )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await cleanup_rows(session_factory, source_ids=source_ids, token=token)


@pytest.mark.asyncio
async def test_runbook_admin_and_scoped_same_target_never_deadlock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = uuid4().hex
    project_key = f"integ-sec1b-prom-deadlock-{token[:8]}"
    source_ids: list[UUID] = []
    admin_target_inserted = asyncio.Event()
    release_admin = asyncio.Event()
    scoped_wait_attempted = asyncio.Event()
    scoped_backend_pid: list[int] = []
    dream_run_ids: list[int] = []
    admin_task: asyncio.Task[Any] | None = None
    scoped_task: asyncio.Task[Any] | None = None
    try:
        async with session_factory() as session:
            async with session.begin():
                dream_run_id = (
                    await session.execute(
                        dream_runs.insert()
                        .values(run_date=date.today(), phase="promote", status="done")
                        .returning(dream_runs.c.id)
                    )
                ).scalar_one()
        dream_run_ids.append(dream_run_id)
        source_id = await insert_learning(
            session_factory, project_key=project_key, token=f"deadlock-{token}"
        )
        source_ids.append(source_id)
        data = promotion_data("runbook", project_key, f"deadlock-{token}")

        admin_task = asyncio.create_task(
            promote(
                "runbook",
                runbook_race_factory(
                    session_factory,
                    role="admin",
                    admin_target_inserted=admin_target_inserted,
                    release_admin=release_admin,
                    scoped_wait_attempted=scoped_wait_attempted,
                    scoped_backend_pid=scoped_backend_pid,
                ),
                data=data,
                source_id=source_id,
                scope=ADMIN_SCOPE,
                dream_run_id=dream_run_id,
            )
        )
        await wait_for_event_or_task(admin_target_inserted, admin_task)

        scoped_task = asyncio.create_task(
            promote(
                "runbook",
                runbook_race_factory(
                    session_factory,
                    role="scoped",
                    admin_target_inserted=admin_target_inserted,
                    release_admin=release_admin,
                    scoped_wait_attempted=scoped_wait_attempted,
                    scoped_backend_pid=scoped_backend_pid,
                ),
                data=data,
                source_id=source_id,
                scope=project_key,
            )
        )
        await wait_for_event_or_task(scoped_wait_attempted, scoped_task)
        assert len(scoped_backend_pid) == 1
        await wait_until_backend_is_locked(session_factory, scoped_backend_pid[0])
        release_admin.set()

        outcomes = await asyncio.wait_for(
            asyncio.gather(admin_task, scoped_task, return_exceptions=True), timeout=8
        )
        errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        successes = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
        assert not any("DeadlockDetectedError" in exception_chain_names(error) for error in errors)
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], sa.exc.IntegrityError)

        async with session_factory() as session:
            target_count = (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(runbooks)
                    .where(
                        runbooks.c.title == data.title,
                        runbooks.c.project_key == project_key,
                    )
                )
            ).scalar_one()
            audit = (
                (
                    await session.execute(
                        sa.select(dream_promotions).where(
                            dream_promotions.c.source_learning_id == source_id
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert target_count == 1
        assert audit["dream_run_id"] == dream_run_id
    finally:
        release_admin.set()
        for task in (admin_task, scoped_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (admin_task, scoped_task) if task is not None),
            return_exceptions=True,
        )
        await cleanup_rows(
            session_factory,
            source_ids=source_ids,
            token=token,
            dream_run_ids=dream_run_ids,
        )
