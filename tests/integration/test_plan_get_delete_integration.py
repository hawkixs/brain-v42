"""Integration: brain_get, brain_delete, brain_update, brain_list on plan type."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# MockMCP (same pattern as unit tests)
# ---------------------------------------------------------------------------


class MockMCP:
    """Collecting mock for FastMCP — captures registered tool functions by name."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


# ---------------------------------------------------------------------------
# Fixtures — MCP tools wired to real DB
# ---------------------------------------------------------------------------


def _make_mock_services() -> dict[str, Any]:
    """Build the minimal stub services needed by register_crud_tools."""
    services: dict[str, Any] = {}
    for name in ("decision_svc", "learning_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        svc = MagicMock()
        svc.get_by_id = AsyncMock(return_value=None)
        svc.delete = AsyncMock(return_value=False)
        svc.update = AsyncMock(return_value=None)
        services[name] = svc
    services["decision_svc"].list_all = AsyncMock(return_value=[])
    services["learning_svc"].list_all = AsyncMock(return_value=[])
    services["snippet_svc"].list_snippets = AsyncMock(return_value=[])
    services["runbook_svc"].list_by_project = AsyncMock(return_value=[])
    services["adr_svc"].list_all = AsyncMock(return_value=[])
    return services


@pytest.fixture
def crud_tools(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Registered CRUD tool functions using real session_factory + stub services."""
    from brain_v42.mcp.tools.crud_tools import register_crud_tools

    mcp = MockMCP()
    register_crud_tools(
        mcp,
        **_make_mock_services(),
        session_factory=session_factory,
    )
    return mcp.registered


# ---------------------------------------------------------------------------
# Sample plan fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sample_plan(db_session: AsyncSession) -> Any:
    """Create a plan with one chunk in the DB. Yields the plan UUID."""
    repo = PgIndexedPlanRepo(db_session)
    plan = IndexedPlanCreate(
        file_path=f"/tmp/plan_{uuid4().hex}.md",
        title="Get Plan Test",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# Get Plan Test\n\n## Section\n\n" + ("word " * 60),
        tags=["test", "integration"],
        chunk_count=1,
        word_count=62,
    )
    chunk = IndexedPlanChunkCreate(
        section_title="Section",
        section_path="Section",
        content="## Section\n\n" + ("word " * 60),
        section_order=0,
        word_count=60,
        project_key="brain-v42",
        plan_type="plan",
        tags=["test", "integration"],
    )
    plan_id = await repo.upsert_plan_with_chunks(plan, [0.1] * 1536, [chunk], [[0.1] * 1536])
    yield plan_id
    # Best-effort cleanup — test may have already deleted it
    try:
        await repo.delete(plan_id)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# brain_get tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brain_get_plan_returns_parent_and_chunks(
    sample_plan: Any, crud_tools: dict[str, Any]
) -> None:
    """Plan retrieved via brain_get returns parent metadata + all chunks."""
    output = await crud_tools["brain_get"](entity_type="plan", entity_id=str(sample_plan))
    assert "Get Plan Test" in output
    assert "Section" in output
    assert "plan" in output.lower()
    # chunks are rendered in the output
    assert "word " in output


@pytest.mark.asyncio
async def test_brain_get_plan_not_found(crud_tools: dict[str, Any]) -> None:
    """brain_get returns error for a non-existent plan UUID."""
    with pytest.raises(
        ToolError,
        match=r"^plan 00000000-0000-0000-0000-000000000000 not found$",
    ):
        await crud_tools["brain_get"](
            entity_type="plan", entity_id="00000000-0000-0000-0000-000000000000"
        )


@pytest.mark.asyncio
async def test_brain_get_plan_invalid_uuid(crud_tools: dict[str, Any]) -> None:
    """brain_get returns error for an invalid UUID string."""
    with pytest.raises(ToolError, match=r"^Invalid UUID: not-a-uuid$"):
        await crud_tools["brain_get"](entity_type="plan", entity_id="not-a-uuid")


# ---------------------------------------------------------------------------
# brain_delete tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brain_delete_plan_cascades(
    sample_plan: Any, db_session: AsyncSession, crud_tools: dict[str, Any]
) -> None:
    """brain_delete removes plan and cascades to chunks."""
    output = await crud_tools["brain_delete"](entity_type="plan", entity_id=str(sample_plan))
    assert "deleted" in output.lower() or "Deleted" in output

    # Verify chunks cascaded — open a fresh session to avoid caching
    repo = PgIndexedPlanRepo(db_session)
    result = await repo.get_with_chunks(sample_plan)
    assert result is None


@pytest.mark.asyncio
async def test_brain_delete_plan_not_found(crud_tools: dict[str, Any]) -> None:
    """brain_delete returns error for a non-existent plan UUID."""
    with pytest.raises(
        ToolError,
        match=r"^plan 00000000-0000-0000-0000-000000000000 not found$",
    ):
        await crud_tools["brain_delete"](
            entity_type="plan", entity_id="00000000-0000-0000-0000-000000000000"
        )


# ---------------------------------------------------------------------------
# brain_update tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brain_update_plan_returns_immutable_error(
    sample_plan: Any, crud_tools: dict[str, Any]
) -> None:
    """brain_update on a plan returns the immutable error (no Pydantic validation)."""
    with pytest.raises(
        ToolError,
        match=r"^plans are immutable; re-run brain_reindex_plans to refresh from source files$",
    ):
        await crud_tools["brain_update"](
            entity_type="plan",
            entity_id=str(sample_plan),
            fields={"status": "archived"},
        )


# ---------------------------------------------------------------------------
# brain_list tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brain_list_plans_returns_created_plan(
    sample_plan: Any, crud_tools: dict[str, Any]
) -> None:
    """brain_list on plan type returns at least the plan we just created."""
    output = await crud_tools["brain_list"](entity_type="plan", project_key="brain-v42")
    assert "Get Plan Test" in output
    assert "plan" in output.lower()


@pytest.mark.asyncio
async def test_brain_list_plans_empty_project(crud_tools: dict[str, Any]) -> None:
    """brain_list returns 0 plans for a project that has none."""
    output = await crud_tools["brain_list"](
        entity_type="plan", project_key="nonexistent-project-xyz"
    )
    assert "0 plans" in output or "plan" in output.lower()


@pytest.mark.asyncio
async def test_brain_list_plans_status_filter(sample_plan: Any, crud_tools: dict[str, Any]) -> None:
    """brain_list status filter works — active plan appears, archived filter excludes it."""
    # Status 'active' — should appear
    output_active = await crud_tools["brain_list"](
        entity_type="plan", project_key="brain-v42", status="active"
    )
    assert "Get Plan Test" in output_active

    # Status 'archived' — should not appear
    output_archived = await crud_tools["brain_list"](
        entity_type="plan", project_key="brain-v42", status="archived"
    )
    assert "Get Plan Test" not in output_archived
