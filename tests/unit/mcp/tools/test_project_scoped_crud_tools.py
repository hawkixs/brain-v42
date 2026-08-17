"""Point-of-use project scoping for generic CRUD MCP tools."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectScope,
    bind_dream_project_scope,
)
from brain_v42.models.decision import Decision
from brain_v42.models.indexed_plan import IndexedPlan
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

PROJECT_KEY = "sec1b-owned"


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


class UnusedResolver:
    async def references_belong_to_project(self, *_args: Any) -> bool:
        raise AssertionError("CRUD point-of-use scoping must not rerun middleware resolution")


def _scope(tool_name: str) -> DreamProjectScope:
    return DreamProjectScope(
        project_key=PROJECT_KEY,
        resolver=UnusedResolver(),
        audit=DreamProjectAudit(principal="dream-codex-synth", phase="synth"),
        tool_name=tool_name,
    )


def _services() -> dict[str, Any]:
    services: dict[str, Any] = {}
    for name in ("decision_svc", "learning_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        svc = MagicMock()
        svc.resolve_id_prefix = AsyncMock(return_value=[])
        svc.get_by_id = AsyncMock(return_value=None)
        svc.update = AsyncMock(return_value=None)
        svc.delete = AsyncMock(return_value=False)
        svc.list_all = AsyncMock(return_value=[])
        services[name] = svc
    services["snippet_svc"].list_snippets = AsyncMock(return_value=[])
    services["runbook_svc"].list_by_project = AsyncMock(return_value=[])
    return services


def _registered_tools(
    access_logger: MagicMock | None = None,
) -> tuple[dict[str, Any], dict[str, Any], MagicMock]:
    from brain_v42.mcp.tools.crud_tools import register_crud_tools

    mcp = MockMCP()
    services = _services()
    session = AsyncMock()
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=context)
    optional_logger = (
        {"access_logger": access_logger}
        if "access_logger" in inspect.signature(register_crud_tools).parameters
        else {}
    )
    register_crud_tools(
        mcp,
        **services,
        session_factory=session_factory,
        **optional_logger,
    )
    return mcp.registered, services, session_factory


@pytest.mark.asyncio
async def test_scoped_entity_get_forwards_authenticated_project() -> None:
    tools, services, _session_factory = _registered_tools()
    entity_id = uuid4()

    with bind_dream_project_scope(_scope("brain_get")):
        await tools["brain_get"]("decision", str(entity_id))

    services["decision_svc"].get_by_id.assert_awaited_once_with(
        entity_id,
        project_key=PROJECT_KEY,
    )


@pytest.mark.asyncio
async def test_scoped_entity_get_logs_only_after_owned_row_is_returned() -> None:
    access_logger = MagicMock()
    tools, services, _session_factory = _registered_tools(access_logger)
    entity_id = uuid4()
    now = datetime.now(UTC)
    services["decision_svc"].get_by_id.return_value = Decision(
        id=entity_id,
        title="Owned",
        description="desc",
        reasoning="reason",
        project_key=PROJECT_KEY,
        created_at=now,
        updated_at=now,
    )

    with bind_dream_project_scope(_scope("brain_get")):
        await tools["brain_get"]("decision", str(entity_id))

    access_logger.log_access.assert_called_once_with("decision", entity_id, "get_by_id")


@pytest.mark.asyncio
async def test_scoped_foreign_entity_get_does_not_log_access() -> None:
    access_logger = MagicMock()
    tools, _services_map, _session_factory = _registered_tools(access_logger)

    with bind_dream_project_scope(_scope("brain_get")):
        await tools["brain_get"]("decision", str(uuid4()))

    access_logger.log_access.assert_not_called()


@pytest.mark.asyncio
async def test_admin_entity_get_preserves_historical_call_shape() -> None:
    tools, services, _session_factory = _registered_tools()
    entity_id = uuid4()

    await tools["brain_get"]("decision", str(entity_id))

    services["decision_svc"].get_by_id.assert_awaited_once_with(entity_id)


@pytest.mark.asyncio
async def test_scoped_entity_update_forwards_authenticated_project() -> None:
    tools, services, _session_factory = _registered_tools()
    entity_id = uuid4()

    with bind_dream_project_scope(_scope("brain_update")):
        await tools["brain_update"]("decision", str(entity_id), {"title": "Scoped"})

    call = services["decision_svc"].update.await_args
    assert call is not None
    assert call.args[0] == entity_id
    assert call.kwargs == {"project_key": PROJECT_KEY}


@pytest.mark.asyncio
async def test_admin_entity_update_preserves_historical_call_shape() -> None:
    tools, services, _session_factory = _registered_tools()
    entity_id = uuid4()

    await tools["brain_update"]("decision", str(entity_id), {"title": "Admin"})

    call = services["decision_svc"].update.await_args
    assert call is not None
    assert call.args[0] == entity_id
    assert len(call.args) == 2
    assert call.kwargs == {}


@pytest.mark.asyncio
async def test_scoped_entity_delete_forwards_authenticated_project() -> None:
    tools, services, _session_factory = _registered_tools()
    entity_id = uuid4()

    with bind_dream_project_scope(_scope("brain_delete")):
        await tools["brain_delete"]("decision", str(entity_id))

    services["decision_svc"].delete.assert_awaited_once_with(
        entity_id,
        project_key=PROJECT_KEY,
    )


@pytest.mark.asyncio
async def test_admin_entity_delete_preserves_historical_call_shape() -> None:
    tools, services, _session_factory = _registered_tools()
    entity_id = uuid4()

    await tools["brain_delete"]("decision", str(entity_id))

    services["decision_svc"].delete.assert_awaited_once_with(entity_id)


@pytest.mark.asyncio
async def test_scoped_plan_get_forwards_authenticated_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _services_map, _session_factory = _registered_tools()
    plan_id = uuid4()
    repo = MagicMock()
    repo.get_with_chunks = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "brain_v42.repositories.pg_indexed_plan_repo.PgIndexedPlanRepo",
        MagicMock(return_value=repo),
    )

    with bind_dream_project_scope(_scope("brain_get")):
        await tools["brain_get"]("plan", str(plan_id))

    repo.get_with_chunks.assert_awaited_once_with(plan_id, project_key=PROJECT_KEY)


@pytest.mark.asyncio
async def test_scoped_plan_get_logs_only_after_owned_parent_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access_logger = MagicMock()
    tools, _services_map, _session_factory = _registered_tools(access_logger)
    plan_id = uuid4()
    now = datetime.now(UTC)
    plan = IndexedPlan(
        id=plan_id,
        file_path="docs/plans/cor1.md",
        title="COR1",
        plan_type="plan",
        project_key=PROJECT_KEY,
        content_hash="a" * 64,
        content="# COR1",
        status="active",
        chunk_count=0,
        word_count=1,
        freshness_status="fresh",
        indexed_at=now,
        created_at=now,
        updated_at=now,
    )
    repo = MagicMock()
    repo.get_with_chunks = AsyncMock(return_value=(plan, []))
    monkeypatch.setattr(
        "brain_v42.repositories.pg_indexed_plan_repo.PgIndexedPlanRepo",
        MagicMock(return_value=repo),
    )

    with bind_dream_project_scope(_scope("brain_get")):
        await tools["brain_get"]("plan", str(plan_id))

    repo.get_with_chunks.assert_awaited_once_with(plan_id, project_key=PROJECT_KEY)
    access_logger.log_access.assert_called_once_with("plan", plan_id, "get_by_id")


@pytest.mark.asyncio
async def test_admin_plan_get_preserves_historical_call_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _services_map, _session_factory = _registered_tools()
    plan_id = uuid4()
    repo = MagicMock()
    repo.get_with_chunks = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "brain_v42.repositories.pg_indexed_plan_repo.PgIndexedPlanRepo",
        MagicMock(return_value=repo),
    )

    await tools["brain_get"]("plan", str(plan_id))

    repo.get_with_chunks.assert_awaited_once_with(plan_id)


@pytest.mark.asyncio
async def test_scoped_plan_delete_forwards_authenticated_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _services_map, _session_factory = _registered_tools()
    plan_id = uuid4()
    repo = MagicMock()
    repo.delete = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "brain_v42.repositories.pg_indexed_plan_repo.PgIndexedPlanRepo",
        MagicMock(return_value=repo),
    )

    with bind_dream_project_scope(_scope("brain_delete")):
        await tools["brain_delete"]("plan", str(plan_id))

    repo.delete.assert_awaited_once_with(plan_id, project_key=PROJECT_KEY)


@pytest.mark.asyncio
async def test_admin_plan_delete_preserves_historical_call_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _services_map, _session_factory = _registered_tools()
    plan_id = uuid4()
    repo = MagicMock()
    repo.delete = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "brain_v42.repositories.pg_indexed_plan_repo.PgIndexedPlanRepo",
        MagicMock(return_value=repo),
    )

    await tools["brain_delete"]("plan", str(plan_id))

    repo.delete.assert_awaited_once_with(plan_id)


def test_project_scope_stays_out_of_public_crud_signatures() -> None:
    tools, _services_map, _session_factory = _registered_tools()

    for tool_name in ("brain_get", "brain_delete", "brain_update"):
        assert "project_key" not in inspect.signature(tools[tool_name]).parameters
