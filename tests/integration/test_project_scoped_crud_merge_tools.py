"""Real PostgreSQL proof for scoped CRUD and atomic merge MCP tools."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import consolidation_log, decisions
from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectScope,
    bind_dream_project_scope,
)
from brain_v42.models.decision import DecisionCreate
from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate
from brain_v42.repositories.pg_consolidation_log import PgConsolidationLogRepo
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo
from brain_v42.services.consolidation import ConsolidationJob
from brain_v42.services.decision_service import DecisionService

pytestmark = pytest.mark.integration


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


class UnusedResolver:
    async def references_belong_to_project(self, *_args: Any) -> bool:
        raise AssertionError("middleware precheck is deliberately simulated before this test")


class _LockSignalingSession:
    """Proxy a real session and expose the backend waiting on the merge lock."""

    def __init__(
        self,
        session: AsyncSession,
        lock_attempted: asyncio.Event,
        backend_pid: list[int],
    ) -> None:
        self._session = session
        self._lock_attempted = lock_attempted
        self._backend_pid = backend_pid

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "FOR UPDATE" in str(statement):
            pid_result = await self._session.execute(sa.text("SELECT pg_backend_pid()"))
            self._backend_pid.append(pid_result.scalar_one())
            self._lock_attempted.set()
        return await self._session.execute(statement, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


def _scope(project_key: str, tool_name: str) -> DreamProjectScope:
    return DreamProjectScope(
        project_key=project_key,
        resolver=UnusedResolver(),
        audit=DreamProjectAudit(principal="dream-codex-synth", phase="synth"),
        tool_name=tool_name,
    )


def _other_services() -> dict[str, Any]:
    services: dict[str, Any] = {}
    for name in ("learning_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        service = MagicMock()
        service.resolve_id_prefix = AsyncMock(return_value=[])
        service.get_by_id = AsyncMock(return_value=None)
        service.update = AsyncMock(return_value=None)
        service.delete = AsyncMock(return_value=False)
        service.list_all = AsyncMock(return_value=[])
        services[name] = service
    services["snippet_svc"].list_snippets = AsyncMock(return_value=[])
    services["runbook_svc"].list_by_project = AsyncMock(return_value=[])
    return services


def _crud_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    from brain_v42.mcp.tools.crud_tools import register_crud_tools

    decision_repo = PgDecisionRepo(session_factory)
    decision_service = DecisionService(decision_repo, AsyncMock())
    decision_service._embedding_svc.embed = AsyncMock(return_value=[0.0] * 1536)
    mcp = MockMCP()
    register_crud_tools(
        mcp,
        decision_svc=decision_service,
        session_factory=session_factory,
        **_other_services(),
    )
    return mcp.registered


def _merge_tool(
    session_factory: Any,
) -> Any:
    from brain_v42.mcp.tools.decay_tools import register_decay_tools

    mcp = MockMCP()
    job = ConsolidationJob(
        session_factory,
        PgConsolidationLogRepo(session_factory),
    )
    register_decay_tools(mcp, session_factory, consolidation_job=job)
    return mcp.registered["brain_merge_entities"]


async def _delete_consolidation_audits(
    session_factory: async_sessionmaker[AsyncSession],
    ids: list[UUID],
) -> None:
    if not ids:
        return
    async with session_factory() as session:
        await session.execute(
            sa.delete(consolidation_log).where(
                sa.or_(
                    consolidation_log.c.source_id.in_(ids),
                    consolidation_log.c.target_id.in_(ids),
                )
            )
        )
        await session.commit()


def _lock_signaling_factory(
    session_factory: async_sessionmaker[AsyncSession],
    lock_attempted: asyncio.Event,
    backend_pid: list[int],
) -> Any:
    @asynccontextmanager
    async def factory() -> Any:
        async with session_factory() as session:
            yield _LockSignalingSession(session, lock_attempted, backend_pid)

    return factory


async def _wait_until_backend_is_lock_blocked(
    session_factory: async_sessionmaker[AsyncSession],
    backend_pid: int,
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


async def _create_decision(
    repo: PgDecisionRepo,
    *,
    project_key: str,
    title: str,
    tags: list[str] | None = None,
) -> UUID:
    row = await repo.create(
        DecisionCreate(
            title=title,
            description=f"{title} description",
            reasoning="SEC1b integration proof",
            project_key=project_key,
            tags=tags or [],
        )
    )
    return row.id


async def _delete_plan(
    session_factory: async_sessionmaker[AsyncSession],
    plan_id: UUID | None,
) -> None:
    if plan_id is None:
        return
    async with session_factory() as session:
        await PgIndexedPlanRepo(session).delete(plan_id)


@pytest.mark.asyncio
async def test_scoped_entity_and_plan_crud_rechecks_ownership_at_tool_use(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owned_project = f"integ-sec1b-tool-owned-{uuid4().hex[:8]}"
    foreign_project = f"integ-sec1b-tool-foreign-{uuid4().hex[:8]}"
    decision_repo = PgDecisionRepo(session_factory)
    tools = _crud_tools(session_factory)
    owned_id: UUID | None = None
    foreign_id: UUID | None = None
    owned_plan_id: UUID | None = None
    foreign_plan_id: UUID | None = None

    try:
        owned_id = await _create_decision(
            decision_repo,
            project_key=owned_project,
            title="SEC1b owned tool decision",
        )
        foreign_id = await _create_decision(
            decision_repo,
            project_key=foreign_project,
            title="SEC1b foreign tool decision",
        )

        async with session_factory() as session:
            plan_repo = PgIndexedPlanRepo(session)
            owned_plan_id = await plan_repo.upsert_plan_with_chunks(
                IndexedPlanCreate(
                    file_path=f"/tmp/sec1b-owned-{uuid4().hex}.md",
                    title="SEC1b owned tool plan",
                    plan_type="plan",
                    project_key=owned_project,
                    content_hash=uuid4().hex * 2,
                    content="# SEC1b owned tool plan",
                    chunk_count=1,
                    word_count=5,
                ),
                [0.0] * 1536,
                [
                    IndexedPlanChunkCreate(
                        section_title="Owned",
                        section_path="Owned",
                        content="Owned project plan chunk",
                        section_order=0,
                        word_count=4,
                        project_key=owned_project,
                        plan_type="plan",
                    )
                ],
                [[0.0] * 1536],
            )
            foreign_plan_id = await plan_repo.upsert_plan_with_chunks(
                IndexedPlanCreate(
                    file_path=f"/tmp/sec1b-foreign-{uuid4().hex}.md",
                    title="SEC1b foreign tool plan",
                    plan_type="plan",
                    project_key=foreign_project,
                    content_hash=uuid4().hex * 2,
                    content="# SEC1b foreign tool plan",
                    chunk_count=0,
                    word_count=5,
                ),
                [0.0] * 1536,
                [],
                [],
            )

        with bind_dream_project_scope(_scope(owned_project, "brain_get")):
            owned_output = await tools["brain_get"]("decision", str(owned_id))
            owned_plan_output = await tools["brain_get"]("plan", str(owned_plan_id))
            with pytest.raises(ToolError, match=rf"^decision {foreign_id} not found$"):
                await tools["brain_get"]("decision", str(foreign_id))
            with pytest.raises(ToolError, match=rf"^plan {foreign_plan_id} not found$"):
                await tools["brain_get"]("plan", str(foreign_plan_id))

        assert "SEC1b owned tool decision" in owned_output
        assert "SEC1b owned tool plan" in owned_plan_output

        with bind_dream_project_scope(_scope(owned_project, "brain_update")):
            with pytest.raises(ToolError, match=rf"^decision {foreign_id} not found$"):
                await tools["brain_update"](
                    "decision",
                    str(foreign_id),
                    {"status": "deprecated"},
                )
            owned_update = await tools["brain_update"](
                "decision",
                str(owned_id),
                {"status": "deprecated"},
            )
        assert owned_update.startswith("ok Updated")
        foreign_after_update = await decision_repo.get_by_id(foreign_id)
        owned_after_update = await decision_repo.get_by_id(owned_id)
        assert foreign_after_update is not None
        assert foreign_after_update.status == "active"
        assert owned_after_update is not None
        assert owned_after_update.status == "deprecated"

        # Admin/STDIO remains global and retains the historical result contract.
        assert "SEC1b foreign tool decision" in await tools["brain_get"](
            "decision", str(foreign_id)
        )

        with bind_dream_project_scope(_scope(owned_project, "brain_delete")):
            with pytest.raises(ToolError, match=rf"^decision {foreign_id} not found$"):
                await tools["brain_delete"]("decision", str(foreign_id))
            with pytest.raises(ToolError, match=rf"^plan {foreign_plan_id} not found$"):
                await tools["brain_delete"]("plan", str(foreign_plan_id))
            owned_delete = await tools["brain_delete"]("decision", str(owned_id))
            owned_plan_delete = await tools["brain_delete"]("plan", str(owned_plan_id))
        assert owned_delete.startswith("ok Deleted")
        assert owned_plan_delete.startswith("ok Deleted")
        assert await decision_repo.get_by_id(foreign_id) is not None
        assert await decision_repo.get_by_id(owned_id) is None
        async with session_factory() as session:
            assert await PgIndexedPlanRepo(session).get_with_chunks(foreign_plan_id) is not None
            assert await PgIndexedPlanRepo(session).get_with_chunks(owned_plan_id) is None
        owned_id = None
        owned_plan_id = None
    finally:
        if owned_id is not None:
            await decision_repo.delete(owned_id)
        if foreign_id is not None:
            await decision_repo.delete(foreign_id)
        await _delete_plan(session_factory, owned_plan_id)
        await _delete_plan(session_factory, foreign_plan_id)


@pytest.mark.asyncio
async def test_scoped_merge_is_atomic_and_refuses_a_foreign_target_without_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owned_project = f"integ-sec1b-merge-owned-{uuid4().hex[:8]}"
    foreign_project = f"integ-sec1b-merge-foreign-{uuid4().hex[:8]}"
    repo = PgDecisionRepo(session_factory)
    merge = _merge_tool(session_factory)
    ids: list[UUID] = []

    try:
        source_id = await _create_decision(
            repo,
            project_key=owned_project,
            title="SEC1b merge source",
            tags=["source"],
        )
        ids.append(source_id)
        target_id = await _create_decision(
            repo,
            project_key=owned_project,
            title="SEC1b merge target",
            tags=["target"],
        )
        ids.append(target_id)
        untouched_source_id = await _create_decision(
            repo,
            project_key=owned_project,
            title="SEC1b untouched source",
            tags=["untouched"],
        )
        ids.append(untouched_source_id)
        foreign_target_id = await _create_decision(
            repo,
            project_key=foreign_project,
            title="SEC1b foreign target",
            tags=["foreign"],
        )
        ids.append(foreign_target_id)
        foreign_source_id = await _create_decision(
            repo,
            project_key=foreign_project,
            title="SEC1b foreign source",
            tags=["foreign-source"],
        )
        ids.append(foreign_source_id)
        untouched_target_id = await _create_decision(
            repo,
            project_key=owned_project,
            title="SEC1b untouched target",
            tags=["untouched-target"],
        )
        ids.append(untouched_target_id)

        with bind_dream_project_scope(_scope(owned_project, "brain_merge_entities")):
            success = await merge("decision", str(source_id), str(target_id))
        assert success.startswith("ok Merged")
        source_after = await repo.get_by_id(source_id)
        target_after = await repo.get_by_id(target_id)
        assert source_after is not None
        assert source_after.merged_into == target_id
        assert source_after.freshness_status == "archived"
        assert target_after is not None
        assert set(target_after.tags) == {"source", "target"}

        with bind_dream_project_scope(_scope(owned_project, "brain_merge_entities")):
            with pytest.raises(
                ToolError,
                match=rf"^Target decision {foreign_target_id} not found$",
            ):
                await merge(
                    "decision",
                    str(untouched_source_id),
                    str(foreign_target_id),
                )
            with pytest.raises(
                ToolError,
                match=rf"^Source decision {foreign_source_id} not found$",
            ):
                await merge(
                    "decision",
                    str(foreign_source_id),
                    str(untouched_target_id),
                )
            with pytest.raises(
                ToolError,
                match=r"^Source and target must be different entities$",
            ):
                await merge(
                    "decision",
                    str(untouched_source_id),
                    str(untouched_source_id),
                )
        untouched_after = await repo.get_by_id(untouched_source_id)
        foreign_after = await repo.get_by_id(foreign_target_id)
        foreign_source_after = await repo.get_by_id(foreign_source_id)
        untouched_target_after = await repo.get_by_id(untouched_target_id)
        assert untouched_after is not None
        assert untouched_after.merged_into is None
        assert untouched_after.freshness_status == "fresh"
        assert untouched_after.tags == ["untouched"]
        assert foreign_after is not None
        assert foreign_after.tags == ["foreign"]
        assert foreign_source_after is not None
        assert foreign_source_after.merged_into is None
        assert foreign_source_after.freshness_status == "fresh"
        assert foreign_source_after.tags == ["foreign-source"]
        assert untouched_target_after is not None
        assert untouched_target_after.tags == ["untouched-target"]
    finally:
        await _delete_consolidation_audits(session_factory, ids)
        for entity_id in ids:
            await repo.delete(entity_id)


@pytest.mark.asyncio
async def test_scoped_merge_rechecks_project_after_waiting_on_concurrent_owner_change(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owned_project = f"integ-sec1b-race-owned-{uuid4().hex[:8]}"
    moved_project = f"integ-sec1b-race-moved-{uuid4().hex[:8]}"
    repo = PgDecisionRepo(session_factory)
    lock_attempted = asyncio.Event()
    backend_pid: list[int] = []
    merge = _merge_tool(_lock_signaling_factory(session_factory, lock_attempted, backend_pid))
    source_id: UUID | None = None
    target_id: UUID | None = None

    try:
        source_id = await _create_decision(
            repo,
            project_key=owned_project,
            title="SEC1b race source",
            tags=["source-before"],
        )
        target_id = await _create_decision(
            repo,
            project_key=owned_project,
            title="SEC1b race target",
            tags=["target-before"],
        )

        async with session_factory() as owner_change_session:
            await owner_change_session.execute(
                sa.select(decisions.c.id).where(decisions.c.id == target_id).with_for_update()
            )

            with bind_dream_project_scope(_scope(owned_project, "brain_merge_entities")):
                merge_task = asyncio.create_task(merge("decision", str(source_id), str(target_id)))

            await asyncio.wait_for(lock_attempted.wait(), timeout=5)
            assert len(backend_pid) == 1
            await _wait_until_backend_is_lock_blocked(session_factory, backend_pid[0])
            assert not merge_task.done(), "merge must be blocked on the target row lock"

            await owner_change_session.execute(
                decisions.update()
                .where(decisions.c.id == target_id)
                .values(project_key=moved_project)
            )
            await owner_change_session.commit()

        with pytest.raises(ToolError, match=rf"^Target decision {target_id} not found$"):
            await asyncio.wait_for(merge_task, timeout=5)

        source_after = await repo.get_by_id(source_id)
        target_after = await repo.get_by_id(target_id)
        assert source_after is not None
        assert source_after.merged_into is None
        assert source_after.freshness_status == "fresh"
        assert source_after.tags == ["source-before"]
        assert target_after is not None
        assert target_after.project_key == moved_project
        assert target_after.tags == ["target-before"]
    finally:
        await _delete_consolidation_audits(
            session_factory,
            [entity_id for entity_id in (source_id, target_id) if entity_id is not None],
        )
        if source_id is not None:
            await repo.delete(source_id)
        if target_id is not None:
            await repo.delete(target_id)
