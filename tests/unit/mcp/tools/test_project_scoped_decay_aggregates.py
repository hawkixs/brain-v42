"""SEC1b project scoping for decay aggregate MCP tools."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.mcp.tools import decay_tools

PROJECT_KEY = "sec1b-owned"


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


def _registered_tools(
    *,
    consolidation_job: Any | None = None,
) -> tuple[dict[str, Any], AsyncMock, MagicMock]:
    session = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.all.return_value = []
    result.scalar_one.return_value = 0
    session.execute = AsyncMock(return_value=result)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=context)
    mcp = MockMCP()
    decay_tools.register_decay_tools(
        mcp,
        session_factory,
        consolidation_job=consolidation_job,
    )
    return mcp.registered, session, session_factory


def _where_sql(statement: Any) -> str:
    rendered = str(statement)
    return rendered.split("WHERE", maxsplit=1)[1] if "WHERE" in rendered else ""


@pytest.mark.asyncio
async def test_scoped_decay_status_filters_all_twelve_aggregate_queries_at_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, session, _factory = _registered_tools()
    scope = SimpleNamespace(project_key=PROJECT_KEY)
    get_scope = MagicMock(return_value=scope)
    monkeypatch.setattr(decay_tools, "get_dream_project_scope", get_scope)

    await tools["brain_decay_status"]()

    statements = [call.args[0] for call in session.execute.await_args_list]
    assert len(statements) == 12
    assert all("project_key" in _where_sql(statement) for statement in statements)
    assert get_scope.call_count == 1


@pytest.mark.asyncio
async def test_admin_decay_status_preserves_global_aggregate_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, session, _factory = _registered_tools()
    get_scope = MagicMock(return_value=None)
    monkeypatch.setattr(decay_tools, "get_dream_project_scope", get_scope)

    await tools["brain_decay_status"]()

    statements = [call.args[0] for call in session.execute.await_args_list]
    assert len(statements) == 12
    assert all("project_key" not in _where_sql(statement) for statement in statements)
    assert get_scope.call_count == 1


@pytest.mark.asyncio
async def test_scoped_consolidation_tool_forwards_authenticated_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = MagicMock()
    job.find_candidates = AsyncMock(return_value=[])
    tools, _session, _factory = _registered_tools(consolidation_job=job)
    monkeypatch.setattr(
        decay_tools,
        "get_dream_project_scope",
        MagicMock(return_value=SimpleNamespace(project_key=PROJECT_KEY)),
    )

    await tools["brain_consolidation_candidates"](entity_type="decision", limit=7)

    job.find_candidates.assert_awaited_once_with(
        entity_type="decision",
        limit=7,
        project_key=PROJECT_KEY,
    )


@pytest.mark.asyncio
async def test_admin_consolidation_tool_preserves_historical_call_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = MagicMock()
    job.find_candidates = AsyncMock(return_value=[])
    tools, _session, _factory = _registered_tools(consolidation_job=job)
    monkeypatch.setattr(decay_tools, "get_dream_project_scope", MagicMock(return_value=None))

    await tools["brain_consolidation_candidates"](entity_type="decision", limit=7)

    job.find_candidates.assert_awaited_once_with(entity_type="decision", limit=7)


def test_decay_public_signatures_and_catalog_are_unchanged() -> None:
    tools, _session, _factory = _registered_tools(consolidation_job=AsyncMock())

    assert set(tools) == {
        "brain_decay_status",
        "brain_refresh_entity",
        "brain_consolidation_candidates",
        "brain_merge_entities",
    }
    assert tuple(inspect.signature(tools["brain_decay_status"]).parameters) == ()
    assert tuple(inspect.signature(tools["brain_consolidation_candidates"]).parameters) == (
        "entity_type",
        "limit",
    )
    assert (
        "project_key" not in inspect.signature(tools["brain_consolidation_candidates"]).parameters
    )
