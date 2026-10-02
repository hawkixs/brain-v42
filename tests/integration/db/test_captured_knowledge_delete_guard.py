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
