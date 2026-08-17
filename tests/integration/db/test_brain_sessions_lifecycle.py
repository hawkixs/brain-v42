"""PostgreSQL integration coverage for concurrent explicit Brain sessions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    brain_session_artifacts,
    brain_sessions,
    decisions,
    project_contexts,
)
from brain_v42.models.brain_session import (
    BrainSessionCaptureConflictError,
    BrainSessionIdentityConflictError,
    BrainSessionInputError,
    BrainSessionStateError,
)
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.services.brain_session_service import BrainSessionService

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def session_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    project_key = f"integ-session-{uuid4().hex[:10]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Brain session integration",
                description="Isolated lifecycle integration fixture",
                current_focus="initial focus",
            )
        )
    try:
        yield project_key
    finally:
        async with session_factory.begin() as session:
            project_sessions = sa.select(brain_sessions.c.id).where(
                brain_sessions.c.project_key == project_key
            )
            await session.execute(
                brain_session_artifacts.delete().where(
                    brain_session_artifacts.c.session_id.in_(project_sessions)
                )
            )
            await session.execute(
                brain_sessions.delete().where(brain_sessions.c.project_key == project_key)
            )
            await session.execute(decisions.delete().where(decisions.c.project_key == project_key))
            await session.execute(
                project_contexts.delete().where(project_contexts.c.project_key == project_key)
            )


def _service(
    session_factory: async_sessionmaker[AsyncSession],
) -> BrainSessionService:
    return BrainSessionService(PgBrainSessionRepo(session_factory))


async def test_same_client_key_is_idempotent_under_concurrent_start(
    session_factory: async_sessionmaker[AsyncSession],
    session_project: str,
) -> None:
    service = _service(session_factory)

    first, second = await asyncio.gather(
        service.start(session_project, "same-request"),
        service.start(session_project, "same-request"),
    )

    assert first.session.id == second.session.id
    assert {first.replayed, second.replayed} == {False, True}
    assert first.open_session_count == second.open_session_count == 1


async def test_concurrent_sessions_close_independently_from_shared_focus(
    session_factory: async_sessionmaker[AsyncSession],
    session_project: str,
) -> None:
    service = _service(session_factory)
    first = await service.start(session_project, "task-a")
    second = await service.start(session_project, "task-b")

    ended_first = await service.end(
        first.session.id,
        "task-a",
        "task A completed",
        "focus from task A",
        first.session.started_focus_revision,
        nothing_to_capture_reason="no durable knowledge produced",
    )

    assert ended_first.current_focus_revision == 1
    ended_second = await service.end(
        second.session.id,
        "task-b",
        "task B completed",
        "focus from task B",
        second.session.started_focus_revision,
        nothing_to_capture_reason="no durable knowledge produced",
    )

    assert ended_first.focus_outcome.value == "applied"
    assert ended_second.focus_outcome.value == "conflict"
    assert ended_second.session.status.value == "ended"
    assert ended_second.current_focus == "focus from task A"
    assert ended_second.current_focus_revision == 1
    with pytest.raises(BrainSessionStateError):
        await service.resume(second.session.id, "task-b")


async def test_invalid_capture_rolls_back_focus_and_keeps_session_open(
    session_factory: async_sessionmaker[AsyncSession],
    session_project: str,
) -> None:
    service = _service(session_factory)
    started = await service.start(session_project, "invalid-capture")

    with pytest.raises(BrainSessionInputError):
        await service.capture(started.session.id, "invalid-capture", [uuid4()])

    resumed = await service.resume(started.session.id, "invalid-capture")
    assert resumed.current_focus == "initial focus"
    assert resumed.current_focus_revision == 0
    assert resumed.session.status.value == "open"


async def test_wrong_client_key_cannot_end_a_peer_session(
    session_factory: async_sessionmaker[AsyncSession],
    session_project: str,
) -> None:
    service = _service(session_factory)
    first = await service.start(session_project, "task-a")
    second = await service.start(session_project, "task-b")

    with pytest.raises(BrainSessionIdentityConflictError):
        await service.end(
            first.session.id,
            "task-b",
            "must not close",
            "must not update focus",
            first.session.started_focus_revision,
            nothing_to_capture_reason="identity mismatch",
        )

    assert (await service.resume(first.session.id, "task-a")).session.status.value == "open"
    assert (await service.resume(second.session.id, "task-b")).session.status.value == "open"


async def test_capture_provenance_has_one_session_owner_under_concurrency(
    session_factory: async_sessionmaker[AsyncSession],
    session_project: str,
) -> None:
    service = _service(session_factory)
    first = await service.start(session_project, "capture-a")
    second = await service.start(session_project, "capture-b")
    knowledge_id = uuid4()
    async with session_factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=knowledge_id,
                title="Session provenance",
                description="One artifact has one producing session",
                reasoning="Concurrent attribution must be serialized",
                project_key=session_project,
            )
        )

    outcomes = await asyncio.gather(
        service.capture(first.session.id, "capture-a", [knowledge_id]),
        service.capture(second.session.id, "capture-b", [knowledge_id]),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, BrainSessionCaptureConflictError) for item in outcomes) == 1
    async with session_factory() as session:
        owner = (
            await session.execute(
                sa.select(brain_session_artifacts.c.session_id).where(
                    brain_session_artifacts.c.knowledge_id == knowledge_id
                )
            )
        ).scalar_one()
    assert owner in {first.session.id, second.session.id}


async def test_capture_is_observable_after_resume_and_abandon(
    session_factory: async_sessionmaker[AsyncSession],
    session_project: str,
) -> None:
    service = _service(session_factory)
    started = await service.start(session_project, "observable-capture")
    knowledge_id = uuid4()
    async with session_factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=knowledge_id,
                title="Observable session attribution",
                description="A resumed or abandoned session retains its ledger view",
                reasoning="Recovery must not hide already persisted attribution",
                project_key=session_project,
            )
        )

    captured = await service.capture(
        started.session.id,
        "observable-capture",
        [knowledge_id],
    )
    resumed = await service.resume(started.session.id, "observable-capture")
    abandoned = await service.abandon(
        started.session.id,
        "observable-capture",
        "work stopped explicitly",
    )
    listed = await service.list(project_key=session_project, status="abandoned")
    replayed = await service.capture(
        started.session.id,
        "observable-capture",
        [knowledge_id],
    )

    assert captured.session.attributed_knowledge_ids == [knowledge_id]
    assert resumed.session.attributed_knowledge_ids == [knowledge_id]
    assert abandoned.session.attributed_knowledge_ids == [knowledge_id]
    assert listed.sessions[0].attributed_knowledge_ids == [knowledge_id]
    assert replayed.replayed is True
    assert replayed.session.attributed_knowledge_ids == [knowledge_id]


async def test_stale_is_observable_and_heartbeat_never_closes_session(
    session_factory: async_sessionmaker[AsyncSession],
    session_project: str,
) -> None:
    service = _service(session_factory)
    started = await service.start(session_project, "stale-task")
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                "UPDATE brain_sessions "
                "SET last_heartbeat_at = NOW() - INTERVAL '25 hours' "
                "WHERE id = :session_id"
            ),
            {"session_id": started.session.id},
        )

    stale = await service.list(project_key=session_project, status="stale")
    assert [item.id for item in stale.sessions] == [started.session.id]
    assert stale.sessions[0].status.value == "open"
    assert stale.sessions[0].is_stale is True

    heartbeat = await service.heartbeat(started.session.id, "stale-task")
    assert heartbeat.session.status.value == "open"
    assert heartbeat.session.is_stale is False
    assert (await service.list(project_key=session_project, status="stale")).sessions == []


async def test_focus_revision_trigger_is_enabled_and_bound_to_expected_function(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    statement = sa.text(
        """
        SELECT
            CAST(trigger_record.tgenabled AS text) AS enabled,
            procedure_record.proname AS function_name,
            pg_catalog.pg_get_triggerdef(trigger_record.oid) AS definition
        FROM pg_catalog.pg_trigger AS trigger_record
        JOIN pg_catalog.pg_class AS table_record
            ON table_record.oid = trigger_record.tgrelid
        JOIN pg_catalog.pg_proc AS procedure_record
            ON procedure_record.oid = trigger_record.tgfoid
        WHERE table_record.relname = 'project_contexts'
          AND trigger_record.tgname = 'project_contexts_focus_revision_trigger'
          AND NOT trigger_record.tgisinternal
        """
    )
    async with session_factory() as session:
        row = (await session.execute(statement)).mappings().one()

    assert row["enabled"] == "O"
    assert row["function_name"] == "increment_project_focus_revision"
    assert "BEFORE UPDATE OF current_focus" in row["definition"]


async def test_database_rejects_null_capture_array_elements(
    session_factory: async_sessionmaker[AsyncSession],
    session_project: str,
) -> None:
    statement = sa.text(
        """
        INSERT INTO brain_sessions (
            project_key, client_key, status, started_focus_revision,
            summary, next_focus, captured_knowledge_ids, focus_outcome,
            focus_at_end, focus_revision_at_end, ended_at
        ) VALUES (
            :project_key, 'invalid-null-capture', 'ended', 0,
            'summary', 'next focus', ARRAY[NULL]::uuid[], 'applied',
            'next focus', 1, NOW()
        )
        """
    )

    with pytest.raises(sa.exc.IntegrityError):
        async with session_factory.begin() as session:
            await session.execute(statement, {"project_key": session_project})


async def test_database_rejects_more_than_one_hundred_capture_ids(
    session_factory: async_sessionmaker[AsyncSession],
    session_project: str,
) -> None:
    statement = sa.text(
        """
        INSERT INTO brain_sessions (
            project_key, client_key, status, started_focus_revision,
            summary, next_focus, captured_knowledge_ids, focus_outcome,
            focus_at_end, focus_revision_at_end, ended_at
        ) VALUES (
            :project_key, 'invalid-oversized-capture', 'ended', 0,
            'summary', 'next focus',
            ARRAY(SELECT gen_random_uuid() FROM generate_series(1, 101)),
            'applied', 'next focus', 1, NOW()
        )
        """
    )

    with pytest.raises(sa.exc.IntegrityError):
        async with session_factory.begin() as session:
            await session.execute(statement, {"project_key": session_project})


@pytest.mark.parametrize(
    ("expected_revision", "focus_outcome", "focus_at_end", "final_revision"),
    [
        (0, "applied", "wrong focus", 1),
        (0, "applied", "next focus", 99),
        (0, "conflict", "shared focus", 0),
        (None, "conflict", "shared focus", None),
    ],
)
async def test_database_rejects_incoherent_focus_outcome_snapshots(
    session_factory: async_sessionmaker[AsyncSession],
    session_project: str,
    expected_revision: int | None,
    focus_outcome: str,
    focus_at_end: str,
    final_revision: int | None,
) -> None:
    statement = sa.text(
        """
        INSERT INTO brain_sessions (
            project_key, client_key, status, started_focus_revision,
            summary, next_focus, captured_knowledge_ids,
            nothing_to_capture_reason, end_expected_focus_revision,
            focus_outcome, focus_at_end, focus_revision_at_end, ended_at
        ) VALUES (
            :project_key, :client_key, 'ended', 0,
            'summary', 'next focus', ARRAY[]::uuid[],
            'no durable artifact', :expected_revision,
            :focus_outcome, :focus_at_end, :final_revision, NOW()
        )
        """
    )

    with pytest.raises(sa.exc.IntegrityError):
        async with session_factory.begin() as session:
            await session.execute(
                statement,
                {
                    "project_key": session_project,
                    "client_key": f"invalid-focus-{uuid4().hex}",
                    "expected_revision": expected_revision,
                    "focus_outcome": focus_outcome,
                    "focus_at_end": focus_at_end,
                    "final_revision": final_revision,
                },
            )
