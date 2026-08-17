"""Unit tests for brain_learn, brain_validate_learning MCP tools.

brain_recall has been removed — use brain_search(types=["learning"]) instead.

Tests use AsyncMock to mock LearningService and verify that the MCP tool
functions call the correct service methods with correct arguments.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.models.learning import Learning
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_learning(
    topic: str = "TDD",
    insight: str = "Write tests first",
    project_key: str | None = "brain-v42",
) -> Learning:
    """Helper to create a Learning instance for testing."""
    return Learning(
        id=uuid4(),
        topic=topic,
        insight=insight,
        source=None,
        source_type="experience",
        confidence="medium",
        project_key=project_key,
        tags=[],
        metadata={},
        created_at=datetime(2026, 3, 1, 12, 0, 0),
        updated_at=datetime(2026, 3, 1, 12, 0, 0),
        validated_at=None,
        embedding=None,
    )


def _make_mcp_with_learning_tools() -> tuple[FastMCP, MagicMock]:
    """Create a test FastMCP instance with mocked learning_svc and other services."""
    mcp = FastMCP("test")
    mock_learning_svc = MagicMock()
    register_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=mock_learning_svc,
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
    )
    return mcp, mock_learning_svc


async def _get_tool_fn(mcp: FastMCP, name: str):
    """Get the underlying function of a registered MCP tool."""
    tool = await mcp.get_tool(name)
    return capture_tool_errors(tool.fn)


# ── brain_learn tests ──────────────────────────────────────────────────────────


class TestBrainLearn:
    """Tests for brain_learn MCP tool."""

    @pytest.mark.asyncio
    async def test_brain_learn_registered(self) -> None:
        """brain_learn tool is registered via register_tools()."""
        mcp, _ = _make_mcp_with_learning_tools()
        tool = await mcp.get_tool("brain_learn")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_brain_learn_calls_create_with_correct_args(self) -> None:
        """brain_learn calls learning_svc.create() with correct LearningCreate data."""
        from brain_v42.models.learning import LearningCreate  # noqa: PLC0415

        mcp, mock_svc = _make_mcp_with_learning_tools()
        learning = _make_learning()
        mock_svc.create = AsyncMock(return_value=learning)

        fn = await _get_tool_fn(mcp, "brain_learn")
        await fn(
            topic="TDD",
            insight="Write tests first",
            project_key="brain-v42",
        )

        mock_svc.create.assert_called_once()
        call_arg = mock_svc.create.call_args[0][0]
        assert isinstance(call_arg, LearningCreate)
        assert call_arg.topic == "TDD"
        assert call_arg.insight == "Write tests first"
        assert call_arg.project_key == "brain-v42"

    @pytest.mark.asyncio
    async def test_brain_learn_returns_confirmation_string(self) -> None:
        """brain_learn returns a confirmation string with topic and id."""
        mcp, mock_svc = _make_mcp_with_learning_tools()
        learning = _make_learning()
        mock_svc.create = AsyncMock(return_value=learning)

        fn = await _get_tool_fn(mcp, "brain_learn")
        result = await fn(topic="TDD", insight="Write tests first")

        assert isinstance(result, str)
        assert "Learned" in result
        assert str(learning.id) in result

    @pytest.mark.asyncio
    async def test_brain_learn_defaults_tags_to_empty_list(self) -> None:
        """brain_learn passes tags=[] when tags parameter is None."""
        mcp, mock_svc = _make_mcp_with_learning_tools()
        learning = _make_learning()
        mock_svc.create = AsyncMock(return_value=learning)

        fn = await _get_tool_fn(mcp, "brain_learn")
        await fn(topic="TDD", insight="Write tests first", tags=None)

        call_arg = mock_svc.create.call_args[0][0]
        assert call_arg.tags == []

    @pytest.mark.asyncio
    async def test_brain_learn_passes_optional_fields(self) -> None:
        """brain_learn passes source, source_type, and confidence to LearningCreate."""
        mcp, mock_svc = _make_mcp_with_learning_tools()
        learning = _make_learning()
        mock_svc.create = AsyncMock(return_value=learning)

        fn = await _get_tool_fn(mcp, "brain_learn")
        await fn(
            topic="TDD",
            insight="Write tests first",
            source="Team retrospective",
            source_type="conversation",
            confidence="high",
        )

        call_arg = mock_svc.create.call_args[0][0]
        assert call_arg.source == "Team retrospective"
        assert call_arg.source_type == "conversation"
        assert call_arg.confidence == "high"

    @pytest.mark.asyncio
    async def test_brain_learn_accepts_automated_source_type(self) -> None:
        """brain_learn accepts source_type='automated' (used by Dream Mode)."""
        mcp, mock_svc = _make_mcp_with_learning_tools()
        learning = _make_learning()
        mock_svc.create = AsyncMock(return_value=learning)

        fn = await _get_tool_fn(mcp, "brain_learn")
        result = await fn(
            topic="Dream Scan",
            insight="Automated report",
            source_type="automated",
        )

        assert "error" not in result.lower()
        mock_svc.create.assert_awaited_once()
        call_arg = mock_svc.create.call_args[0][0]
        assert call_arg.source_type == "automated"

    @pytest.mark.asyncio
    async def test_brain_learn_rejects_invalid_source_type(self) -> None:
        """brain_learn rejects invalid source_type values."""
        mcp, mock_svc = _make_mcp_with_learning_tools()

        async with Client(mcp) as client:
            with pytest.raises(ToolError) as exc_info:
                await client.call_tool(
                    "brain_learn",
                    {
                        "topic": "Test",
                        "insight": "Test insight",
                        "source_type": "invalid_type",
                    },
                )

        assert "source_type" in str(exc_info.value)
        assert "invalid_type" in str(exc_info.value)
        mock_svc.create.assert_not_called()


# ── brain_validate_learning tests ─────────────────────────────────────────────


class TestBrainValidateLearning:
    """Tests for brain_validate_learning MCP tool."""

    @pytest.mark.asyncio
    async def test_brain_validate_learning_registered(self) -> None:
        """brain_validate_learning tool is registered via register_tools()."""
        mcp, _ = _make_mcp_with_learning_tools()
        tool = await mcp.get_tool("brain_validate_learning")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_brain_validate_learning_calls_validate_with_uuid(self) -> None:
        """brain_validate_learning calls learning_svc.validate(UUID(learning_id))."""
        mcp, mock_svc = _make_mcp_with_learning_tools()
        learning = _make_learning()
        learning_id = str(learning.id)
        mock_svc.validate = AsyncMock(return_value=learning)

        fn = await _get_tool_fn(mcp, "brain_validate_learning")
        await fn(learning_id=learning_id)

        mock_svc.validate.assert_called_once_with(UUID(learning_id))

    @pytest.mark.asyncio
    async def test_brain_validate_learning_returns_confirmation_string(self) -> None:
        """brain_validate_learning returns a confirmation string on success."""
        mcp, mock_svc = _make_mcp_with_learning_tools()
        learning = _make_learning()
        validated_learning = learning.model_copy(
            update={"validated_at": datetime(2026, 3, 2, 10, 0, 0)}
        )
        mock_svc.validate = AsyncMock(return_value=validated_learning)

        fn = await _get_tool_fn(mcp, "brain_validate_learning")
        result = await fn(learning_id=str(learning.id))

        assert isinstance(result, str)
        assert "Learning validated" in result
        assert str(learning.id) in result

    @pytest.mark.asyncio
    async def test_brain_validate_learning_returns_error_when_not_found(self) -> None:
        """brain_validate_learning returns error string when validate() returns None."""
        mcp, mock_svc = _make_mcp_with_learning_tools()
        learning_id = str(uuid4())
        mock_svc.validate = AsyncMock(return_value=None)

        fn = await _get_tool_fn(mcp, "brain_validate_learning")
        result = await fn(learning_id=learning_id)

        assert isinstance(result, str)
        assert learning_id[:8] in result
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_brain_validate_learning_returns_string_type(self) -> None:
        """brain_validate_learning returns a string, not a dict."""
        mcp, mock_svc = _make_mcp_with_learning_tools()
        learning = _make_learning()
        mock_svc.validate = AsyncMock(return_value=learning)

        fn = await _get_tool_fn(mcp, "brain_validate_learning")
        result = await fn(learning_id=str(learning.id))

        assert isinstance(result, str)
        assert "Learning validated" in result
