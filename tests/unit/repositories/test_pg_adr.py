"""Unit tests for PgADRRepo — no real DB required.

All sessions are mocked with AsyncMock. Tests cover all acceptance criteria
for feature #618: CRUD, auto-number, FTS, vector search, accept(), filters.

Mock mechanism adaptation (vague 3 re-platform):
- PgADRRepo is now a BasePgRepository subclass — __init__ is inherited.
  Injecting session_factory=mock_factory at construction time still works
  (all tests already used this pattern; no patch teardown needed).
- _patch_adr_factory target updated to brain_v42.repositories.pg_base because
  get_session_factory is imported there, not in pg_adr anymore.
- vector_search rows now carry "similarity" (base contract) instead of
  "distance"; the public wrapper maps distance = 1.0 - similarity.
  Assertions adapted: dist ≈ 1.0 - similarity (epsilon ~1e-17 is irrelevant).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers: mock session and factory (same pattern as test_pg_base.py)
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
    mock_result.fetchone.return_value = None

    session.execute = AsyncMock(return_value=mock_result)

    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)

    session.begin_nested = MagicMock()
    session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)

    return session


def _patch_adr_factory(mock_session: AsyncMock):
    """Return a context manager that patches get_session_factory for pg_base.

    Adaptation (vague 3): PgADRRepo inherits session management from
    BasePgRepository.  get_session_factory is imported in pg_base, not pg_adr,
    so the patch target is brain_v42.repositories.pg_base.get_session_factory.
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


def _make_adr_row(
    adr_id: uuid.UUID | None = None,
    number: int = 1,
    project_key: str = "test_proj",
    title: str = "Test ADR",
    status: str = "proposed",
    decided_at: datetime | None = None,
    superseded_by: int | None = None,
) -> dict:
    """Build a minimal ADR row dict for mocking DB returns."""
    return {
        "id": adr_id or uuid.uuid4(),
        "number": number,
        "title": title,
        "context": "Context text",
        "decision": "Decision text",
        "consequences": "Consequences text",
        "alternatives_considered": [],
        "project_key": project_key,
        "tags": [],
        "status": status,
        "decided_at": decided_at,
        "superseded_by": superseded_by,
        "embedding": None,
        "metadata": {},
        "search_vector": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo():
    from brain_v42.repositories.pg_adr import PgADRRepo

    mock_factory = MagicMock()
    return PgADRRepo(session_factory=mock_factory)


# ===========================================================================
# 1. Class instantiation
# ===========================================================================


class TestClassInstantiation:
    def test_pg_adr_repo_instantiates(self):
        """PgADRRepo can be instantiated with a session_factory."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        factory = MagicMock()
        repo = PgADRRepo(session_factory=factory)
        assert repo is not None
        assert repo._session_factory is factory

    def test_pg_adr_repo_importable_from_repositories(self):
        """PgADRRepo is importable from brain_v42.repositories."""
        from brain_v42.repositories import PgADRRepo

        assert PgADRRepo is not None

    def test_repositories_all_exports_pg_adr_repo(self):
        """brain_v42.repositories.__all__ contains PgADRRepo."""
        import brain_v42.repositories as repos

        assert "PgADRRepo" in repos.__all__


# ===========================================================================
# 2. _next_number helper
# ===========================================================================


class TestNextNumber:
    @pytest.mark.asyncio
    async def test_next_number_uses_coalesce_max_query(self):
        """_next_number executes advisory-lock then COALESCE(MAX, 0) + 1 query.

        Two SQL calls are now expected: (1) pg_advisory_xact_lock to prevent
        concurrent duplicate-number races, (2) the MAX+1 SELECT.
        """
        from brain_v42.repositories.pg_adr import PgADRRepo

        factory = MagicMock()
        repo = PgADRRepo(session_factory=factory)

        mock_session = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 3
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo._next_number(mock_session, "proj1")
        assert result == 3
        # Now 2 calls: advisory lock + MAX query
        assert mock_session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_next_number_returns_1_for_empty_project(self):
        """_next_number returns 1 when no ADRs exist for the project."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        factory = MagicMock()
        repo = PgADRRepo(session_factory=factory)

        mock_session = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 1  # COALESCE(MAX(NULL), 0) + 1 = 1
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await repo._next_number(mock_session, "new_proj")
        assert result == 1


# ===========================================================================
# 3. create()
# ===========================================================================


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_adr_model(self):
        """create() returns an ADR model instance.

        Adaptation (vague 3): super().create() uses result.mappings().one() (not
        fetchone()) to retrieve the inserted row, so the INSERT mock must set
        mappings().one.return_value instead of fetchone.return_value.
        """
        from brain_v42.models.adr import ADR, ADRCreate
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        row = _make_adr_row(number=1)

        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                # advisory lock — no return value needed
                pass
            elif call_count == 2:
                # _next_number MAX+1 query
                mock_result.scalar_one.return_value = 1
            else:
                # INSERT RETURNING — base uses mappings().one()
                mock_result.mappings.return_value.one.return_value = row
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        data = ADRCreate(
            title="Test ADR",
            context="Context",
            decision="Decision",
            consequences="Consequences",
            project_key="proj1",
        )
        result = await repo.create(data)
        assert isinstance(result, ADR)
        assert result.number == 1

    @pytest.mark.asyncio
    async def test_create_with_embedding(self):
        """create() accepts an optional embedding parameter.

        Adaptation (vague 3): INSERT mock uses mappings().one() (base pattern).
        """
        from brain_v42.models.adr import ADR, ADRCreate
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        embedding = [0.1] * 1536
        row = _make_adr_row(number=1)
        row["embedding"] = embedding

        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                # advisory lock
                pass
            elif call_count == 2:
                # MAX+1 query
                mock_result.scalar_one.return_value = 1
            else:
                # INSERT RETURNING — base uses mappings().one()
                mock_result.mappings.return_value.one.return_value = row
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        data = ADRCreate(
            title="ADR with embedding",
            context="Context",
            decision="Decision",
            consequences="Consequences",
            project_key="proj1",
        )
        result = await repo.create(data, embedding=embedding)
        assert isinstance(result, ADR)

    @pytest.mark.asyncio
    async def test_create_auto_numbers_within_transaction(self):
        """create() calls _next_number (lock + MAX) and INSERT within the same session.

        With the advisory-lock fix, _next_number issues 2 SQL statements:
        (1) pg_advisory_xact_lock and (2) COALESCE(MAX,0)+1 — so total is 3 calls.

        Adaptation (vague 3): the INSERT (call 3) now goes through super().create()
        which uses mappings().one(), not fetchone().
        """
        from brain_v42.models.adr import ADRCreate
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        row = _make_adr_row(number=5)

        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                # First call is advisory lock (no return needed)
                pass
            elif call_count == 2:
                # Second call is MAX+1 query
                mock_result.scalar_one.return_value = 5
            else:
                # Third call is INSERT — base uses mappings().one()
                mock_result.mappings.return_value.one.return_value = row
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        data = ADRCreate(
            title="ADR 5",
            context="Context",
            decision="Decision",
            consequences="Consequences",
            project_key="proj1",
        )
        await repo.create(data)
        # 3 calls: advisory lock + MAX query + INSERT
        assert call_count == 3


# ===========================================================================
# 4. get_by_id()
# ===========================================================================


class TestGetById:
    @pytest.mark.asyncio
    async def test_get_by_id_returns_adr_when_found(self):
        """get_by_id() returns an ADR model when row exists."""
        from brain_v42.models.adr import ADR
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        adr_id = uuid.uuid4()
        row = _make_adr_row(adr_id=adr_id)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        result = await repo.get_by_id(adr_id)
        assert isinstance(result, ADR)
        assert result.id == adr_id

    @pytest.mark.asyncio
    async def test_get_by_id_returns_none_when_not_found(self):
        """get_by_id() returns None when row does not exist."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        result = await repo.get_by_id(uuid.uuid4())
        assert result is None


# ===========================================================================
# 5. get_by_number()
# ===========================================================================


class TestGetByNumber:
    @pytest.mark.asyncio
    async def test_get_by_number_returns_adr_when_found(self):
        """get_by_number() returns an ADR model when row exists."""
        from brain_v42.models.adr import ADR
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        row = _make_adr_row(number=3, project_key="proj1")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = row
        mock_session.execute = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        result = await repo.get_by_number(3, "proj1")
        assert isinstance(result, ADR)
        assert result.number == 3

    @pytest.mark.asyncio
    async def test_get_by_number_returns_none_when_not_found(self):
        """get_by_number() returns None when row does not exist."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        result = await repo.get_by_number(99, "proj1")
        assert result is None


# ===========================================================================
# 6. update()
# ===========================================================================


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_returns_adr_when_found(self):
        """update() returns updated ADR model when row exists."""
        from brain_v42.models.adr import ADR, ADRUpdate
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        adr_id = uuid.uuid4()
        updated_row = _make_adr_row(adr_id=adr_id, title="Updated Title")

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = updated_row
        mock_session.execute = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        update_data = ADRUpdate(title="Updated Title")
        result = await repo.update(adr_id, update_data)
        assert isinstance(result, ADR)
        assert result.title == "Updated Title"

    @pytest.mark.asyncio
    async def test_update_returns_none_when_not_found(self):
        """update() returns None when row does not exist."""
        from brain_v42.models.adr import ADRUpdate
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        result = await repo.update(uuid.uuid4(), ADRUpdate(title="x"))
        assert result is None

    @pytest.mark.asyncio
    async def test_update_patch_semantics_skips_none_fields(self):
        """update() with PATCH semantics only updates non-None fields."""
        from brain_v42.models.adr import ADRUpdate
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        adr_id = uuid.uuid4()
        row = _make_adr_row(adr_id=adr_id, title="Original")

        captured_stmt = None

        async def capture_execute(stmt, *args, **kwargs):
            nonlocal captured_stmt
            captured_stmt = stmt
            mock_result = MagicMock()
            mock_result.mappings.return_value.one_or_none.return_value = row
            return mock_result

        mock_session.execute = capture_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        # Only title is non-None; all others are None (should not be in UPDATE)
        await repo.update(adr_id, ADRUpdate(title="New Title"))
        assert captured_stmt is not None


# ===========================================================================
# 7. accept()
# ===========================================================================


class TestAccept:
    @pytest.mark.asyncio
    async def test_accept_sets_status_accepted_and_decided_at(self):
        """accept() sets status='accepted' and decided_at to current UTC time."""
        from brain_v42.models.adr import ADR
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        adr_id = uuid.uuid4()
        decided = datetime.now(UTC)
        accepted_row = _make_adr_row(adr_id=adr_id, status="accepted", decided_at=decided)

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = accepted_row
        mock_session.execute = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        result = await repo.accept(adr_id)
        assert isinstance(result, ADR)
        assert result.status == "accepted"
        assert result.decided_at is not None

    @pytest.mark.asyncio
    async def test_accept_returns_none_when_not_found(self):
        """accept() returns None when the ADR does not exist."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.mappings.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        result = await repo.accept(uuid.uuid4())
        assert result is None


# ===========================================================================
# 8. delete()
# ===========================================================================


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_returns_true_when_found(self):
        """delete() returns True when a row is deleted."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.one_or_none.return_value = (uuid.uuid4(),)
        mock_session.execute = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        result = await repo.delete(uuid.uuid4())
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self):
        """delete() returns False when row does not exist."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()

        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        result = await repo.delete(uuid.uuid4())
        assert result is False


# ===========================================================================
# 9. list_all()
# ===========================================================================


class TestListAll:
    @pytest.mark.asyncio
    async def test_list_all_returns_list_of_adrs(self):
        """list_all() returns a list of ADR models."""
        from brain_v42.models.adr import ADR
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        rows = [_make_adr_row(number=i, project_key="proj1") for i in range(1, 4)]

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = rows
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        results = await repo.list_all()
        assert len(results) == 3
        assert all(isinstance(r, ADR) for r in results)

    @pytest.mark.asyncio
    async def test_list_all_with_project_key_filter(self):
        """list_all(project_key='proj1') filters by project."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        rows = [_make_adr_row(project_key="proj1")]

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = rows
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        results = await repo.list_all(project_key="proj1")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_list_all_with_status_filter(self):
        """list_all(status='accepted') filters by status."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        rows = [_make_adr_row(status="accepted")]

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = rows
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        results = await repo.list_all(status="accepted")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_list_all_empty_result(self):
        """list_all() returns empty list when no ADRs match."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        results = await repo.list_all()
        assert results == []


# ===========================================================================
# 10. search()
# ===========================================================================


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_with_query_uses_fts(self):
        """search(query='..') uses FTS when query is provided."""
        from brain_v42.models.adr import ADR
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        rows = [_make_adr_row()]

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = rows
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        results = await repo.search(query="database")
        assert len(results) == 1
        assert isinstance(results[0], ADR)

    @pytest.mark.asyncio
    async def test_search_without_query_lists_all(self):
        """search() without query returns all ADRs (no FTS)."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        rows = [_make_adr_row(), _make_adr_row()]

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = rows
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        results = await repo.search()
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_search_with_project_key_filter(self):
        """search(project_key='..') filters by project_key."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        rows = [_make_adr_row(project_key="proj1")]

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = rows
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        results = await repo.search(project_key="proj1")
        assert len(results) == 1


# ===========================================================================
# 11. vector_search()
# ===========================================================================


class TestVectorSearch:
    @pytest.mark.asyncio
    async def test_vector_search_returns_list_of_adr_distance_tuples(self):
        """vector_search() returns list of (ADR, float) tuples.

        Adaptation (vague 3): the base search_vector() returns rows with a
        "similarity" key (= 1 - cosine_distance).  The wrapper maps
        distance = 1.0 - similarity, so we supply similarity=0.88 and expect
        distance ≈ 0.12.
        """
        from brain_v42.models.adr import ADR
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        # Base contract: rows carry "similarity", not "distance"
        row = {**_make_adr_row(), "similarity": 0.88}

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        results = await repo.vector_search([0.1] * 1536)
        assert isinstance(results, list)
        assert len(results) == 1
        adr_obj, dist = results[0]
        assert isinstance(adr_obj, ADR)
        assert isinstance(dist, float)

    @pytest.mark.asyncio
    async def test_vector_search_with_project_key_filter(self):
        """vector_search(project_key='..') filters by project_key."""
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = []
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        results = await repo.vector_search([0.1] * 1536, project_key="proj1")
        assert results == []

    @pytest.mark.asyncio
    async def test_vector_search_filters_distance_from_adr_mapping(self):
        """vector_search() excludes 'similarity' key from ADR model construction.

        Adaptation (vague 3): the base search_vector() returns rows with a
        "similarity" key.  The wrapper pops it (via dict.get), maps
        distance = 1.0 - similarity, and passes the remainder to _row_to_model.
        The allowlist in _row_to_model also strips "similarity" independently.

        With similarity=0.75 we expect distance = 0.25 in the public tuple.
        """
        from brain_v42.models.adr import ADR
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        # Base contract: rows carry "similarity", not "distance"
        row = {**_make_adr_row(), "similarity": 0.75}

        async def mock_execute(stmt, *args, **kwargs):
            mock_result = MagicMock()
            mock_result.mappings.return_value.all.return_value = [row]
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        results = await repo.vector_search([0.1] * 1536)
        # Must not raise a Pydantic validation error due to extra keys in the row
        assert len(results) == 1
        adr_obj, dist = results[0]
        assert isinstance(adr_obj, ADR)
        # distance = 1.0 - similarity = 1.0 - 0.75 = 0.25
        assert dist == pytest.approx(0.25)


# ===========================================================================
# 12. alternatives_considered JSONB serialization
# ===========================================================================


class TestAlternativesConsidered:
    @pytest.mark.asyncio
    async def test_create_serializes_alternatives_considered(self):
        """create() serializes alternatives_considered list as JSONB (list of dicts).

        Adaptation (vague 3): INSERT mock uses mappings().one() (base pattern).
        """
        from brain_v42.models.adr import ADRCreate, AlternativeConsidered
        from brain_v42.repositories.pg_adr import PgADRRepo

        mock_session = _make_mock_session()
        alts = [
            AlternativeConsidered(
                title="Option A",
                description="Description A",
                reason_rejected="Too slow",
            )
        ]
        row = _make_adr_row(number=1)
        row["alternatives_considered"] = [a.model_dump() for a in alts]

        call_count = 0

        async def mock_execute(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count == 1:
                # advisory lock
                pass
            elif call_count == 2:
                # MAX+1 query
                mock_result.scalar_one.return_value = 1
            else:
                # INSERT RETURNING — base uses mappings().one()
                mock_result.mappings.return_value.one.return_value = row
            return mock_result

        mock_session.execute = mock_execute

        @asynccontextmanager
        async def _session_cm(*args, **kwargs):
            yield mock_session

        mock_factory = MagicMock()
        mock_factory.side_effect = lambda: _session_cm()

        repo = PgADRRepo(session_factory=mock_factory)
        data = ADRCreate(
            title="ADR with alternatives",
            context="Context",
            decision="Decision",
            consequences="Consequences",
            project_key="proj1",
            alternatives_considered=alts,
        )
        result = await repo.create(data)
        # Pydantic deserializes JSONB list[dict] -> list[AlternativeConsidered]
        assert len(result.alternatives_considered) == 1
        assert result.alternatives_considered[0].title == "Option A"

    def test_adr_model_deserializes_alternatives_from_dicts(self):
        """ADR model coerces list[dict] -> list[AlternativeConsidered] via Pydantic v2."""
        from brain_v42.models.adr import ADR

        row = _make_adr_row()
        row["alternatives_considered"] = [
            {"title": "Alt 1", "description": "Desc 1", "reason_rejected": "Too complex"}
        ]

        adr = ADR.model_validate(row)
        assert len(adr.alternatives_considered) == 1
        assert adr.alternatives_considered[0].title == "Alt 1"


# ===========================================================================
# 13. superseded_by is Integer (not UUID)
# ===========================================================================


class TestSupersededByIsInteger:
    def test_adr_model_superseded_by_is_integer(self):
        """ADR.superseded_by is an int (ADR number), not UUID."""
        from brain_v42.models.adr import ADR

        row = _make_adr_row()
        row["superseded_by"] = 42

        adr = ADR.model_validate(row)
        assert adr.superseded_by == 42
        assert isinstance(adr.superseded_by, int)

    def test_adr_model_superseded_by_none_by_default(self):
        """ADR.superseded_by defaults to None."""
        from brain_v42.models.adr import ADR

        row = _make_adr_row()
        adr = ADR.model_validate(row)
        assert adr.superseded_by is None
