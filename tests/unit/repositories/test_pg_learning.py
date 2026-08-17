"""Unit tests for PgLearningRepo — mocked async session, no real DB.

Mock mechanism adaptation (vague 3 re-platform):
- PgLearningRepo no longer defines its own __init__ or _session: the base
  BasePgRepository absorbs session_factory injection.
- _make_repo() now injects a mock session_factory instead of patching repo._session.
  The factory yields a mock AsyncSession that tracks execute() calls.
- Write operations use ``async with sess.begin()`` (base pattern) instead of
  explicit session.commit(). Assertions that previously checked
  ``mock_session.commit.assert_awaited_once()`` are adapted to check that
  ``sess.begin().__aexit__`` was awaited (commit path taken via context manager)
  or, equivalently, that ``sess.begin()`` was entered for write paths and NOT
  entered for read-only paths.
- No behavioral assertions are weakened: return-type checks, value checks, and
  None-guard checks are all preserved verbatim.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.models.learning import Learning, LearningCreate, LearningUpdate
from brain_v42.repositories.pg_learning import PgLearningRepo

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

SAMPLE_UUID = uuid.uuid4()
SAMPLE_ROW = {
    "id": SAMPLE_UUID,
    "topic": "Python async",
    "insight": "Always use asynccontextmanager for cleanup",
    "source": "docs",
    "source_type": "documentation",
    "confidence": "high",
    "project_key": "brain_v42",
    "tags": ["python", "async"],
    "validated_at": None,
    "embedding": None,
    "metadata": {},
    "search_vector": None,
    "created_at": datetime(2026, 2, 28, tzinfo=UTC),
    "updated_at": datetime(2026, 2, 28, tzinfo=UTC),
    # freshness_status not in learning model — leave it absent (model ignores extras)
}


def _make_mock_session() -> AsyncMock:
    """Create an AsyncMock mimicking SQLAlchemy AsyncSession.

    Includes a ``begin()`` async-CM mock so the base's ``async with sess.begin()``
    write pattern works without errors.  Tests can assert on
    ``session.begin.return_value.__aexit__.assert_awaited_once()`` to verify that
    the commit path was taken.
    """
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

    session.execute = AsyncMock(return_value=mock_result)

    # begin() must be a synchronous CM factory returning an async CM
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)

    return session


def _make_factory(mock_session: AsyncMock) -> MagicMock:
    """Return a mock session_factory that always yields *mock_session*."""

    @asynccontextmanager
    async def _session_cm(*args: Any, **kwargs: Any):
        yield mock_session

    factory = MagicMock()
    factory.side_effect = lambda: _session_cm()
    return factory


def _make_repo() -> tuple[PgLearningRepo, AsyncMock]:
    """Return a (repo, mock_session) pair where the repo is wired to mock_session.

    Adaptation from vague 3 re-platform: ``repo._session`` no longer exists;
    the base absorbs session management.  We inject a mock factory so the base's
    ``get_session()`` / ``_maybe_session()`` yield *mock_session*.
    """
    mock_session = _make_mock_session()
    factory = _make_factory(mock_session)
    repo = PgLearningRepo(factory)
    return repo, mock_session


def _set_execute_result(mock_session: AsyncMock, row: dict | None, *, method: str = "one") -> None:
    """Configure mock_session.execute to return a single row via the given mapping method."""
    mock_result = MagicMock()
    mock_mappings = MagicMock()
    mock_result.mappings.return_value = mock_mappings
    mock_mappings.one.return_value = row if row is not None else {}
    mock_mappings.one_or_none.return_value = row
    mock_mappings.first.return_value = row
    mock_mappings.all.return_value = [row] if row is not None else []
    mock_result.one_or_none.return_value = (row.get("id"),) if row else None
    mock_result.scalar_one.return_value = 0
    mock_session.execute = AsyncMock(return_value=mock_result)


# ---------------------------------------------------------------------------
# Tests: importability and class structure
# ---------------------------------------------------------------------------


class TestPgLearningRepoImport:
    def test_importable(self) -> None:
        """PgLearningRepo must be importable from brain_v42.repositories.pg_learning."""
        from brain_v42.repositories.pg_learning import PgLearningRepo as Cls  # noqa: F401

        assert Cls is not None

    def test_exported_from_package(self) -> None:
        """PgLearningRepo must be exported from brain_v42.repositories."""
        from brain_v42.repositories import PgLearningRepo as Cls  # noqa: F401

        assert Cls is not None

    def test_inherits_base(self) -> None:
        """PgLearningRepo must subclass the BasePgRepository."""
        from brain_v42.repositories.pg_base import BasePgRepository
        from brain_v42.repositories.pg_learning import PgLearningRepo

        assert issubclass(PgLearningRepo, BasePgRepository)

    def test_has_session_method(self) -> None:
        """PgLearningRepo must expose a session context manager via the base.

        After vague-3 re-platform, ``_session`` is removed; the base provides
        ``get_session()`` which fulfils the same contract.
        """
        from brain_v42.repositories.pg_learning import PgLearningRepo

        repo = PgLearningRepo(MagicMock())
        assert hasattr(repo, "get_session")

    def test_has_all_public_methods(self) -> None:
        """PgLearningRepo must have all 8 public methods."""
        from brain_v42.repositories.pg_learning import PgLearningRepo

        expected_methods = [
            "create",
            "get_by_id",
            "update",
            "delete",
            "list_all",
            "search_fts",
            "search_vector",
            "validate",
        ]
        for method_name in expected_methods:
            assert hasattr(PgLearningRepo, method_name), f"Missing method: {method_name}"


# ---------------------------------------------------------------------------
# Tests: create
# ---------------------------------------------------------------------------


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_learning(self) -> None:
        """create() returns a Learning Pydantic model."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, dict(SAMPLE_ROW))

        data = LearningCreate(
            topic="Python async",
            insight="Always use asynccontextmanager for cleanup",
            source="docs",
            source_type="documentation",
            confidence="high",
            project_key="brain_v42",
            tags=["python", "async"],
        )
        result = await repo.create(data)

        assert isinstance(result, Learning)
        assert result.topic == "Python async"
        # Write path: begin() context manager must have been entered and exited.
        mock_session.begin.return_value.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_without_embedding(self) -> None:
        """create() works without an embedding."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, dict(SAMPLE_ROW))

        data = LearningCreate(topic="Test", insight="Test insight")
        result = await repo.create(data)

        assert isinstance(result, Learning)

    @pytest.mark.asyncio
    async def test_create_with_embedding(self) -> None:
        """create() accepts an optional embedding vector."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, dict(SAMPLE_ROW))

        data = LearningCreate(topic="Test", insight="Test insight")
        embedding = [0.1] * 1536
        result = await repo.create(data, embedding=embedding)

        assert isinstance(result, Learning)
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_logs_info(self) -> None:
        """create() logs an info-level message."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, dict(SAMPLE_ROW))

        data = LearningCreate(topic="Test", insight="Test insight")
        with patch("brain_v42.repositories.pg_learning.logger") as mock_logger:
            await repo.create(data)
            mock_logger.info.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_calls_commit(self) -> None:
        """create() triggers a commit (via begin() context manager exit) after insert."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, dict(SAMPLE_ROW))

        data = LearningCreate(topic="Test", insight="Test insight")
        await repo.create(data)

        # begin().__aexit__ called once = commit path taken
        mock_session.begin.return_value.__aexit__.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: get_by_id
# ---------------------------------------------------------------------------


class TestGetById:
    @pytest.mark.asyncio
    async def test_get_by_id_found(self) -> None:
        """get_by_id() returns a Learning when row exists."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, dict(SAMPLE_ROW))

        result = await repo.get_by_id(SAMPLE_UUID)

        assert isinstance(result, Learning)
        assert result.id == SAMPLE_UUID

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self) -> None:
        """get_by_id() returns None when row does not exist."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, None)

        result = await repo.get_by_id(uuid.uuid4())

        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_id_does_not_commit(self) -> None:
        """get_by_id() is a read-only operation, no transaction needed."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, None)

        await repo.get_by_id(uuid.uuid4())

        # Read path: begin() must NOT have been called
        mock_session.begin.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: update
# ---------------------------------------------------------------------------


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_returns_learning(self) -> None:
        """update() returns updated Learning on success."""
        repo, mock_session = _make_repo()
        updated_row = {**SAMPLE_ROW, "confidence": "low"}
        _set_execute_result(mock_session, updated_row)

        update_data = LearningUpdate(confidence="low")
        result = await repo.update(SAMPLE_UUID, update_data)

        assert isinstance(result, Learning)
        mock_session.begin.return_value.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_not_found_returns_none(self) -> None:
        """update() returns None when the row does not exist."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, None)

        update_data = LearningUpdate(confidence="low")
        result = await repo.update(uuid.uuid4(), update_data)

        assert result is None

    @pytest.mark.asyncio
    async def test_update_empty_data_falls_back_to_get_by_id(self) -> None:
        """update() with no set fields calls get_by_id instead of UPDATE."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, dict(SAMPLE_ROW))

        update_data = LearningUpdate()  # no fields set
        await repo.update(SAMPLE_UUID, update_data)

        # Falls back to get_by_id: one execute call, no write transaction
        assert mock_session.execute.await_count == 1
        mock_session.begin.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_with_embedding(self) -> None:
        """update() accepts an optional embedding vector."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, dict(SAMPLE_ROW))

        update_data = LearningUpdate(topic="Updated topic")
        embedding = [0.2] * 1536
        result = await repo.update(SAMPLE_UUID, update_data, embedding=embedding)

        assert isinstance(result, Learning)
        mock_session.begin.return_value.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_calls_commit(self) -> None:
        """update() triggers a commit (via begin() context manager exit) after update."""
        repo, mock_session = _make_repo()
        _set_execute_result(mock_session, dict(SAMPLE_ROW))

        update_data = LearningUpdate(insight="New insight")
        await repo.update(SAMPLE_UUID, update_data)

        mock_session.begin.return_value.__aexit__.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: delete
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_found_returns_true(self) -> None:
        """delete() returns True when the row was deleted."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.one_or_none = MagicMock(return_value=(SAMPLE_UUID,))
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.delete(SAMPLE_UUID)

        assert result is True
        mock_session.begin.return_value.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_not_found_returns_false(self) -> None:
        """delete() returns False when the row does not exist."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.one_or_none = MagicMock(return_value=None)
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.delete(uuid.uuid4())

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_logs_info_on_success(self) -> None:
        """delete() logs info message when deletion succeeds."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.one_or_none = MagicMock(return_value=(SAMPLE_UUID,))
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("brain_v42.repositories.pg_learning.logger") as mock_logger:
            await repo.delete(SAMPLE_UUID)
            mock_logger.info.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_calls_commit(self) -> None:
        """delete() triggers a commit (via begin() context manager exit) after deletion."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.one_or_none = MagicMock(return_value=(SAMPLE_UUID,))
        mock_session.execute = AsyncMock(return_value=mock_result)

        await repo.delete(SAMPLE_UUID)

        mock_session.begin.return_value.__aexit__.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: update sets updated_at
# ---------------------------------------------------------------------------


class TestUpdateSetsUpdatedAt:
    @pytest.mark.asyncio
    async def test_update_includes_updated_at_in_values(self) -> None:
        """update() must set updated_at in the SQL UPDATE statement."""
        repo, mock_session = _make_repo()
        updated_row = {**SAMPLE_ROW, "confidence": "low"}
        _set_execute_result(mock_session, updated_row)

        update_data = LearningUpdate(confidence="low")
        await repo.update(SAMPLE_UUID, update_data)

        # Inspect the SQL statement passed to execute
        call_args = mock_session.execute.call_args
        stmt = call_args[0][0]
        # The compiled parameters must include 'updated_at' as a bound param
        compiled = stmt.compile()
        assert "updated_at" in compiled.params, "UPDATE must include updated_at in params"


# ---------------------------------------------------------------------------
# Tests: delete consistency
# ---------------------------------------------------------------------------


class TestDeleteConsistency:
    @pytest.mark.asyncio
    async def test_delete_uses_one_or_none(self) -> None:
        """delete() must use one_or_none() for result extraction (not first())."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.one_or_none = MagicMock(return_value=(SAMPLE_UUID,))
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.delete(SAMPLE_UUID)
        assert result is True
        mock_result.one_or_none.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: list_all
# ---------------------------------------------------------------------------


class TestListAll:
    @pytest.mark.asyncio
    async def test_list_all_returns_learnings(self) -> None:
        """list_all() returns a list of Learning models."""
        repo, mock_session = _make_repo()
        row_dict = dict(SAMPLE_ROW)
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_dict]
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.list_all()

        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], Learning)

    @pytest.mark.asyncio
    async def test_list_all_with_project_key_filter(self) -> None:
        """list_all() with project_key filter passes it to the query."""
        repo, mock_session = _make_repo()
        row_dict = dict(SAMPLE_ROW)
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_dict]
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.list_all(project_key="brain_v42")

        assert isinstance(result, list)
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_list_all_with_confidence_filter(self) -> None:
        """list_all() with confidence filter passes it to the query."""
        repo, mock_session = _make_repo()
        row_dict = dict(SAMPLE_ROW)
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_dict]
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.list_all(confidence="high")

        assert isinstance(result, list)
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_list_all_with_tags_filter(self) -> None:
        """list_all() with tags filter applies overlap check."""
        repo, mock_session = _make_repo()
        row_dict = dict(SAMPLE_ROW)
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_dict]
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.list_all(tags=["python"])

        assert isinstance(result, list)
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_list_all_empty_result(self) -> None:
        """list_all() returns empty list when no rows match."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.list_all()

        assert result == []

    @pytest.mark.asyncio
    async def test_list_all_uses_pagination(self) -> None:
        """list_all() accepts limit and offset parameters."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.list_all(limit=5, offset=10)

        assert result == []
        mock_session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: search_fts
# ---------------------------------------------------------------------------


class TestSearchFts:
    @pytest.mark.asyncio
    async def test_search_fts_returns_learnings(self) -> None:
        """search_fts() returns a list of Learning models."""
        repo, mock_session = _make_repo()
        # Row with 'rank' key (as returned by FTS query with ts_rank)
        row_with_rank = {**SAMPLE_ROW, "rank": 0.75}
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_with_rank]
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.search_fts("python async")

        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], Learning)

    @pytest.mark.asyncio
    async def test_search_fts_excludes_rank_key(self) -> None:
        """search_fts() strips 'rank' from the row before model validation."""
        repo, mock_session = _make_repo()
        row_with_rank = {**SAMPLE_ROW, "rank": 0.75}
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_with_rank]
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.search_fts("test")

        # The Learning model should not have a 'rank' attribute
        assert not hasattr(result[0], "rank")

    @pytest.mark.asyncio
    async def test_search_fts_with_project_key(self) -> None:
        """search_fts() applies project_key filter."""
        repo, mock_session = _make_repo()
        row_with_rank = {**SAMPLE_ROW, "rank": 0.5}
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_with_rank]
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.search_fts("test", project_key="brain_v42")

        assert isinstance(result, list)
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_fts_with_confidence(self) -> None:
        """search_fts() applies confidence filter."""
        repo, mock_session = _make_repo()
        row_with_rank = {**SAMPLE_ROW, "rank": 0.5}
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_with_rank]
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.search_fts("test", confidence="high")

        assert isinstance(result, list)
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_fts_empty_result(self) -> None:
        """search_fts() returns empty list when no FTS matches."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.search_fts("nonexistent query")

        assert result == []

    @pytest.mark.asyncio
    async def test_search_fts_uses_pagination(self) -> None:
        """search_fts() accepts limit and offset parameters."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_result.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.search_fts("test", limit=5, offset=10)

        assert result == []
        mock_session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: search_vector (pgvector)
# ---------------------------------------------------------------------------


class TestSearchVector:
    @pytest.mark.asyncio
    async def test_search_vector_returns_tuples(self) -> None:
        """search_vector() returns list of (Learning, float) tuples."""
        repo, mock_session = _make_repo()
        row_with_similarity = {**SAMPLE_ROW, "similarity": 0.75}
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_with_similarity]
        mock_session.execute = AsyncMock(return_value=mock_result)

        query_embedding = [0.1] * 1536
        result = await repo.search_vector(query_embedding)

        assert isinstance(result, list)
        assert len(result) == 1
        learning, similarity = result[0]
        assert isinstance(learning, Learning)
        assert isinstance(similarity, float)

    @pytest.mark.asyncio
    async def test_search_vector_similarity_calculation(self) -> None:
        """search_vector() correctly reads similarity from the base row."""
        repo, mock_session = _make_repo()
        similarity_value = 0.7
        row_with_similarity = {**SAMPLE_ROW, "similarity": similarity_value}
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_with_similarity]
        mock_session.execute = AsyncMock(return_value=mock_result)

        query_embedding = [0.1] * 1536
        result = await repo.search_vector(query_embedding)

        _, similarity = result[0]
        assert abs(similarity - similarity_value) < 1e-6

    @pytest.mark.asyncio
    async def test_search_vector_with_project_key(self) -> None:
        """search_vector() applies project_key filter."""
        repo, mock_session = _make_repo()
        row_with_similarity = {**SAMPLE_ROW, "similarity": 0.9}
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_with_similarity]
        mock_session.execute = AsyncMock(return_value=mock_result)

        query_embedding = [0.1] * 1536
        result = await repo.search_vector(query_embedding, project_key="brain_v42")

        assert isinstance(result, list)
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_vector_with_confidence(self) -> None:
        """search_vector() applies confidence filter."""
        repo, mock_session = _make_repo()
        row_with_similarity = {**SAMPLE_ROW, "similarity": 0.9}
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [row_with_similarity]
        mock_session.execute = AsyncMock(return_value=mock_result)

        query_embedding = [0.1] * 1536
        result = await repo.search_vector(query_embedding, confidence="high")

        assert isinstance(result, list)
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_vector_empty_result(self) -> None:
        """search_vector() returns empty list when no embeddings match."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        query_embedding = [0.1] * 1536
        result = await repo.search_vector(query_embedding)

        assert result == []


# ---------------------------------------------------------------------------
# Tests: validate
# ---------------------------------------------------------------------------


class TestValidate:
    @pytest.mark.asyncio
    async def test_validate_sets_validated_at(self) -> None:
        """validate() returns Learning with validated_at set."""
        repo, mock_session = _make_repo()
        validated_row = {**SAMPLE_ROW, "validated_at": datetime(2026, 2, 28, tzinfo=UTC)}
        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = validated_row
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.validate(SAMPLE_UUID)

        assert isinstance(result, Learning)
        assert result.validated_at is not None
        # validate() uses _maybe_session(None, write=True) → begin() context manager
        mock_session.begin.return_value.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_validate_not_found_returns_none(self) -> None:
        """validate() returns None when the learning does not exist."""
        repo, mock_session = _make_repo()
        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.validate(uuid.uuid4())

        assert result is None

    @pytest.mark.asyncio
    async def test_validate_logs_info(self) -> None:
        """validate() logs an info-level message on success."""
        repo, mock_session = _make_repo()
        validated_row = {**SAMPLE_ROW, "validated_at": datetime(2026, 2, 28, tzinfo=UTC)}
        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = validated_row
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("brain_v42.repositories.pg_learning.logger") as mock_logger:
            await repo.validate(SAMPLE_UUID)
            mock_logger.info.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_calls_commit(self) -> None:
        """validate() triggers a commit (via begin() context manager exit) after update."""
        repo, mock_session = _make_repo()
        validated_row = {**SAMPLE_ROW, "validated_at": datetime(2026, 2, 28, tzinfo=UTC)}
        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = validated_row
        mock_session.execute = AsyncMock(return_value=mock_result)

        await repo.validate(SAMPLE_UUID)

        mock_session.begin.return_value.__aexit__.assert_awaited_once()
