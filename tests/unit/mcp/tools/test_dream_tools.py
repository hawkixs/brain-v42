"""Unit tests for dream MCP tools: brain_backfill_links_batch."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.services.link_result import LinkJobResult
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
def mock_graph() -> AsyncMock:
    g = AsyncMock()
    g.find_unlinked_nodes = AsyncMock(return_value=[])
    return g


@pytest.fixture
def mock_auto_linker() -> AsyncMock:
    linker = AsyncMock()
    linker.auto_link = AsyncMock(return_value=LinkJobResult())
    return linker


@pytest.fixture
def tools(
    session_factory: MagicMock,
    mock_auto_linker: AsyncMock,
    mock_graph: AsyncMock,
) -> dict[str, Any]:
    from brain_v42.mcp.tools.dream_tools import register_dream_tools

    mcp = MockMCP()
    register_dream_tools(
        mcp,
        session_factory=session_factory,
        auto_linker=mock_auto_linker,
        graph_service=mock_graph,
    )
    return mcp.registered


class TestBrainBackfillLinksBatch:
    @pytest.mark.asyncio
    async def test_tool_is_registered(self, tools: dict[str, Any]) -> None:
        """brain_backfill_links_batch is registered as an MCP tool."""
        assert "brain_backfill_links_batch" in tools

    @pytest.mark.asyncio
    async def test_returns_error_when_graph_service_none(
        self,
        session_factory: MagicMock,
        mock_auto_linker: AsyncMock,
    ) -> None:
        """Returns error string containing 'not configured' when graph_service is None."""
        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=None,
        )
        result = await mcp.registered["brain_backfill_links_batch"]()
        assert isinstance(result, str)
        assert "not configured" in result
        assert result and result[0].isalnum()

    @pytest.mark.asyncio
    async def test_returns_error_when_auto_linker_none(
        self,
        session_factory: MagicMock,
        mock_graph: AsyncMock,
    ) -> None:
        """Returns error string containing 'not configured' when auto_linker is None."""
        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=None,
            graph_service=mock_graph,
        )
        result = await mcp.registered["brain_backfill_links_batch"]()
        assert isinstance(result, str)
        assert "not configured" in result
        assert result and result[0].isalnum()

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_unlinked_entities(
        self,
        tools: dict[str, Any],
        mock_graph: AsyncMock,
    ) -> None:
        """Returns summary showing 0 processed when no unlinked nodes found."""
        mock_graph.find_unlinked_nodes.return_value = []

        result = await tools["brain_backfill_links_batch"]()

        assert isinstance(result, str)
        assert "0" in result

    @pytest.mark.asyncio
    async def test_calls_auto_link_for_each_unlinked_entity(
        self,
        session_factory: MagicMock,
        mock_graph: AsyncMock,
        mock_auto_linker: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """auto_link is called for each unlinked entity that has an embedding."""
        entity_id = uuid4()
        embedding = [0.1] * 1536

        # One unlinked Decision node
        mock_graph.find_unlinked_nodes.return_value = [str(entity_id)]

        # DB query returns one row with embedding
        mock_row = MagicMock()
        mock_row.id = entity_id
        mock_row.embedding = embedding
        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[mock_row])
        session.execute = AsyncMock(return_value=mock_result)

        _link_result = LinkJobResult()
        _link_result.created.append({"id": uuid4(), "entity_type": "Decision", "similarity": 0.8})
        mock_auto_linker.auto_link = AsyncMock(return_value=_link_result)

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=mock_graph,
        )

        result = await mcp.registered["brain_backfill_links_batch"](entity_type="Decision")

        assert isinstance(result, str)
        mock_auto_linker.auto_link.assert_called_once()
        call_kwargs = mock_auto_linker.auto_link.call_args
        # Verify it was called with the correct entity_type and entity_id
        assert call_kwargs[1]["entity_type"] == "Decision" or call_kwargs[0][0] == "Decision"

    @pytest.mark.asyncio
    async def test_summary_contains_processed_and_links(
        self,
        session_factory: MagicMock,
        mock_graph: AsyncMock,
        mock_auto_linker: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """Result string contains entities processed and links created counts."""
        entity_id = uuid4()
        embedding = [0.1] * 10

        mock_graph.find_unlinked_nodes.return_value = [str(entity_id)]

        mock_row = MagicMock()
        mock_row.id = entity_id
        mock_row.embedding = embedding
        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[mock_row])
        session.execute = AsyncMock(return_value=mock_result)

        _link_result = LinkJobResult()
        _link_result.created.append({"id": uuid4(), "entity_type": "Decision", "similarity": 0.75})
        mock_auto_linker.auto_link = AsyncMock(return_value=_link_result)

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=mock_graph,
        )

        result = await mcp.registered["brain_backfill_links_batch"](entity_type="Decision")

        assert isinstance(result, str)
        # Should mention at least "1" entity processed and "1" link created
        assert "1" in result

    @pytest.mark.asyncio
    async def test_skips_none_id_from_graph_without_crashing(
        self,
        session_factory: MagicMock,
        mock_graph: AsyncMock,
        mock_auto_linker: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """Regression: 2026-04-09 connect phase crashed with TypeError
        ``one of the hex, bytes, bytes_le, fields, or int arguments must be
        given`` when calling ``brain_backfill_links_batch(entity_type=None)``.

        Root cause: ``find_unlinked_nodes`` can return None values when a
        Neo4j node is missing its ``id`` property (more frequent without
        the entity-type label filter, because orphan/legacy nodes are not
        excluded). The handler caught ``(ValueError, AttributeError)`` from
        ``UUID(raw_id)`` but ``UUID(None)`` raises ``TypeError``, which
        propagated and aborted the entire backfill call.

        The fix must skip None ids and still process valid ones.
        """
        valid_id = uuid4()
        # find_unlinked_nodes returns one valid uuid plus a None (orphan
        # neo4j node) plus a malformed string. Only the valid one should
        # make it through to auto_link.
        mock_graph.find_unlinked_nodes.return_value = [
            str(valid_id),
            None,
            "not-a-uuid",
        ]

        mock_row = MagicMock()
        mock_row.id = valid_id
        mock_row.embedding = [0.1] * 10
        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[mock_row])
        session.execute = AsyncMock(return_value=mock_result)

        _link_result = LinkJobResult()
        _link_result.created.append({"id": uuid4(), "entity_type": "Learning", "similarity": 0.7})
        mock_auto_linker.auto_link = AsyncMock(return_value=_link_result)

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=mock_graph,
        )

        # Must NOT raise TypeError. Must return a successful summary.
        result = await mcp.registered["brain_backfill_links_batch"](entity_type=None)

        assert isinstance(result, str)
        assert result.startswith("Backfill complete:")
        # Valid id was processed exactly once; the None and malformed entries
        # were silently skipped.
        mock_auto_linker.auto_link.assert_called_once()
        called_kwargs = mock_auto_linker.auto_link.call_args.kwargs
        assert called_kwargs["entity_id"] == valid_id

    @pytest.mark.asyncio
    async def test_returns_zero_summary_when_all_ids_are_none(
        self,
        tools: dict[str, Any],
        mock_graph: AsyncMock,
        mock_auto_linker: AsyncMock,
    ) -> None:
        """If every id from the graph is None, the tool returns a clean
        zero-processed summary instead of crashing.
        """
        mock_graph.find_unlinked_nodes.return_value = [None, None, None]

        result = await tools["brain_backfill_links_batch"]()

        assert isinstance(result, str)
        assert "0" in result
        assert result.startswith("Backfill complete:")
        mock_auto_linker.auto_link.assert_not_called()


class TestGetClusters:
    @pytest.mark.asyncio
    async def test_returns_error_when_no_graph(
        self,
        session_factory: MagicMock,
        mock_auto_linker: AsyncMock,
    ) -> None:
        """Returns error string containing 'not configured' when graph_service is None."""
        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=None,
        )
        result = await mcp.registered["brain_get_clusters"]()
        assert isinstance(result, str)
        assert "not configured" in result

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_edges(
        self,
        session_factory: MagicMock,
        mock_auto_linker: AsyncMock,
        mock_graph: AsyncMock,
    ) -> None:
        """Returns '0 clusters' when get_all_related_edges returns empty list."""
        mock_graph.get_all_related_edges = AsyncMock(return_value=[])

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=mock_graph,
        )
        result = await mcp.registered["brain_get_clusters"]()
        assert isinstance(result, str)
        assert "0 clusters" in result

    @pytest.mark.asyncio
    async def test_finds_connected_components(
        self,
        session_factory: MagicMock,
        mock_auto_linker: AsyncMock,
        mock_graph: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """Three nodes A-B, B-C form 1 cluster — result mentions the cluster."""
        id_a = str(uuid4())
        id_b = str(uuid4())
        id_c = str(uuid4())

        mock_graph.get_all_related_edges = AsyncMock(return_value=[(id_a, id_b), (id_b, id_c)])

        # Mock PG query: return metadata rows for the three nodes
        row_a = MagicMock()
        row_a.id = id_a
        row_a.title = "Entity A"
        row_a.entity_type = "Decision"

        row_b = MagicMock()
        row_b.id = id_b
        row_b.title = "Entity B"
        row_b.entity_type = "Decision"

        row_c = MagicMock()
        row_c.id = id_c
        row_c.title = "Entity C"
        row_c.entity_type = "Decision"

        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[row_a, row_b, row_c])
        session.execute = AsyncMock(return_value=mock_result)

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=mock_graph,
        )
        result = await mcp.registered["brain_get_clusters"]()
        assert isinstance(result, str)
        assert "1 cluster" in result

    @pytest.mark.asyncio
    async def test_filters_by_min_size(
        self,
        session_factory: MagicMock,
        mock_auto_linker: AsyncMock,
        mock_graph: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """2 connected nodes with min_size=3 → '0 clusters' after filtering."""
        id_a = str(uuid4())
        id_b = str(uuid4())

        mock_graph.get_all_related_edges = AsyncMock(return_value=[(id_a, id_b)])

        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[])
        session.execute = AsyncMock(return_value=mock_result)

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=mock_graph,
        )
        result = await mcp.registered["brain_get_clusters"](min_size=3)
        assert isinstance(result, str)
        assert "0 clusters" in result

    @pytest.mark.asyncio
    async def test_summary_only_omits_member_listing(
        self,
        session_factory: MagicMock,
        mock_auto_linker: AsyncMock,
        mock_graph: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """summary_only=True → output lists cluster sizes but NOT individual members.

        Regression guard for the SYNTH brain_get_clusters ceiling (2026-04-21):
        >98k chars output at every min_size tested because each member got its
        own line. SYNTH had to pivot to thematic brain_search instead of using
        the cluster tool. summary_only=True must produce bounded output that
        scales with cluster count, not with cluster size.
        """
        id_a = str(uuid4())
        id_b = str(uuid4())
        id_c = str(uuid4())

        mock_graph.get_all_related_edges = AsyncMock(return_value=[(id_a, id_b), (id_b, id_c)])

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=mock_graph,
        )
        result = await mcp.registered["brain_get_clusters"](summary_only=True)

        assert isinstance(result, str)
        assert "1 cluster" in result
        assert "3" in result  # size of the cluster
        # Must NOT contain per-member enrichment lines. The format "[Decision]"
        # and "[Learning]" etc. are added only by the full-detail path.
        assert "[Decision]" not in result
        assert "[Learning]" not in result
        # UUIDs of individual members must not appear in summary output.
        assert id_a not in result
        assert id_b not in result
        assert id_c not in result

    @pytest.mark.asyncio
    async def test_summary_only_skips_pg_enrichment_query(
        self,
        session_factory: MagicMock,
        mock_auto_linker: AsyncMock,
        mock_graph: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """summary_only=True must NOT issue the per-member PG enrichment query.

        The enrichment query loads id + entity_type + title for every node in
        every retained cluster. Skipping it is what makes the summary mode
        cheap even when clusters have 1000+ members.
        """
        id_a = str(uuid4())
        id_b = str(uuid4())

        mock_graph.get_all_related_edges = AsyncMock(return_value=[(id_a, id_b)])
        session.execute = AsyncMock()  # will assert NOT called

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=mock_graph,
        )
        await mcp.registered["brain_get_clusters"](summary_only=True)

        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_summary_only_output_is_bounded_on_huge_cluster(
        self,
        session_factory: MagicMock,
        mock_auto_linker: AsyncMock,
        mock_graph: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """Output size stays small (<2000 chars) for a single 1000-member cluster.

        Empirically this is the pathological input that broke SYNTH on
        2026-04-21: clusters grew to the point where the tool output itself
        exceeded the agent's token budget. summary_only must scale with
        cluster count, not member count.
        """
        # Build a chain of 1000 nodes → one giant cluster.
        ids = [str(uuid4()) for _ in range(1000)]
        edges = [(ids[i], ids[i + 1]) for i in range(len(ids) - 1)]

        mock_graph.get_all_related_edges = AsyncMock(return_value=edges)

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=mock_graph,
        )
        result = await mcp.registered["brain_get_clusters"](summary_only=True)

        assert isinstance(result, str)
        assert len(result) < 2000, (
            f"summary_only output must stay bounded; got {len(result)} chars for a "
            "1000-member single cluster"
        )
        # The cluster size itself must surface so SYNTH can decide whether to drill in.
        assert "1000" in result

    @pytest.mark.asyncio
    async def test_summary_only_false_preserves_existing_behavior(
        self,
        session_factory: MagicMock,
        mock_auto_linker: AsyncMock,
        mock_graph: AsyncMock,
        session: AsyncMock,
    ) -> None:
        """Default (summary_only=False) still returns per-member enrichment.

        Backwards-compat guard: existing callers of brain_get_clusters must
        not change behavior when summary_only is not passed.
        """
        id_a = str(uuid4())
        id_b = str(uuid4())

        mock_graph.get_all_related_edges = AsyncMock(return_value=[(id_a, id_b)])

        row_a = MagicMock()
        row_a.id = id_a
        row_a.title = "Entity A"
        row_a.entity_type = "Decision"
        row_b = MagicMock()
        row_b.id = id_b
        row_b.title = "Entity B"
        row_b.entity_type = "Decision"

        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[row_a, row_b])
        session.execute = AsyncMock(return_value=mock_result)

        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=mock_auto_linker,
            graph_service=mock_graph,
        )
        result = await mcp.registered["brain_get_clusters"]()

        assert "Entity A" in result
        assert "Entity B" in result
        assert "[Decision]" in result
