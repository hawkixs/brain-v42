"""Unit tests for brain_propose_adr, brain_accept_adr, brain_list_adrs MCP tools.

Tests use AsyncMock to mock ADRService — no real DB or FastMCP server needed.
Tool functions are accessed via `await mcp.get_tool(name)` then `tool.fn(...)`.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastmcp import FastMCP

from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.models.adr import ADR, AlternativeConsidered
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_adr(**kwargs) -> ADR:
    """Helper to create an ADR instance for testing."""
    defaults = {
        "id": uuid4(),
        "number": 1,
        "title": "Use PostgreSQL",
        "context": "Need persistent store",
        "decision": "PostgreSQL + pgvector",
        "consequences": "Requires migrations",
        "alternatives_considered": [],
        "project_key": "brain-v42",
        "tags": [],
        "status": "proposed",
        "decided_at": None,
        "superseded_by": None,
        "embedding": None,
        "metadata": {},
        "created_at": datetime(2026, 3, 1, 12, 0, 0),
        "updated_at": datetime(2026, 3, 1, 12, 0, 0),
    }
    defaults.update(kwargs)
    return ADR.model_validate(defaults)


def _make_mcp_with_adr_tools() -> tuple[FastMCP, MagicMock]:
    """Create a test FastMCP instance with mocked adr_svc and other services."""
    mcp = FastMCP("test-brain")
    mock_adr_svc = MagicMock()
    register_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=mock_adr_svc,
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
    )
    return mcp, mock_adr_svc


async def _get_tool_fn(mcp: FastMCP, name: str):
    """Get the underlying function of a registered MCP tool."""
    tool = await mcp.get_tool(name)
    return capture_tool_errors(tool.fn)


# ── brain_propose_adr tests ────────────────────────────────────────────────────


class TestBrainProposeAdr:
    """Tests for brain_propose_adr MCP tool."""

    @pytest.mark.asyncio
    async def test_brain_propose_adr_registered(self) -> None:
        """brain_propose_adr tool is registered via register_tools()."""
        mcp, _ = _make_mcp_with_adr_tools()
        tool = await mcp.get_tool("brain_propose_adr")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_propose_adr_calls_service_create(self) -> None:
        """brain_propose_adr calls adr_svc.create() with correct ADRCreate data."""
        from brain_v42.models.adr import ADRCreate  # noqa: PLC0415

        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr()
        mock_svc.create = AsyncMock(return_value=adr)

        fn = await _get_tool_fn(mcp, "brain_propose_adr")
        await fn(
            title="Use PostgreSQL",
            context="Need persistent store",
            decision="PostgreSQL + pgvector",
            consequences="Requires migrations",
            project_key="brain-v42",
        )

        mock_svc.create.assert_called_once()
        call_arg = mock_svc.create.call_args[0][0]
        assert isinstance(call_arg, ADRCreate)
        assert call_arg.title == "Use PostgreSQL"
        assert call_arg.context == "Need persistent store"
        assert call_arg.decision == "PostgreSQL + pgvector"
        assert call_arg.consequences == "Requires migrations"
        assert call_arg.project_key == "brain-v42"

    @pytest.mark.asyncio
    async def test_propose_adr_returns_confirmation_string(self) -> None:
        """brain_propose_adr returns a confirmation string with ADR number and title."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr(number=3, title="Custom ADR")
        mock_svc.create = AsyncMock(return_value=adr)

        fn = await _get_tool_fn(mcp, "brain_propose_adr")
        result = await fn(
            title="Custom ADR",
            context="ctx",
            decision="dec",
            consequences="cons",
            project_key="proj",
        )

        assert isinstance(result, str)
        assert "ADR #3 proposed" in result

    @pytest.mark.asyncio
    async def test_propose_adr_returns_string_with_id(self) -> None:
        """brain_propose_adr returns string containing short id."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr()
        mock_svc.create = AsyncMock(return_value=adr)

        fn = await _get_tool_fn(mcp, "brain_propose_adr")
        result = await fn(
            title="Use PostgreSQL",
            context="ctx",
            decision="dec",
            consequences="cons",
            project_key="brain-v42",
        )

        assert isinstance(result, str)
        assert str(adr.id)[:8] in result

    @pytest.mark.asyncio
    async def test_propose_adr_converts_alternatives_to_pydantic_models(self) -> None:
        """brain_propose_adr converts alternatives_considered dicts to AlternativeConsidered."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr()
        mock_svc.create = AsyncMock(return_value=adr)

        alternatives = [
            {"title": "Neo4j", "description": "Graph DB", "reason_rejected": "Too heavy"}
        ]
        fn = await _get_tool_fn(mcp, "brain_propose_adr")
        await fn(
            title="ADR",
            context="ctx",
            decision="dec",
            consequences="cons",
            project_key="proj",
            alternatives_considered=alternatives,
        )

        call_arg = mock_svc.create.call_args[0][0]
        assert len(call_arg.alternatives_considered) == 1
        assert isinstance(call_arg.alternatives_considered[0], AlternativeConsidered)
        assert call_arg.alternatives_considered[0].title == "Neo4j"
        assert call_arg.alternatives_considered[0].reason_rejected == "Too heavy"

    @pytest.mark.asyncio
    async def test_propose_adr_defaults_alternatives_to_empty(self) -> None:
        """brain_propose_adr passes alternatives_considered=[] when parameter is None."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr()
        mock_svc.create = AsyncMock(return_value=adr)

        fn = await _get_tool_fn(mcp, "brain_propose_adr")
        await fn(
            title="ADR",
            context="ctx",
            decision="dec",
            consequences="cons",
            project_key="proj",
            alternatives_considered=None,
        )

        call_arg = mock_svc.create.call_args[0][0]
        assert call_arg.alternatives_considered == []

    @pytest.mark.asyncio
    async def test_propose_adr_passes_tags(self) -> None:
        """brain_propose_adr passes tags list to ADRCreate."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr()
        mock_svc.create = AsyncMock(return_value=adr)

        fn = await _get_tool_fn(mcp, "brain_propose_adr")
        await fn(
            title="ADR",
            context="ctx",
            decision="dec",
            consequences="cons",
            project_key="proj",
            tags=["database", "architecture"],
        )

        call_arg = mock_svc.create.call_args[0][0]
        assert call_arg.tags == ["database", "architecture"]

    @pytest.mark.asyncio
    async def test_propose_adr_defaults_tags_to_empty(self) -> None:
        """brain_propose_adr passes tags=[] when tags parameter is None."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr()
        mock_svc.create = AsyncMock(return_value=adr)

        fn = await _get_tool_fn(mcp, "brain_propose_adr")
        await fn(
            title="ADR",
            context="ctx",
            decision="dec",
            consequences="cons",
            project_key="proj",
            tags=None,
        )

        call_arg = mock_svc.create.call_args[0][0]
        assert call_arg.tags == []


# ── brain_accept_adr tests ─────────────────────────────────────────────────────


class TestBrainAcceptAdr:
    """Tests for brain_accept_adr MCP tool."""

    @pytest.mark.asyncio
    async def test_brain_accept_adr_registered(self) -> None:
        """brain_accept_adr tool is registered via register_tools()."""
        mcp, _ = _make_mcp_with_adr_tools()
        tool = await mcp.get_tool("brain_accept_adr")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_accept_adr_calls_service_accept_with_uuid(self) -> None:
        """brain_accept_adr calls adr_svc.accept(UUID(adr_id))."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr(status="accepted")
        mock_svc.accept = AsyncMock(return_value=adr)
        adr_id = str(adr.id)

        fn = await _get_tool_fn(mcp, "brain_accept_adr")
        await fn(adr_id=adr_id)

        mock_svc.accept.assert_called_once_with(UUID(adr_id))

    @pytest.mark.asyncio
    async def test_accept_adr_returns_confirmation_string(self) -> None:
        """brain_accept_adr returns a confirmation string on success."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr(status="accepted")
        mock_svc.accept = AsyncMock(return_value=adr)

        fn = await _get_tool_fn(mcp, "brain_accept_adr")
        result = await fn(adr_id=str(adr.id))

        assert isinstance(result, str)
        assert "ADR #1 accepted" in result

    @pytest.mark.asyncio
    async def test_accept_adr_returns_error_when_not_found(self) -> None:
        """brain_accept_adr returns error string when service returns None."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr_id = str(uuid4())
        mock_svc.accept = AsyncMock(return_value=None)

        fn = await _get_tool_fn(mcp, "brain_accept_adr")
        result = await fn(adr_id=adr_id)

        assert isinstance(result, str)
        assert adr_id[:8] in result
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_accept_adr_returns_string_with_id(self) -> None:
        """brain_accept_adr returns string containing the ADR short id."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr(status="accepted", decided_at=datetime(2026, 3, 2, 10, 0, 0))
        mock_svc.accept = AsyncMock(return_value=adr)

        fn = await _get_tool_fn(mcp, "brain_accept_adr")
        result = await fn(adr_id=str(adr.id))

        assert isinstance(result, str)
        assert str(adr.id)[:8] in result


# ── brain_list_adrs tests ──────────────────────────────────────────────────────


class TestBrainListAdrs:
    """Tests for brain_list_adrs MCP tool."""

    @pytest.mark.asyncio
    async def test_brain_list_adrs_registered(self) -> None:
        """brain_list_adrs tool is registered via register_tools()."""
        mcp, _ = _make_mcp_with_adr_tools()
        tool = await mcp.get_tool("brain_list_adrs")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_list_adrs_calls_service_list_all_with_filters(self) -> None:
        """brain_list_adrs calls adr_svc.list_all() with correct filters."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adrs = [_make_adr(), _make_adr(number=2)]
        mock_svc.list_all = AsyncMock(return_value=adrs)

        fn = await _get_tool_fn(mcp, "brain_list_adrs")
        await fn(project_key="brain-v42", status="proposed", limit=10)

        mock_svc.list_all.assert_called_once_with(
            project_key="brain-v42",
            status="proposed",
            limit=10,
            offset=0,
            include_archived=False,
        )

    @pytest.mark.asyncio
    async def test_list_adrs_returns_formatted_string(self) -> None:
        """brain_list_adrs returns a formatted markdown string with ADR details."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr(title="Decision X")
        mock_svc.list_all = AsyncMock(return_value=[adr])

        fn = await _get_tool_fn(mcp, "brain_list_adrs")
        result = await fn()

        assert isinstance(result, str)
        assert "1 ADR" in result
        assert "Decision X" in result

    @pytest.mark.asyncio
    async def test_list_adrs_returns_empty_result_string(self) -> None:
        """brain_list_adrs returns '0 ADRs found' when service returns empty list."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        mock_svc.list_all = AsyncMock(return_value=[])

        fn = await _get_tool_fn(mcp, "brain_list_adrs")
        result = await fn()

        assert isinstance(result, str)
        assert "0 ADRs found" in result

    @pytest.mark.asyncio
    async def test_list_adrs_default_limit_is_20(self) -> None:
        """brain_list_adrs uses limit=20 as default."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        mock_svc.list_all = AsyncMock(return_value=[])

        fn = await _get_tool_fn(mcp, "brain_list_adrs")
        await fn()

        call_kwargs = mock_svc.list_all.call_args.kwargs
        assert call_kwargs.get("limit") == 20

    @pytest.mark.asyncio
    async def test_list_adrs_returns_string_with_format_id(self) -> None:
        """brain_list_adrs returns string containing short id."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr()
        mock_svc.list_all = AsyncMock(return_value=[adr])

        fn = await _get_tool_fn(mcp, "brain_list_adrs")
        result = await fn()

        assert isinstance(result, str)
        assert str(adr.id)[:8] in result

    @pytest.mark.asyncio
    async def test_list_adrs_with_no_filters_passes_none(self) -> None:
        """brain_list_adrs passes project_key=None and status=None when not specified."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        mock_svc.list_all = AsyncMock(return_value=[])

        fn = await _get_tool_fn(mcp, "brain_list_adrs")
        await fn()

        mock_svc.list_all.assert_called_once_with(
            project_key=None,
            status=None,
            limit=20,
            offset=0,
            include_archived=False,
        )

    @pytest.mark.asyncio
    async def test_list_adrs_passes_offset(self) -> None:
        """brain_list_adrs passes offset parameter to adr_svc.list_all()."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        mock_svc.list_all = AsyncMock(return_value=[])

        fn = await _get_tool_fn(mcp, "brain_list_adrs")
        await fn(offset=5)

        mock_svc.list_all.assert_called_once_with(
            project_key=None,
            status=None,
            limit=20,
            offset=5,
            include_archived=False,
        )


# ── brain_deprecate_adr tests ────────────────────────────────────────────────


class TestBrainDeprecateAdr:
    """Tests for brain_deprecate_adr MCP tool."""

    @pytest.mark.asyncio
    async def test_brain_deprecate_adr_registered(self) -> None:
        """brain_deprecate_adr tool is registered via register_tools()."""
        mcp, _ = _make_mcp_with_adr_tools()
        tool = await mcp.get_tool("brain_deprecate_adr")
        assert tool is not None

    @pytest.mark.asyncio
    async def test_deprecate_adr_calls_service_with_uuid_and_reason(self) -> None:
        """brain_deprecate_adr calls adr_svc.deprecate(UUID, reason=...)."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr(status="deprecated")
        mock_svc.deprecate = AsyncMock(return_value=adr)
        adr_id = str(adr.id)

        fn = await _get_tool_fn(mcp, "brain_deprecate_adr")
        await fn(adr_id=adr_id, reason="No longer relevant")

        mock_svc.deprecate.assert_called_once_with(UUID(adr_id), reason="No longer relevant")

    @pytest.mark.asyncio
    async def test_deprecate_adr_returns_confirmation_string(self) -> None:
        """brain_deprecate_adr returns confirmation with ADR number and title."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr(number=5, title="Old Decision", status="deprecated")
        mock_svc.deprecate = AsyncMock(return_value=adr)

        fn = await _get_tool_fn(mcp, "brain_deprecate_adr")
        result = await fn(adr_id=str(adr.id))

        assert isinstance(result, str)
        assert "ADR #5 deprecated" in result

    @pytest.mark.asyncio
    async def test_deprecate_adr_returns_error_when_not_found(self) -> None:
        """brain_deprecate_adr returns error string when service returns None."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr_id = str(uuid4())
        mock_svc.deprecate = AsyncMock(return_value=None)

        fn = await _get_tool_fn(mcp, "brain_deprecate_adr")
        result = await fn(adr_id=adr_id)

        assert isinstance(result, str)
        assert adr_id[:8] in result
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_deprecate_adr_reason_defaults_to_none(self) -> None:
        """brain_deprecate_adr passes reason=None when not specified."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr(status="deprecated")
        mock_svc.deprecate = AsyncMock(return_value=adr)

        fn = await _get_tool_fn(mcp, "brain_deprecate_adr")
        await fn(adr_id=str(adr.id))

        mock_svc.deprecate.assert_called_once_with(UUID(str(adr.id)), reason=None)

    @pytest.mark.asyncio
    async def test_deprecate_adr_returns_string_with_id(self) -> None:
        """brain_deprecate_adr returns string containing short id."""
        mcp, mock_svc = _make_mcp_with_adr_tools()
        adr = _make_adr(status="deprecated")
        mock_svc.deprecate = AsyncMock(return_value=adr)

        fn = await _get_tool_fn(mcp, "brain_deprecate_adr")
        result = await fn(adr_id=str(adr.id))

        assert isinstance(result, str)
        assert str(adr.id)[:8] in result
