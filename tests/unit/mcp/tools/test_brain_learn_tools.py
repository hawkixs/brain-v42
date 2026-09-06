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
from brain_v42.repositories.pg_graph_ledger import UnknownGraphEndpoint
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.services.learning_service import LearningService
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
    async def test_brain_learn_returns_confirmation_without_warnings_key_by_default(self) -> None:
        """No `warnings:` noise in the common case — nothing degraded."""
        mcp, mock_svc = _make_mcp_with_learning_tools()
        learning = _make_learning()
        mock_svc.create = AsyncMock(return_value=learning)

        fn = await _get_tool_fn(mcp, "brain_learn")
        result = await fn(topic="TDD", insight="Write tests first")

        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_brain_learn_surfaces_graph_warnings_in_confirmation(self) -> None:
        """Regression for the 2026-09-06 incident: a degraded related_to
        relation (endpoint not yet registered in the graph ledger) must
        reach the agent as a visible warning, not a silent success and not
        a raised tool error — the row is already committed by this point."""
        mcp, mock_svc = _make_mcp_with_learning_tools()
        learning = _make_learning().model_copy(
            update={
                "graph_warnings": [
                    "relation RELATED_TO to 11111111-1111-1111-1111-111111111111 "
                    "was not staged (missing_node)"
                ]
            }
        )
        mock_svc.create = AsyncMock(return_value=learning)

        fn = await _get_tool_fn(mcp, "brain_learn")
        result = await fn(
            topic="TDD",
            insight="Write tests first",
            related_to=[{"id": "11111111-1111-1111-1111-111111111111", "type": "RELATED_TO"}],
        )

        assert isinstance(result, str)
        assert "Learned" in result
        assert str(learning.id) in result
        assert "warnings" in result
        assert "11111111-1111-1111-1111-111111111111" in result

    @pytest.mark.asyncio
    async def test_brain_learn_confirmation_renders_the_documented_unknown_endpoint_marker(
        self,
    ) -> None:
        """End-to-end proof for docs/MCP_TOOLS.md's brain_learn example: the
        marker is rendered by driving a REAL ``LearningService`` through
        ``graph_upsert_entity``'s fail-soft ``UnknownGraphEndpoint`` path and
        the real ``format_confirmation`` — not a hand-typed fixture string
        that can silently drift from what the code actually produces."""
        mcp = FastMCP("test")
        mock_repo = MagicMock(spec=PgLearningRepo)
        learning = _make_learning()
        mock_repo.create = AsyncMock(return_value=learning)
        mock_graph = MagicMock()
        mock_graph.requires_durable_write_success = True
        mock_graph.upsert_node = AsyncMock(return_value="ok")
        mock_graph.link_to_project = AsyncMock(return_value="ok")
        mock_graph.create_relation = AsyncMock(
            side_effect=UnknownGraphEndpoint("one or more UUID endpoints are not registered")
        )
        real_learning_svc = LearningService(pg_repo=mock_repo, graph=mock_graph)
        register_tools(
            mcp,
            decision_svc=MagicMock(),
            learning_svc=real_learning_svc,
            snippet_svc=MagicMock(),
            runbook_svc=MagicMock(),
            adr_svc=MagicMock(),
            project_context_svc=MagicMock(),
            brain_svc=MagicMock(),
        )
        fn = await _get_tool_fn(mcp, "brain_learn")
        related_uuid = "11111111-1111-1111-1111-111111111111"

        result = await fn(
            topic="TDD",
            insight="Write tests first",
            project_key="brain-v42",
            related_to=[{"id": related_uuid, "type": "RELATED_TO"}],
        )

        assert result == (
            f"ok Learned (id:{learning.id}, "
            f"warnings:relation RELATED_TO to {related_uuid} was not staged (unknown_endpoint))"
        )

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
