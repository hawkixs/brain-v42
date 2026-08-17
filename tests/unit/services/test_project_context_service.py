"""Unit tests for ProjectContextService.

All tests use AsyncMock/MagicMock — no real DB required.
Tests verify behavior of ProjectContextService as a thin delegation layer.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from brain_v42.models.project_context import ProjectContext, ProjectContextCreate
from brain_v42.services.project_context_service import ProjectContextService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_project_context(**kwargs) -> ProjectContext:
    """Helper to build a ProjectContext with sensible defaults."""
    defaults = {
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
    }
    defaults.update(kwargs)
    return ProjectContext.model_validate(defaults)


def make_mock_repo() -> MagicMock:
    """Mock PgProjectContextRepo with all async methods."""
    from brain_v42.repositories.pg_project_context import PgProjectContextRepo

    repo = MagicMock(spec=PgProjectContextRepo)
    repo.get_or_create = AsyncMock()
    repo.get_by_key = AsyncMock()
    repo.update_focus = AsyncMock()
    repo.refresh_counts = AsyncMock()
    return repo


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


class TestImportability:
    def test_importable_from_module(self) -> None:
        """ProjectContextService is importable from brain_v42.services.project_context_service."""
        from brain_v42.services.project_context_service import ProjectContextService  # noqa: F401

    def test_importable_from_package(self) -> None:
        """ProjectContextService is importable from brain_v42.services (barrel)."""
        from brain_v42.services import ProjectContextService  # noqa: F401


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_constructor_stores_repo(self) -> None:
        """Constructor accepts pg_repo and stores it as self.repo."""
        from brain_v42.services.project_context_service import ProjectContextService

        mock_repo = make_mock_repo()
        svc = ProjectContextService(pg_repo=mock_repo)
        assert svc.repo is mock_repo

    def test_no_embedding_service_dependency(self) -> None:
        """ProjectContextService does NOT require EmbeddingService — only pg_repo."""
        import inspect

        from brain_v42.services.project_context_service import ProjectContextService

        sig = inspect.signature(ProjectContextService.__init__)
        param_names = list(sig.parameters.keys())
        # Must have pg_repo, must NOT have embedding_svc
        assert "pg_repo" in param_names
        assert "embedding_svc" not in param_names


# ---------------------------------------------------------------------------
# get_or_create()
# ---------------------------------------------------------------------------


class TestGetOrCreate:
    async def test_delegates_to_repo_and_returns_result(self) -> None:
        """get_or_create(data) delegates to repo.get_or_create(data) and returns result."""
        from brain_v42.services.project_context_service import ProjectContextService

        mock_repo = make_mock_repo()
        ctx = make_project_context()
        mock_repo.get_or_create.return_value = ctx

        svc = ProjectContextService(pg_repo=mock_repo)
        data = ProjectContextCreate(
            project_key="brain-v42",
            name="Brain V42",
            description="Test project",
        )
        result = await svc.get_or_create(data)

        mock_repo.get_or_create.assert_called_once_with(data)
        assert result is ctx

    async def test_get_or_create_is_async(self) -> None:
        """get_or_create is a coroutine (async)."""
        import inspect

        from brain_v42.services.project_context_service import ProjectContextService

        assert inspect.iscoroutinefunction(ProjectContextService.get_or_create)

    async def test_get_or_create_passes_plan_scan_paths(self) -> None:
        """get_or_create passes plan_scan_paths through to repo."""
        from brain_v42.services.project_context_service import ProjectContextService

        mock_repo = make_mock_repo()
        ctx = make_project_context(plan_scan_paths=["/home/plans"])
        mock_repo.get_or_create.return_value = ctx

        svc = ProjectContextService(pg_repo=mock_repo)
        data = ProjectContextCreate(
            project_key="brain-v42",
            name="Brain V42",
            description="Test project",
            plan_scan_paths=["/home/plans"],
        )
        result = await svc.get_or_create(data)

        assert result.plan_scan_paths == ["/home/plans"]

    async def test_get_or_create_passes_gitlab_project_path(self) -> None:
        """get_or_create passes gitlab_project_path through to repo."""
        from brain_v42.services.project_context_service import ProjectContextService

        mock_repo = make_mock_repo()
        ctx = make_project_context(gitlab_project_path="hawkixs_project/brain_v42")
        mock_repo.get_or_create.return_value = ctx

        svc = ProjectContextService(pg_repo=mock_repo)
        data = ProjectContextCreate(
            project_key="brain-v42",
            name="Brain V42",
            description="Test project",
            gitlab_project_path="hawkixs_project/brain_v42",
        )
        result = await svc.get_or_create(data)

        assert result.gitlab_project_path == "hawkixs_project/brain_v42"


# ---------------------------------------------------------------------------
# get_by_key()
# ---------------------------------------------------------------------------


class TestGetByKey:
    async def test_delegates_to_repo_and_returns_result(self) -> None:
        """get_by_key(project_key) delegates to repo.get_by_key and returns result."""
        from brain_v42.services.project_context_service import ProjectContextService

        mock_repo = make_mock_repo()
        ctx = make_project_context()
        mock_repo.get_by_key.return_value = ctx

        svc = ProjectContextService(pg_repo=mock_repo)
        result = await svc.get_by_key("brain-v42")

        mock_repo.get_by_key.assert_called_once_with("brain-v42")
        assert result is ctx

    async def test_returns_none_when_not_found(self) -> None:
        """get_by_key() propagates None when repo returns None (not found)."""
        from brain_v42.services.project_context_service import ProjectContextService

        mock_repo = make_mock_repo()
        mock_repo.get_by_key.return_value = None

        svc = ProjectContextService(pg_repo=mock_repo)
        result = await svc.get_by_key("nonexistent-project")

        assert result is None

    async def test_get_by_key_is_async(self) -> None:
        """get_by_key is a coroutine (async)."""
        import inspect

        from brain_v42.services.project_context_service import ProjectContextService

        assert inspect.iscoroutinefunction(ProjectContextService.get_by_key)


# ---------------------------------------------------------------------------
# update_focus()
# ---------------------------------------------------------------------------


class TestUpdateFocus:
    async def test_delegates_to_repo_and_returns_result(self) -> None:
        """update_focus(project_key, focus) delegates to repo.update_focus and returns result."""
        from brain_v42.services.project_context_service import ProjectContextService

        mock_repo = make_mock_repo()
        ctx = make_project_context(current_focus="Working on M3 services")
        mock_repo.update_focus.return_value = ctx

        svc = ProjectContextService(pg_repo=mock_repo)
        result = await svc.update_focus("brain-v42", "Working on M3 services")

        mock_repo.update_focus.assert_called_once_with("brain-v42", "Working on M3 services", None)
        assert result is ctx

    async def test_delegates_with_blockers(self) -> None:
        """update_focus(project_key, focus, blockers) passes blockers to repo."""
        from brain_v42.services.project_context_service import ProjectContextService

        mock_repo = make_mock_repo()
        ctx = make_project_context(current_focus="M3 services", blockers=["ONNX not loaded"])
        mock_repo.update_focus.return_value = ctx

        svc = ProjectContextService(pg_repo=mock_repo)
        result = await svc.update_focus("brain-v42", "M3 services", blockers=["ONNX not loaded"])

        mock_repo.update_focus.assert_called_once_with(
            "brain-v42", "M3 services", ["ONNX not loaded"]
        )
        assert result is ctx

    async def test_returns_none_when_not_found(self) -> None:
        """update_focus() propagates None when project_key does not exist."""
        from brain_v42.services.project_context_service import ProjectContextService

        mock_repo = make_mock_repo()
        mock_repo.update_focus.return_value = None

        svc = ProjectContextService(pg_repo=mock_repo)
        result = await svc.update_focus("nonexistent", "some focus")

        assert result is None

    async def test_update_focus_is_async(self) -> None:
        """update_focus is a coroutine (async)."""
        import inspect

        from brain_v42.services.project_context_service import ProjectContextService

        assert inspect.iscoroutinefunction(ProjectContextService.update_focus)


# ---------------------------------------------------------------------------
# refresh_counts()
# ---------------------------------------------------------------------------


class TestRefreshCounts:
    async def test_delegates_to_repo_and_returns_result(self) -> None:
        """refresh_counts(project_key) delegates to repo.refresh_counts and returns result."""
        from brain_v42.services.project_context_service import ProjectContextService

        mock_repo = make_mock_repo()
        ctx = make_project_context(decisions_count=3, learnings_count=5)
        mock_repo.refresh_counts.return_value = ctx

        svc = ProjectContextService(pg_repo=mock_repo)
        result = await svc.refresh_counts("brain-v42")

        mock_repo.refresh_counts.assert_called_once_with("brain-v42")
        assert result is ctx

    async def test_returns_none_when_not_found(self) -> None:
        """refresh_counts() propagates None when project_key does not exist."""
        from brain_v42.services.project_context_service import ProjectContextService

        mock_repo = make_mock_repo()
        mock_repo.refresh_counts.return_value = None

        svc = ProjectContextService(pg_repo=mock_repo)
        result = await svc.refresh_counts("nonexistent")

        assert result is None

    async def test_refresh_counts_is_async(self) -> None:
        """refresh_counts is a coroutine (async)."""
        import inspect

        from brain_v42.services.project_context_service import ProjectContextService

        assert inspect.iscoroutinefunction(ProjectContextService.refresh_counts)


# ---------------------------------------------------------------------------
# list_all()
# ---------------------------------------------------------------------------


class TestListAll:
    async def test_list_all_delegates_to_repo(self) -> None:
        """list_all() delegates to repo.list_all() and returns result."""
        from brain_v42.services.project_context_service import ProjectContextService

        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[])
        svc = ProjectContextService(pg_repo=repo)
        result = await svc.list_all()
        assert result == []
        repo.list_all.assert_called_once()

    async def test_list_all_returns_project_contexts(self) -> None:
        """list_all() returns list of ProjectContext objects from repo."""
        from brain_v42.services.project_context_service import ProjectContextService

        ctx = make_project_context()
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[ctx])
        svc = ProjectContextService(pg_repo=repo)
        result = await svc.list_all()
        assert len(result) == 1
        assert result[0].project_key == ctx.project_key

    async def test_list_all_is_async(self) -> None:
        """list_all is a coroutine (async)."""
        import inspect

        from brain_v42.services.project_context_service import ProjectContextService

        assert inspect.iscoroutinefunction(ProjectContextService.list_all)


# ---------------------------------------------------------------------------
# Graph write-through
# ---------------------------------------------------------------------------


class TestProjectContextGraphWriteThrough:
    async def test_get_or_create_calls_graph_upsert(self) -> None:
        """get_or_create() upserts Project through its stable business key."""
        mock_repo = make_mock_repo()
        ctx = make_project_context(project_key="brain-v42", name="Brain V42")
        mock_repo.get_or_create.return_value = ctx
        mock_graph = MagicMock()
        mock_graph.upsert_project = AsyncMock()

        svc = ProjectContextService(pg_repo=mock_repo, graph=mock_graph)
        data = ProjectContextCreate(
            project_key="brain-v42",
            name="Brain V42",
            description="Test",
        )
        await svc.get_or_create(data)

        mock_graph.upsert_project.assert_awaited_once_with("brain-v42", ctx.id, "Brain V42")

    async def test_get_or_create_graph_failure_does_not_raise(self) -> None:
        """get_or_create() succeeds even if graph.upsert_project raises."""
        mock_repo = make_mock_repo()
        ctx = make_project_context()
        mock_repo.get_or_create.return_value = ctx
        mock_graph = MagicMock()
        mock_graph.upsert_project = AsyncMock(side_effect=RuntimeError("neo4j down"))

        svc = ProjectContextService(pg_repo=mock_repo, graph=mock_graph)
        data = ProjectContextCreate(
            project_key="brain-v42",
            name="Brain V42",
            description="Test",
        )
        result = await svc.get_or_create(data)
        assert result is ctx

    async def test_get_or_create_without_graph_works(self) -> None:
        """get_or_create() works normally when graph=None."""
        mock_repo = make_mock_repo()
        ctx = make_project_context()
        mock_repo.get_or_create.return_value = ctx

        svc = ProjectContextService(pg_repo=mock_repo)
        data = ProjectContextCreate(
            project_key="brain-v42",
            name="Brain V42",
            description="Test",
        )
        result = await svc.get_or_create(data)
        assert result is ctx
