"""TDD tests for toolsux audit wave 4:
  - Anti-token-bomb bounds on plan chunks, clusters, runbook list, roadmap features
  - Limit clamps (max 100) on brain_search, brain_list, brain_list_adrs
  - UUID contract: parse_uuid helper + error-returning sites instead of ValueError

All tests written RED-first — they document the *required* behaviour and should
fail on the unmodified codebase, then pass after implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from tests.unit.mcp._tool_error_adapter import capture_tool_errors

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _dt() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# MockMCP — same pattern used across the test suite
# ---------------------------------------------------------------------------


class MockMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


# ===========================================================================
# 1. parse_uuid helper (parsing.py)
# ===========================================================================


class TestParseUUID:
    """parse_uuid(value) -> UUID | None — never raises ValueError."""

    def test_valid_uuid_returns_uuid_object(self) -> None:
        from brain_v42.mcp.tools.parsing import parse_uuid

        uid = uuid4()
        result = parse_uuid(str(uid))
        assert result == uid

    def test_invalid_string_returns_none(self) -> None:
        from brain_v42.mcp.tools.parsing import parse_uuid

        assert parse_uuid("not-a-uuid") is None

    def test_empty_string_returns_none(self) -> None:
        from brain_v42.mcp.tools.parsing import parse_uuid

        assert parse_uuid("") is None

    def test_none_input_returns_none(self) -> None:
        from brain_v42.mcp.tools.parsing import parse_uuid

        assert parse_uuid(None) is None  # type: ignore[arg-type]

    def test_bare_hex_is_valid(self) -> None:
        """Python's UUID() accepts 32-char hex without dashes — parse_uuid follows suit."""
        from brain_v42.mcp.tools.parsing import parse_uuid

        # "deadbeef..." is a valid UUID in hex form — Python accepts it
        result = parse_uuid("deadbeefdeadbeefdeadbeefdeadbeef")
        assert result is not None
        assert str(result) == "deadbeef-dead-beef-dead-beefdeadbeef"

    def test_uuid_object_passthrough(self) -> None:
        """UUID objects are returned as-is (convenience)."""
        from brain_v42.mcp.tools.parsing import parse_uuid

        uid = uuid4()
        result = parse_uuid(uid)
        assert result == uid


# ===========================================================================
# 2. UUID contract — tools must return format_error, not raise ValueError
# ===========================================================================


def _make_brain_tools() -> tuple[dict[str, Any], dict[str, Any]]:
    """Register brain_tools and return (registered_tools, mock_services)."""
    from brain_v42.mcp.tools.brain_tools import register_tools

    mcp = MockMCP()
    svcs: dict[str, Any] = {}
    for k in ("decision_svc", "learning_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        svc = MagicMock()
        for method in ("get_by_id", "create", "update", "delete", "list_all"):
            setattr(svc, method, AsyncMock(return_value=None))
        svcs[k] = svc
    svcs["decision_svc"].supersede = AsyncMock(return_value=None)
    svcs["decision_svc"].get_supersession_chain = AsyncMock(return_value=[])

    adr_mock = MagicMock()
    adr_mock.accept = AsyncMock(return_value=None)
    adr_mock.deprecate = AsyncMock(return_value=None)
    adr_mock.create_with_promotion = AsyncMock(return_value=None)
    adr_mock.create = AsyncMock(return_value=None)
    adr_mock.list_all = AsyncMock(return_value=[])
    svcs["adr_svc"] = adr_mock

    learning_mock = MagicMock()
    learning_mock.validate = AsyncMock(return_value=None)
    learning_mock.create = AsyncMock(return_value=None)
    learning_mock.list_all = AsyncMock(return_value=[])
    svcs["learning_svc"] = learning_mock

    ctx_svc = MagicMock()
    ctx_svc.get_context = AsyncMock(return_value=None)
    ctx_svc.upsert_context = AsyncMock(return_value=None)
    svcs["project_context_svc"] = ctx_svc

    brain_svc = MagicMock()
    brain_svc.search = AsyncMock(return_value=MagicMock(results=[], total=0, degraded=None))
    brain_svc.what_do_i_know_about = AsyncMock(
        return_value=MagicMock(
            by_type=MagicMock(decisions=[], learnings=[], snippets=[], runbooks=[], adrs=[]),
            total=0,
            degraded=None,
        )
    )
    svcs["brain_svc"] = brain_svc

    register_tools(
        mcp,
        decision_svc=svcs["decision_svc"],
        learning_svc=svcs["learning_svc"],
        snippet_svc=svcs["snippet_svc"],
        runbook_svc=svcs["runbook_svc"],
        adr_svc=svcs["adr_svc"],
        project_context_svc=svcs["project_context_svc"],
        brain_svc=svcs["brain_svc"],
    )
    return mcp.registered, svcs


class TestUUIDContractBrainTools:
    """Invalid UUIDs must return an unprefixed error, not raise ValueError."""

    @pytest.mark.asyncio
    async def test_supersede_decision_invalid_uuid_returns_error(self) -> None:
        tools, _ = _make_brain_tools()
        result = await tools["brain_supersede_decision"](
            old_decision_id="bad-uuid",
            title="T",
            context="C",
            decision_made="D",
            reasoning="R",
        )
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"
        assert "UUID" in result or "uuid" in result.lower() or "Invalid" in result

    @pytest.mark.asyncio
    async def test_get_supersession_chain_invalid_uuid_returns_error(self) -> None:
        tools, _ = _make_brain_tools()
        result = await tools["brain_get_supersession_chain"](decision_id="bad-uuid")
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"

    @pytest.mark.asyncio
    async def test_validate_learning_invalid_uuid_returns_error(self) -> None:
        tools, _ = _make_brain_tools()
        result = await tools["brain_validate_learning"](learning_id="bad-uuid")
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"

    @pytest.mark.asyncio
    async def test_accept_adr_invalid_uuid_returns_error(self) -> None:
        tools, _ = _make_brain_tools()
        result = await tools["brain_accept_adr"](adr_id="bad-uuid")
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"

    @pytest.mark.asyncio
    async def test_deprecate_adr_invalid_uuid_returns_error(self) -> None:
        tools, _ = _make_brain_tools()
        result = await tools["brain_deprecate_adr"](adr_id="bad-uuid")
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"


class TestUUIDContractRunbookTools:
    """brain_get_runbook and brain_execute_runbook invalid UUIDs return plain errors."""

    def _make_tools(self) -> tuple[dict[str, Any], MagicMock]:
        from brain_v42.mcp.tools.runbook_tools import register_runbook_tools

        mcp = MockMCP()
        svc = MagicMock()
        svc.get_by_id = AsyncMock(return_value=None)
        svc.record_execution = AsyncMock(return_value=None)
        svc.list_by_project = AsyncMock(return_value=[])
        register_runbook_tools(mcp, svc)
        return mcp.registered, svc

    @pytest.mark.asyncio
    async def test_get_runbook_invalid_uuid_returns_error(self) -> None:
        tools, _ = self._make_tools()
        result = await tools["brain_get_runbook"](runbook_id="bad-uuid")
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"

    @pytest.mark.asyncio
    async def test_execute_runbook_invalid_uuid_returns_error(self) -> None:
        tools, _ = self._make_tools()
        result = await tools["brain_execute_runbook"](runbook_id="bad-uuid")
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"


class TestUUIDContractSnippetTools:
    """brain_use_snippet invalid UUID must return a plain error."""

    def _make_tools(self) -> tuple[dict[str, Any], AsyncMock]:
        from brain_v42.mcp.tools.snippet_tools import register_snippet_tools

        mcp = MockMCP()
        svc = AsyncMock()
        svc.increment_use = AsyncMock(return_value=None)
        register_snippet_tools(mcp, svc)
        return mcp.registered, svc

    @pytest.mark.asyncio
    async def test_use_snippet_invalid_uuid_returns_error(self) -> None:
        tools, _ = self._make_tools()
        result = await tools["brain_use_snippet"](snippet_id="bad-uuid")
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"


# ===========================================================================
# 3. Anti-token-bomb: brain_get("plan") — chunk budget
# ===========================================================================


class TestBrainGetPlanTokenBomb:
    """brain_get plan with many chunks must include a truncation notice."""

    def _make_plan(self, title: str = "Plan", n_chunks: int = 0) -> Any:
        from brain_v42.models.indexed_plan import IndexedPlan

        return IndexedPlan.model_validate(
            {
                "id": uuid4(),
                "title": title,
                "plan_type": "spec",
                "status": "active",
                "project_key": "test",
                "file_path": f"/docs/{title.lower().replace(' ', '_')}.md",
                "chunk_count": n_chunks,
                "word_count": n_chunks * 100,
                "content_hash": "abc123",
                "content": "full content here",
                "freshness_status": "fresh",
                "indexed_at": _dt(),
                "tags": [],
                "created_at": _dt(),
                "updated_at": _dt(),
            }
        )

    def _make_chunk(self, plan_id: Any, order: int, content: str = "x" * 500) -> Any:
        from brain_v42.models.indexed_plan_chunk import IndexedPlanChunk

        return IndexedPlanChunk.model_validate(
            {
                "id": uuid4(),
                "plan_id": plan_id,
                "section_order": order,
                "section_path": f"§{order + 1}",
                "section_title": f"Section {order + 1}",
                "plan_type": "spec",
                "content": content,
                "word_count": len(content.split()),
                "project_key": "test",
                "status": "active",
                "tags": [],
                "created_at": _dt(),
            }
        )

    @pytest.mark.asyncio
    async def test_large_plan_output_contains_truncation_notice(self) -> None:
        """When plan output exceeds max_chars, a truncation notice is emitted."""
        from brain_v42.mcp.tools.crud_tools import _format_plan_detail

        plan = self._make_plan("Big Plan", n_chunks=30)
        # 30 chunks * 500 chars each = 15 000 chars > budget of 8 000
        chunks = [self._make_chunk(plan.id, i, "x" * 500) for i in range(30)]
        result = _format_plan_detail(plan, chunks, max_chars=8000)
        assert "omis" in result or "omitted" in result, (
            "Truncation notice expected when output exceeds max_chars"
        )

    @pytest.mark.asyncio
    async def test_small_plan_output_has_no_truncation_notice(self) -> None:
        """When plan fits within max_chars, no truncation notice is added."""
        from brain_v42.mcp.tools.crud_tools import _format_plan_detail

        plan = self._make_plan("Small Plan", n_chunks=2)
        chunks = [self._make_chunk(plan.id, i, "short content") for i in range(2)]
        result = _format_plan_detail(plan, chunks, max_chars=8000)
        assert "omis" not in result and "omitted" not in result


# ===========================================================================
# 4. Anti-token-bomb: brain_get_clusters non-summary
# ===========================================================================


class TestGetClustersTokenBomb:
    """brain_get_clusters must cap members_per_cluster and total output."""

    def _make_tools(self, edges: list[tuple[str, str]]) -> dict[str, Any]:
        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        mock_graph = AsyncMock()
        mock_graph.get_all_related_edges = AsyncMock(return_value=edges)

        # Build a session that returns empty result from execute()
        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[])
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx)

        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=None,
            graph_service=mock_graph,
        )
        return mcp.registered

    @pytest.mark.asyncio
    async def test_cluster_members_capped_per_cluster(self) -> None:
        """When a cluster has > max_members_per_cluster items, output is capped."""
        # Connect 60 nodes to a single hub to form one big cluster
        first = str(uuid4())
        edges = [(first, str(uuid4())) for _ in range(60)]

        tools = self._make_tools(edges)
        result = await tools["brain_get_clusters"](
            min_size=2,
            limit=5,
            summary_only=False,
            max_members_per_cluster=20,
        )
        assert "omis" in result or "omitted" in result, (
            f"Expected truncation notice for oversized cluster, got: {result[:300]!r}"
        )

    @pytest.mark.asyncio
    async def test_small_cluster_not_truncated(self) -> None:
        """Cluster under cap has no truncation notice."""
        first = str(uuid4())
        edges = [(first, str(uuid4())) for _ in range(3)]
        tools = self._make_tools(edges)
        result = await tools["brain_get_clusters"](
            min_size=2,
            limit=5,
            summary_only=False,
            max_members_per_cluster=20,
        )
        # Small cluster should not trigger truncation
        assert "omitted" not in result or "0" in result


# ===========================================================================
# 5. Anti-token-bomb: brain_get_roadmap — feature cap
# ===========================================================================


class TestRoadmapTokenBomb:
    """format_roadmap must cap features per project and emit a truncation notice."""

    def _count_feature_data_rows(self, result: str) -> int:
        """Count data rows (lines starting with '| Feature N') in roadmap output."""
        return sum(
            1
            for line in result.splitlines()
            if line.startswith("| Feature ") and "Status" not in line
        )

    @pytest.mark.asyncio
    async def test_format_roadmap_caps_features(self) -> None:
        """format_roadmap with >max_features_per_project truncates and appends notice."""
        from brain_v42.mcp.tools.formatters import format_roadmap

        features = [
            {
                "name": f"Feature {i}",
                "status": "planned",
                "artifact_count": {},
                "last_activity": None,
                "pinned": False,
            }
            for i in range(60)
        ]
        projects = [
            {
                "project_key": "big-proj",
                "current_phase": "dev",
                "features": features,
            }
        ]
        result = format_roadmap(projects, max_features_per_project=20)
        assert "omis" in result or "omitted" in result, (
            f"Expected truncation notice, got: {result[:400]!r}"
        )
        # Count data feature rows — should be capped at 20 not 60
        row_count = self._count_feature_data_rows(result)
        assert row_count <= 20, f"Expected ≤20 feature rows, got {row_count}"

    @pytest.mark.asyncio
    async def test_format_roadmap_no_truncation_when_under_cap(self) -> None:
        """format_roadmap with ≤max_features_per_project emits all features."""
        from brain_v42.mcp.tools.formatters import format_roadmap

        features = [
            {
                "name": f"Feature {i}",
                "status": "planned",
                "artifact_count": {},
                "last_activity": None,
                "pinned": False,
            }
            for i in range(5)
        ]
        projects = [{"project_key": "proj", "current_phase": None, "features": features}]
        result = format_roadmap(projects, max_features_per_project=20)
        assert "omis" not in result and "omitted" not in result
        assert self._count_feature_data_rows(result) == 5


# ===========================================================================
# 6. Anti-token-bomb: brain_get_runbook project branch — limit + summary
# ===========================================================================


class TestGetRunbookProjectBranchLimit:
    """brain_get_runbook(project_key=...) must apply a limit and return summary rows."""

    def _make_tools(self, runbooks: list[Any]) -> tuple[dict[str, Any], MagicMock]:
        from brain_v42.mcp.tools.runbook_tools import register_runbook_tools

        mcp = MockMCP()
        svc = MagicMock()
        svc.list_by_project = AsyncMock(return_value=runbooks)
        svc.get_by_id = AsyncMock(return_value=None)
        svc.get_by_title = AsyncMock(return_value=None)
        svc.record_execution = AsyncMock(return_value=None)
        register_runbook_tools(mcp, svc)
        return mcp.registered, svc

    def _make_runbook(self, title: str = "RB") -> Any:
        from brain_v42.models.runbook import Runbook, RunbookStep

        return Runbook.model_validate(
            {
                "id": uuid4(),
                "title": title,
                "description": "desc",
                "project_key": "test",
                "trigger": "manual",
                "steps": [RunbookStep(order=1, title="S1")],
                "created_at": _dt(),
                "updated_at": _dt(),
            }
        )

    @pytest.mark.asyncio
    async def test_project_list_passes_limit_to_service(self) -> None:
        """brain_get_runbook project branch passes limit+1 kwarg to list_by_project.

        We fetch clamped+1 rows to detect overflow without a COUNT query.
        The service receives limit+1 (here 5+1=6); the result is trimmed to 5.
        """
        rbs = [self._make_runbook(f"RB{i}") for i in range(5)]
        tools, svc = self._make_tools(rbs)
        await tools["brain_get_runbook"](project_key="test", limit=5)
        svc.list_by_project.assert_called_once()
        call_kwargs = svc.list_by_project.call_args
        # Service must be called with limit+1 (=6) to detect pagination overflow.
        passed_limit = call_kwargs.kwargs.get("limit")
        assert passed_limit is not None and passed_limit == 6, (
            f"Service must receive clamped+1=6; call was {call_kwargs}"
        )

    @pytest.mark.asyncio
    async def test_project_list_clamps_limit_to_max(self) -> None:
        """brain_get_runbook project branch clamps limit to _RUNBOOK_LIST_LIMIT_MAX.

        The service is called with clamped+1 to detect overflow, so the maximum
        value passed is _RUNBOOK_LIST_LIMIT_MAX + 1.
        """
        rbs = [self._make_runbook(f"RB{i}") for i in range(5)]
        tools, svc = self._make_tools(rbs)
        # Pass an absurdly large limit — service should receive clamped+1 value
        await tools["brain_get_runbook"](project_key="test", limit=9999)
        svc.list_by_project.assert_called_once()
        call_kwargs = svc.list_by_project.call_args
        passed_limit = call_kwargs.kwargs.get("limit")
        # _RUNBOOK_LIST_LIMIT_MAX is 50; we fetch 51 to detect overflow
        assert passed_limit is not None and passed_limit <= 51, (
            f"Expected clamped+1 limit ≤51, got {passed_limit}"
        )


# ===========================================================================
# 7. Limit clamps on brain_search, brain_list, brain_list_adrs
# ===========================================================================


class TestLimitClamps:
    """brain_search, brain_list, brain_list_adrs must clamp limit to [1, 100]."""

    @pytest.mark.asyncio
    async def test_brain_search_limit_clamped_to_100(self) -> None:
        """brain_search passes min(limit, 100) to brain_svc.search."""
        tools, svcs = _make_brain_tools()
        await tools["brain_search"](query="test", limit=9999)
        call_kwargs = svcs["brain_svc"].search.call_args
        passed_limit = call_kwargs.kwargs.get("limit") or (
            call_kwargs.args[2] if len(call_kwargs.args) > 2 else None
        )
        assert passed_limit is not None and passed_limit <= 100, (
            f"brain_search must clamp limit to ≤100; got {passed_limit}"
        )

    @pytest.mark.asyncio
    async def test_brain_search_limit_below_1_becomes_1(self) -> None:
        """brain_search clamps limit<1 to 1."""
        tools, svcs = _make_brain_tools()
        await tools["brain_search"](query="test", limit=0)
        call_kwargs = svcs["brain_svc"].search.call_args
        passed_limit = call_kwargs.kwargs.get("limit") or (
            call_kwargs.args[2] if len(call_kwargs.args) > 2 else None
        )
        assert passed_limit is not None and passed_limit >= 1, (
            f"brain_search must clamp limit<1 to 1; got {passed_limit}"
        )

    @pytest.mark.asyncio
    async def test_brain_list_adrs_limit_clamped_to_100(self) -> None:
        """brain_list_adrs clamps limit to ≤100."""
        tools, svcs = _make_brain_tools()
        await tools["brain_list_adrs"](limit=9999)
        call_kwargs = svcs["adr_svc"].list_all.call_args
        passed_limit = call_kwargs.kwargs.get("limit") or (
            next(
                (a for a in call_kwargs.args if isinstance(a, int) and a <= 100),
                None,
            )
        )
        assert passed_limit is not None and passed_limit <= 100, (
            f"brain_list_adrs must clamp limit to ≤100; got {passed_limit} (call: {call_kwargs})"
        )

    @pytest.mark.asyncio
    async def test_brain_list_adrs_limit_below_1_becomes_1(self) -> None:
        """brain_list_adrs clamps limit<1 to 1."""
        tools, svcs = _make_brain_tools()
        await tools["brain_list_adrs"](limit=0)
        call_kwargs = svcs["adr_svc"].list_all.call_args
        passed_limit = call_kwargs.kwargs.get("limit")
        if passed_limit is None:
            passed_limit = call_kwargs.args[2] if len(call_kwargs.args) > 2 else None
        assert passed_limit is not None and passed_limit >= 1

    @pytest.mark.asyncio
    async def test_brain_list_limit_clamped_to_100(self) -> None:
        """brain_list (decisions) clamps limit to ≤100."""
        from unittest.mock import MagicMock

        from brain_v42.mcp.tools.crud_tools import register_crud_tools

        mcp = MockMCP()
        decision_svc = MagicMock()
        decision_svc.list_all = AsyncMock(return_value=[])
        learning_svc = MagicMock()
        learning_svc.list_all = AsyncMock(return_value=[])
        snippet_svc = MagicMock()
        snippet_svc.list_snippets = AsyncMock(return_value=[])
        runbook_svc = MagicMock()
        runbook_svc.list_by_project = AsyncMock(return_value=[])
        adr_svc = MagicMock()
        adr_svc.list_all = AsyncMock(return_value=[])
        session_factory = MagicMock()

        register_crud_tools(
            mcp,
            decision_svc=decision_svc,
            learning_svc=learning_svc,
            snippet_svc=snippet_svc,
            runbook_svc=runbook_svc,
            adr_svc=adr_svc,
            session_factory=session_factory,
        )
        await mcp.registered["brain_list"](entity_type="decision", limit=9999)
        call_kwargs = decision_svc.list_all.call_args
        passed_limit = call_kwargs.kwargs.get("limit")
        assert passed_limit is not None and passed_limit <= 100, (
            f"brain_list must clamp limit to ≤100; got {passed_limit}"
        )

    @pytest.mark.asyncio
    async def test_brain_list_limit_below_1_becomes_1(self) -> None:
        """brain_list clamps limit<1 to 1."""
        from unittest.mock import MagicMock

        from brain_v42.mcp.tools.crud_tools import register_crud_tools

        mcp = MockMCP()
        decision_svc = MagicMock()
        decision_svc.list_all = AsyncMock(return_value=[])
        register_crud_tools(
            mcp,
            decision_svc=decision_svc,
            learning_svc=MagicMock(list_all=AsyncMock(return_value=[])),
            snippet_svc=MagicMock(list_snippets=AsyncMock(return_value=[])),
            runbook_svc=MagicMock(list_by_project=AsyncMock(return_value=[])),
            adr_svc=MagicMock(list_all=AsyncMock(return_value=[])),
            session_factory=MagicMock(),
        )
        await mcp.registered["brain_list"](entity_type="decision", limit=-5)
        call_kwargs = decision_svc.list_all.call_args
        passed_limit = call_kwargs.kwargs.get("limit")
        assert passed_limit is not None and passed_limit >= 1


# ===========================================================================
# 8. BLOCKER: format_roadmap cap must NOT apply to scoped (single-project) call
# ===========================================================================


class TestRoadmapScopedNotCapped:
    """Scoped brain_get_roadmap must be able to return all features (no global cap)."""

    def _make_tools(self, projects_data: list[dict]) -> dict[str, Any]:
        from brain_v42.mcp.tools.roadmap_tools import register_roadmap_tools

        mcp = MockMCP()
        svc = MagicMock()
        svc.get_roadmap = AsyncMock(
            return_value=[MagicMock(model_dump=MagicMock(return_value=p)) for p in projects_data]
        )
        register_roadmap_tools(
            mcp,
            svc,
            feature_svc=MagicMock(),
            feature_creation_svc=None,
        )
        return mcp.registered

    @pytest.mark.asyncio
    async def test_scoped_roadmap_returns_all_features_above_default_cap(self) -> None:
        """brain_get_roadmap(project_key='X') on a 60-feature project must NOT truncate.

        Before the fix: format_roadmap applied max_features_per_project=20
        unconditionally, so scoped calls were silently truncated and the
        truncation notice's suggestion ('use brain_get_roadmap(project_key=…)')
        was circular (the very call that produced the truncated output).
        After the fix: scoped calls pass a high cap so all features are returned.
        """
        features = [
            {
                "name": f"Feature {i}",
                "status": "planned",
                "artifact_count": {},
                "last_activity": None,
                "pinned": False,
            }
            for i in range(60)
        ]
        projects = [{"project_key": "big-proj", "current_phase": None, "features": features}]
        tools = self._make_tools(projects)
        result = await tools["brain_get_roadmap"](project_key="big-proj")
        row_count = sum(
            1
            for line in result.splitlines()
            if line.startswith("| Feature ") and "Status" not in line
        )
        assert row_count == 60, (
            f"Scoped brain_get_roadmap must return all 60 features, got {row_count}. "
            f"The cap must only apply to the all-projects branch."
        )

    @pytest.mark.asyncio
    async def test_all_projects_roadmap_still_caps_features(self) -> None:
        """brain_get_roadmap() (all-projects) still applies the cap and emits notice."""
        features = [
            {
                "name": f"F{i}",
                "status": "planned",
                "artifact_count": {},
                "last_activity": None,
                "pinned": False,
            }
            for i in range(60)
        ]
        projects = [{"project_key": "big-proj", "current_phase": None, "features": features}]
        tools = self._make_tools(projects)
        # all-projects: no project_key arg
        result = await tools["brain_get_roadmap"]()
        assert "omis" in result or "omitted" in result, (
            "All-projects branch must still cap and emit truncation notice"
        )


# ===========================================================================
# 9. BLOCKER: UUID contract for source_learning_id in brain_propose_adr
#    and brain_create_runbook
# ===========================================================================


class TestUUIDContractSourceLearningId:
    """Malformed source_learning_id must return an unprefixed error, not raise ValueError."""

    @pytest.mark.asyncio
    async def test_propose_adr_invalid_source_learning_id_returns_error(self) -> None:
        """brain_propose_adr with an invalid source_learning_id returns a plain error."""
        tools, _ = _make_brain_tools()
        result = await tools["brain_propose_adr"](
            title="T",
            context="C",
            decision="D",
            consequences="Q",
            project_key="proj",
            source_learning_id="not-a-uuid",
            auto_accept=True,
        )
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"
        assert "UUID" in result or "Invalid" in result, (
            f"Error must mention UUID invalidity, got: {result!r}"
        )

    @pytest.mark.asyncio
    async def test_create_runbook_invalid_source_learning_id_returns_error(self) -> None:
        """brain_create_runbook with an invalid source_learning_id returns a plain error."""
        from brain_v42.mcp.tools.runbook_tools import register_runbook_tools

        mcp = MockMCP()
        svc = MagicMock()
        svc.create = AsyncMock(return_value=None)
        svc.create_with_promotion = AsyncMock(return_value=None)
        register_runbook_tools(mcp, svc)
        result = await mcp.registered["brain_create_runbook"](
            title="My Runbook",
            description="desc",
            project_key="proj",
            trigger="manual",
            steps=[{"order": 1, "title": "Step 1"}],
            source_learning_id="not-a-uuid",
        )
        assert result and result[0].isalnum(), f"Expected an unprefixed error, got: {result!r}"
        assert "UUID" in result or "Invalid" in result, (
            f"Error must mention UUID invalidity, got: {result!r}"
        )


# ===========================================================================
# 10. MAJOR: brain_get plan exposes max_chars param and notice is not circular
# ===========================================================================


class TestBrainGetPlanMaxCharsParam:
    """brain_get for plan must expose max_chars and have a truthful notice."""

    def _make_crud_tools(self, plan: Any, chunks: list[Any]) -> dict[str, Any]:
        from brain_v42.mcp.tools.crud_tools import register_crud_tools

        mcp = MockMCP()
        session_mock = AsyncMock()
        repo_mock = MagicMock()
        repo_mock.get_with_chunks = AsyncMock(return_value=(plan, chunks))

        async def fake_session_context():
            return session_mock

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session_mock)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx)

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "brain_v42.repositories.pg_indexed_plan_repo.PgIndexedPlanRepo",
            return_value=repo_mock,
        ):
            register_crud_tools(
                mcp,
                decision_svc=MagicMock(list_all=AsyncMock(return_value=[])),
                learning_svc=MagicMock(list_all=AsyncMock(return_value=[])),
                snippet_svc=MagicMock(list_snippets=AsyncMock(return_value=[])),
                runbook_svc=MagicMock(list_by_project=AsyncMock(return_value=[])),
                adr_svc=MagicMock(list_all=AsyncMock(return_value=[])),
                session_factory=session_factory,
            )
        return mcp.registered

    def _make_plan_and_chunks(self, n_chunks: int = 30) -> tuple[Any, list[Any]]:
        from brain_v42.models.indexed_plan import IndexedPlan
        from brain_v42.models.indexed_plan_chunk import IndexedPlanChunk

        dt = datetime(2026, 1, 1, tzinfo=UTC)
        plan_id = uuid4()
        plan = IndexedPlan.model_validate(
            {
                "id": plan_id,
                "title": "Big Plan",
                "plan_type": "spec",
                "status": "active",
                "project_key": "test",
                "file_path": "/docs/big_plan.md",
                "chunk_count": n_chunks,
                "word_count": n_chunks * 100,
                "content_hash": "abc123",
                "content": "full content here",
                "freshness_status": "fresh",
                "indexed_at": dt,
                "tags": [],
                "created_at": dt,
                "updated_at": dt,
            }
        )
        chunks = []
        for i in range(n_chunks):
            chunks.append(
                IndexedPlanChunk.model_validate(
                    {
                        "id": uuid4(),
                        "plan_id": plan_id,
                        "section_order": i,
                        "section_path": f"§{i + 1}",
                        "section_title": f"Section {i + 1}",
                        "plan_type": "spec",
                        "content": "x" * 500,
                        "word_count": 50,
                        "project_key": "test",
                        "status": "active",
                        "tags": [],
                        "created_at": dt,
                    }
                )
            )
        return plan, chunks

    @pytest.mark.asyncio
    async def test_truncation_notice_not_circular(self) -> None:
        """Truncation notice must not suggest calling brain_get which itself truncates.

        The notice must offer a REAL escape hatch (e.g. max_chars=… param or
        brain_search) — not a brain_get call that goes through the same cap.
        """
        from brain_v42.mcp.tools.crud_tools import _format_plan_detail

        plan, chunks = self._make_plan_and_chunks(30)
        result = _format_plan_detail(plan, chunks, max_chars=8000)
        # The circular notice would say: brain_get(entity_type='plan', entity_id=…)
        # with no mention of a different approach
        assert "omis" in result or "omitted" in result  # confirm truncation happened
        # Must NOT suggest a plain brain_get(entity_type='plan', …) without a real param
        # The notice must mention either max_chars or brain_search — not just brain_get alone
        notice_part = result.split("… (")[1] if "… (" in result else result
        assert "brain_search" in notice_part or "max_chars" in notice_part, (
            f"Notice must mention brain_search or max_chars escape hatch, got: {notice_part!r}"
        )


# ===========================================================================
# 11. MAJOR: brain_get_clusters notice must mention max_members_per_cluster
# ===========================================================================


class TestGetClustersNoticeContent:
    """Truncation notice in brain_get_clusters must mention the real escape hatch."""

    def _make_tools(self, edges: list[tuple[str, str]]) -> dict[str, Any]:
        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        mock_graph = AsyncMock()
        mock_graph.get_all_related_edges = AsyncMock(return_value=edges)

        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[])
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx)

        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=None,
            graph_service=mock_graph,
        )
        return mcp.registered

    @pytest.mark.asyncio
    async def test_truncation_notice_mentions_real_param(self) -> None:
        """Truncation notice must NOT mention entity_type (non-existent param).

        Before the fix: the notice said 'filtrez par entity_type pour paginer'
        but brain_get_clusters has no entity_type parameter — non-actionable.
        After the fix: the notice mentions max_members_per_cluster or summary_only.
        """
        first = str(uuid4())
        edges = [(first, str(uuid4())) for _ in range(60)]
        tools = self._make_tools(edges)
        result = await tools["brain_get_clusters"](
            min_size=2, limit=5, summary_only=False, max_members_per_cluster=10
        )
        assert "omis" in result or "omitted" in result  # confirm truncation
        # Must NOT suggest entity_type (non-existent param)
        assert "entity_type" not in result, (
            f"Notice must not mention non-existent 'entity_type' param, got: {result!r}"
        )
        # Must mention a real escape hatch
        assert "max_members_per_cluster" in result or "summary_only" in result, (
            f"Notice must mention max_members_per_cluster or summary_only, got: {result!r}"
        )


# ===========================================================================
# 12. MINOR: test_small_cluster_not_truncated — strengthen assertion
# ===========================================================================


class TestSmallClusterAssertionStrengthened:
    """Replacing the weak negative guard with a strict one."""

    def _make_tools(self, edges: list[tuple[str, str]]) -> dict[str, Any]:
        from brain_v42.mcp.tools.dream_tools import register_dream_tools

        mcp = MockMCP()
        mock_graph = AsyncMock()
        mock_graph.get_all_related_edges = AsyncMock(return_value=edges)

        mock_result = MagicMock()
        mock_result.fetchall = MagicMock(return_value=[])
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx)

        register_dream_tools(
            mcp,
            session_factory=session_factory,
            auto_linker=None,
            graph_service=mock_graph,
        )
        return mcp.registered

    @pytest.mark.asyncio
    async def test_small_cluster_no_truncation_strict(self) -> None:
        """Small cluster (3 members < cap 20): no 'omis' or 'omitted' in output."""
        first = str(uuid4())
        edges = [(first, str(uuid4())) for _ in range(3)]
        tools = self._make_tools(edges)
        result = await tools["brain_get_clusters"](
            min_size=2, limit=5, summary_only=False, max_members_per_cluster=20
        )
        assert "omis" not in result and "omitted" not in result, (
            f"Small cluster should not trigger truncation notice, got: {result!r}"
        )


# ===========================================================================
# 13. MINOR: brain_get_runbook project list — "N omis" notice when cut
# ===========================================================================


class TestRunbookProjectListOmitNotice:
    """brain_get_runbook project list must emit a notice when more exist than limit."""

    def _make_tools_with_limit_plus_one(self, limit: int) -> tuple[dict[str, Any], MagicMock]:
        from brain_v42.mcp.tools.runbook_tools import register_runbook_tools
        from brain_v42.models.runbook import Runbook, RunbookStep

        mcp = MockMCP()
        svc = MagicMock()
        dt = datetime(2026, 1, 1, tzinfo=UTC)

        # Service returns limit+1 runbooks (simulates "there are more")
        rbs = [
            Runbook.model_validate(
                {
                    "id": uuid4(),
                    "title": f"RB{i}",
                    "description": "desc",
                    "project_key": "test",
                    "trigger": "manual",
                    "steps": [RunbookStep(order=1, title="S1")],
                    "created_at": dt,
                    "updated_at": dt,
                }
            )
            for i in range(limit + 1)
        ]
        svc.list_by_project = AsyncMock(return_value=rbs)
        svc.get_by_id = AsyncMock(return_value=None)
        svc.get_by_title = AsyncMock(return_value=None)
        svc.record_execution = AsyncMock(return_value=None)
        register_runbook_tools(mcp, svc)
        return mcp.registered, svc

    @pytest.mark.asyncio
    async def test_runbook_list_emits_notice_when_more_exist(self) -> None:
        """When service returns limit+1 rows, the output must include an omit notice."""
        tools, svc = self._make_tools_with_limit_plus_one(limit=5)
        # Patch list_by_project to be called with limit+1 (fetch limit+1 to detect overflow)
        result = await tools["brain_get_runbook"](project_key="test", limit=5)
        # Either a pagination notice is present OR all rows are shown (acceptable fallback)
        # The key requirement: if more runbooks exist than the limit, a notice appears
        has_notice = "omis" in result or "omitted" in result or "brain_search" in result
        assert has_notice, (
            f"When more runbooks exist than limit, output must include an omit notice. "
            f"Got: {result[:400]!r}"
        )


# ===========================================================================
# 14. MINOR: parsing.py docstring must not claim bare-hex returns None
# ===========================================================================


class TestParseUUIDDocstring:
    """parse_uuid docstring must not contradict the tested bare-hex behavior."""

    def test_docstring_does_not_claim_no_hyphens_returns_none(self) -> None:
        """The docstring 'Returns None for: ... no hyphens' contradicts test_bare_hex_is_valid.

        After the fix: the 'no hyphens' entry must be removed from the Returns None list.
        """
        import inspect

        from brain_v42.mcp.tools.parsing import parse_uuid

        doc = inspect.getdoc(parse_uuid) or ""
        # Old incorrect docstring contained 'no hyphens' in the Returns None section
        assert "no hyphens" not in doc, (
            f"Docstring must not claim 'no hyphens' returns None (bare-hex IS accepted). "
            f"Got docstring:\n{doc}"
        )


# ===========================================================================
# 15. MINOR: _format_plan_detail contiguous truncation (break at first overflow)
# ===========================================================================


class TestPlanDetailContiguousTruncation:
    """_format_plan_detail must stop at first overflow, not skip-and-continue."""

    def _make_plan_and_chunks(self) -> tuple[Any, list[Any]]:
        from brain_v42.models.indexed_plan import IndexedPlan
        from brain_v42.models.indexed_plan_chunk import IndexedPlanChunk

        dt = datetime(2026, 1, 1, tzinfo=UTC)
        plan_id = uuid4()
        plan = IndexedPlan.model_validate(
            {
                "id": plan_id,
                "title": "Test Plan",
                "plan_type": "spec",
                "status": "active",
                "project_key": "test",
                "file_path": "/docs/test.md",
                "chunk_count": 5,
                "word_count": 500,
                "content_hash": "abc",
                "content": "x",
                "freshness_status": "fresh",
                "indexed_at": dt,
                "tags": [],
                "created_at": dt,
                "updated_at": dt,
            }
        )
        # 5 chunks: chunks 0,1 are small (~50 chars each), chunk 2 is huge (exceeds budget),
        # chunks 3,4 are small again — with skip-and-continue §4 and §5 would appear,
        # but with break-at-first-overflow they should NOT appear in the output.
        sizes = [50, 50, 9000, 50, 50]
        chunks = []
        for i, size in enumerate(sizes):
            chunks.append(
                IndexedPlanChunk.model_validate(
                    {
                        "id": uuid4(),
                        "plan_id": plan_id,
                        "section_order": i,
                        "section_path": f"§{i + 1}",
                        "section_title": f"Section {i + 1}",
                        "plan_type": "spec",
                        "content": "y" * size,
                        "word_count": size // 5,
                        "project_key": "test",
                        "status": "active",
                        "tags": [],
                        "created_at": dt,
                    }
                )
            )
        return plan, chunks

    def test_truncation_is_contiguous_prefix(self) -> None:
        """After the first chunk that exceeds budget, all subsequent chunks are omitted.

        Before fix (skip-and-continue): §3 oversized is skipped, §4 and §5 (small)
        still appear — producing non-contiguous output (gaps in §-numbering).
        After fix (break): §3 triggers the cut; §4 and §5 never appear.
        """
        from brain_v42.mcp.tools.crud_tools import _format_plan_detail

        plan, chunks = self._make_plan_and_chunks()
        # Budget of 8000: header ~200 chars, chunks 0+1 ~200 chars total => fine,
        # chunk 2 is 9000 chars => exceeds remaining budget => break here
        result = _format_plan_detail(plan, chunks, max_chars=8_000)
        # §4 and §5 must NOT appear in output (they come after the first overflow)
        assert "§4" not in result, (
            f"§4 must be omitted after first overflow chunk, got: {result[:600]!r}"
        )
        assert "§5" not in result, (
            f"§5 must be omitted after first overflow chunk, got: {result[:600]!r}"
        )
        # §1 and §2 must be present
        assert "§1" in result, "§1 must be rendered (fits within budget)"
        assert "§2" in result, "§2 must be rendered (fits within budget)"


# ===========================================================================
# 9. GLOBAL cap on the number of projects, and its escape hatch (ticket 316a8b50)
# ===========================================================================


class TestRoadmapProjectCapEscapeHatch:
    """The project cap must be bypassable, and its escape hatch bounded.

    Measured on 2026-08-10: 30 projects rendered, 33,943 characters. An escape
    hatch understood as "no cap at all" would return 61,882 characters — we would
    invent a worst case 1.8× worse than the one we claim to fix.
    """

    def _make_tools(self, projects_data: list[dict]) -> dict[str, Any]:
        from brain_v42.mcp.tools.roadmap_tools import register_roadmap_tools

        mcp = MockMCP()
        svc = MagicMock()
        svc.get_roadmap = AsyncMock(
            return_value=[MagicMock(model_dump=MagicMock(return_value=p)) for p in projects_data]
        )
        register_roadmap_tools(mcp, svc, feature_svc=MagicMock(), feature_creation_svc=None)
        return mcp.registered

    @staticmethod
    def _projects(count: int) -> list[dict]:
        return [
            {
                "project_key": f"proj-{i:02d}",
                "current_phase": None,
                "last_activity": f"2026-08-{(i % 28) + 1:02d}T00:00:00",
                "features": [
                    {
                        "name": f"f-{i}",
                        "status": "planned",
                        "artifact_count": {},
                        "last_activity": None,
                        "pinned": False,
                    }
                ],
            }
            for i in range(count)
        ]

    @pytest.mark.asyncio
    async def test_all_projects_is_capped_and_says_so(self) -> None:
        tools = self._make_tools(self._projects(30))
        result = await tools["brain_get_roadmap"]()
        assert sum(1 for line in result.splitlines() if line.startswith("### ")) == 10
        assert "project(s) omitted" in result

    @pytest.mark.asyncio
    async def test_full_true_restores_every_project(self) -> None:
        tools = self._make_tools(self._projects(30))
        result = await tools["brain_get_roadmap"](full=True)
        assert sum(1 for line in result.splitlines() if line.startswith("### ")) == 30
        assert "project(s) omitted" not in result

    @pytest.mark.asyncio
    async def test_full_true_does_not_lift_the_per_project_feature_cap(self) -> None:
        """The escape hatch's NEGATIVE probe.

        It catches the most tempting lazy implementation —
        ``max_features = 10_000 if (project_key or full) else 20`` — which would
        turn ``full=True`` into "no cap" and double the worst case.
        """
        features = [
            {
                "name": f"Feature {i}",
                "status": "planned",
                "artifact_count": {},
                "last_activity": None,
                "pinned": False,
            }
            for i in range(60)
        ]
        tools = self._make_tools(
            [{"project_key": "big-proj", "current_phase": None, "features": features}]
        )

        result = await tools["brain_get_roadmap"](full=True)

        rows = sum(
            1
            for line in result.splitlines()
            if line.startswith("| Feature ") and "Status" not in line
        )
        assert rows <= 20, f"full=True a levé le cap de features : {rows} lignes"
        assert "feature(s) omitted" in result

    @pytest.mark.asyncio
    async def test_a_scoped_call_never_emits_a_project_omission_notice(self) -> None:
        """A regression guard, honestly declared as such.

        It passes trivially today. Its value is later: it fires if anyone applies
        the project cap before the ``if project_key`` test, or stops neutralizing
        it on the scoped branch.
        """
        tools = self._make_tools(self._projects(30))
        result = await tools["brain_get_roadmap"](project_key="proj-01")
        assert "project(s) omitted" not in result
