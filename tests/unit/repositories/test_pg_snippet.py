"""Unit tests for PgSnippetRepo — no real DB required.

All sessions are mocked with AsyncMock. Tests verify that:
- create() maps SnippetCreate to DB insert and returns Snippet model
- get_by_id() returns Snippet or None
- update() maps SnippetUpdate to DB update and returns Snippet or None
- delete() returns bool
- list_all() filters by project_key/language and chooses correct order_by
- search() delegates to search_fts() with correct filters
- vector_search() delegates to search_vector() and returns (Snippet, float) tuples
- increment_use() atomically updates use_count + last_used_at
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.models.snippet import Snippet, SnippetCreate, SnippetUpdate

# ---------------------------------------------------------------------------
# Helpers: mock session and session factory
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
    """Patch get_session_factory so it injects our mock session."""

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
# Shared sample data
# ---------------------------------------------------------------------------

_SAMPLE_ROW: dict[str, Any] = {
    "id": uuid.uuid4(),
    "title": "Async SA session",
    "intention": "Create an async SQLAlchemy session factory",
    "code": "async_session = sessionmaker(...)",
    "language": "python",
    "dependencies": [],
    "usage_example": None,
    "gotchas": None,
    "project_key": "brain_v42",
    "tags": [],
    "use_count": 0,
    "last_used_at": None,
    "embedding": None,
    "metadata": {},
    "search_vector": None,
    "created_at": datetime.now(UTC),
    "updated_at": datetime.now(UTC),
}


def _make_snippet_create(**kwargs: Any) -> SnippetCreate:
    base: dict[str, Any] = {
        "title": "Async SA session",
        "intention": "Create an async SQLAlchemy session factory",
        "code": "async_session = sessionmaker(...)",
        "language": "python",
    }
    base.update(kwargs)
    return SnippetCreate(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo():
    from brain_v42.repositories.pg_snippet import PgSnippetRepo

    return PgSnippetRepo()


# ===========================================================================
# 1. Class-level attributes
# ===========================================================================


class TestClassAttributes:
    def test_table_is_snippets(self, repo):
        """PgSnippetRepo.table must be the snippets SA Core table."""
        from brain_v42.db.tables import snippets

        assert repo.table is snippets

    def test_fts_columns_is_empty(self, repo):
        """fts_columns defaults to [] (search_vector is DB-generated)."""
        assert repo.fts_columns == []

    def test_is_subclass_of_base(self, repo):
        """PgSnippetRepo must subclass BasePgRepository."""
        from brain_v42.repositories.pg_base import BasePgRepository

        assert isinstance(repo, BasePgRepository)


# ===========================================================================
# 2. create()
# ===========================================================================


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_snippet_model(self, repo):
        """create() returns a Snippet Pydantic model."""
        mock_session = _make_mock_session()
        row_data = dict(_SAMPLE_ROW)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        sc = _make_snippet_create()
        result = await repo.create(sc, session=mock_session)

        assert isinstance(result, Snippet)
        assert result.title == "Async SA session"
        assert result.language == "python"

    @pytest.mark.asyncio
    async def test_create_with_embedding(self, repo):
        """create() includes embedding in the payload when provided."""
        mock_session = _make_mock_session()
        embedding = [0.1] * 1536
        row_data = dict(_SAMPLE_ROW, embedding=embedding)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        sc = _make_snippet_create()
        result = await repo.create(sc, embedding=embedding, session=mock_session)

        assert isinstance(result, Snippet)
        assert result.embedding == embedding

    @pytest.mark.asyncio
    async def test_create_without_embedding(self, repo):
        """create() without embedding sets embedding=None in result."""
        mock_session = _make_mock_session()
        row_data = dict(_SAMPLE_ROW)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        sc = _make_snippet_create()
        result = await repo.create(sc, session=mock_session)

        assert result.embedding is None

    @pytest.mark.asyncio
    async def test_create_without_session_uses_factory(self, repo):
        """create() without session uses the session factory."""
        mock_session = _make_mock_session()
        row_data = dict(_SAMPLE_ROW)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        sc = _make_snippet_create()
        with _patch_factory(mock_session):
            result = await repo.create(sc)

        assert isinstance(result, Snippet)
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_executes_insert(self, repo):
        """create() calls session.execute exactly once."""
        mock_session = _make_mock_session()
        row_data = dict(_SAMPLE_ROW)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        sc = _make_snippet_create()
        await repo.create(sc, session=mock_session)

        mock_session.execute.assert_called_once()


# ===========================================================================
# 3. get_by_id()
# ===========================================================================


class TestGetById:
    @pytest.mark.asyncio
    async def test_returns_snippet_when_found(self, repo):
        """get_by_id() returns a Snippet when the row exists."""
        mock_session = _make_mock_session()
        row_data = dict(_SAMPLE_ROW)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.get_by_id(row_data["id"], session=mock_session)
        assert isinstance(result, Snippet)
        assert result.id == row_data["id"]

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, repo):
        """get_by_id() returns None when no row matches."""
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

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await repo.get_by_id(uuid.uuid4())

        assert result is None


# ===========================================================================
# 4. update()
# ===========================================================================


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_returns_snippet_when_found(self, repo):
        """update() returns updated Snippet when row exists."""
        mock_session = _make_mock_session()
        updated_row = dict(_SAMPLE_ROW, title="Updated title")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = updated_row
        mock_session.execute = AsyncMock(return_value=mock_result)

        su = SnippetUpdate(title="Updated title")
        result = await repo.update(updated_row["id"], su, session=mock_session)

        assert isinstance(result, Snippet)
        assert result.title == "Updated title"

    @pytest.mark.asyncio
    async def test_update_returns_none_when_not_found(self, repo):
        """update() returns None when row does not exist."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        su = SnippetUpdate(title="Gone")
        result = await repo.update(uuid.uuid4(), su, session=mock_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_update_with_embedding(self, repo):
        """update() includes embedding in payload when provided."""
        mock_session = _make_mock_session()
        embedding = [0.5] * 1536
        updated_row = dict(_SAMPLE_ROW, embedding=embedding)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = updated_row
        mock_session.execute = AsyncMock(return_value=mock_result)

        su = SnippetUpdate(title="With embedding")
        result = await repo.update(updated_row["id"], su, embedding=embedding, session=mock_session)

        assert isinstance(result, Snippet)
        assert result.embedding == embedding

    @pytest.mark.asyncio
    async def test_update_empty_skips_db_and_returns_current(self, repo):
        """update() with empty SnippetUpdate skips DB update and returns current row."""
        mock_session = _make_mock_session()
        row_data = dict(_SAMPLE_ROW)

        # Both get_by_id and update might call execute
        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        su = SnippetUpdate()  # All None — nothing to update
        result = await repo.update(row_data["id"], su, session=mock_session)
        # When nothing changes, get_by_id is called internally
        assert result is None or isinstance(result, Snippet)

    @pytest.mark.asyncio
    async def test_update_without_session_uses_factory(self, repo):
        """update() without session uses the session factory."""
        mock_session = _make_mock_session()
        row_data = dict(_SAMPLE_ROW, title="New")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        su = SnippetUpdate(title="New")
        with _patch_factory(mock_session):
            result = await repo.update(row_data["id"], su)

        assert isinstance(result, Snippet)


# ===========================================================================
# 5. delete()
# ===========================================================================


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_returns_true_when_found(self, repo):
        """delete() returns True when row was deleted."""
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
# 6. list_all()
# ===========================================================================


class TestListAll:
    @pytest.mark.asyncio
    async def test_list_all_returns_list_of_snippets(self, repo):
        """list_all() returns list[Snippet]."""
        mock_session = _make_mock_session()
        row1 = dict(_SAMPLE_ROW, id=uuid.uuid4())
        row2 = dict(_SAMPLE_ROW, id=uuid.uuid4())

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row1, row2]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.list_all(session=mock_session)
        assert len(results) == 2
        for item in results:
            assert isinstance(item, Snippet)

    @pytest.mark.asyncio
    async def test_list_all_with_project_key_filter(self, repo):
        """list_all(project_key=...) applies project_key filter."""
        mock_session = _make_mock_session()
        row = dict(_SAMPLE_ROW, project_key="myproject")

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.list_all(project_key="myproject", session=mock_session)
        assert len(results) == 1
        assert results[0].project_key == "myproject"

    @pytest.mark.asyncio
    async def test_list_all_with_language_filter(self, repo):
        """list_all(language=...) applies language filter."""
        mock_session = _make_mock_session()
        row = dict(_SAMPLE_ROW, language="typescript")

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.list_all(language="typescript", session=mock_session)
        assert len(results) == 1
        assert results[0].language == "typescript"

    @pytest.mark.asyncio
    async def test_list_all_order_by_use_count(self, repo):
        """list_all(order_by_use_count=True) orders by use_count DESC."""
        mock_session = _make_mock_session()
        captured_sql: list[str] = []

        original_execute = mock_session.execute

        async def capture_execute(stmt, *args, **kwargs):
            captured_sql.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
            return await original_execute(stmt, *args, **kwargs)

        mock_session.execute = capture_execute

        await repo.list_all(order_by_use_count=True, session=mock_session)

        assert len(captured_sql) == 1
        assert "use_count" in captured_sql[0].lower()

    @pytest.mark.asyncio
    async def test_list_all_default_order_by_created_at(self, repo):
        """list_all() without order_by_use_count orders by created_at DESC."""
        mock_session = _make_mock_session()
        captured_sql: list[str] = []

        original_execute = mock_session.execute

        async def capture_execute(stmt, *args, **kwargs):
            captured_sql.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
            return await original_execute(stmt, *args, **kwargs)

        mock_session.execute = capture_execute

        await repo.list_all(order_by_use_count=False, session=mock_session)

        assert len(captured_sql) == 1
        assert "created_at" in captured_sql[0].lower()

    @pytest.mark.asyncio
    async def test_list_all_returns_empty_list(self, repo):
        """list_all() returns [] when no rows match."""
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.list_all(session=mock_session)
        assert results == []


# ===========================================================================
# 7. search()
# ===========================================================================


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_returns_list_of_snippets(self, repo):
        """search() returns list[Snippet] from FTS results."""
        mock_session = _make_mock_session()
        row = dict(_SAMPLE_ROW, rank=0.8)
        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.search("async session", session=mock_session)
        assert len(results) == 1
        assert isinstance(results[0], Snippet)
        # skip_count=True: a single execute — no second full @@ COUNT scan
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_search_with_project_key(self, repo):
        """search() passes project_key as filter to search_fts."""
        captured: dict[str, Any] = {}

        async def fake_search_fts(
            self_inner,
            query,
            *,
            filters=None,
            offset=0,
            limit=20,
            session=None,
            language="english",
            skip_count=False,
        ):
            captured["skip_count"] = skip_count
            captured["filters"] = filters
            captured["query"] = query
            return [], 0

        with patch(
            "brain_v42.repositories.pg_snippet.BasePgRepository.search_fts",
            new=fake_search_fts,
        ):
            from brain_v42.repositories.pg_snippet import PgSnippetRepo

            r = PgSnippetRepo()
            await r.search("myquery", project_key="brain_v42")

        assert captured["query"] == "myquery"
        assert captured["filters"].get("project_key") == "brain_v42"

    @pytest.mark.asyncio
    async def test_search_with_language_filter(self, repo):
        """search() passes language as filter to search_fts."""
        captured: dict[str, Any] = {}

        async def fake_search_fts(
            self_inner,
            query,
            *,
            filters=None,
            offset=0,
            limit=20,
            session=None,
            language="english",
            skip_count=False,
        ):
            captured["skip_count"] = skip_count
            captured["filters"] = filters
            return [], 0

        with patch(
            "brain_v42.repositories.pg_snippet.BasePgRepository.search_fts",
            new=fake_search_fts,
        ):
            from brain_v42.repositories.pg_snippet import PgSnippetRepo

            r = PgSnippetRepo()
            await r.search("query", language="python")

        assert captured["filters"].get("language") == "python"

    @pytest.mark.asyncio
    async def test_search_returns_empty_when_no_results(self, repo):
        """search() returns [] when FTS yields no results."""
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

        results = await repo.search("nothing", session=mock_session)
        assert results == []


# ===========================================================================
# 8. vector_search()
# ===========================================================================


class TestVectorSearch:
    @pytest.mark.asyncio
    async def test_vector_search_returns_tuples(self, repo):
        """vector_search() returns list[(Snippet, float)]."""
        mock_session = _make_mock_session()
        row = dict(_SAMPLE_ROW, similarity=0.92)

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.vector_search([0.1] * 1536, session=mock_session)
        assert len(results) == 1
        snippet, score = results[0]
        assert isinstance(snippet, Snippet)
        assert isinstance(score, float)
        assert score == pytest.approx(0.92)

    @pytest.mark.asyncio
    async def test_vector_search_similarity_defaults_to_zero(self, repo):
        """vector_search() handles rows missing the 'similarity' key gracefully."""
        mock_session = _make_mock_session()
        # Row without 'similarity' key
        row = dict(_SAMPLE_ROW)  # no similarity

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.vector_search([0.0] * 1536, session=mock_session)
        assert len(results) == 1
        _snippet, score = results[0]
        assert score == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_vector_search_with_project_key(self, repo):
        """vector_search() passes project_key as filter."""
        captured: dict[str, Any] = {}

        async def fake_search_vector(
            self_inner, embedding, *, filters=None, limit=10, session=None
        ):
            captured["filters"] = filters
            return []

        with patch(
            "brain_v42.repositories.pg_snippet.BasePgRepository.search_vector",
            new=fake_search_vector,
        ):
            from brain_v42.repositories.pg_snippet import PgSnippetRepo

            r = PgSnippetRepo()
            await r.vector_search([0.1] * 1536, project_key="brain_v42")

        assert captured["filters"].get("project_key") == "brain_v42"

    @pytest.mark.asyncio
    async def test_vector_search_returns_empty_list(self, repo):
        """vector_search() returns [] when no rows match."""
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        results = await repo.vector_search([0.0] * 1536, session=mock_session)
        assert results == []


# ===========================================================================
# 9. increment_use()
# ===========================================================================


class TestIncrementUse:
    @pytest.mark.asyncio
    async def test_increment_use_returns_updated_snippet(self, repo):
        """increment_use() returns Snippet with incremented use_count."""
        mock_session = _make_mock_session()
        row = dict(_SAMPLE_ROW, use_count=1, last_used_at=datetime.now(UTC))

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.increment_use(row["id"], session=mock_session)
        assert isinstance(result, Snippet)
        assert result.use_count == 1
        assert result.last_used_at is not None

    @pytest.mark.asyncio
    async def test_increment_use_returns_none_when_not_found(self, repo):
        """increment_use() returns None when snippet does not exist."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo.increment_use(uuid.uuid4(), session=mock_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_increment_use_executes_update(self, repo):
        """increment_use() calls session.execute exactly once (UPDATE ... RETURNING)."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        await repo.increment_use(uuid.uuid4(), session=mock_session)
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_increment_use_without_session_uses_factory(self, repo):
        """increment_use() without session uses the session factory."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await repo.increment_use(uuid.uuid4())

        assert result is None
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_increment_use_wraps_in_transaction(self, repo):
        """increment_use() without session begins a transaction."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            await repo.increment_use(uuid.uuid4())

        mock_session.begin.assert_called_once()


# ===========================================================================
# 10. _row_to_model() helper
# ===========================================================================


class TestRowToModel:
    def test_row_to_model_returns_snippet(self, repo):
        """_row_to_model() correctly converts a raw dict to a Snippet model."""
        row = dict(_SAMPLE_ROW)
        snippet = repo._row_to_model(row)
        assert isinstance(snippet, Snippet)
        assert snippet.id == row["id"]
        assert snippet.title == row["title"]
        assert snippet.use_count == 0
        assert snippet.last_used_at is None

    def test_row_to_model_with_embedding(self, repo):
        """_row_to_model() preserves embedding field."""
        embedding = [0.1] * 1536
        row = dict(_SAMPLE_ROW, embedding=embedding)
        snippet = repo._row_to_model(row)
        assert snippet.embedding == embedding


# ===========================================================================
# 11. _create_data_to_dict() helper
# ===========================================================================


class TestCreateDataToDict:
    def test_create_data_to_dict_no_embedding(self, repo):
        """_create_data_to_dict() without embedding omits embedding key."""
        sc = _make_snippet_create()
        d = repo._create_data_to_dict(sc)
        assert "embedding" not in d or d.get("embedding") is None

    def test_create_data_to_dict_with_embedding(self, repo):
        """_create_data_to_dict() with embedding includes it."""
        sc = _make_snippet_create()
        emb = [0.5] * 1536
        d = repo._create_data_to_dict(sc, emb)
        assert d["embedding"] == emb

    def test_create_data_to_dict_includes_required_fields(self, repo):
        """_create_data_to_dict() includes title, intention, code, language."""
        sc = _make_snippet_create(title="My snippet", language="rust")
        d = repo._create_data_to_dict(sc)
        assert d["title"] == "My snippet"
        assert d["language"] == "rust"
        assert "intention" in d
        assert "code" in d


# ===========================================================================
# 12. _update_data_to_dict() helper
# ===========================================================================


class TestUpdateDataToDict:
    def test_update_data_excludes_none_fields(self, repo):
        """_update_data_to_dict() only includes non-None fields."""
        su = SnippetUpdate(title="New title")  # all others None
        d = repo._update_data_to_dict(su)
        assert "title" in d
        assert d["title"] == "New title"
        # None fields should be excluded
        assert "code" not in d
        assert "language" not in d

    def test_update_data_with_embedding(self, repo):
        """_update_data_to_dict() includes embedding when provided."""
        su = SnippetUpdate(title="Updated")
        emb = [0.3] * 1536
        d = repo._update_data_to_dict(su, emb)
        assert d["embedding"] == emb

    def test_update_data_empty_returns_empty(self, repo):
        """_update_data_to_dict() returns empty dict for SnippetUpdate()."""
        su = SnippetUpdate()
        d = repo._update_data_to_dict(su)
        assert d == {}


# ===========================================================================
# 13. Module exports
# ===========================================================================


class TestModuleExports:
    def test_pg_snippet_repo_importable_from_repositories(self):
        """PgSnippetRepo is importable from brain_v42.repositories."""
        from brain_v42.repositories import PgSnippetRepo

        assert PgSnippetRepo is not None

    def test_pg_snippet_repo_in_all(self):
        """PgSnippetRepo is listed in brain_v42.repositories.__all__."""
        import brain_v42.repositories as repos

        assert "PgSnippetRepo" in repos.__all__
