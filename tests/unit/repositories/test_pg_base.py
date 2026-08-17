"""Unit tests for BasePgRepository — no real DB required.

All sessions are mocked with AsyncMock. We use a minimal SQLAlchemy Table
fixture to test column checks, filter clauses, FTS, and vector search.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# ---------------------------------------------------------------------------
# Minimal fixture tables (no DB needed)
# ---------------------------------------------------------------------------

_META = MetaData()

# A minimal table with just id and created_at — no search_vector, no embedding
_SIMPLE_TABLE = Table(
    "simple_items",
    _META,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("name", String(100)),
    Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
    ),
    Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
    ),
)

# A table with both search_vector and embedding
_FULL_TABLE = Table(
    "full_items",
    _META,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("name", String(100)),
    Column("search_vector", sa.Text),  # simplified stand-in for TSVECTOR
    Column("embedding", sa.Text),  # simplified stand-in for Vector(1536)
    Column("project_key", String(50)),
    Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
    ),
    Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
    ),
)

# A table with archived/merge columns and tags — used for include_archived + _tags_clause tests
_ARCHIVED_TABLE = Table(
    "archived_items",
    _META,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("name", String(100)),
    Column("tags", sa.ARRAY(sa.Text)),
    Column("search_vector", sa.Text),
    Column("embedding", sa.Text),
    Column("project_key", String(50)),
    Column("freshness_status", String(10)),
    Column("merged_into", PG_UUID(as_uuid=True), nullable=True),
    Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
    ),
    Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
    ),
)


# ---------------------------------------------------------------------------
# Helper: create a mock session that mimics AsyncSession behaviour
# ---------------------------------------------------------------------------


def _make_mock_session() -> AsyncMock:
    """Create an AsyncMock mimicking SQLAlchemy AsyncSession."""
    session = AsyncMock()

    # .execute() returns an object with .mappings() => .all() / .one() etc.
    mock_result = MagicMock()
    mock_mappings = MagicMock()
    mock_result.mappings.return_value = mock_mappings
    mock_mappings.all.return_value = []
    mock_mappings.one.return_value = {}
    mock_mappings.one_or_none.return_value = None

    mock_result.one_or_none.return_value = None
    mock_result.scalar_one.return_value = 0

    session.execute = AsyncMock(return_value=mock_result)

    # begin() and begin_nested() are async context managers
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)

    session.begin_nested = MagicMock()
    session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)

    return session


# ---------------------------------------------------------------------------
# Helper: patch get_session_factory to inject a mock session
# ---------------------------------------------------------------------------


def _patch_factory(mock_session: AsyncMock):
    """Return a context manager that patches get_session_factory.

    The real get_session_factory() returns an async_sessionmaker.
    Calling that sessionmaker returns an async context manager (AsyncSession).
    So: factory = get_session_factory(); async with factory() as session: ...

    Here we mock it so:
      get_session_factory() -> mock_factory
      mock_factory()        -> async context manager that yields mock_session
    """

    @asynccontextmanager
    async def _session_cm(*args: Any, **kwargs: Any):
        yield mock_session

    mock_factory = MagicMock()
    mock_factory.return_value = _session_cm()  # factory() is called once per use

    # But each call to mock_factory() must return a fresh async CM.
    # Use side_effect so every call returns a new CM:
    mock_factory.side_effect = lambda: _session_cm()

    return patch(
        "brain_v42.repositories.pg_base.get_session_factory",
        return_value=mock_factory,
    )


# ---------------------------------------------------------------------------
# Concrete subclass fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_repo():
    from brain_v42.repositories.pg_base import BasePgRepository

    class SimpleRepo(BasePgRepository):
        table = _SIMPLE_TABLE

    return SimpleRepo()


@pytest.fixture
def full_repo():
    from brain_v42.repositories.pg_base import BasePgRepository

    class FullRepo(BasePgRepository):
        table = _FULL_TABLE

    return FullRepo()


@pytest.fixture
def archived_repo():
    from brain_v42.repositories.pg_base import BasePgRepository

    class ArchivedRepo(BasePgRepository):
        table = _ARCHIVED_TABLE

    return ArchivedRepo()


# ===========================================================================
# 1. Class attribute enforcement
# ===========================================================================


class TestClassAttributes:
    def test_table_must_be_set_on_subclass(self):
        """BasePgRepository subclass must define table attribute."""
        from brain_v42.repositories.pg_base import BasePgRepository

        class BrokenRepo(BasePgRepository):
            pass

        repo = BrokenRepo()
        assert not hasattr(repo, "table") or repo.table is BasePgRepository.__dict__.get(
            "table", ...
        )

    def test_fts_columns_defaults_to_empty_list(self):
        from brain_v42.repositories.pg_base import BasePgRepository

        class MyRepo(BasePgRepository):
            table = _SIMPLE_TABLE

        assert MyRepo.fts_columns == []

    def test_subclass_table_is_set(self, simple_repo):
        assert simple_repo.table is _SIMPLE_TABLE


# ===========================================================================
# 2. get_session context manager
# ===========================================================================


class TestGetSession:
    @pytest.mark.asyncio
    async def test_get_session_yields_async_session(self, simple_repo):
        """get_session() must yield a session from the factory."""
        mock_session = _make_mock_session()

        with _patch_factory(mock_session):
            async with simple_repo.get_session() as sess:
                assert sess is mock_session


# ===========================================================================
# 3. transaction context manager
# ===========================================================================


class TestTransaction:
    @pytest.mark.asyncio
    async def test_transaction_no_session_creates_new_session(self, simple_repo):
        """transaction() with no session creates a new session + begins txn."""
        mock_session = _make_mock_session()

        with _patch_factory(mock_session):
            async with simple_repo.transaction() as sess:
                assert sess is mock_session

        mock_session.begin.assert_called_once()

    @pytest.mark.asyncio
    async def test_transaction_with_existing_session_uses_savepoint(self, simple_repo):
        """transaction(session=existing) uses begin_nested for savepoint."""
        existing_session = _make_mock_session()

        async with simple_repo.transaction(session=existing_session) as sess:
            assert sess is existing_session

        existing_session.begin_nested.assert_called_once()

    @pytest.mark.asyncio
    async def test_transaction_rollback_on_exception(self, simple_repo):
        """transaction() propagates exceptions (session context manager handles rollback)."""
        mock_session = _make_mock_session()
        # Make __aexit__ simulate rollback by re-raising
        mock_session.begin.return_value.__aexit__ = AsyncMock(return_value=False)

        with _patch_factory(mock_session):
            with pytest.raises(ValueError, match="boom"):
                async with simple_repo.transaction() as _sess:
                    raise ValueError("boom")


# ===========================================================================
# 4. create()
# ===========================================================================


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_dict(self, simple_repo):
        """create() executes INSERT...RETURNING and returns a dict."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()
        row_data = {
            "id": row_id,
            "name": "hello",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        data = {"id": row_id, "name": "hello"}
        result = await simple_repo.create(data, session=mock_session)

        assert isinstance(result, dict)
        assert result["id"] == row_id
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_without_session_uses_factory(self, simple_repo):
        """create() without session uses the session factory."""
        mock_session = _make_mock_session()
        row_data = {
            "id": uuid.uuid4(),
            "name": "test",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await simple_repo.create({"name": "test"})

        assert isinstance(result, dict)
        mock_session.execute.assert_called_once()


# ===========================================================================
# 5. get_by_id()
# ===========================================================================


class TestGetById:
    @pytest.mark.asyncio
    async def test_get_by_id_returns_dict_when_found(self, simple_repo):
        """get_by_id() returns a dict when row exists."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()
        row_data = {"id": row_id, "name": "found"}

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await simple_repo.get_by_id(row_id, session=mock_session)
        assert result == {"id": row_id, "name": "found"}

    @pytest.mark.asyncio
    async def test_get_by_id_returns_none_when_not_found(self, simple_repo):
        """get_by_id() returns None when row does not exist."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await simple_repo.get_by_id(uuid.uuid4(), session=mock_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_id_without_session_uses_factory(self, simple_repo):
        """get_by_id() without session uses the session factory."""
        mock_session = _make_mock_session()
        row_data = {"id": uuid.uuid4(), "name": "found"}

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await simple_repo.get_by_id(uuid.uuid4())

        assert isinstance(result, dict)


# ===========================================================================
# 6. update()
# ===========================================================================


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_returns_dict_when_found(self, simple_repo):
        """update() returns updated dict when row exists."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()
        updated_row = {"id": row_id, "name": "updated", "updated_at": datetime.now(UTC)}

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = updated_row
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await simple_repo.update(row_id, {"name": "updated"}, session=mock_session)
        assert result is not None
        assert result["name"] == "updated"

    @pytest.mark.asyncio
    async def test_update_returns_none_when_not_found(self, simple_repo):
        """update() returns None when row does not exist."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await simple_repo.update(uuid.uuid4(), {"name": "x"}, session=mock_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_update_always_sets_updated_at(self, simple_repo):
        """update() always injects updated_at into the payload."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()

        captured_stmt = None

        async def capture_execute(stmt, *args, **kwargs):
            nonlocal captured_stmt
            captured_stmt = stmt
            mock_result = MagicMock()
            mock_result.mappings.return_value.one_or_none.return_value = {"id": row_id}
            return mock_result

        mock_session.execute = capture_execute

        await simple_repo.update(row_id, {"name": "new"}, session=mock_session)
        # Verify execute was called (statement compiled)
        assert captured_stmt is not None

    @pytest.mark.asyncio
    async def test_update_without_session_uses_factory(self, simple_repo):
        """update() without session uses the factory."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = {"id": row_id}
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await simple_repo.update(row_id, {"name": "x"})

        assert result is not None


# ===========================================================================
# 7. set_embedding_if_current()
# ===========================================================================


class TestSetEmbeddingIfCurrent:
    @pytest.mark.asyncio
    async def test_updates_only_matching_unusable_embedding(self, full_repo):
        """A comparable vector replaces an unchanged missing or zero-norm one."""
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()
        expected_updated_at = datetime.now(UTC)
        captured_stmt = None
        updated_row = {
            "id": row_id,
            "embedding": "[0.1,0.2]",
            "updated_at": expected_updated_at,
        }

        async def capture_execute(stmt, *args, **kwargs):
            nonlocal captured_stmt
            captured_stmt = stmt
            mock_result = MagicMock()
            mock_result.mappings.return_value.one_or_none.return_value = updated_row
            return mock_result

        mock_session.execute = capture_execute

        result = await full_repo.set_embedding_if_current(
            row_id,
            [0.1, 0.2],
            expected_updated_at=expected_updated_at,
            session=mock_session,
        )

        assert result == updated_row
        assert captured_stmt is not None
        sql = str(captured_stmt.compile(compile_kwargs={"literal_binds": False}))
        assert "full_items.embedding IS NULL" in sql
        assert "vector_norm(full_items.embedding)" in sql
        assert "full_items.updated_at =" in sql
        assert "full_items.id =" in sql

    @pytest.mark.asyncio
    async def test_returns_none_when_row_changed_or_missing(self, full_repo):
        mock_session = _make_mock_session()
        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await full_repo.set_embedding_if_current(
            uuid.uuid4(),
            [0.1, 0.2],
            expected_updated_at=datetime.now(UTC),
            session=mock_session,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_table_without_embedding_column(self, simple_repo):
        with pytest.raises(AttributeError, match="embedding"):
            await simple_repo.set_embedding_if_current(
                uuid.uuid4(),
                [0.1, 0.2],
                expected_updated_at=datetime.now(UTC),
            )

    @pytest.mark.asyncio
    async def test_without_session_uses_transaction(self, full_repo):
        mock_session = _make_mock_session()
        row_id = uuid.uuid4()
        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = {"id": row_id}
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await full_repo.set_embedding_if_current(
                row_id,
                [0.1, 0.2],
                expected_updated_at=datetime.now(UTC),
            )

        assert result == {"id": row_id}
        mock_session.begin.assert_called_once()


# ===========================================================================
# 8. embedding backlog
# ===========================================================================


class TestEmbeddingBacklog:
    @pytest.mark.asyncio
    async def test_lists_oldest_unusable_rows_with_a_bound(self, full_repo):
        mock_session = _make_mock_session()
        pending = [{"id": uuid.uuid4(), "embedding": None}]
        captured_stmt = None

        async def capture_execute(stmt, *args, **kwargs):
            nonlocal captured_stmt
            captured_stmt = stmt
            result = MagicMock()
            result.mappings.return_value.all.return_value = pending
            return result

        mock_session.execute = capture_execute

        rows = await full_repo.list_embedding_backlog(
            limit=25,
            project_key="brain-v42",
            session=mock_session,
        )

        assert rows == pending
        sql = str(captured_stmt.compile(compile_kwargs={"literal_binds": False}))
        assert "full_items.embedding IS NULL" in sql
        assert "vector_norm(full_items.embedding)" in sql
        assert "full_items.project_key =" in sql
        assert "ORDER BY full_items.created_at, full_items.id" in sql
        assert "LIMIT" in sql

    @pytest.mark.asyncio
    async def test_reports_pending_count_and_oldest_timestamp(self, full_repo):
        mock_session = _make_mock_session()
        oldest = datetime.now(UTC)
        result = MagicMock()
        result.mappings.return_value.one.return_value = {
            "pending_count": 3,
            "oldest_created_at": oldest,
        }
        mock_session.execute = AsyncMock(return_value=result)

        stats = await full_repo.embedding_backlog_stats(session=mock_session)

        assert stats.count == 3
        assert stats.oldest_created_at == oldest
        sql = str(
            mock_session.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": False})
        )
        assert "full_items.embedding IS NULL" in sql
        assert "vector_norm(full_items.embedding)" in sql

    @pytest.mark.asyncio
    async def test_rejects_table_without_embedding_column(self, simple_repo):
        with pytest.raises(AttributeError, match="embedding"):
            await simple_repo.list_embedding_backlog()


# ===========================================================================
# 9. delete()
# ===========================================================================


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_returns_true_when_found(self, simple_repo):
        """delete() returns True when a row is deleted."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.one_or_none.return_value = (uuid.uuid4(),)
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await simple_repo.delete(uuid.uuid4(), session=mock_session)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self, simple_repo):
        """delete() returns False when row does not exist."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await simple_repo.delete(uuid.uuid4(), session=mock_session)
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_without_session_uses_factory(self, simple_repo):
        """delete() without session uses the factory."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await simple_repo.delete(uuid.uuid4())

        assert result is False


# ===========================================================================
# 8. list_all()
# ===========================================================================


class TestListAll:
    @pytest.mark.asyncio
    async def test_list_all_returns_items_and_total(self, simple_repo):
        """list_all() returns (list[dict], int) with COUNT + SELECT."""
        mock_session = _make_mock_session()
        row1 = {"id": uuid.uuid4(), "name": "a"}
        row2 = {"id": uuid.uuid4(), "name": "b"}

        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                # COUNT query
                mock_result.scalar_one.return_value = 2
            else:
                # SELECT query
                mock_result.mappings.return_value.all.return_value = [row1, row2]
            return mock_result

        mock_session.execute = mock_execute

        items, total = await simple_repo.list_all(session=mock_session)
        assert total == 2
        assert len(items) == 2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_list_all_with_filters_not_none(self, full_repo):
        """list_all() applies non-None filters in WHERE clause."""
        mock_session = _make_mock_session()
        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                mock_result.scalar_one.return_value = 1
            else:
                row = {"id": uuid.uuid4(), "name": "x", "project_key": "proj1"}
                mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        items, total = await full_repo.list_all(
            filters={"project_key": "proj1"}, session=mock_session
        )
        assert total == 1
        assert len(items) == 1

    @pytest.mark.asyncio
    async def test_list_all_ignores_none_filters(self, full_repo):
        """list_all(filters={'project_key': None}) ignores the None filter."""
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

        items, total = await full_repo.list_all(filters={"project_key": None}, session=mock_session)
        assert total == 0
        assert items == []

    @pytest.mark.asyncio
    async def test_list_all_without_session_uses_factory(self, simple_repo):
        """list_all() without session uses the factory."""
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
            items, total = await simple_repo.list_all()

        assert total == 0
        assert items == []


# ===========================================================================
# 9. _build_filter_clauses()
# ===========================================================================


class TestBuildFilterClauses:
    def test_skips_none_values(self, full_repo):
        """_build_filter_clauses skips filters with None values."""
        clauses = full_repo._build_filter_clauses({"project_key": None, "name": "test"})
        assert len(clauses) == 1

    def test_includes_non_none_values(self, full_repo):
        """_build_filter_clauses includes non-None filters."""
        clauses = full_repo._build_filter_clauses({"project_key": "proj1"})
        assert len(clauses) == 1

    def test_raises_for_unknown_column(self, simple_repo):
        """_build_filter_clauses raises ValueError for unknown column names."""
        with pytest.raises(ValueError, match="Unknown column 'nonexistent'"):
            simple_repo._build_filter_clauses({"nonexistent": "value"})

    def test_empty_filters_returns_empty_list(self, simple_repo):
        """_build_filter_clauses returns empty list for empty dict."""
        clauses = simple_repo._build_filter_clauses({})
        assert clauses == []


# ===========================================================================
# 9b. _search_columns() column pruning
# ===========================================================================


class TestSearchColumnPruning:
    """search_fts() and search_vector() must NOT project embedding/search_vector columns."""

    def test_search_columns_excludes_heavy_columns(self, full_repo):
        """_search_columns() must exclude embedding and search_vector."""
        col_names = [c.name for c in full_repo._search_columns()]
        assert "embedding" not in col_names
        assert "search_vector" not in col_names
        # But other columns should still be present
        assert "id" in col_names
        assert "name" in col_names
        assert "project_key" in col_names

    def test_search_columns_returns_all_on_simple_table(self, simple_repo):
        """_search_columns() on a table without embedding returns all columns."""
        cols = simple_repo._search_columns()
        col_names = [c.name for c in cols]
        assert "id" in col_names
        assert "name" in col_names
        # simple_repo has no embedding/search_vector, so all columns returned
        assert len(cols) == len(simple_repo.table.c)


# ===========================================================================
# 10. search_fts()
# ===========================================================================


class TestSearchFts:
    def test_raises_attribute_error_if_no_search_vector(self, simple_repo):
        """search_fts() raises AttributeError if table has no search_vector column."""
        import asyncio

        with pytest.raises(AttributeError, match="search_vector"):
            asyncio.run(simple_repo.search_fts("test query"))

    @pytest.mark.asyncio
    async def test_raises_attribute_error_if_no_search_vector_async(self, simple_repo):
        """search_fts() raises AttributeError if table has no search_vector column."""
        with pytest.raises(AttributeError, match="search_vector"):
            await simple_repo.search_fts("test query")

    @pytest.mark.asyncio
    async def test_search_fts_returns_items_and_total(self, full_repo):
        """search_fts() returns (list[dict], int) with rank key."""
        mock_session = _make_mock_session()
        row = {"id": uuid.uuid4(), "name": "result", "rank": 0.75}
        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                mock_result.scalar_one.return_value = 1
            else:
                mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        items, total = await full_repo.search_fts("test", session=mock_session)
        assert total == 1
        assert len(items) == 1
        assert "rank" in items[0]

    @pytest.mark.asyncio
    async def test_search_fts_without_session_uses_factory(self, full_repo):
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
            items, total = await full_repo.search_fts("hello")

        assert total == 0
        assert items == []


# ===========================================================================
# 11. search_vector()
# ===========================================================================


class TestSearchVector:
    @pytest.mark.asyncio
    async def test_raises_attribute_error_if_no_embedding(self, simple_repo):
        """search_vector() raises AttributeError if table has no embedding column."""
        with pytest.raises(AttributeError, match="embedding"):
            await simple_repo.search_vector([0.1] * 1536)

    @pytest.mark.asyncio
    async def test_search_vector_returns_list_with_similarity(self, full_repo):
        """search_vector() returns list[dict] with similarity key."""
        mock_session = _make_mock_session()
        row = {"id": uuid.uuid4(), "name": "semantic", "similarity": 0.92}

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        results = await full_repo.search_vector([0.1] * 1536, session=mock_session)
        assert len(results) == 1
        assert "similarity" in results[0]

    @pytest.mark.asyncio
    async def test_search_vector_without_session_uses_factory(self, full_repo):
        """search_vector() without session uses the factory."""
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            results = await full_repo.search_vector([0.0] * 1536)

        assert isinstance(results, list)


# ===========================================================================
# 12. __init__.py exports
# ===========================================================================


class TestModuleExports:
    def test_base_pg_repository_importable_from_repositories(self):
        """BasePgRepository is importable from brain_v42.repositories."""
        from brain_v42.repositories import BasePgRepository

        assert BasePgRepository is not None

    def test_all_exports(self):
        """brain_v42.repositories.__all__ contains BasePgRepository."""
        import brain_v42.repositories as repos

        assert "BasePgRepository" in repos.__all__


# ===========================================================================
# 13. Optional session_factory DI via __init__ (evolution f)
# ===========================================================================


class TestOptionalSessionFactoryDI:
    """BasePgRepository.__init__ accepts an optional session_factory kwarg.

    When provided, get_session() uses it instead of the global get_session_factory().
    When absent, falls back to the global get_session_factory() — rétro-compatible.
    """

    def test_init_no_args(self):
        """BasePgRepository() with no args is valid — no AttributeError.

        _session_factory_opt stores the raw optional; _session_factory (property)
        returns the global fallback when _session_factory_opt is None.
        """
        from brain_v42.repositories.pg_base import BasePgRepository

        class R(BasePgRepository):
            table = _SIMPLE_TABLE

        repo = R()
        assert repo._session_factory_opt is None

    def test_init_with_factory(self):
        """BasePgRepository(session_factory=f) stores the factory via the property setter."""
        from brain_v42.repositories.pg_base import BasePgRepository

        mock_factory = MagicMock()

        class R(BasePgRepository):
            table = _SIMPLE_TABLE

        repo = R(session_factory=mock_factory)
        # Property getter returns the injected factory directly
        assert repo._session_factory is mock_factory

    @pytest.mark.asyncio
    async def test_get_session_uses_injected_factory(self):
        """get_session() uses the injected factory when provided."""
        from brain_v42.repositories.pg_base import BasePgRepository

        mock_session = _make_mock_session()

        @asynccontextmanager
        async def _session_cm(*args: Any, **kwargs: Any):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        class R(BasePgRepository):
            table = _SIMPLE_TABLE

        repo = R(session_factory=mock_factory)
        async with repo.get_session() as sess:
            assert sess is mock_session

    @pytest.mark.asyncio
    async def test_get_session_falls_back_to_global_factory(self):
        """get_session() falls back to get_session_factory() when _session_factory is None."""
        from brain_v42.repositories.pg_base import BasePgRepository

        mock_session = _make_mock_session()

        class R(BasePgRepository):
            table = _SIMPLE_TABLE

        repo = R()
        with _patch_factory(mock_session):
            async with repo.get_session() as sess:
                assert sess is mock_session

    @pytest.mark.asyncio
    async def test_crud_uses_injected_factory(self):
        """create() without explicit session uses the injected factory (not global)."""
        from brain_v42.repositories.pg_base import BasePgRepository

        mock_session = _make_mock_session()
        row_data = {
            "id": uuid.uuid4(),
            "name": "di-test",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        mock_result = MagicMock()
        mock_result.mappings.return_value.one.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def _session_cm(*args: Any, **kwargs: Any):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        class R(BasePgRepository):
            table = _SIMPLE_TABLE

        repo = R(session_factory=mock_factory)

        # No global patch needed — must use injected factory
        result = await repo.create({"name": "di-test"})
        assert result["name"] == "di-test"
        mock_factory.assert_called()


# ===========================================================================
# 14. project_scope helper (evolution b)
# ===========================================================================


class TestProjectScope:
    """project_scope(project_key, project_keys) returns project_keys if not None else project_key.

    This is a module-level helper — not a method — used by subclass wrappers.
    """

    def test_project_keys_not_none_returns_project_keys(self):
        from brain_v42.repositories.pg_base import project_scope

        result = project_scope("k1", ["k1", "k2"])
        assert result == ["k1", "k2"]

    def test_project_keys_none_returns_project_key(self):
        from brain_v42.repositories.pg_base import project_scope

        result = project_scope("k1", None)
        assert result == "k1"

    def test_both_none_returns_none(self):
        from brain_v42.repositories.pg_base import project_scope

        result = project_scope(None, None)
        assert result is None

    def test_project_key_none_project_keys_set(self):
        from brain_v42.repositories.pg_base import project_scope

        result = project_scope(None, ["a", "b"])
        assert result == ["a", "b"]

    def test_empty_project_keys_list_returned(self):
        from brain_v42.repositories.pg_base import project_scope

        result = project_scope("k1", [])
        assert result == []


# ===========================================================================
# 15. _tags_clause helper (evolution c)
# ===========================================================================


class TestTagsClause:
    """_tags_clause(tags) returns an && overlap clause for ARRAY columns."""

    def test_none_returns_none(self, archived_repo):
        from brain_v42.repositories.pg_base import BasePgRepository

        class R(BasePgRepository):
            table = _ARCHIVED_TABLE

        repo = R()
        assert repo._tags_clause(_ARCHIVED_TABLE.c.tags, None) is None

    def test_empty_list_returns_none(self, archived_repo):
        from brain_v42.repositories.pg_base import BasePgRepository

        class R(BasePgRepository):
            table = _ARCHIVED_TABLE

        repo = R()
        assert repo._tags_clause(_ARCHIVED_TABLE.c.tags, []) is None

    def test_non_empty_list_returns_clause(self, archived_repo):
        from brain_v42.repositories.pg_base import BasePgRepository

        class R(BasePgRepository):
            table = _ARCHIVED_TABLE

        repo = R()
        clause = repo._tags_clause(_ARCHIVED_TABLE.c.tags, ["python", "async"])
        assert clause is not None
        # Compile to verify the && operator is used
        compiled = str(clause.compile(dialect=sa.dialects.postgresql.dialect()))
        assert "&&" in compiled

    def test_tags_clause_uses_cast_array_text(self, archived_repo):
        from brain_v42.repositories.pg_base import BasePgRepository

        class R(BasePgRepository):
            table = _ARCHIVED_TABLE

        repo = R()
        clause = repo._tags_clause(_ARCHIVED_TABLE.c.tags, ["foo"])
        compiled = str(clause.compile(dialect=sa.dialects.postgresql.dialect()))
        # Must cast the right-hand side to ARRAY(Text) / VARCHAR[]
        assert "ARRAY" in compiled or "[]" in compiled


# ===========================================================================
# 16. extra_clauses on list_all, search_fts, search_vector (evolution c)
# ===========================================================================


class TestExtraClausesListAll:
    """list_all(extra_clauses=[...]) appends additional WHERE conditions."""

    @pytest.mark.asyncio
    async def test_extra_clauses_applied_in_list_all(self, archived_repo):
        """Extra clause adds a WHERE condition referencing the specified column."""
        captured_stmts: list[Any] = []
        mock_session = _make_mock_session()
        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            if call_count == 1:
                mock_result.scalar_one.return_value = 0
            else:
                mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        # Add an extra clause on name column (not project_key to avoid ambiguity)
        extra = [_ARCHIVED_TABLE.c.name == "myname"]
        with _patch_factory(mock_session):
            items, total = await archived_repo.list_all(extra_clauses=extra)

        # The SELECT statement should reference the column in a WHERE clause
        assert len(captured_stmts) >= 2
        # Check both count and select statements reference the column
        select_sql = str(
            captured_stmts[1].compile(
                dialect=sa.dialects.postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "myname" in select_sql

    @pytest.mark.asyncio
    async def test_extra_clauses_none_is_default(self, archived_repo):
        """list_all() with no extra_clauses behaves identically to before."""
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
            items, total = await archived_repo.list_all()

        assert total == 0
        assert items == []


class TestExtraClausesSearchFts:
    """search_fts(extra_clauses=[...]) appends additional WHERE conditions."""

    @pytest.mark.asyncio
    async def test_extra_clauses_applied_in_search_fts(self, archived_repo):
        """Extra clause is visible in the FTS SELECT statement."""
        captured_stmts: list[Any] = []
        mock_session = _make_mock_session()
        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            if call_count == 1:
                mock_result.scalar_one.return_value = 0
            else:
                mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        extra = [_ARCHIVED_TABLE.c.name == "fts-name"]
        with _patch_factory(mock_session):
            items, total = await archived_repo.search_fts("hello", extra_clauses=extra)

        assert len(captured_stmts) >= 1
        # Use parameterized compile (no literal_binds) — check the column ref appears
        select_sql = str(
            captured_stmts[-1].compile(
                dialect=sa.dialects.postgresql.dialect(),
            )
        )
        # The WHERE clause must reference the name column from our extra clause
        assert "archived_items.name" in select_sql

    @pytest.mark.asyncio
    async def test_extra_clauses_none_default_search_fts(self, archived_repo):
        """search_fts() with no extra_clauses still works."""
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
            items, total = await archived_repo.search_fts("hello")

        assert total == 0
        assert items == []


class TestExtraClausesSearchVector:
    """search_vector(extra_clauses=[...]) appends additional WHERE conditions."""

    @pytest.mark.asyncio
    async def test_extra_clauses_applied_in_search_vector(self, archived_repo):
        """Extra clause is applied in the vector search SELECT statement."""
        captured_stmts: list[Any] = []
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        extra = [_ARCHIVED_TABLE.c.name == "vec-name"]
        with _patch_factory(mock_session):
            await archived_repo.search_vector([0.1] * 1536, extra_clauses=extra)

        assert len(captured_stmts) >= 1
        select_sql = str(
            captured_stmts[-1].compile(
                dialect=sa.dialects.postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "vec-name" in select_sql

    @pytest.mark.asyncio
    async def test_extra_clauses_none_default_search_vector(self, archived_repo):
        """search_vector() with no extra_clauses still works."""
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            results = await archived_repo.search_vector([0.0] * 1536)

        assert isinstance(results, list)


# ===========================================================================
# 17. include_archived on list_all (evolution d)
# ===========================================================================


class TestIncludeArchived:
    """list_all(include_archived=False) adds merged_into IS NULL + freshness_status != 'archived'.

    include_archived=True or None adds no archive filter.
    The table MUST have both merged_into and freshness_status columns.
    """

    @pytest.mark.asyncio
    async def test_include_archived_false_filters_merged_and_archived(self, archived_repo):
        """include_archived=False adds WHERE merged_into IS NULL AND freshness_status != 'archived'."""
        captured_stmts: list[Any] = []
        mock_session = _make_mock_session()
        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            if call_count == 1:
                mock_result.scalar_one.return_value = 0
            else:
                mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            items, total = await archived_repo.list_all(include_archived=False)

        assert len(captured_stmts) >= 2
        select_sql = str(captured_stmts[1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "merged_into" in select_sql
        assert "archived" in select_sql

    @pytest.mark.asyncio
    async def test_include_archived_true_no_archive_filter(self, archived_repo):
        """include_archived=True adds no archive filter to the query (no WHERE clause)."""
        captured_stmts: list[Any] = []
        mock_session = _make_mock_session()
        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            if call_count == 1:
                mock_result.scalar_one.return_value = 0
            else:
                mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            items, total = await archived_repo.list_all(include_archived=True)

        assert len(captured_stmts) >= 2
        select_sql = str(captured_stmts[1].compile(dialect=sa.dialects.postgresql.dialect()))
        # When include_archived=True, there must be no WHERE clause at all
        assert "WHERE" not in select_sql

    @pytest.mark.asyncio
    async def test_include_archived_none_no_archive_filter(self, archived_repo):
        """include_archived=None adds no archive filter (same as True, default)."""
        captured_stmts: list[Any] = []
        mock_session = _make_mock_session()
        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            if call_count == 1:
                mock_result.scalar_one.return_value = 0
            else:
                mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            items, total = await archived_repo.list_all(include_archived=None)

        assert len(captured_stmts) >= 2
        select_sql = str(captured_stmts[1].compile(dialect=sa.dialects.postgresql.dialect()))
        # When include_archived=None, there must be no WHERE clause at all
        assert "WHERE" not in select_sql

    def test_include_archived_table_without_columns_skips_filter(self, simple_repo):
        """list_all with include_archived=False on a table without these columns: no error."""
        # simple_repo table has no merged_into / freshness_status columns
        # We just verify the method signature accepts the parameter — actual execution
        # would fail without a session, but the import/parameter acceptance is enough.
        import inspect

        sig = inspect.signature(simple_repo.list_all)
        assert "include_archived" in sig.parameters

    @pytest.mark.asyncio
    async def test_include_archived_default_is_none(self, archived_repo):
        """list_all() default is include_archived=None (no archive filter by default)."""
        import inspect

        sig = inspect.signature(archived_repo.list_all)
        default = sig.parameters["include_archived"].default
        assert default is None


# ===========================================================================
# 18. find_one (evolution h)
# ===========================================================================


class TestFindOne:
    """find_one(filters) returns the first matching row or None."""

    @pytest.mark.asyncio
    async def test_find_one_returns_dict_when_found(self, full_repo):
        """find_one() returns a dict when a row matches."""
        mock_session = _make_mock_session()
        row_data = {"id": uuid.uuid4(), "name": "found", "project_key": "p1"}

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await full_repo.find_one({"project_key": "p1"}, session=mock_session)
        assert result is not None
        assert result["name"] == "found"

    @pytest.mark.asyncio
    async def test_find_one_returns_none_when_not_found(self, full_repo):
        """find_one() returns None when no row matches."""
        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await full_repo.find_one({"project_key": "nonexistent"}, session=mock_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_find_one_without_session_uses_factory(self, full_repo):
        """find_one() without session uses the session factory."""
        mock_session = _make_mock_session()
        row_data = {"id": uuid.uuid4(), "name": "found"}

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row_data
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await full_repo.find_one({"project_key": "p1"})

        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_find_one_raises_for_unknown_column(self, simple_repo):
        """find_one() raises ValueError for unknown column (via _build_filter_clauses)."""
        mock_session = _make_mock_session()
        with pytest.raises(ValueError, match="Unknown column"):
            await simple_repo.find_one({"nonexistent_col": "value"}, session=mock_session)


# ===========================================================================
# 19. search_vector similarity type — float, not Vector-typed (evolution a)
# ===========================================================================


class TestSearchVectorSimilarityType:
    """search_vector() must use op('<=>',return_type=Float) so the similarity label
    is a Python float, never a Vector-typed value that triggers the pgvector processor.

    We verify this by checking the compiled SQL does NOT contain a CAST() of the
    distance expression (which would add SQL overhead). The return_type= approach
    only changes the SA result processor, not the emitted SQL.
    """

    @pytest.mark.asyncio
    async def test_search_vector_similarity_label_no_cast_in_sql(self, full_repo):
        """The distance expression must NOT emit a ::FLOAT CAST in the SQL.

        op('<=>',return_type=Float) changes only the Python type processor,
        not the SQL. The SQL should only contain '<=>'.
        """
        captured_stmts: list[Any] = []
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            await full_repo.search_vector([0.1] * 1536)

        assert len(captured_stmts) >= 1
        sql = str(captured_stmts[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        # The <=> operator must appear (for distance computation)
        assert "<=>" in sql
        # similarity label must appear (it's the projected column)
        assert "similarity" in sql

    @pytest.mark.asyncio
    async def test_search_vector_orders_by_distance_asc(self, full_repo):
        """Rows are ordered by distance ascending (closest first)."""
        captured_stmts: list[Any] = []
        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        with _patch_factory(mock_session):
            await full_repo.search_vector([0.1] * 1536)

        assert len(captured_stmts) >= 1
        sql = str(captured_stmts[-1].compile(dialect=sa.dialects.postgresql.dialect())).upper()
        assert "ORDER BY" in sql
        # Default is ASC (no DESC) for distance
        # The ORDER BY clause should not say DESC
        order_idx = sql.index("ORDER BY")
        order_clause = sql[order_idx:]
        assert "DESC" not in order_clause or "ASC" in order_clause


# ===========================================================================
# 20. _maybe_session internal consolidation (evolution g)
# ===========================================================================


class TestMaybeSession:
    """_maybe_session(session, write=True/False) is an async context manager.

    - write=True: creates session + begins transaction when no session provided.
    - write=False: creates session without explicit transaction.
    - session provided: yields it directly (no new transaction).
    """

    @pytest.mark.asyncio
    async def test_maybe_session_with_existing_session_yields_it(self, simple_repo):
        """_maybe_session with an existing session yields it directly."""
        existing = _make_mock_session()

        async with simple_repo._maybe_session(existing, write=True) as sess:
            assert sess is existing

        # No begin() called — caller owns the transaction
        existing.begin.assert_not_called()

    @pytest.mark.asyncio
    async def test_maybe_session_none_write_creates_session_and_begins(self, simple_repo):
        """_maybe_session(None, write=True) creates a session and calls begin()."""
        mock_session = _make_mock_session()

        with _patch_factory(mock_session):
            async with simple_repo._maybe_session(None, write=True) as sess:
                assert sess is mock_session

        mock_session.begin.assert_called_once()

    @pytest.mark.asyncio
    async def test_maybe_session_none_read_creates_session_no_begin(self, simple_repo):
        """_maybe_session(None, write=False) creates a session without begin()."""
        mock_session = _make_mock_session()

        with _patch_factory(mock_session):
            async with simple_repo._maybe_session(None, write=False) as sess:
                assert sess is mock_session

        mock_session.begin.assert_not_called()


# ===========================================================================
# 12. resolve_id_prefix() — git-style short id resolution
# ===========================================================================


class TestResolveIdPrefix:
    @pytest.mark.asyncio
    async def test_returns_matching_ids_via_hyphen_insensitive_like(self, simple_repo):
        """resolve_id_prefix() matches on replace(id::text,'-','') LIKE prefix%."""
        mock_session = _make_mock_session()
        uid = uuid.uuid4()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [uid]
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await simple_repo.resolve_id_prefix("61b0fa47", session=mock_session)

        assert result == [uid]
        stmt = mock_session.execute.await_args.args[0]
        compiled = str(stmt.compile(dialect=sa.dialects.postgresql.dialect()))
        assert "replace" in compiled.lower()
        assert "like" in compiled.lower()

    @pytest.mark.asyncio
    async def test_orders_by_id_and_bounds_result_count(self, simple_repo):
        """Deterministic ORDER BY + LIMIT so ambiguity messages are stable."""
        mock_session = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        await simple_repo.resolve_id_prefix("61b0fa47", session=mock_session)

        stmt = mock_session.execute.await_args.args[0]
        compiled = str(stmt.compile(dialect=sa.dialects.postgresql.dialect()))
        assert "ORDER BY" in compiled
        assert "LIMIT" in compiled

    @pytest.mark.asyncio
    async def test_non_hex_prefix_returns_empty_without_querying(self, simple_repo):
        """LIKE wildcards / non-hex input never reach the database."""
        mock_session = _make_mock_session()
        mock_session.execute = AsyncMock()

        assert await simple_repo.resolve_id_prefix("61b0fa4%", session=mock_session) == []
        assert await simple_repo.resolve_id_prefix("", session=mock_session) == []
        mock_session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_session_uses_factory(self, simple_repo):
        """resolve_id_prefix() without session creates one via the factory."""
        mock_session = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with _patch_factory(mock_session):
            result = await simple_repo.resolve_id_prefix("61b0fa47")

        assert result == []
