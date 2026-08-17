"""Read-path guard: read/lookup tools canonicalize project_key at the boundary.

Regression for the exact incident in learning 7bc821a1: a session called
``brain_session_start("brain_v42")`` (underscore) and got "no project context
found" because the underscore key never matched the canonical ``brain-v42``
data. After the fix, the underscore key is canonicalized before it reaches the
services, so the right project is found.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.brain_session import (
    BrainSession,
    BrainSessionStartResult,
    BrainSessionStatus,
)


class MockMCP:
    """Collecting mock for FastMCP (same pattern as the dream-tools tests)."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


def _session_services() -> dict[str, Any]:
    pc = AsyncMock()
    pc.get_by_key = AsyncMock(return_value=None)
    dec = AsyncMock()
    dec.list_all = AsyncMock(return_value=[])
    learn = AsyncMock()
    learn.list_all = AsyncMock(return_value=[])
    dream = AsyncMock()
    dream.killswitch_state = AsyncMock(return_value=None)
    dream.last_failure = AsyncMock(return_value=None)
    feat = AsyncMock()
    feat.roadmap_alive = AsyncMock(return_value=[])
    feat.stale_pinned = AsyncMock(return_value=[])
    now = datetime.now(UTC)
    brain = MagicMock()
    brain.start = AsyncMock(
        return_value=BrainSessionStartResult(
            session=BrainSession(
                id=uuid4(),
                project_key="brain-v42",
                client_key="test-task",
                status=BrainSessionStatus.OPEN,
                started_focus=None,
                started_focus_revision=0,
                started_at=now,
                updated_at=now,
            ),
            replayed=False,
            open_session_count=1,
        )
    )
    return {
        "pc": pc,
        "dec": dec,
        "learn": learn,
        "dream": dream,
        "feat": feat,
        "brain": brain,
    }


def _register_session(svcs: dict[str, Any]) -> dict[str, Any]:
    from brain_v42.mcp.tools.session_tools import register_session_tools

    mcp = MockMCP()
    register_session_tools(
        mcp,
        svcs["pc"],
        svcs["dec"],
        svcs["learn"],
        svcs["dream"],
        svcs["feat"],
        svcs["brain"],
    )
    return mcp.registered


class TestSessionStartCanonicalizes:
    @pytest.mark.asyncio
    async def test_underscore_key_reaches_services_as_hyphen(self) -> None:
        svcs = _session_services()
        tools = _register_session(svcs)

        await tools["brain_session_start"]("brain_v42", "test-task")

        svcs["brain"].start.assert_awaited_once_with(
            project_key="brain_v42",
            client_key="test-task",
        )
        svcs["pc"].get_by_key.assert_awaited_with("brain-v42")
        svcs["dec"].list_all.assert_awaited_with(project_key="brain-v42", limit=3)
        svcs["learn"].list_all.assert_awaited_with(project_key="brain-v42", limit=3)
        svcs["feat"].roadmap_alive.assert_awaited_with(project_key="brain-v42", limit=5)

    @pytest.mark.asyncio
    async def test_canonical_key_passes_through(self) -> None:
        svcs = _session_services()
        tools = _register_session(svcs)

        await tools["brain_session_start"]("brain-v42", "test-task")

        svcs["pc"].get_by_key.assert_awaited_with("brain-v42")
