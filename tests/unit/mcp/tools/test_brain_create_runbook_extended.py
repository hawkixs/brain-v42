"""Unit tests for the T7 extension of brain_create_runbook (Dream v3 Spec A).

Adds two optional kwargs — source_learning_id + dream_run_id — that route
through RunbookService.create_with_promotion. No auto_accept for runbooks
(no proposed/accepted state machine). IntegrityError translation mirrors T6.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from brain_v42.models.runbook import Runbook, RunbookStep
from tests.unit.mcp._tool_error_adapter import capture_tool_errors


def _make_runbook(**kwargs: Any) -> Runbook:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "title": "rb",
        "description": "d",
        "project_key": "brain-v42",
        "trigger": "t",
        "prerequisites": [],
        "steps": [RunbookStep(order=1, title="s")],
        "rollback_steps": [],
        "estimated_duration": None,
        "tags": [],
        "metadata": {},
        "execution_count": 0,
        "last_executed_at": None,
        "last_execution_status": None,
        "embedding": None,
        "created_at": datetime(2026, 4, 18),
        "updated_at": datetime(2026, 4, 18),
    }
    defaults.update(kwargs)
    return Runbook.model_validate(defaults)


class _MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


@pytest.fixture
def tools() -> tuple[dict[str, Any], MagicMock]:
    from brain_v42.mcp.tools.runbook_tools import register_runbook_tools

    svc = MagicMock()
    svc.create = AsyncMock(return_value=_make_runbook())
    svc.create_with_promotion = AsyncMock(return_value=_make_runbook())
    mcp = _MockMCP()
    register_runbook_tools(mcp, svc)
    return mcp.registered, svc


_SOURCE_ID = "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
async def test_brain_create_runbook_backcompat(
    tools: tuple[dict[str, Any], MagicMock],
) -> None:
    """No new kwargs → legacy create path."""
    registered, svc = tools
    result = await registered["brain_create_runbook"](
        title="T",
        description="d",
        project_key="brain-v42",
        trigger="t",
        steps=[{"title": "s"}],
    )
    assert "Runbook created" in result
    svc.create.assert_awaited_once()
    svc.create_with_promotion.assert_not_called()


@pytest.mark.asyncio
async def test_brain_create_runbook_happy_path_calls_create_with_promotion(
    tools: tuple[dict[str, Any], MagicMock],
) -> None:
    """source_learning_id set → routes to create_with_promotion."""
    registered, svc = tools
    result = await registered["brain_create_runbook"](
        title="T",
        description="d",
        project_key="brain-v42",
        trigger="t",
        steps=[{"title": "s"}],
        source_learning_id=_SOURCE_ID,
        dream_run_id=None,
    )
    svc.create_with_promotion.assert_awaited_once()
    kwargs = svc.create_with_promotion.await_args.kwargs
    assert str(kwargs["source_learning_id"]) == _SOURCE_ID
    svc.create.assert_not_called()
    assert "promoted" in result.lower() or "runbook" in result.lower()


@pytest.mark.asyncio
async def test_brain_create_runbook_translates_duplicate_source_integrity_error(
    tools: tuple[dict[str, Any], MagicMock],
) -> None:
    """IntegrityError (dup source) → typed error, not unhandled exception."""
    registered, svc = tools
    svc.create_with_promotion.side_effect = IntegrityError("stmt", {}, Exception("dup"))

    result = await registered["brain_create_runbook"](
        title="T",
        description="d",
        project_key="brain-v42",
        trigger="t",
        steps=[{"title": "s"}],
        source_learning_id=_SOURCE_ID,
    )
    assert "already" in result.lower() and "materialized" in result.lower()
