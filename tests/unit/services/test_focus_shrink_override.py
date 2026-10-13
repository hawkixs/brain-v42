"""A destructive replacement needs a verified elevation before any mutation."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.focus_slot import FocusSlotError
from brain_v42.provenance import (
    set_current_principal,
    set_current_transport,
)
from brain_v42.services.brain_session_service import BrainSessionService
from brain_v42.services.roadmap_service import RoadmapService
from tests.unit.mcp._tool_error_adapter import capture_tool_errors


class MockMCP:
    """Collect functions without bypassing their error handling."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorate(function: Any) -> Any:
            self.registered[function.__name__] = capture_tool_errors(function)
            return function

        return decorate


@pytest.mark.parametrize("writer", ["end", "update", "tool"])
@pytest.mark.parametrize("identity", ["absent", "not_elevated", "checker_failure", "no_connection"])
async def test_override_is_refused_before_any_repository_call(
    writer: str, identity: str, caller: AsyncMock
) -> None:
    if identity != "absent":
        set_current_principal("workstation-claude")
    if identity == "checker_failure":
        caller.side_effect = RuntimeError("private repository failure")
    if identity == "no_connection":
        set_current_transport(None)
    repo = MagicMock()
    repo.end = AsyncMock()
    repo.absorb_derived_capture_outcome = AsyncMock()
    factory = MagicMock()
    if writer == "tool":
        from brain_v42.mcp.tools.project_context_tools import register_project_context_tools

        mcp = MockMCP()
        coordinator = AsyncMock()
        register_project_context_tools(mcp, AsyncMock(), coordinator)
        result = await mcp.registered["brain_update_project_focus"](
            "brain-v42", "short", 0, allow_focus_shrink=True
        )
        assert "focus_shrink_override_refused" in result
        assert "/brain-admin" in result and "operator relay" in result
        assert "private repository failure" not in result
        coordinator.update_project_focus.assert_not_awaited()
    else:
        with pytest.raises(FocusSlotError, match="^focus_shrink_override_refused: ") as refused:
            if writer == "end":
                await BrainSessionService(repo).end(
                    uuid4(), "operator", "summary", "short", 0, allow_focus_shrink=True
                )
            else:
                await RoadmapService(factory).update_project_focus(
                    "brain-v42", "short", expected_focus_revision=0, allow_focus_shrink=True
                )
        assert "/brain-admin" in str(refused.value) and "operator relay" in str(refused.value)
        assert "private repository failure" not in str(refused.value)
    repo.end.assert_not_awaited()
    repo.absorb_derived_capture_outcome.assert_not_awaited()
    factory.assert_not_called()
    if identity in {"absent", "no_connection"}:
        caller.assert_not_awaited()


async def test_elevated_end_passes_the_override(caller: AsyncMock) -> None:
    set_current_principal("workstation-claude")
    caller.return_value = True
    repo = MagicMock()
    repo.end = AsyncMock()
    repo.absorb_derived_capture_outcome = AsyncMock(return_value=None)
    await BrainSessionService(repo).end(
        uuid4(), "operator", "summary", "short", 0, allow_focus_shrink=True
    )
    assert repo.end.await_args.kwargs["allow_focus_shrink"] is True
    connection, principal, now = caller.await_args.args
    assert (connection, principal) == ("a" * 32, "workstation-claude")
    assert now.tzinfo is UTC and now <= datetime.now(UTC)


async def test_elevated_update_tool_passes_the_override(caller: AsyncMock) -> None:
    from brain_v42.mcp.tools.project_context_tools import register_project_context_tools

    set_current_principal("workstation-claude")
    caller.return_value = True
    coordinator = AsyncMock()
    mcp = MockMCP()
    register_project_context_tools(mcp, AsyncMock(), coordinator)
    await mcp.registered["brain_update_project_focus"](
        "brain-v42", "short", 0, allow_focus_shrink=True
    )
    assert coordinator.update_project_focus.await_args.kwargs["allow_focus_shrink"] is True


async def test_set_context_reports_the_focus_write_refusal() -> None:
    from brain_v42.mcp.tools.project_context_tools import register_project_context_tools

    context_service = AsyncMock()
    context_service.get_or_create.side_effect = FocusSlotError(
        "focus_write_requires_update_project_focus",
        "Use brain_update_project_focus with its CAS and shrink guard",
    )
    mcp = MockMCP()
    register_project_context_tools(mcp, context_service)
    result = await mcp.registered["brain_set_project_context"](
        project_key="brain-v42", name="Brain", description="Memory", current_focus="short"
    )
    assert "focus_write_requires_update_project_focus" in result
    assert "brain_update_project_focus" in result
    assert "CAS" in result
