"""Failure-first MCP boundary tests for SEC1b promotions."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectAuthorizationError,
    DreamProjectScope,
    authorize_dream_project_request,
    bind_dream_project_scope,
)
from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.models.adr import ADR
from brain_v42.models.runbook import Runbook, RunbookStep
from brain_v42.repositories.pg_adr import SourceLearningNotFound
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

PROJECT_KEY = "sec1b-owned"
SOURCE_ID = "11111111-1111-1111-1111-111111111111"


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


class ResolverMustNotRun:
    async def references_belong_to_project(self, *_args: Any) -> bool:
        raise AssertionError("resolver must not run in point-of-use or dream_run denial tests")


def scope(tool_name: str) -> DreamProjectScope:
    return DreamProjectScope(
        project_key=PROJECT_KEY,
        resolver=ResolverMustNotRun(),
        audit=DreamProjectAudit(principal="dream-codex-synth", phase="synth"),
        tool_name=tool_name,
    )


def adr() -> ADR:
    now = datetime.now(UTC)
    return ADR.model_validate(
        {
            "id": uuid4(),
            "number": 8,
            "title": "Scoped ADR",
            "context": "Context",
            "decision": "Decision",
            "consequences": "Consequences",
            "alternatives_considered": [],
            "project_key": PROJECT_KEY,
            "tags": [],
            "status": "accepted",
            "decided_at": now,
            "superseded_by": None,
            "embedding": None,
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        }
    )


def runbook() -> Runbook:
    now = datetime.now(UTC)
    return Runbook.model_validate(
        {
            "id": uuid4(),
            "title": "Scoped runbook",
            "description": "Description",
            "project_key": PROJECT_KEY,
            "trigger": "Trigger",
            "prerequisites": [],
            "steps": [RunbookStep(order=1, title="Step")],
            "rollback_steps": [],
            "estimated_duration": None,
            "tags": [],
            "metadata": {},
            "execution_count": 0,
            "last_executed_at": None,
            "last_execution_status": None,
            "embedding": None,
            "created_at": now,
            "updated_at": now,
        }
    )


def registered_tools() -> tuple[dict[str, Any], MagicMock, MagicMock]:
    mcp = MockMCP()
    adr_svc = MagicMock()
    adr_svc.create_with_promotion = AsyncMock(return_value=adr())
    runbook_svc = MagicMock()
    runbook_svc.create_with_promotion = AsyncMock(return_value=runbook())
    register_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=runbook_svc,
        adr_svc=adr_svc,
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
    )
    return mcp.registered, adr_svc, runbook_svc


def adr_args(*, dream_run_id: int | None = None) -> dict[str, Any]:
    return {
        "title": "Scoped ADR",
        "context": "Context",
        "decision": "Decision",
        "consequences": "Consequences",
        "project_key": PROJECT_KEY,
        "source_learning_id": SOURCE_ID,
        "auto_accept": True,
        "dream_run_id": dream_run_id,
    }


def runbook_args(*, dream_run_id: int | None = None) -> dict[str, Any]:
    return {
        "title": "Scoped runbook",
        "description": "Description",
        "project_key": PROJECT_KEY,
        "trigger": "Trigger",
        "steps": [{"title": "Step"}],
        "source_learning_id": SOURCE_ID,
        "dream_run_id": dream_run_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["brain_propose_adr", "brain_create_runbook"])
async def test_scoped_tool_passes_context_project_to_promotion_service(tool_name: str) -> None:
    tools, adr_svc, runbook_svc = registered_tools()

    with bind_dream_project_scope(scope(tool_name)):
        await tools[tool_name](
            **(adr_args() if tool_name == "brain_propose_adr" else runbook_args())
        )

    service = adr_svc if tool_name == "brain_propose_adr" else runbook_svc
    assert service.create_with_promotion.await_args.kwargs["project_key"] == PROJECT_KEY


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["brain_propose_adr", "brain_create_runbook"])
async def test_admin_tool_omits_scope_kwarg_and_preserves_dream_run(tool_name: str) -> None:
    tools, adr_svc, runbook_svc = registered_tools()

    await tools[tool_name](
        **(
            adr_args(dream_run_id=73)
            if tool_name == "brain_propose_adr"
            else runbook_args(dream_run_id=73)
        )
    )

    service = adr_svc if tool_name == "brain_propose_adr" else runbook_svc
    kwargs = service.create_with_promotion.await_args.kwargs
    assert "project_key" not in kwargs
    assert kwargs["dream_run_id"] == 73


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["brain_propose_adr", "brain_create_runbook"])
async def test_scoped_unavailable_source_returns_same_non_enumerating_error(
    tool_name: str,
) -> None:
    tools, adr_svc, runbook_svc = registered_tools()
    service = adr_svc if tool_name == "brain_propose_adr" else runbook_svc
    service.create_with_promotion.side_effect = SourceLearningNotFound("source learning not found")

    with bind_dream_project_scope(scope(tool_name)):
        output = await tools[tool_name](
            **(adr_args() if tool_name == "brain_propose_adr" else runbook_args())
        )

    assert "not found" in output.lower()
    assert SOURCE_ID not in output
    assert PROJECT_KEY not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["brain_propose_adr", "brain_create_runbook"])
async def test_admin_keeps_historical_source_exception(tool_name: str) -> None:
    tools, adr_svc, runbook_svc = registered_tools()
    service = adr_svc if tool_name == "brain_propose_adr" else runbook_svc
    service.create_with_promotion.side_effect = SourceLearningNotFound("historical")

    with pytest.raises(SourceLearningNotFound, match="historical"):
        await tools[tool_name](
            **(adr_args() if tool_name == "brain_propose_adr" else runbook_args())
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["brain_propose_adr", "brain_create_runbook"])
async def test_dream_run_id_remains_denied_before_handler(tool_name: str) -> None:
    with pytest.raises(DreamProjectAuthorizationError) as raised:
        await authorize_dream_project_request(
            tool_name=tool_name,
            arguments={
                "project_key": PROJECT_KEY,
                "source_learning_id": SOURCE_ID,
                "dream_run_id": 73,
            },
            project_key=PROJECT_KEY,
            resolver=ResolverMustNotRun(),
            audit=DreamProjectAudit(principal="dream-codex-synth", phase="synth"),
        )
    assert raised.value.reason == "dream_run_forbidden"


def test_public_tool_signatures_have_no_internal_scope_parameter() -> None:
    tools, _adr_svc, _runbook_svc = registered_tools()
    assert tuple(inspect.signature(tools["brain_propose_adr"]).parameters) == (
        "title",
        "context",
        "decision",
        "consequences",
        "project_key",
        "alternatives_considered",
        "tags",
        "source_learning_id",
        "auto_accept",
        "dream_run_id",
    )
    assert tuple(inspect.signature(tools["brain_create_runbook"]).parameters) == (
        "title",
        "description",
        "project_key",
        "trigger",
        "steps",
        "prerequisites",
        "rollback_steps",
        "estimated_duration",
        "tags",
        "source_learning_id",
        "dream_run_id",
    )
