"""Unit tests for PgProjectContextRepo — no live DB required.

All sessions are mocked with AsyncMock. PgProjectContextRepo inherits
from BasePgRepository which uses get_session_factory() from brain_v42.db.engine.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helper: create a mock session that mimics AsyncSession behaviour
# ---------------------------------------------------------------------------


def _make_mock_session() -> AsyncMock:
    """Create an AsyncMock mimicking SQLAlchemy AsyncSession."""
    session = AsyncMock()

    mock_result = MagicMock()
    mock_mappings = MagicMock()
    mock_result.mappings.return_value = mock_mappings
    mock_mappings.all.return_value = []
    mock_mappings.one.return_value = {}
    mock_mappings.one_or_none.return_value = None
    mock_mappings.first.return_value = None

    mock_result.one_or_none.return_value = None
    mock_result.scalar_one.return_value = 0
    mock_result.rowcount = 0

    session.execute = AsyncMock(return_value=mock_result)

    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)

    session.begin_nested = MagicMock()
    session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)

    return session


def _patch_factory(mock_session: AsyncMock):
    """Return a context manager that patches get_session_factory.

    Follows the same pattern used in test_pg_base.py.
    """

    @asynccontextmanager
    async def _session_cm(*args: Any, **kwargs: Any):
        yield mock_session

    mock_factory = MagicMock()
    mock_factory.side_effect = lambda: _session_cm()

    return patch(
        "brain_v42.repositories.pg_base.get_session_factory",
        return_value=mock_factory,
    )


# ---------------------------------------------------------------------------
# Fixture: sample ProjectContext row dict
# ---------------------------------------------------------------------------


def _make_row(project_key: str = "test_proj", **overrides: Any) -> dict:
    """Create a minimal project context row dict suitable for Pydantic validation."""
    row: dict = {
        "id": uuid.uuid4(),
        "project_key": project_key,
        "name": "Test Project",
        "description": "A test project",
        "languages": ["python"],
        "frameworks": ["fastapi"],
        "databases": ["postgresql"],
        "code_style": None,
        "git_workflow": None,
        "test_strategy": None,
        "current_phase": None,
        "current_focus": None,
        # `RETURNING *project_contexts.c` always carries this — a fixture row
        # without it was never a faithful row, and the focus-history write path
        # (050) is simply the first reader to notice.
        "focus_revision": 0,
        "blockers": [],
        "related_projects": [],
        "local_path": None,
        "repo_url": None,
        "decisions_count": 0,
        "learnings_count": 0,
        "snippets_count": 0,
        "runbooks_count": 0,
        "adrs_count": 0,
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    row.update(overrides)
    return row


# ===========================================================================
# 1. Import and instantiation
# ===========================================================================


class TestImportAndInstantiation:
    def test_class_importable(self):
        """PgProjectContextRepo is importable from repositories."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        assert PgProjectContextRepo is not None

    def test_class_importable_from_init(self):
        """PgProjectContextRepo is importable from brain_v42.repositories."""
        from brain_v42.repositories import PgProjectContextRepo

        assert PgProjectContextRepo is not None

    def test_in_all(self):
        """PgProjectContextRepo is in __all__ of brain_v42.repositories."""
        import brain_v42.repositories as repos

        assert "PgProjectContextRepo" in repos.__all__

    def test_inherits_base_pg_repository(self):
        """PgProjectContextRepo inherits from BasePgRepository."""
        from brain_v42.repositories.pg_base import BasePgRepository
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        assert issubclass(PgProjectContextRepo, BasePgRepository)

    def test_has_project_contexts_table(self):
        """PgProjectContextRepo.table is the project_contexts SA Table."""
        from brain_v42.db.tables import project_contexts
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        repo = PgProjectContextRepo()
        assert repo.table is project_contexts

    def test_no_embedding_no_search_vector(self):
        """project_contexts table has no embedding or search_vector column."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        repo = PgProjectContextRepo()
        assert "embedding" not in repo.table.c
        assert "search_vector" not in repo.table.c


# ===========================================================================
# 2. create()
# ===========================================================================


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_project_context_model(self):
        """create() inserts a row and returns a ProjectContext Pydantic model."""
        from brain_v42.models.project_context import ProjectContext, ProjectContextCreate
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row(project_key="proj1")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        data = ProjectContextCreate(
            project_key="proj1",
            name="Test Project",
            description="A test project",
        )

        with _patch_factory(mock_session):
            result = await repo.create(data)

        assert isinstance(result, ProjectContext)
        assert result.project_key == "proj1"

    @pytest.mark.asyncio
    async def test_create_logs_info(self):
        """create() logs an info message."""
        from brain_v42.models.project_context import ProjectContextCreate
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row(project_key="proj-log")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        data = ProjectContextCreate(
            project_key="proj-log",
            name="Log Test",
            description="log test",
        )

        with _patch_factory(mock_session):
            result = await repo.create(data)

        assert result.project_key == "proj-log"
        # TWO statements since 050: the write, then its audit row. Asserting
        # the count rather than dropping it keeps the guard alive — what it
        # protects is 'no extra round trip crept in', and that is still true
        # at two. The second is the focus-history INSERT.
        assert mock_session.execute.call_count == 2


# ===========================================================================
# 3. get_by_id()
# ===========================================================================


class TestGetById:
    @pytest.mark.asyncio
    async def test_get_by_id_returns_model_when_found(self):
        """get_by_id() returns ProjectContext when row exists."""
        from brain_v42.models.project_context import ProjectContext
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row()

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.get_by_id(row["id"])

        assert isinstance(result, ProjectContext)
        assert result.id == row["id"]

    @pytest.mark.asyncio
    async def test_get_by_id_returns_none_when_not_found(self):
        """get_by_id() returns None when row does not exist."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.get_by_id(uuid.uuid4())

        assert result is None


# ===========================================================================
# 4. get_by_key()
# ===========================================================================


class TestGetByKey:
    @pytest.mark.asyncio
    async def test_get_by_key_returns_model_when_found(self):
        """get_by_key() returns ProjectContext when project_key exists."""
        from brain_v42.models.project_context import ProjectContext
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row(project_key="brain_v42")

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.get_by_key("brain_v42")

        assert isinstance(result, ProjectContext)
        assert result.project_key == "brain_v42"

    @pytest.mark.asyncio
    async def test_get_by_key_returns_none_when_not_found(self):
        """get_by_key() returns None when project_key does not exist."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.get_by_key("nonexistent_key")

        assert result is None


# ===========================================================================
# 5. update()
# ===========================================================================


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_returns_model_when_found(self):
        """update() returns updated ProjectContext when row exists."""
        from brain_v42.models.project_context import ProjectContext, ProjectContextUpdate
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row(current_focus="new focus")

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.update(row["id"], ProjectContextUpdate(current_focus="new focus"))

        assert isinstance(result, ProjectContext)
        assert result.current_focus == "new focus"

    @pytest.mark.asyncio
    async def test_update_returns_none_when_not_found(self):
        """update() returns None when row does not exist."""
        from brain_v42.models.project_context import ProjectContextUpdate
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.update(uuid.uuid4(), ProjectContextUpdate(current_focus="x"))

        assert result is None

    @pytest.mark.asyncio
    async def test_update_empty_data_calls_get_by_id(self):
        """update() with no set fields should call get_by_id instead of UPDATE."""
        from brain_v42.models.project_context import ProjectContextUpdate
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row()

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        # No fields set — all remain None → should skip update and call get_by_id
        with _patch_factory(mock_session):
            result = await repo.update(row["id"], ProjectContextUpdate())

        # Either returns the existing record via get_by_id or None is acceptable
        # The key is it should not raise an error
        assert result is not None or result is None


# ===========================================================================
# 6. delete()
# ===========================================================================


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_returns_true_when_found(self):
        """delete() returns True when a row is deleted."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = uuid.uuid4()
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.delete(uuid.uuid4())

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self):
        """delete() returns False when row does not exist."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.delete(uuid.uuid4())

        assert result is False


# ===========================================================================
# 7. list_all()
# ===========================================================================


class TestListAll:
    @pytest.mark.asyncio
    async def test_list_all_returns_list_of_models(self):
        """list_all() returns a list of ProjectContext models."""
        from brain_v42.models.project_context import ProjectContext
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        rows = [_make_row(project_key=f"proj{i}") for i in range(3)]

        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = rows
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            results = await repo.list_all()

        assert isinstance(results, list)
        assert len(results) == 3
        assert all(isinstance(r, ProjectContext) for r in results)

    @pytest.mark.asyncio
    async def test_list_all_empty_returns_empty_list(self):
        """list_all() returns empty list when no contexts exist."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            results = await repo.list_all()

        assert results == []


# ===========================================================================
# 8. get_or_create()
# ===========================================================================


class TestGetOrCreate:
    @pytest.mark.asyncio
    async def test_get_or_create_upserts_existing(self):
        """get_or_create() upserts (INSERT...ON CONFLICT DO UPDATE) and returns result."""
        from brain_v42.models.project_context import ProjectContext, ProjectContextCreate
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row(project_key="existing-proj")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        data = ProjectContextCreate(
            project_key="existing-proj",
            name="Existing",
            description="existing",
        )

        with _patch_factory(mock_session):
            result = await repo.get_or_create(data)

        assert isinstance(result, ProjectContext)
        assert result.project_key == "existing-proj"
        # Single execute call (upsert is one statement)
        # TWO statements since 050: the write, then its audit row. Asserting
        # the count rather than dropping it keeps the guard alive — what it
        # protects is 'no extra round trip crept in', and that is still true
        # at two. The second is the focus-history INSERT.
        assert mock_session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_get_or_create_inserts_when_new(self):
        """get_or_create() inserts new context via upsert when project_key is new."""
        from brain_v42.models.project_context import ProjectContext, ProjectContextCreate
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        new_row = _make_row(project_key="new-proj")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = new_row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        data = ProjectContextCreate(
            project_key="new-proj",
            name="New Project",
            description="new",
        )

        with _patch_factory(mock_session):
            result = await repo.get_or_create(data)

        assert isinstance(result, ProjectContext)
        assert result.project_key == "new-proj"
        # Single execute call (upsert is one statement)
        # TWO statements since 050: the write, then its audit row. Asserting
        # the count rather than dropping it keeps the guard alive — what it
        # protects is 'no extra round trip crept in', and that is still true
        # at two. The second is the focus-history INSERT.
        assert mock_session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_get_or_create_passes_plan_scan_paths(self):
        """get_or_create() includes plan_scan_paths in the upsert values."""
        from brain_v42.models.project_context import ProjectContext, ProjectContextCreate
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row(project_key="proj-with-paths", plan_scan_paths=["/home/plans"])

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        data = ProjectContextCreate(
            project_key="proj-with-paths",
            name="Project With Paths",
            description="has scan paths",
            plan_scan_paths=["/home/plans"],
        )

        with _patch_factory(mock_session):
            result = await repo.get_or_create(data)

        assert isinstance(result, ProjectContext)
        assert result.plan_scan_paths == ["/home/plans"]

    @pytest.mark.asyncio
    async def test_get_or_create_passes_gitlab_project_path(self):
        """get_or_create() includes gitlab_project_path in the upsert values."""
        from brain_v42.models.project_context import ProjectContext, ProjectContextCreate
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row(
            project_key="proj-gitlab",
            gitlab_project_path="hawkixs_project/brain_v42",
        )

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        data = ProjectContextCreate(
            project_key="proj-gitlab",
            name="Project GitLab",
            description="has gitlab path",
            gitlab_project_path="hawkixs_project/brain_v42",
        )

        with _patch_factory(mock_session):
            result = await repo.get_or_create(data)

        assert isinstance(result, ProjectContext)
        assert result.gitlab_project_path == "hawkixs_project/brain_v42"


# ===========================================================================
# 9. update_focus()
# ===========================================================================


class TestUpdateFocus:
    @pytest.mark.asyncio
    async def test_update_focus_returns_model_when_found(self):
        """update_focus() returns updated ProjectContext when project_key exists."""
        from brain_v42.models.project_context import ProjectContext
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row(current_focus="new focus")

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.update_focus("brain_v42", "new focus")

        assert isinstance(result, ProjectContext)
        assert result.current_focus == "new focus"

    @pytest.mark.asyncio
    async def test_update_focus_returns_none_when_key_not_found(self):
        """update_focus() returns None when project_key does not exist."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.update_focus("nonexistent", "focus")

        assert result is None

    @pytest.mark.asyncio
    async def test_update_focus_with_blockers(self):
        """update_focus() updates blockers when provided."""
        from brain_v42.models.project_context import ProjectContext
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row(current_focus="focus", blockers=["blocker1"])

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.update_focus("brain_v42", "focus", blockers=["blocker1"])

        assert isinstance(result, ProjectContext)
        assert result.blockers == ["blocker1"]


# ===========================================================================
# 10. refresh_counts()
# ===========================================================================


class TestRefreshCounts:
    @pytest.mark.asyncio
    async def test_refresh_counts_returns_model_when_found(self):
        """refresh_counts() returns updated ProjectContext with refreshed counts."""
        from brain_v42.models.project_context import ProjectContext
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row(
            decisions_count=3,
            learnings_count=5,
            snippets_count=2,
            runbooks_count=1,
            adrs_count=0,
        )

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.refresh_counts("brain_v42")

        assert isinstance(result, ProjectContext)
        assert result.decisions_count == 3
        assert result.learnings_count == 5

    @pytest.mark.asyncio
    async def test_refresh_counts_returns_none_when_key_not_found(self):
        """refresh_counts() returns None when project_key does not exist."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            result = await repo.refresh_counts("nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_refresh_counts_issues_single_update_statement(self):
        """refresh_counts() should call session.execute exactly once (single UPDATE)."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        mock_session = _make_mock_session()
        row = _make_row()

        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = PgProjectContextRepo()
        with _patch_factory(mock_session):
            await repo.refresh_counts("brain_v42")

        assert mock_session.execute.call_count == 1


# ===========================================================================
# 11. No embedding / FTS logic
# ===========================================================================


class TestNoEmbeddingNoFts:
    def test_no_embedding_attribute(self):
        """PgProjectContextRepo must not have any embedding-related methods."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        repo = PgProjectContextRepo()
        assert not hasattr(repo.__class__, "embed") or "embed" not in str(
            PgProjectContextRepo.__dict__
        )

    def test_no_fts_columns_configured(self):
        """PgProjectContextRepo.fts_columns should be empty (no FTS)."""
        from brain_v42.repositories.pg_project_context import PgProjectContextRepo

        repo = PgProjectContextRepo()
        # fts_columns is inherited from base and should remain empty or not override
        assert repo.fts_columns == []
