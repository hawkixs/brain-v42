"""Unit tests for MCP project context tools:
brain_set_project_context, brain_update_project_focus, brain_list_projects.

brain_get_project_context has been removed — use brain_session_start instead.
All service calls are mocked with AsyncMock — no real DB needed.
FastMCP is mocked as a simple decorator collector.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP
from pydantic import ValidationError

from brain_v42.models.project_context import ProjectContext
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_project_context(**kwargs: Any) -> ProjectContext:
    """Build a ProjectContext with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project_key": "brain-v42",
        "name": "Brain V42",
        "description": "Second Cerveau MCP server",
        "languages": ["python"],
        "frameworks": ["fastmcp"],
        "databases": ["postgresql"],
        "code_style": None,
        "git_workflow": None,
        "test_strategy": None,
        "current_phase": None,
        "current_focus": None,
        "blockers": [],
        "related_projects": [],
        "local_path": None,
        "repo_url": None,
        "metadata": {},
        "decisions_count": 0,
        "learnings_count": 0,
        "snippets_count": 0,
        "runbooks_count": 0,
        "adrs_count": 0,
        "created_at": datetime(2024, 1, 1),
        "updated_at": datetime(2024, 1, 1),
    }
    defaults.update(kwargs)
    return ProjectContext.model_validate(defaults)


class MockMCP:
    """Collecting mock for FastMCP — stores registered tools by function name."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = capture_tool_errors(fn)
            return fn

        return decorator


@pytest.fixture
def mock_project_context_svc() -> AsyncMock:
    """Mock ProjectContextService with all async methods."""
    svc = AsyncMock()
    return svc


@pytest.fixture
def mock_roadmap_svc() -> AsyncMock:
    """Mock RoadmapService with all async methods."""
    svc = AsyncMock()

    async def update_project_focus(
        _project_key: str,
        current_focus: str,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            current_focus=current_focus.strip(),
            focus_revision=4,
            features_updated=("Core Monitoring",),
            features_unpinned=(),
        )

    svc.update_project_focus = AsyncMock(
        side_effect=update_project_focus,
    )
    svc.update_feature_statuses = AsyncMock(return_value=0)
    svc.unpin_features = AsyncMock(return_value=0)
    return svc


@pytest.fixture
def tools(mock_project_context_svc: AsyncMock) -> tuple[dict[str, Any], AsyncMock]:
    """Register project context tools and return (registered_tools_dict, mock_svc)."""
    from brain_v42.mcp.tools.project_context_tools import register_project_context_tools

    mcp = MockMCP()
    register_project_context_tools(mcp, mock_project_context_svc)
    return mcp.registered, mock_project_context_svc


@pytest.fixture
def tools_with_roadmap(
    mock_project_context_svc: AsyncMock,
    mock_roadmap_svc: AsyncMock,
) -> tuple[dict[str, Any], AsyncMock, AsyncMock]:
    """Register project context tools with roadmap_svc and return (tools, ctx_svc, roadmap_svc)."""
    from brain_v42.mcp.tools.project_context_tools import register_project_context_tools

    mcp = MockMCP()
    register_project_context_tools(mcp, mock_project_context_svc, roadmap_svc=mock_roadmap_svc)
    return mcp.registered, mock_project_context_svc, mock_roadmap_svc


# ---------------------------------------------------------------------------
# brain_set_project_context
# ---------------------------------------------------------------------------


class TestBrainSetProjectContext:
    async def test_returns_confirmation_string(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_set_project_context returns confirmation string with project_key."""
        registered, svc = tools
        ctx = make_project_context(project_key="my-proj", name="My Project")
        svc.get_or_create.return_value = ctx

        result = await registered["brain_set_project_context"](
            project_key="my-proj",
            name="My Project",
            description="A test project",
        )

        assert isinstance(result, str)
        assert "Project context set" in result
        assert "my-proj" in result

    async def test_calls_get_or_create_with_correct_fields(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_set_project_context calls svc.get_or_create() with correct ProjectContextCreate."""
        registered, svc = tools
        ctx = make_project_context()
        svc.get_or_create.return_value = ctx

        await registered["brain_set_project_context"](
            project_key="brain-v42",
            name="Brain V42",
            description="Second Cerveau",
            languages=["python"],
            frameworks=["fastmcp"],
            databases=["postgresql"],
        )

        svc.get_or_create.assert_called_once()
        call_arg = svc.get_or_create.call_args[0][0]
        assert call_arg.project_key == "brain-v42"
        assert call_arg.name == "Brain V42"
        assert call_arg.description == "Second Cerveau"
        assert call_arg.languages == ["python"]
        assert call_arg.frameworks == ["fastmcp"]
        assert call_arg.databases == ["postgresql"]

    async def test_defaults_languages_frameworks_databases_to_empty_lists(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_set_project_context defaults list fields to [] when None is passed."""
        registered, svc = tools
        ctx = make_project_context()
        svc.get_or_create.return_value = ctx

        await registered["brain_set_project_context"](
            project_key="brain-v42",
            name="Brain V42",
            description="desc",
            languages=None,
            frameworks=None,
            databases=None,
        )

        call_arg = svc.get_or_create.call_args[0][0]
        assert call_arg.languages == []
        assert call_arg.frameworks == []
        assert call_arg.databases == []

    async def test_defaults_blockers_to_empty_list(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_set_project_context defaults blockers to [] when None is passed."""
        registered, svc = tools
        ctx = make_project_context()
        svc.get_or_create.return_value = ctx

        await registered["brain_set_project_context"](
            project_key="brain-v42",
            name="Brain V42",
            description="desc",
            blockers=None,
        )

        call_arg = svc.get_or_create.call_args[0][0]
        assert call_arg.blockers == []

    async def test_defaults_related_projects_to_empty_list(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_set_project_context defaults related_projects to [] when None is passed."""
        registered, svc = tools
        ctx = make_project_context()
        svc.get_or_create.return_value = ctx

        await registered["brain_set_project_context"](
            project_key="brain-v42",
            name="Brain V42",
            description="desc",
            related_projects=None,
        )

        call_arg = svc.get_or_create.call_args[0][0]
        assert call_arg.related_projects == []

    async def test_passes_optional_nullable_fields(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_set_project_context passes optional fields like code_style, git_workflow."""
        registered, svc = tools
        ctx = make_project_context()
        svc.get_or_create.return_value = ctx

        await registered["brain_set_project_context"](
            project_key="brain-v42",
            name="Brain V42",
            description="desc",
            code_style="black+ruff",
            git_workflow="trunk-based",
            test_strategy="TDD",
            current_phase="M3",
            current_focus="Services layer",
        )

        call_arg = svc.get_or_create.call_args[0][0]
        assert call_arg.code_style == "black+ruff"
        assert call_arg.git_workflow == "trunk-based"
        assert call_arg.test_strategy == "TDD"
        assert call_arg.current_phase == "M3"
        assert call_arg.current_focus == "Services layer"

    async def test_never_returns_error(self, tools: tuple[dict[str, Any], AsyncMock]) -> None:
        """brain_set_project_context never returns an error (get_or_create always succeeds)."""
        registered, svc = tools
        ctx = make_project_context()
        svc.get_or_create.return_value = ctx

        result = await registered["brain_set_project_context"](
            project_key="brain-v42",
            name="Brain V42",
            description="desc",
        )

        assert isinstance(result, str)
        assert "Project context set" in result

    async def test_passes_plan_scan_paths(self, tools: tuple[dict[str, Any], AsyncMock]) -> None:
        """brain_set_project_context passes plan_scan_paths to ProjectContextCreate."""
        registered, svc = tools
        ctx = make_project_context()
        svc.get_or_create.return_value = ctx

        await registered["brain_set_project_context"](
            project_key="brain-v42",
            name="Brain V42",
            description="desc",
            plan_scan_paths=["/home/user/plans", "/tmp/docs"],
        )

        call_arg = svc.get_or_create.call_args[0][0]
        assert call_arg.plan_scan_paths == ["/home/user/plans", "/tmp/docs"]

    async def test_plan_scan_paths_defaults_to_empty_list(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_set_project_context defaults plan_scan_paths to [] when None."""
        registered, svc = tools
        ctx = make_project_context()
        svc.get_or_create.return_value = ctx

        await registered["brain_set_project_context"](
            project_key="brain-v42",
            name="Brain V42",
            description="desc",
        )

        call_arg = svc.get_or_create.call_args[0][0]
        assert call_arg.plan_scan_paths == []

    async def test_passes_gitlab_project_path(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_set_project_context passes gitlab_project_path to ProjectContextCreate."""
        registered, svc = tools
        ctx = make_project_context()
        svc.get_or_create.return_value = ctx

        await registered["brain_set_project_context"](
            project_key="brain-v42",
            name="Brain V42",
            description="desc",
            gitlab_project_path="hawkixs_project/brain_v42",
        )

        call_arg = svc.get_or_create.call_args[0][0]
        assert call_arg.gitlab_project_path == "hawkixs_project/brain_v42"

    async def test_gitlab_project_path_defaults_to_none(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_set_project_context defaults gitlab_project_path to None when not provided."""
        registered, svc = tools
        ctx = make_project_context()
        svc.get_or_create.return_value = ctx

        await registered["brain_set_project_context"](
            project_key="brain-v42",
            name="Brain V42",
            description="desc",
        )

        call_arg = svc.get_or_create.call_args[0][0]
        assert call_arg.gitlab_project_path is None


# ---------------------------------------------------------------------------
# brain_update_project_focus
# ---------------------------------------------------------------------------


class TestBrainUpdateProjectFocus:
    async def test_schema_requires_non_negative_strict_revision(self) -> None:
        from brain_v42.mcp.tools.project_context_tools import register_project_context_tools

        context_svc = AsyncMock()
        roadmap_svc = AsyncMock()
        server = FastMCP("project-focus-cas-test")
        register_project_context_tools(server, context_svc, roadmap_svc=roadmap_svc)
        tool = await server.get_tool("brain_update_project_focus")
        assert tool is not None
        revision_schema = tool.parameters["properties"]["expected_focus_revision"]
        assert revision_schema["minimum"] == 0

        with pytest.raises(ValidationError):
            await tool.run(
                {
                    "project_key": "brain-v42",
                    "current_focus": "stabilisation",
                    "expected_focus_revision": True,
                }
            )

        roadmap_svc.update_project_focus.assert_not_called()

    async def test_uses_atomic_cas_coordinator_and_returns_revision(
        self,
        tools_with_roadmap: tuple[dict[str, Any], AsyncMock, AsyncMock],
    ) -> None:
        registered, ctx_svc, roadmap_svc = tools_with_roadmap

        result = await registered["brain_update_project_focus"](
            project_key="brain-v42",
            current_focus="Stabilisation",
            expected_focus_revision=3,
            blockers=["none"],
            feature_status={"Core Monitoring": "deployed"},
        )

        roadmap_svc.update_project_focus.assert_awaited_once_with(
            "brain-v42",
            "Stabilisation",
            expected_focus_revision=3,
            blockers=["none"],
            feature_status={"Core Monitoring": "deployed"},
            unpin=None,
        )
        ctx_svc.update_focus.assert_not_awaited()
        assert "focus_revision:4" in result

    async def test_conflict_is_actionable_and_does_not_fallback_to_unsafe_write(
        self,
        tools_with_roadmap: tuple[dict[str, Any], AsyncMock, AsyncMock],
    ) -> None:
        from brain_v42.services import roadmap_service

        registered, ctx_svc, roadmap_svc = tools_with_roadmap
        roadmap_svc.update_project_focus.side_effect = roadmap_service.ProjectFocusConflictError(
            current_focus="newer focus",
            current_revision=8,
        )

        result = await registered["brain_update_project_focus"](
            project_key="brain-v42",
            current_focus="stale write",
            expected_focus_revision=7,
        )

        assert result and result[0].isalnum()
        assert "revision 8" in result
        assert "newer focus" in result
        ctx_svc.update_focus.assert_not_awaited()

    async def test_missing_atomic_coordinator_refuses_every_write(
        self,
        tools: tuple[dict[str, Any], AsyncMock],
    ) -> None:
        registered, ctx_svc = tools

        result = await registered["brain_update_project_focus"](
            project_key="brain-v42",
            current_focus="must not persist",
            expected_focus_revision=0,
        )

        assert result and result[0].isalnum()
        assert "unavailable" in result.lower()
        ctx_svc.update_focus.assert_not_awaited()

    async def test_returns_confirmation_with_project_focus_and_revision(
        self,
        tools_with_roadmap: tuple[dict[str, Any], AsyncMock, AsyncMock],
    ) -> None:
        registered, _, _ = tools_with_roadmap

        result = await registered["brain_update_project_focus"](
            project_key="brain-v42",
            current_focus="Working on M3 services",
            expected_focus_revision=3,
        )

        assert "Focus updated" in result
        assert "Working on M3 services" in result
        assert "brain-v42" in result
        assert "focus_revision:4" in result

    async def test_confirmation_uses_normalized_committed_focus(
        self,
        tools_with_roadmap: tuple[dict[str, Any], AsyncMock, AsyncMock],
    ) -> None:
        registered, _, _ = tools_with_roadmap

        result = await registered["brain_update_project_focus"](
            project_key="brain-v42",
            current_focus="  Stabilisation  ",
            expected_focus_revision=3,
        )

        assert "focus:Stabilisation" in result
        assert "focus:  Stabilisation  " not in result

    async def test_defaults_optional_batch_parts_to_none(
        self,
        tools_with_roadmap: tuple[dict[str, Any], AsyncMock, AsyncMock],
    ) -> None:
        registered, _, roadmap_svc = tools_with_roadmap

        await registered["brain_update_project_focus"](
            project_key="brain-v42",
            current_focus="Implementing MCP tools",
            expected_focus_revision=3,
        )

        roadmap_svc.update_project_focus.assert_awaited_once_with(
            "brain-v42",
            "Implementing MCP tools",
            expected_focus_revision=3,
            blockers=None,
            feature_status=None,
            unpin=None,
        )

    async def test_passes_blockers_and_roadmap_changes_in_one_call(
        self,
        tools_with_roadmap: tuple[dict[str, Any], AsyncMock, AsyncMock],
    ) -> None:
        registered, _, roadmap_svc = tools_with_roadmap

        await registered["brain_update_project_focus"](
            project_key="brain-v42",
            current_focus="M3 services",
            expected_focus_revision=3,
            blockers=["ONNX not loaded"],
            feature_status={"Core Monitoring": "deployed", "GPU Collector": "building"},
            unpin=["Old Feature"],
        )

        roadmap_svc.update_project_focus.assert_awaited_once_with(
            "brain-v42",
            "M3 services",
            expected_focus_revision=3,
            blockers=["ONNX not loaded"],
            feature_status={"Core Monitoring": "deployed", "GPU Collector": "building"},
            unpin=["Old Feature"],
        )
        roadmap_svc.update_feature_statuses.assert_not_awaited()
        roadmap_svc.unpin_features.assert_not_awaited()

    async def test_returns_project_not_found_error_without_unsafe_fallback(
        self,
        tools_with_roadmap: tuple[dict[str, Any], AsyncMock, AsyncMock],
    ) -> None:
        from brain_v42.services import roadmap_service

        registered, ctx_svc, roadmap_svc = tools_with_roadmap
        roadmap_svc.update_project_focus.side_effect = roadmap_service.ProjectFocusNotFoundError(
            'Project "nonexistent" not found'
        )

        result = await registered["brain_update_project_focus"](
            project_key="nonexistent",
            current_focus="something",
            expected_focus_revision=0,
        )

        assert result and result[0].isalnum()
        assert "nonexistent" in result
        assert "not found" in result
        ctx_svc.update_focus.assert_not_awaited()

    async def test_validation_error_is_returned_without_unsafe_fallback(
        self,
        tools_with_roadmap: tuple[dict[str, Any], AsyncMock, AsyncMock],
    ) -> None:
        from brain_v42.services import roadmap_service

        registered, ctx_svc, roadmap_svc = tools_with_roadmap
        roadmap_svc.update_project_focus.side_effect = roadmap_service.ProjectFocusValidationError(
            "invalid feature status"
        )

        result = await registered["brain_update_project_focus"](
            project_key="brain-v42",
            current_focus="M3 services",
            expected_focus_revision=3,
            feature_status={"GPU Collector": "in_progress"},
        )

        assert result and result[0].isalnum()
        assert "invalid feature status" in result
        ctx_svc.update_focus.assert_not_awaited()


class TestBrainListProjects:
    async def test_returns_formatted_list_of_projects(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_list_projects returns formatted markdown with project details."""
        registered, svc = tools
        ctx1 = make_project_context(project_key="brain-v42", name="Brain V42", current_focus="M5")
        ctx2 = make_project_context(project_key="watchk", name="Watchk", current_phase="beta")
        svc.list_all.return_value = [ctx1, ctx2]

        result = await registered["brain_list_projects"]()

        assert isinstance(result, str)
        assert "2 projects" in result
        assert "brain-v42" in result
        assert "watchk" in result

    async def test_calls_service_list_all(self, tools: tuple[dict[str, Any], AsyncMock]) -> None:
        """brain_list_projects calls project_context_svc.list_all()."""
        registered, svc = tools
        svc.list_all.return_value = []

        await registered["brain_list_projects"]()

        svc.list_all.assert_called_once()

    async def test_returns_empty_result_string(
        self, tools: tuple[dict[str, Any], AsyncMock]
    ) -> None:
        """brain_list_projects returns '0 projects' when no projects exist."""
        registered, svc = tools
        svc.list_all.return_value = []

        result = await registered["brain_list_projects"]()

        assert isinstance(result, str)
        assert "0 projects" in result

    async def test_includes_focus_and_phase(self, tools: tuple[dict[str, Any], AsyncMock]) -> None:
        """brain_list_projects includes current_focus and current_phase in output."""
        registered, svc = tools
        ctx = make_project_context(
            project_key="brain-v42",
            name="Brain V42",
            current_focus="Working on tools",
            current_phase="M5",
        )
        svc.list_all.return_value = [ctx]

        result = await registered["brain_list_projects"]()

        assert "Working on tools" in result
        assert "M5" in result


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


class TestImportability:
    def test_register_function_importable_from_module(self) -> None:
        """register_project_context_tools is importable from project_context_tools module."""
        from brain_v42.mcp.tools.project_context_tools import (  # noqa: F401
            register_project_context_tools,
        )

    def test_register_function_importable_from_package(self) -> None:
        """register_project_context_tools is importable from brain_v42.mcp.tools (barrel)."""
        from brain_v42.mcp.tools import register_project_context_tools  # noqa: F401


class TestSetProjectContextSchemaContract:
    """Item 8 d'af3b58dd : le plus gros inputSchema du catalogue, sous contrat.

    16 paramètres à plat ne se réduisent pas sans casser la compat des
    appelants (la forme plate EST le schéma public) — ce qui se contracte :
    (1) la signature ne peut pas dériver du modèle qu'elle alimente — un 17e
    paramètre fantôme ou un champ du modèle non exposé rougit ici ; (2) chaque
    paramètre est documenté dans le docstring Args — un schéma de cette taille
    sans doc par champ est illisible pour l'agent qui le remplit.
    """

    def _tool_fn(self):
        import inspect

        from brain_v42.mcp.tools.project_context_tools import register_project_context_tools

        captured = {}

        class _MCP:
            def tool(self, **_kw):
                def deco(fn):
                    captured[fn.__name__] = fn
                    return fn

                return deco

        from unittest.mock import MagicMock

        register_project_context_tools(_MCP(), MagicMock())
        return captured["brain_set_project_context"], inspect

    def test_every_parameter_feeds_the_create_model_and_nothing_drifts(self) -> None:
        from brain_v42.models.project_context import ProjectContextCreate

        fn, inspect_mod = self._tool_fn()
        params = set(inspect_mod.signature(fn).parameters)

        assert params <= set(ProjectContextCreate.model_fields), (
            "un paramètre du tool ne correspond à aucun champ du modèle : "
            f"{sorted(params - set(ProjectContextCreate.model_fields))}"
        )

    def test_every_parameter_is_documented_in_the_args_block(self) -> None:
        fn, inspect_mod = self._tool_fn()
        doc = fn.__doc__ or ""

        undocumented = [
            name for name in inspect_mod.signature(fn).parameters if f"{name}:" not in doc
        ]
        assert not undocumented, f"paramètres sans doc Args : {undocumented}"
