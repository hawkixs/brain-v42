"""TDD tests for SELECT SLIM — embedding/search_vector excluded from list/search paths.

Workstream: repos-perf (2026-07-02)

Chantier 1: list_all, search_fts, search_vector must NOT project embedding or search_vector
            in pg_base, pg_decision, pg_learning, pg_adr.

Strategy: capture the compiled SQL string via str(stmt.compile(...)) or intercept the
          statement passed to session.execute and inspect its exported_columns.
          We use the compiled SQL string approach since it survives mock interception.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# ---------------------------------------------------------------------------
# Shared helpers (reuse patterns from existing test_pg_base.py)
# ---------------------------------------------------------------------------

_META = MetaData()

_HEAVY_TABLE = Table(
    "heavy_items",
    _META,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("name", String(100)),
    Column("project_key", String(50)),
    Column("search_vector", sa.Text),  # stands-in for TSVECTOR
    Column("embedding", sa.Text),  # stands-in for Vector(1536)
    Column("created_at", sa.DateTime(timezone=True), nullable=False),
    Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def _make_mock_session() -> AsyncMock:
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
    return session


def _patch_factory(mock_session: AsyncMock):
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
def heavy_repo():
    from brain_v42.repositories.pg_base import BasePgRepository

    class HeavyRepo(BasePgRepository):
        table = _HEAVY_TABLE

    return HeavyRepo()


# ===========================================================================
# 1. BasePgRepository.list_all — must exclude embedding and search_vector
# ===========================================================================


class TestBaseListAllSlim:
    """list_all() in BasePgRepository must use _list_columns() that excludes heavy cols."""

    @pytest.mark.asyncio
    async def test_list_all_does_not_select_embedding(self, heavy_repo):
        """list_all() SELECT statement must not reference 'embedding' column."""
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
            await heavy_repo.list_all()

        # The second stmt is the SELECT (first is COUNT)
        assert len(captured_stmts) >= 2
        select_stmt = captured_stmts[1]
        sql = str(select_stmt.compile(dialect=sa.dialects.postgresql.dialect()))
        assert "embedding" not in sql, f"embedding found in list_all SQL: {sql}"

    @pytest.mark.asyncio
    async def test_list_all_does_not_select_search_vector(self, heavy_repo):
        """list_all() SELECT statement must not reference 'search_vector' column."""
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
            await heavy_repo.list_all()

        assert len(captured_stmts) >= 2
        select_stmt = captured_stmts[1]
        sql = str(select_stmt.compile(dialect=sa.dialects.postgresql.dialect()))
        assert "search_vector" not in sql, f"search_vector found in list_all SQL: {sql}"

    @pytest.mark.asyncio
    async def test_list_all_still_selects_other_columns(self, heavy_repo):
        """list_all() must still select id, name, project_key, created_at."""
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
            await heavy_repo.list_all()

        assert len(captured_stmts) >= 2
        select_stmt = captured_stmts[1]
        sql = str(select_stmt.compile(dialect=sa.dialects.postgresql.dialect()))
        assert "name" in sql
        assert "project_key" in sql


# ===========================================================================
# 2. pg_decision.list_all — must exclude embedding and search_vector
# ===========================================================================


def _make_decision_factory_patch():
    """Patch for pg_decision which uses get_session() directly."""
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()

    captured_stmts: list[Any] = []

    async def mock_execute(stmt, *args, **kwargs):
        captured_stmts.append(stmt)
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        return mock_result

    mock_session.execute = mock_execute

    @asynccontextmanager
    async def _session_cm(*args: Any, **kwargs: Any):
        yield mock_session

    mock_factory = MagicMock()
    mock_factory.side_effect = lambda: _session_cm()

    return mock_factory, mock_session, captured_stmts


class TestDecisionListAllSlim:
    @pytest.mark.asyncio
    async def test_list_all_does_not_select_embedding(self):
        """pg_decision.list_all() must not project embedding column."""
        from brain_v42.repositories.pg_decision import PgDecisionRepo

        captured_stmts: list[Any] = []
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def mock_execute(stmt, *args, **kwargs):
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args: Any, **kwargs: Any):
            yield mock_session

        with patch("brain_v42.repositories.pg_decision.PgDecisionRepo.get_session") as m:
            m.return_value = _session_cm()
            repo = PgDecisionRepo()
            # Patch get_session at instance level
            repo.get_session = _session_cm  # type: ignore[method-assign]
            await repo.list_all()

        assert len(captured_stmts) >= 1
        sql = str(captured_stmts[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "embedding" not in sql, f"embedding found in decision.list_all: {sql}"

    @pytest.mark.asyncio
    async def test_list_all_does_not_select_search_vector(self):
        """pg_decision.list_all() must not project search_vector column."""
        from brain_v42.repositories.pg_decision import PgDecisionRepo

        captured_stmts: list[Any] = []
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def mock_execute(stmt, *args, **kwargs):
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args: Any, **kwargs: Any):
            yield mock_session

        repo = PgDecisionRepo()
        repo.get_session = _session_cm  # type: ignore[method-assign]
        await repo.list_all()

        assert len(captured_stmts) >= 1
        sql = str(captured_stmts[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "search_vector" not in sql, f"search_vector found in decision.list_all: {sql}"


class TestDecisionSearchFtsSlim:
    @pytest.mark.asyncio
    async def test_search_fts_does_not_select_embedding(self):
        """pg_decision.search_fts() must not project embedding column."""
        from brain_v42.repositories.pg_decision import PgDecisionRepo

        captured_stmts: list[Any] = []
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def mock_execute(stmt, *args, **kwargs):
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args: Any, **kwargs: Any):
            yield mock_session

        repo = PgDecisionRepo()
        repo.get_session = _session_cm  # type: ignore[method-assign]
        await repo.search_fts("postgres")

        assert len(captured_stmts) >= 1
        sql = str(captured_stmts[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "embedding" not in sql, f"embedding found in decision.search_fts: {sql}"


class TestDecisionVectorSearchSlim:
    @pytest.mark.asyncio
    async def test_vector_search_does_not_select_search_vector(self):
        """pg_decision.search_vector() must not project search_vector column."""
        from brain_v42.repositories.pg_decision import PgDecisionRepo

        captured_stmts: list[Any] = []
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()

        async def mock_execute(stmt, *args, **kwargs):
            captured_stmts.append(stmt)
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args: Any, **kwargs: Any):
            yield mock_session

        repo = PgDecisionRepo()
        repo.get_session = _session_cm  # type: ignore[method-assign]
        await repo.search_vector([0.1] * 1536)

        assert len(captured_stmts) >= 1
        sql = str(captured_stmts[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "search_vector" not in sql, f"search_vector found in decision.vector_search: {sql}"


# ===========================================================================
# 3. pg_learning.list_all / search_fts — must exclude embedding and search_vector
# ===========================================================================


def _make_learning_repo_with_capture() -> tuple[Any, list[Any]]:
    """Create a PgLearningRepo with a mock factory that captures SQL statements.

    Adaptation (vague 3 re-platform): ``repo._session`` no longer exists on
    PgLearningRepo — the base absorbs session management via ``get_session()``.
    We now inject a mock ``session_factory`` so ``BasePgRepository.get_session()``
    (and ``_maybe_session``) yield the capturing session.
    """
    from brain_v42.repositories.pg_learning import PgLearningRepo

    captured_stmts: list[Any] = []
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    # begin() mock required for write-path methods (validate uses _maybe_session write=True)
    mock_session.begin = MagicMock()
    mock_session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    mock_session.begin.return_value.__aexit__ = AsyncMock(return_value=False)

    async def mock_execute(stmt, *args, **kwargs):
        captured_stmts.append(stmt)
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_result.mappings.return_value.first.return_value = None
        mock_result.scalar_one.return_value = 0
        return mock_result

    mock_session.execute = mock_execute

    @asynccontextmanager
    async def _session_cm(*args: Any, **kwargs: Any):
        yield mock_session

    mock_factory = MagicMock()
    mock_factory.side_effect = lambda: _session_cm()

    repo = PgLearningRepo(session_factory=mock_factory)
    return repo, captured_stmts


class TestLearningListAllSlim:
    @pytest.mark.asyncio
    async def test_list_all_does_not_select_embedding(self):
        """pg_learning.list_all() must not project embedding column."""
        repo, captured = _make_learning_repo_with_capture()
        await repo.list_all()
        assert len(captured) >= 1
        sql = str(captured[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "embedding" not in sql, f"embedding found in learning.list_all: {sql}"

    @pytest.mark.asyncio
    async def test_list_all_does_not_select_search_vector(self):
        """pg_learning.list_all() must not project search_vector column."""
        repo, captured = _make_learning_repo_with_capture()
        await repo.list_all()
        assert len(captured) >= 1
        sql = str(captured[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "search_vector" not in sql, f"search_vector found in learning.list_all: {sql}"


class TestLearningSearchFtsSlim:
    @pytest.mark.asyncio
    async def test_search_fts_does_not_select_embedding(self):
        """pg_learning.search_fts() must not project embedding column."""
        repo, captured = _make_learning_repo_with_capture()
        await repo.search_fts("python")
        assert len(captured) >= 1
        sql = str(captured[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "embedding" not in sql, f"embedding found in learning.search_fts: {sql}"


class TestLearningVectorSearchSlim:
    """pg_learning.search_vector() must NOT project the embedding column.

    Chantier 1 2nd-pass fix: the previous implementation excluded only
    search_vector but kept embedding in the SELECT, fetching ~12-19 KB/row
    unnecessarily (no caller reads Learning.embedding from this path).
    The embedding column is still used in the WHERE (IS NOT NULL) and for
    the distance ORDER BY; it just must not appear in the column projection.
    """

    @pytest.mark.asyncio
    async def test_vector_search_does_not_select_embedding(self):
        """pg_learning.search_vector() SELECT must not project the embedding column.

        The embedding column is legitimately referenced in the distance expression
        (learnings.embedding <=> query_vec) and the WHERE clause (IS NOT NULL),
        but it must NOT appear as a bare projected column in the SELECT list.
        We detect this by asserting that 'learnings.embedding,' (comma after the
        column reference) is absent — SA always adds a comma after each column in
        a multi-column SELECT, so this pattern matches only standalone projections.
        """
        import re

        repo, captured = _make_learning_repo_with_capture()
        await repo.search_vector([0.1] * 1536)
        assert len(captured) >= 1
        sql = str(captured[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        # Match 'learnings.embedding' followed by a comma or newline (standalone column projection)
        # This pattern excludes 'learnings.embedding <=>' (the distance expression)
        bare_projection = re.search(r"learnings\.embedding\s*(?:,|\n)", sql)
        assert bare_projection is None, (
            f"learnings.embedding projected as bare column in search_vector SELECT:\n{sql}"
        )

    @pytest.mark.asyncio
    async def test_vector_search_does_not_select_search_vector(self):
        """pg_learning.search_vector() SELECT must not project search_vector."""
        repo, captured = _make_learning_repo_with_capture()
        await repo.search_vector([0.1] * 1536)
        assert len(captured) >= 1
        sql = str(captured[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        select_part = sql.split("FROM")[0] if "FROM" in sql else sql
        assert "search_vector" not in select_part.lower(), (
            f"search_vector found in SELECT projection of learning.search_vector:\n{sql}"
        )

    @pytest.mark.asyncio
    async def test_vector_search_still_orders_by_distance(self):
        """pg_learning.search_vector() must still order by distance (embedding used in ORDER BY)."""
        repo, captured = _make_learning_repo_with_capture()
        await repo.search_vector([0.1] * 1536)
        assert len(captured) >= 1
        sql = str(captured[-1].compile(dialect=sa.dialects.postgresql.dialect())).upper()
        # Must have an ORDER BY referencing distance / the <=> operator
        assert "ORDER BY" in sql, "search_vector must ORDER BY distance"
        # Must have LIMIT
        assert "LIMIT" in sql, "search_vector must have LIMIT"


# ===========================================================================
# 4. pg_adr.list_all / search / vector_search — must exclude embedding and search_vector
# ===========================================================================


def _make_adr_repo_with_capture() -> tuple[Any, list[Any]]:
    from brain_v42.repositories.pg_adr import PgADRRepo

    captured_stmts: list[Any] = []
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.begin = MagicMock()
    mock_session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    mock_session.begin.return_value.__aexit__ = AsyncMock(return_value=False)

    async def mock_execute(stmt, *args, **kwargs):
        captured_stmts.append(stmt)
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_result.scalar_one.return_value = 0
        mock_result.fetchone.return_value = None
        return mock_result

    mock_session.execute = mock_execute

    @asynccontextmanager
    async def _session_cm(*args: Any, **kwargs: Any):
        yield mock_session

    mock_factory = MagicMock()
    mock_factory.side_effect = lambda: _session_cm()

    repo = PgADRRepo(session_factory=mock_factory)
    return repo, captured_stmts


class TestADRListAllSlim:
    @pytest.mark.asyncio
    async def test_list_all_does_not_select_embedding(self):
        """pg_adr.list_all() must not project embedding column."""
        repo, captured = _make_adr_repo_with_capture()
        await repo.list_all()
        assert len(captured) >= 1
        sql = str(captured[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "embedding" not in sql, f"embedding found in adr.list_all: {sql}"

    @pytest.mark.asyncio
    async def test_list_all_does_not_select_search_vector(self):
        """pg_adr.list_all() must not project search_vector column."""
        repo, captured = _make_adr_repo_with_capture()
        await repo.list_all()
        assert len(captured) >= 1
        sql = str(captured[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "search_vector" not in sql, f"search_vector found in adr.list_all: {sql}"


class TestADRSearchSlim:
    @pytest.mark.asyncio
    async def test_search_fts_branch_does_not_select_embedding(self):
        """pg_adr.search(query=..) FTS branch must not project embedding."""
        repo, captured = _make_adr_repo_with_capture()
        await repo.search(query="database migration")
        assert len(captured) >= 1
        sql = str(captured[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "embedding" not in sql, f"embedding found in adr.search (FTS): {sql}"

    @pytest.mark.asyncio
    async def test_search_no_query_branch_does_not_select_embedding(self):
        """pg_adr.search() no-query branch must not project embedding."""
        repo, captured = _make_adr_repo_with_capture()
        await repo.search()
        assert len(captured) >= 1
        sql = str(captured[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "embedding" not in sql, f"embedding found in adr.search (no-query): {sql}"


class TestADRVectorSearchSlim:
    @pytest.mark.asyncio
    async def test_vector_search_does_not_select_search_vector(self):
        """pg_adr.vector_search() must not project search_vector column."""
        repo, captured = _make_adr_repo_with_capture()
        await repo.vector_search([0.1] * 1536)
        assert len(captured) >= 1
        sql = str(captured[-1].compile(dialect=sa.dialects.postgresql.dialect()))
        assert "search_vector" not in sql, f"search_vector found in adr.vector_search: {sql}"
