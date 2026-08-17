"""Unit tests for brain_assign_domain MCP tool.

TDD: Written BEFORE implementation — all tests must fail RED first.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from tests.unit.mcp._tool_error_adapter import capture_tool_errors


class MockMCP:
    """Collecting mock for FastMCP."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


class MockGraph:
    def __init__(self, upsert_result: str = "ok", link_result: str = "created") -> None:
        self.upsert_domain = AsyncMock(return_value=upsert_result)
        self.link_entity_to_domain = AsyncMock(return_value=link_result)
        self.find_orphans_for_classification = AsyncMock(return_value=[])


@pytest.fixture
def session() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def session_factory(session: AsyncMock) -> MagicMock:
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


@pytest.fixture
def mock_graph() -> MockGraph:
    return MockGraph()


@pytest.fixture
def tools(
    session_factory: MagicMock,
    mock_graph: MockGraph,
) -> dict[str, Any]:
    from brain_v42.mcp.tools.dream_tools import register_dream_tools

    mcp = MockMCP()
    register_dream_tools(
        mcp,
        session_factory=session_factory,
        auto_linker=None,
        graph_service=mock_graph,
    )
    return mcp.registered


def make_tools(
    session_factory: MagicMock,
    graph_service: Any,
) -> dict[str, Any]:
    from brain_v42.mcp.tools.dream_tools import register_dream_tools

    mcp = MockMCP()
    register_dream_tools(
        mcp,
        session_factory=session_factory,
        auto_linker=None,
        graph_service=graph_service,
    )
    return mcp.registered


class TestBrainAssignDomain:
    @pytest.mark.asyncio
    async def test_tool_is_registered(self, tools: dict[str, Any]) -> None:
        """brain_assign_domain is registered as an MCP tool."""
        assert "brain_assign_domain" in tools

    @pytest.mark.asyncio
    async def test_returns_created_on_new_edge(
        self,
        session_factory: MagicMock,
    ) -> None:
        """Valid UUID + valid domain, upsert='ok', link=created -> returns 'created'."""
        graph = MockGraph(upsert_result="ok", link_result="created")
        tools = make_tools(session_factory, graph)
        entity_id = str(uuid4())

        result = await tools["brain_assign_domain"](entity_id=entity_id, domain_name="infra")

        assert result == "created"
        graph.upsert_domain.assert_called_once_with("infra")
        graph.link_entity_to_domain.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_matched_on_existing_edge(
        self,
        session_factory: MagicMock,
    ) -> None:
        """When edge already exists, graph returns 'matched' -> tool returns 'matched'."""
        graph = MockGraph(upsert_result="ok", link_result="matched")
        tools = make_tools(session_factory, graph)
        entity_id = str(uuid4())

        result = await tools["brain_assign_domain"](entity_id=entity_id, domain_name="ml")

        assert result == "matched"

    @pytest.mark.asyncio
    async def test_returns_error_on_write_failure(
        self,
        session_factory: MagicMock,
    ) -> None:
        """When Neo4j write fails (link_entity_to_domain), graph returns 'error' -> tool returns 'error'."""
        graph = MockGraph(upsert_result="ok", link_result="error")
        tools = make_tools(session_factory, graph)
        entity_id = str(uuid4())

        result = await tools["brain_assign_domain"](entity_id=entity_id, domain_name="backend")

        assert result == "error"

    @pytest.mark.asyncio
    async def test_returns_invalid_domain_on_bad_name(
        self,
        session_factory: MagicMock,
    ) -> None:
        """When upsert_domain returns 'invalid_domain' (name not in ALLOWED_DOMAINS),
        link is NOT called and tool returns 'invalid_domain'.

        Contract change: upsert_domain now returns Literal['ok','invalid_domain','error']
        instead of bool. 'invalid_domain' is reserved for ALLOWED_DOMAINS validation
        failures (no write performed). The old bool contract mapped False to both
        invalid-name and neo4j-error cases, making them indistinguishable to the tool.
        """
        graph = MockGraph(upsert_result="invalid_domain", link_result="created")
        tools = make_tools(session_factory, graph)
        entity_id = str(uuid4())

        result = await tools["brain_assign_domain"](entity_id=entity_id, domain_name="not-a-domain")

        assert result == "invalid_domain"
        graph.upsert_domain.assert_called_once_with("not-a-domain")
        graph.link_entity_to_domain.assert_not_called()

    @pytest.mark.asyncio
    async def test_neo4j_down_on_upsert_returns_error_not_invalid_domain(
        self,
        session_factory: MagicMock,
    ) -> None:
        """BLOCKER: Neo4j is down during upsert_domain (valid domain) → tool returns 'error',
        NOT 'invalid_domain'.

        Before the fix, upsert_domain returned False on both invalid-name AND neo4j-error,
        so brain_assign_domain mapped both to 'invalid_domain'. The LLM agent would then
        incorrectly conclude the domain name itself is wrong and stop retrying.

        With the tri-state contract, upsert_domain returns:
          'ok'           — write succeeded
          'invalid_domain' — name not in ALLOWED_DOMAINS (no write, docstring contract)
          'error'        — _run failed (Neo4j down, timeout, etc.)

        brain_assign_domain must propagate 'error' unchanged instead of returning 'invalid_domain'.
        """
        graph = MockGraph(upsert_result="error", link_result="created")
        tools = make_tools(session_factory, graph)
        entity_id = str(uuid4())

        result = await tools["brain_assign_domain"](entity_id=entity_id, domain_name="infra")

        assert result == "error", (
            f"Expected 'error' when Neo4j is down (upsert_domain returns 'error'), got '{result}'. "
            "The old bool-False contract incorrectly returned 'invalid_domain' for both "
            "invalid names and neo4j failures."
        )
        graph.link_entity_to_domain.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_invalid_entity_id_on_bad_uuid(
        self,
        session_factory: MagicMock,
    ) -> None:
        """Non-UUID entity_id -> returns 'invalid_entity_id', no graph calls made."""
        graph = MockGraph(upsert_result=True, link_result="created")
        tools = make_tools(session_factory, graph)

        result = await tools["brain_assign_domain"](entity_id="not-a-uuid", domain_name="infra")

        assert result == "invalid_entity_id"
        graph.upsert_domain.assert_not_called()
        graph.link_entity_to_domain.assert_not_called()

    @pytest.mark.asyncio
    async def test_graph_unavailable_returns_format_error(
        self,
        session_factory: MagicMock,
    ) -> None:
        """When graph_service is None, returns a format_error string containing 'not configured'."""
        tools = make_tools(session_factory, graph_service=None)
        entity_id = str(uuid4())

        result = await tools["brain_assign_domain"](entity_id=entity_id, domain_name="infra")

        assert isinstance(result, str)
        assert "not configured" in result
