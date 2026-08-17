"""Point-of-use project scoping for the canonical merge delegation."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectScope,
    bind_dream_project_scope,
)
from brain_v42.services.consolidation import ConsolidationEntityNotFoundError
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
        raise AssertionError("merge point-of-use must not rerun middleware resolution")


class BombSessionFactory:
    def __call__(self) -> Any:
        raise AssertionError("the merge MCP handler must not own SQL")


def _scope() -> DreamProjectScope:
    return DreamProjectScope(
        project_key=PROJECT_KEY,
        resolver=UnusedResolver(),
        audit=DreamProjectAudit(principal="dream-codex-synth", phase="synth"),
        tool_name="brain_merge_entities",
    )


def _registered_merge(job: MagicMock) -> Any:
    from brain_v42.mcp.tools.decay_tools import register_decay_tools

    mcp = MockMCP()
    register_decay_tools(mcp, BombSessionFactory(), consolidation_job=job)
    return mcp.registered["brain_merge_entities"]


@pytest.mark.asyncio
async def test_scoped_merge_forwards_the_identical_capability_to_the_job() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job = MagicMock()
    job.merge = AsyncMock(return_value=None)
    merge = _registered_merge(job)
    scope = _scope()

    with bind_dream_project_scope(scope):
        result = await merge("decision", str(source_id), str(target_id))

    assert result.startswith("ok Merged")
    job.merge.assert_awaited_once_with(
        "decision",
        source_id,
        target_id,
        authorization=scope,
    )


@pytest.mark.asyncio
async def test_admin_merge_delegates_with_explicit_none_authorization() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job = MagicMock()
    job.merge = AsyncMock(return_value=None)
    merge = _registered_merge(job)

    result = await merge("decision", str(source_id), str(target_id))

    assert result.startswith("ok Merged")
    job.merge.assert_awaited_once_with(
        "decision",
        source_id,
        target_id,
        authorization=None,
    )


@pytest.mark.asyncio
async def test_merge_rejects_identical_ids_without_calling_the_job() -> None:
    entity_id = uuid4()
    job = MagicMock()
    job.merge = AsyncMock()
    merge = _registered_merge(job)

    with bind_dream_project_scope(_scope()):
        result = await merge("decision", str(entity_id), str(entity_id))

    assert result and result[0].isalnum()
    assert "different" in result.lower()
    job.merge.assert_not_awaited()


@pytest.mark.asyncio
async def test_scoped_missing_target_is_formatted_from_job_outcome() -> None:
    source_id = uuid4()
    target_id = uuid4()
    job = MagicMock()
    job.merge = AsyncMock(
        side_effect=ConsolidationEntityNotFoundError(f"Target decision {target_id} not found")
    )
    merge = _registered_merge(job)

    with bind_dream_project_scope(_scope()):
        result = await merge("decision", str(source_id), str(target_id))

    assert result and result[0].isalnum()
    assert f"Target decision {target_id} not found" in result
    job.merge.assert_awaited_once()
