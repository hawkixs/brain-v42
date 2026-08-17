"""Unit tests for decay MCP tools: brain_decay_status, brain_refresh_entity,
brain_consolidation_candidates, brain_merge_entities."""

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
def tools(session_factory: MagicMock) -> dict[str, Any]:
    from brain_v42.mcp.tools.decay_tools import register_decay_tools

    mcp = MockMCP()
    register_decay_tools(mcp, session_factory)
    return mcp.registered


class TestBrainDecayStatus:
    @pytest.mark.asyncio
    async def test_returns_markdown_table(self, tools: dict[str, Any], session: AsyncMock) -> None:
        """brain_decay_status returns compact per-type summary (no markdown table)."""
        # Mock: each entity type query returns empty result
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_scalar = MagicMock()
        mock_scalar.scalar_one.return_value = 0
        session.execute = AsyncMock(
            side_effect=[
                # For each of 6 decay types: status counts query + deletion candidates query
                mock_result,
                mock_scalar,  # decision
                mock_result,
                mock_scalar,  # learning
                mock_result,
                mock_scalar,  # snippet
                mock_result,
                mock_scalar,  # runbook
                mock_result,
                mock_scalar,  # adr
                mock_result,
                mock_scalar,  # plan
            ]
        )

        result = await tools["brain_decay_status"]()

        assert isinstance(result, str)
        assert "## Decay status" in result
        # Compact form: empty corpus renders a single "all 0" line, no table.
        assert "| Type |" not in result
        assert "all 0" in result
        assert session.execute.await_count == 12

    @pytest.mark.asyncio
    async def test_tool_is_registered(self, tools: dict[str, Any]) -> None:
        """brain_decay_status is registered as an MCP tool."""
        assert "brain_decay_status" in tools


class TestBrainRefreshEntity:
    @pytest.mark.asyncio
    async def test_refresh_plan_targets_indexed_plans(
        self, tools: dict[str, Any], session: AsyncMock
    ) -> None:
        """Manual decay refresh supports canonical plan parents."""
        entity_id = uuid4()
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = (entity_id,)
        session.execute = AsyncMock(return_value=mock_result)

        result = await tools["brain_refresh_entity"](
            entity_type="plan",
            entity_id=str(entity_id),
        )

        statement = session.execute.await_args.args[0]
        assert result.startswith("ok Refreshed")
        assert "indexed_plans" in str(statement)

    @pytest.mark.asyncio
    async def test_refresh_success(self, tools: dict[str, Any], session: AsyncMock) -> None:
        """brain_refresh_entity refreshes an entity."""
        entity_id = uuid4()
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = (entity_id,)
        session.execute = AsyncMock(return_value=mock_result)

        result = await tools["brain_refresh_entity"](
            entity_type="decision",
            entity_id=str(entity_id),
        )

        assert isinstance(result, str)
        assert result.startswith("ok Refreshed")
        assert "type:decision" in result
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_unknown_type(self, tools: dict[str, Any]) -> None:
        """brain_refresh_entity rejects unknown entity type."""
        result = await tools["brain_refresh_entity"](
            entity_type="unknown",
            entity_id=str(uuid4()),
        )
        assert isinstance(result, str)
        assert result and result[0].isalnum()

    @pytest.mark.asyncio
    async def test_refresh_invalid_uuid(self, tools: dict[str, Any]) -> None:
        """brain_refresh_entity rejects invalid UUID."""
        result = await tools["brain_refresh_entity"](
            entity_type="decision",
            entity_id="not-a-uuid",
        )
        assert isinstance(result, str)
        assert result and result[0].isalnum()

    @pytest.mark.asyncio
    async def test_refresh_not_found(self, tools: dict[str, Any], session: AsyncMock) -> None:
        """brain_refresh_entity returns error when entity not found."""
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        result = await tools["brain_refresh_entity"](
            entity_type="learning",
            entity_id=str(uuid4()),
        )
        assert isinstance(result, str)
        assert result and result[0].isalnum()
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_tool_is_registered(self, tools: dict[str, Any]) -> None:
        """brain_refresh_entity is registered as an MCP tool."""
        assert "brain_refresh_entity" in tools


# ── Consolidation fixtures ───────────────────────────────────────────────


@pytest.fixture
def consolidation_job() -> AsyncMock:
    job = AsyncMock()
    job.find_candidates = AsyncMock(return_value=[])
    job.merge = AsyncMock(return_value=None)
    return job


@pytest.fixture
def tools_with_consolidation(
    session_factory: MagicMock,
    consolidation_job: AsyncMock,
) -> dict[str, Any]:
    from brain_v42.mcp.tools.decay_tools import register_decay_tools

    mcp = MockMCP()
    register_decay_tools(
        mcp,
        session_factory,
        consolidation_job=consolidation_job,
    )
    return mcp.registered


# ── Consolidation Candidates tests ───────────────────────────────────────


class TestBrainConsolidationCandidates:
    @pytest.mark.asyncio
    async def test_returns_candidates(
        self,
        tools_with_consolidation: dict[str, Any],
        consolidation_job: AsyncMock,
    ) -> None:
        """brain_consolidation_candidates returns markdown with candidates."""
        consolidation_job.find_candidates.return_value = [
            {
                "entity_type": "learning",
                "id_a": "id1",
                "id_b": "id2",
                "similarity": 0.95,
                "title_a": "A",
                "title_b": "B",
            }
        ]
        result = await tools_with_consolidation["brain_consolidation_candidates"]()
        assert isinstance(result, str)
        assert "## 1 consolidation candidate" in result
        assert "learning" in result
        assert "0.95" in result

    @pytest.mark.asyncio
    async def test_passes_entity_type_filter(
        self,
        tools_with_consolidation: dict[str, Any],
        consolidation_job: AsyncMock,
    ) -> None:
        """brain_consolidation_candidates passes entity_type to job."""
        await tools_with_consolidation["brain_consolidation_candidates"](
            entity_type="decision", limit=5
        )
        consolidation_job.find_candidates.assert_called_once_with(entity_type="decision", limit=5)

    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self, session_factory: MagicMock) -> None:
        """brain_consolidation_candidates returns error when consolidation_job is None."""
        from brain_v42.mcp.tools.decay_tools import register_decay_tools

        mcp = MockMCP()
        register_decay_tools(mcp, session_factory)
        result = await mcp.registered["brain_consolidation_candidates"]()
        assert isinstance(result, str)
        assert result and result[0].isalnum()

    @pytest.mark.asyncio
    async def test_tool_registered(self, tools_with_consolidation: dict[str, Any]) -> None:
        assert "brain_consolidation_candidates" in tools_with_consolidation


# ── Merge Entities tests ─────────────────────────────────────────────────


class TestBrainMergeEntities:
    @pytest.mark.asyncio
    async def test_merge_rejects_plan_from_consolidation_registry(
        self,
        tools_with_consolidation: dict[str, Any],
        session: AsyncMock,
    ) -> None:
        """Adding plans to decay must not widen consolidation support."""
        result = await tools_with_consolidation["brain_merge_entities"](
            entity_type="plan",
            source_id=str(uuid4()),
            target_id=str(uuid4()),
        )

        assert result and result[0].isalnum()
        assert "Unknown entity type: plan" in result
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_merge_success(
        self,
        tools_with_consolidation: dict[str, Any],
        consolidation_job: AsyncMock,
    ) -> None:
        """brain_merge_entities delegates the canonical merge once."""
        source_id = uuid4()
        target_id = uuid4()

        result = await tools_with_consolidation["brain_merge_entities"](
            entity_type="decision",
            source_id=str(source_id),
            target_id=str(target_id),
        )
        assert isinstance(result, str)
        assert result.startswith("ok Merged")
        assert "type:decision" in result
        consolidation_job.merge.assert_awaited_once_with(
            "decision",
            source_id,
            target_id,
            authorization=None,
        )

    @pytest.mark.asyncio
    async def test_merge_unknown_type(self, tools_with_consolidation: dict[str, Any]) -> None:
        """brain_merge_entities rejects unknown entity type."""
        result = await tools_with_consolidation["brain_merge_entities"](
            entity_type="unknown",
            source_id=str(uuid4()),
            target_id=str(uuid4()),
        )
        assert isinstance(result, str)
        assert result and result[0].isalnum()

    @pytest.mark.asyncio
    async def test_merge_invalid_uuid(self, tools_with_consolidation: dict[str, Any]) -> None:
        """brain_merge_entities rejects invalid UUID."""
        result = await tools_with_consolidation["brain_merge_entities"](
            entity_type="decision",
            source_id="not-a-uuid",
            target_id=str(uuid4()),
        )
        assert isinstance(result, str)
        assert result and result[0].isalnum()

    @pytest.mark.asyncio
    async def test_merge_source_not_found(
        self,
        tools_with_consolidation: dict[str, Any],
        consolidation_job: AsyncMock,
    ) -> None:
        """brain_merge_entities returns error when source not found."""
        from brain_v42.services.consolidation import ConsolidationEntityNotFoundError

        source_id = uuid4()
        consolidation_job.merge.side_effect = ConsolidationEntityNotFoundError(
            f"Source decision {source_id} not found"
        )

        result = await tools_with_consolidation["brain_merge_entities"](
            entity_type="decision",
            source_id=str(source_id),
            target_id=str(uuid4()),
        )
        assert isinstance(result, str)
        assert result and result[0].isalnum()
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_merge_target_not_found(
        self,
        tools_with_consolidation: dict[str, Any],
        consolidation_job: AsyncMock,
    ) -> None:
        """brain_merge_entities returns error when target not found."""
        from brain_v42.services.consolidation import ConsolidationEntityNotFoundError

        source_id = uuid4()
        target_id = uuid4()
        consolidation_job.merge.side_effect = ConsolidationEntityNotFoundError(
            f"Target decision {target_id} not found"
        )

        result = await tools_with_consolidation["brain_merge_entities"](
            entity_type="decision",
            source_id=str(source_id),
            target_id=str(target_id),
        )
        assert isinstance(result, str)
        assert result and result[0].isalnum()
        assert f"Target decision {target_id} not found" in result

    @pytest.mark.asyncio
    async def test_tool_registered(self, tools_with_consolidation: dict[str, Any]) -> None:
        assert "brain_merge_entities" in tools_with_consolidation
