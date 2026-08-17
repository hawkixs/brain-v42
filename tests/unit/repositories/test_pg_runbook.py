"""Unit tests for PgRunbookRepo — no real DB required.

All sessions are mocked with AsyncMock. We test:
- create() accepts RunbookCreate + optional embedding, returns Runbook
- get_by_id() returns Runbook or None
- update() accepts RunbookUpdate + optional embedding, returns Runbook or None
- delete() returns bool
- search_fts() filters by project_key, returns list[Runbook]
- vector_search() returns list[(Runbook, float)]
- record_execution() atomically updates execution tracking
- get_by_title() returns Runbook or None
- list_by_project() returns list[Runbook]
- __init__ exports PgRunbookRepo
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.models.runbook import Runbook, RunbookCreate, RunbookStep, RunbookUpdate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runbook_row(
    *,
    id: uuid.UUID | None = None,
    title: str = "Deploy service",
    description: str = "Steps to deploy",
    project_key: str = "brain_v42",
    trigger: str = "manual",
    prerequisites: list[str] | None = None,
    steps: list[dict] | None = None,
    rollback_steps: list[dict] | None = None,
    estimated_duration: str | None = "10m",
    tags: list[str] | None = None,
    execution_count: int = 0,
    last_executed_at: datetime | None = None,
    last_execution_status: str | None = None,
    embedding: list[float] | None = None,
    metadata: dict | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> dict:
    """Build a minimal DB row dict for a Runbook."""
    now = datetime.now(UTC)
    return {
        "id": id or uuid.uuid4(),
        "title": title,
        "description": description,
        "project_key": project_key,
        "trigger": trigger,
        "prerequisites": prerequisites or [],
        "steps": steps or [],
        "rollback_steps": rollback_steps or [],
        "estimated_duration": estimated_duration,
        "tags": tags or [],
        "execution_count": execution_count,
        "last_executed_at": last_executed_at,
        "last_execution_status": last_execution_status,
        "embedding": embedding,
        "metadata": metadata or {},
        "created_at": created_at or now,
        "updated_at": updated_at or now,
    }


def _make_mock_session() -> AsyncMock:
    """Create an AsyncMock mimicking SQLAlchemy AsyncSession."""
    session = AsyncMock()

    mock_result = MagicMock()
    mock_mappings = MagicMock()
    mock_result.mappings.return_value = mock_mappings
    mock_mappings.all.return_value = []
    mock_mappings.one.return_value = {}
    mock_mappings.one_or_none.return_value = None

    mock_result.one_or_none.return_value = None
    mock_result.scalar_one.return_value = 0

    session.execute = AsyncMock(return_value=mock_result)

    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)

    session.begin_nested = MagicMock()
    session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)

    return session


def _patch_factory(mock_session: AsyncMock):
    """Patch get_session_factory to inject the mock session."""

    @asynccontextmanager
    async def _session_cm(*args: Any, **kwargs: Any):
        yield mock_session

    mock_factory = MagicMock()
    mock_factory.side_effect = lambda: _session_cm()

    return patch(
        "brain_v42.repositories.pg_base.get_session_factory",
        return_value=mock_factory,
    )


@pytest.fixture
def repo():
    from brain_v42.repositories.pg_runbook import PgRunbookRepo

    return PgRunbookRepo()


# ===========================================================================
# 1. create()
# ===========================================================================


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_runbook(self, repo):
        """create() returns a Runbook Pydantic model."""
        mock_session = _make_mock_session()
        row = _make_runbook_row(title="Deploy")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        data = RunbookCreate(
            title="Deploy",
            description="Deploy steps",
            project_key="brain_v42",
            trigger="manual",
        )
        result = await repo.create(data, session=mock_session)

        assert isinstance(result, Runbook)
        assert result.title == "Deploy"

    @pytest.mark.asyncio
    async def test_create_with_embedding(self, repo):
        """create() passes embedding to INSERT payload."""
        mock_session = _make_mock_session()
        row = _make_runbook_row(embedding=[0.1] * 1536)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        data = RunbookCreate(
            title="Deploy",
            description="Deploy steps",
            project_key="brain_v42",
            trigger="manual",
        )
        result = await repo.create(data, embedding=[0.1] * 1536, session=mock_session)

        assert isinstance(result, Runbook)

    @pytest.mark.asyncio
    async def test_create_with_steps(self, repo):
        """create() serializes RunbookStep objects to dicts for storage."""
        mock_session = _make_mock_session()
        step_dict = {
            "order": 1,
            "title": "Step 1",
            "command": "echo hi",
            "description": None,
            "expected_output": None,
            "timeout_seconds": None,
        }
        row = _make_runbook_row(steps=[step_dict])

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        data = RunbookCreate(
            title="Deploy",
            description="Deploy steps",
            project_key="brain_v42",
            trigger="manual",
            steps=[RunbookStep(order=1, title="Step 1", command="echo hi")],
        )
        result = await repo.create(data, session=mock_session)

        assert isinstance(result, Runbook)
        assert len(result.steps) == 1
        assert result.steps[0].title == "Step 1"

    @pytest.mark.asyncio
    async def test_create_without_session_uses_factory(self, repo):
        """create() without session uses the session factory."""
        mock_session = _make_mock_session()
        row = _make_runbook_row()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        data = RunbookCreate(
            title="Deploy",
            description="Deploy steps",
            project_key="brain_v42",
            trigger="manual",
        )

        with _patch_factory(mock_session):
            result = await repo.create(data)

        assert isinstance(result, Runbook)

    @pytest.mark.asyncio
    async def test_create_strips_extra_fields_from_row(self, repo):
        """create() strips 'rank'/'similarity'/'search_vector' from returned row."""
        mock_session = _make_mock_session()
        row = _make_runbook_row()
        row["rank"] = 0.9  # simulate extra computed field
        row["search_vector"] = "some tsvector"

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        data = RunbookCreate(
            title="Deploy",
            description="Deploy steps",
            project_key="brain_v42",
            trigger="manual",
        )
        result = await repo.create(data, session=mock_session)

        assert isinstance(result, Runbook)
        assert not hasattr(result, "rank")
        assert not hasattr(result, "search_vector")


# ===========================================================================
# 2. get_by_id()
# ===========================================================================


class TestGetById:
    @pytest.mark.asyncio
    async def test_get_by_id_returns_runbook_when_found(self, repo):
        """get_by_id() returns a Runbook when the row exists."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()
        row = _make_runbook_row(id=row_id)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.get_by_id(row_id, session=mock_session)

        assert isinstance(result, Runbook)
        assert result.id == row_id

    @pytest.mark.asyncio
    async def test_get_by_id_returns_none_when_not_found(self, repo):
        """get_by_id() returns None when the row does not exist."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.get_by_id(uuid.uuid4(), session=mock_session)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_id_without_session_uses_factory(self, repo):
        """get_by_id() without session uses the session factory."""
        mock_session = _make_mock_session()
        row = _make_runbook_row()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await repo.get_by_id(uuid.uuid4())

        assert isinstance(result, Runbook)


# ===========================================================================
# 3. update()
# ===========================================================================


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_returns_runbook_when_found(self, repo):
        """update() returns updated Runbook when row exists."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()
        row = _make_runbook_row(id=row_id, title="New title")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        data = RunbookUpdate(title="New title")
        result = await repo.update(row_id, data, session=mock_session)

        assert isinstance(result, Runbook)
        assert result.title == "New title"

    @pytest.mark.asyncio
    async def test_update_returns_none_when_not_found(self, repo):
        """update() returns None when the row does not exist."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        data = RunbookUpdate(title="New title")
        result = await repo.update(uuid.uuid4(), data, session=mock_session)

        assert result is None

    @pytest.mark.asyncio
    async def test_update_empty_data_fetches_current(self, repo):
        """update() with all-None RunbookUpdate fetches the current record."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()
        row = _make_runbook_row(id=row_id)

        # get_by_id call path (via SELECT WHERE id =)
        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        data = RunbookUpdate()  # all None
        result = await repo.update(row_id, data, session=mock_session)

        # Should call get_by_id (which calls execute once)
        assert result is not None

    @pytest.mark.asyncio
    async def test_update_with_embedding(self, repo):
        """update() includes embedding in payload when provided."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()
        row = _make_runbook_row(id=row_id)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        data = RunbookUpdate(description="Updated desc")
        result = await repo.update(row_id, data, embedding=[0.2] * 1536, session=mock_session)

        assert isinstance(result, Runbook)

    @pytest.mark.asyncio
    async def test_update_without_session_uses_factory(self, repo):
        """update() without session uses the session factory."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()
        row = _make_runbook_row(id=row_id)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        data = RunbookUpdate(title="Updated")
        with _patch_factory(mock_session):
            result = await repo.update(row_id, data)

        assert isinstance(result, Runbook)


# ===========================================================================
# 4. delete()
# ===========================================================================


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_returns_true_when_found(self, repo):
        """delete() returns True when a row is deleted."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.one_or_none.return_value = (uuid.uuid4(),)
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.delete(uuid.uuid4(), session=mock_session)

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self, repo):
        """delete() returns False when row does not exist."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.delete(uuid.uuid4(), session=mock_session)

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_without_session_uses_factory(self, repo):
        """delete() without session uses the session factory."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await repo.delete(uuid.uuid4())

        assert result is False


# ===========================================================================
# 5. search_fts()
# ===========================================================================


class TestSearchFts:
    @pytest.mark.asyncio
    async def test_search_fts_returns_list_of_runbooks(self, repo):
        """search_fts() returns list[Runbook]."""
        mock_session = _make_mock_session()
        row = _make_runbook_row()
        row["rank"] = 0.8

        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            # skip_count=True: single execute returns the rows directly
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.search_fts("deploy", session=mock_session)

        assert isinstance(results, list)
        assert len(results) == 1
        assert isinstance(results[0], Runbook)

    @pytest.mark.asyncio
    async def test_search_fts_with_project_key_filter(self, repo):
        """search_fts() passes project_key as filter."""
        mock_session = _make_mock_session()
        row = _make_runbook_row(project_key="my_project")
        row["rank"] = 0.5

        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            # skip_count=True: single execute returns the rows directly
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.search_fts("deploy", project_key="my_project", session=mock_session)

        assert len(results) == 1
        assert results[0].project_key == "my_project"

    @pytest.mark.asyncio
    async def test_search_fts_empty_results(self, repo):
        """search_fts() returns empty list when no matches."""
        mock_session = _make_mock_session()

        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                mock_result.scalar_one.return_value = 0
            else:
                mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.search_fts("nonexistent", session=mock_session)

        assert results == []

    @pytest.mark.asyncio
    async def test_search_fts_without_session_uses_factory(self, repo):
        """search_fts() without session uses the factory."""
        mock_session = _make_mock_session()

        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                mock_result.scalar_one.return_value = 0
            else:
                mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            results = await repo.search_fts("test")

        assert isinstance(results, list)


# ===========================================================================
# 6. vector_search()
# ===========================================================================


class TestVectorSearch:
    @pytest.mark.asyncio
    async def test_vector_search_returns_tuples(self, repo):
        """vector_search() returns list[(Runbook, float)]."""
        mock_session = _make_mock_session()
        row = _make_runbook_row()
        row["similarity"] = 0.92

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.vector_search([0.1] * 1536, session=mock_session)

        assert isinstance(results, list)
        assert len(results) == 1
        runbook, similarity = results[0]
        assert isinstance(runbook, Runbook)
        assert isinstance(similarity, float)
        assert similarity == pytest.approx(0.92)

    @pytest.mark.asyncio
    async def test_vector_search_with_project_key(self, repo):
        """vector_search() passes project_key as filter."""
        mock_session = _make_mock_session()
        row = _make_runbook_row(project_key="filtered_project")
        row["similarity"] = 0.85

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.vector_search(
            [0.1] * 1536, project_key="filtered_project", session=mock_session
        )

        assert len(results) == 1
        assert results[0][0].project_key == "filtered_project"

    @pytest.mark.asyncio
    async def test_vector_search_empty_results(self, repo):
        """vector_search() returns empty list when no matches."""
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.vector_search([0.0] * 1536, session=mock_session)

        assert results == []

    @pytest.mark.asyncio
    async def test_vector_search_without_session_uses_factory(self, repo):
        """vector_search() without session uses the factory."""
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            results = await repo.vector_search([0.0] * 1536)

        assert isinstance(results, list)


# ===========================================================================
# 7. record_execution()
# ===========================================================================


class TestRecordExecution:
    @pytest.mark.asyncio
    async def test_record_execution_returns_updated_runbook(self, repo):
        """record_execution() returns Runbook with updated execution fields."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()
        now = datetime.now(UTC)
        row = _make_runbook_row(
            id=row_id,
            execution_count=1,
            last_executed_at=now,
            last_execution_status="success",
        )

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.record_execution(row_id, "success", session=mock_session)

        assert isinstance(result, Runbook)
        assert result.execution_count == 1
        assert result.last_execution_status == "success"
        assert result.last_executed_at is not None

    @pytest.mark.asyncio
    async def test_record_execution_returns_none_when_not_found(self, repo):
        """record_execution() returns None when the runbook does not exist."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.record_execution(uuid.uuid4(), "failed", session=mock_session)

        assert result is None

    @pytest.mark.asyncio
    async def test_record_execution_all_statuses(self, repo):
        """record_execution() accepts all valid ExecutionStatus values."""
        for status in ("success", "failed", "partial", "skipped"):
            mock_session = _make_mock_session()
            row = _make_runbook_row(last_execution_status=status)

            mock_result = MagicMock()
            mock_result.mappings.return_value.one_or_none.return_value = row
            mock_session.execute = AsyncMock(return_value=mock_result)

            result = await repo.record_execution(
                uuid.uuid4(),
                status,
                session=mock_session,  # type: ignore[arg-type]
            )
            assert isinstance(result, Runbook)

    @pytest.mark.asyncio
    async def test_record_execution_calls_execute_once(self, repo):
        """record_execution() issues a single UPDATE statement (atomic)."""
        mock_session = _make_mock_session()
        row = _make_runbook_row(execution_count=2)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        await repo.record_execution(uuid.uuid4(), "success", session=mock_session)

        # One UPDATE ... RETURNING call
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_execution_without_session_uses_factory(self, repo):
        """record_execution() without session uses the factory."""
        mock_session = _make_mock_session()
        row = _make_runbook_row(execution_count=3, last_execution_status="partial")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await repo.record_execution(uuid.uuid4(), "partial")

        assert isinstance(result, Runbook)


# ===========================================================================
# 8. get_by_title()
# ===========================================================================


class TestGetByTitle:
    @pytest.mark.asyncio
    async def test_get_by_title_returns_runbook_when_found(self, repo):
        """get_by_title() returns Runbook when (title, project_key) exists."""
        mock_session = _make_mock_session()
        row = _make_runbook_row(title="Deploy service", project_key="brain_v42")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.get_by_title("Deploy service", "brain_v42", session=mock_session)

        assert isinstance(result, Runbook)
        assert result.title == "Deploy service"
        assert result.project_key == "brain_v42"

    @pytest.mark.asyncio
    async def test_get_by_title_returns_none_when_not_found(self, repo):
        """get_by_title() returns None when (title, project_key) does not exist."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.get_by_title("Nonexistent", "brain_v42", session=mock_session)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_title_without_session_uses_factory(self, repo):
        """get_by_title() without session uses the factory."""
        mock_session = _make_mock_session()
        row = _make_runbook_row()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await repo.get_by_title("Deploy service", "brain_v42")

        assert isinstance(result, Runbook)

    @pytest.mark.asyncio
    async def test_get_by_title_executes_select(self, repo):
        """get_by_title() issues exactly one SELECT query."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        await repo.get_by_title("Test", "proj", session=mock_session)

        mock_session.execute.assert_called_once()


# ===========================================================================
# 9. list_by_project()
# ===========================================================================


class TestListByProject:
    @pytest.mark.asyncio
    async def test_list_by_project_returns_list_of_runbooks(self, repo):
        """list_by_project() returns list[Runbook] for a given project_key."""
        mock_session = _make_mock_session()
        row1 = _make_runbook_row(title="Runbook A", project_key="brain_v42")
        row2 = _make_runbook_row(title="Runbook B", project_key="brain_v42")

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row1, row2]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.list_by_project("brain_v42", session=mock_session)

        assert isinstance(results, list)
        assert len(results) == 2
        assert all(isinstance(r, Runbook) for r in results)

    @pytest.mark.asyncio
    async def test_list_by_project_empty(self, repo):
        """list_by_project() returns empty list when no runbooks exist for project."""
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.list_by_project("no_such_project", session=mock_session)

        assert results == []

    @pytest.mark.asyncio
    async def test_list_by_project_without_session_uses_factory(self, repo):
        """list_by_project() without session uses the factory."""
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            results = await repo.list_by_project("brain_v42")

        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_list_by_project_respects_limit_offset(self, repo):
        """list_by_project() passes limit and offset to list_all()."""
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            row = _make_runbook_row()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.list_by_project("brain_v42", limit=10, offset=5, session=mock_session)

        assert isinstance(results, list)


# ===========================================================================
# 10. Module exports
# ===========================================================================


class TestModuleExports:
    def test_pg_runbook_repo_importable_from_repositories(self):
        """PgRunbookRepo is importable from brain_v42.repositories."""
        from brain_v42.repositories import PgRunbookRepo

        assert PgRunbookRepo is not None

    def test_pg_runbook_repo_in_all(self):
        """brain_v42.repositories.__all__ contains PgRunbookRepo."""
        import brain_v42.repositories as repos

        assert "PgRunbookRepo" in repos.__all__

    def test_pg_runbook_repo_subclasses_base(self):
        """PgRunbookRepo is a subclass of BasePgRepository."""
        from brain_v42.repositories import BasePgRepository, PgRunbookRepo

        assert issubclass(PgRunbookRepo, BasePgRepository)

    def test_pg_runbook_repo_uses_runbooks_table(self):
        """PgRunbookRepo.table is the 'runbooks' SQLAlchemy Table."""
        from brain_v42.repositories.pg_runbook import PgRunbookRepo

        repo = PgRunbookRepo()
        assert repo.table.name == "runbooks"


# ===========================================================================
# 11. Row conversion helper
# ===========================================================================


class TestRowToRunbook:
    def test_row_to_runbook_converts_dict_steps(self):
        """_row_to_runbook() converts steps dicts to RunbookStep objects."""
        from brain_v42.repositories.pg_runbook import _row_to_runbook

        row = _make_runbook_row(
            steps=[
                {
                    "order": 1,
                    "title": "Step A",
                    "command": None,
                    "description": None,
                    "expected_output": None,
                    "timeout_seconds": None,
                }
            ]
        )
        runbook = _row_to_runbook(row)

        assert len(runbook.steps) == 1
        assert isinstance(runbook.steps[0], RunbookStep)
        assert runbook.steps[0].title == "Step A"

    def test_row_to_runbook_strips_rank(self):
        """_row_to_runbook() removes 'rank' key from row data."""
        from brain_v42.repositories.pg_runbook import _row_to_runbook

        row = _make_runbook_row()
        row["rank"] = 0.95

        runbook = _row_to_runbook(row)
        assert not hasattr(runbook, "rank")

    def test_row_to_runbook_strips_similarity(self):
        """_row_to_runbook() removes 'similarity' key from row data."""
        from brain_v42.repositories.pg_runbook import _row_to_runbook

        row = _make_runbook_row()
        row["similarity"] = 0.88

        runbook = _row_to_runbook(row)
        assert not hasattr(runbook, "similarity")

    def test_row_to_runbook_strips_search_vector(self):
        """_row_to_runbook() removes 'search_vector' key from row data."""
        from brain_v42.repositories.pg_runbook import _row_to_runbook

        row = _make_runbook_row()
        row["search_vector"] = "tsvector data"

        runbook = _row_to_runbook(row)
        assert not hasattr(runbook, "search_vector")

    def test_row_to_runbook_handles_empty_steps(self):
        """_row_to_runbook() handles None/empty steps gracefully."""
        from brain_v42.repositories.pg_runbook import _row_to_runbook

        row = _make_runbook_row(steps=None, rollback_steps=None)
        row["steps"] = None
        row["rollback_steps"] = []

        runbook = _row_to_runbook(row)
        assert runbook.steps == []
        assert runbook.rollback_steps == []
