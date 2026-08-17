"""Integration tests for PgDecisionRepo — full CRUD, FTS, vector search, supersession.

These tests run against a real PostgreSQL+pgvector instance.
All tests use a unique project_key per test for isolation.

PgDecisionRepo uses the global get_session_factory() via BasePgRepository.get_session().
For integration tests we patch brain_v42.repositories.pg_base.get_session_factory
to return the test session_factory so that all DB operations use the test engine.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.models.decision import DecisionCreate, DecisionUpdate
from brain_v42.repositories.pg_decision import PgDecisionRepo

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _unique_key() -> str:
    """Return a unique project_key per test call."""
    return f"integ-{uuid.uuid4().hex[:8]}"


def _make_create(
    project_key: str,
    title: str = "Test Decision",
    description: str = "A test description",
    reasoning: str = "For integration testing",
    tags: list[str] | None = None,
) -> DecisionCreate:
    return DecisionCreate(
        title=title,
        description=description,
        reasoning=reasoning,
        project_key=project_key,
        tags=tags or [],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def decision_repo(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> PgDecisionRepo:
    """PgDecisionRepo with its global session factory patched to use the test engine."""
    monkeypatch.setattr(
        "brain_v42.repositories.pg_base.get_session_factory",
        lambda: session_factory,
    )
    return PgDecisionRepo()


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------


class TestDecisionCRUD:
    """Full CRUD lifecycle against real PostgreSQL."""

    async def test_create_and_get_by_id(self, decision_repo: PgDecisionRepo) -> None:
        """Creates a Decision, fetches by id — all fields match."""
        pk = _unique_key()
        data = _make_create(pk, title="Create and get decision")

        created = await decision_repo.create(data)

        assert created.id is not None
        assert created.title == "Create and get decision"
        assert created.project_key == pk
        assert created.status == "active"

        fetched = await decision_repo.get_by_id(created.id)

        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.title == created.title
        assert fetched.project_key == pk
        assert fetched.description == data.description
        assert fetched.reasoning == data.reasoning

    async def test_list_all_returns_created_decision(self, decision_repo: PgDecisionRepo) -> None:
        """Created decision appears in list_all with project_key filter."""
        pk = _unique_key()
        data = _make_create(pk, title="List all test decision")

        created = await decision_repo.create(data)
        results = await decision_repo.list_all(project_key=pk)

        assert len(results) >= 1
        ids = [d.id for d in results]
        assert created.id in ids

    async def test_update_changes_title(self, decision_repo: PgDecisionRepo) -> None:
        """Update changes the title field of an existing decision."""
        pk = _unique_key()
        data = _make_create(pk, title="Original title")
        created = await decision_repo.create(data)

        update_data = DecisionUpdate(title="Updated title")
        updated = await decision_repo.update(created.id, update_data)

        assert updated is not None
        assert updated.id == created.id
        assert updated.title == "Updated title"
        assert updated.description == data.description  # unchanged

    async def test_update_returns_none_for_missing_id(self, decision_repo: PgDecisionRepo) -> None:
        """Updating a non-existent UUID returns None."""
        missing_id = uuid.uuid4()
        update_data = DecisionUpdate(title="Should not matter")

        result = await decision_repo.update(missing_id, update_data)

        assert result is None

    async def test_delete_returns_true_and_removes_record(
        self, decision_repo: PgDecisionRepo
    ) -> None:
        """Delete returns True and get_by_id returns None afterwards."""
        pk = _unique_key()
        data = _make_create(pk, title="Decision to delete")
        created = await decision_repo.create(data)

        deleted = await decision_repo.delete(created.id)
        assert deleted is True

        fetched = await decision_repo.get_by_id(created.id)
        assert fetched is None

    async def test_delete_returns_false_for_missing(self, decision_repo: PgDecisionRepo) -> None:
        """Deleting a non-existent UUID returns False."""
        missing_id = uuid.uuid4()

        result = await decision_repo.delete(missing_id)

        assert result is False


# ---------------------------------------------------------------------------
# Full-text search tests
# ---------------------------------------------------------------------------


class TestDecisionFTS:
    """Full-text search on decisions — uses PostgreSQL tsvector."""

    async def test_search_fts_finds_by_title_keyword(self, decision_repo: PgDecisionRepo) -> None:
        """FTS finds a decision by a unique keyword in its title."""
        pk = _unique_key()
        unique_word = f"xyzfts{uuid.uuid4().hex[:6]}"
        data = _make_create(pk, title=f"Decision with {unique_word} in title")
        await decision_repo.create(data)

        results = await decision_repo.search_fts(unique_word, project_key=pk)

        assert len(results) >= 1
        titles = [d.title for d, _ in results]
        assert any(unique_word in t for t in titles)

    async def test_search_fts_empty_for_nonexistent_term(
        self, decision_repo: PgDecisionRepo
    ) -> None:
        """FTS returns [] for a term that matches no decision."""
        pk = _unique_key()
        # Create a decision to ensure the project_key exists
        data = _make_create(pk, title="Some real decision")
        await decision_repo.create(data)

        # Search for a random nonsense term
        nonsense = f"zzznomatch{uuid.uuid4().hex}"
        results = await decision_repo.search_fts(nonsense, project_key=pk)

        assert results == []

    async def test_search_fts_returns_score_tuples(self, decision_repo: PgDecisionRepo) -> None:
        """FTS results are (Decision, float) tuples with score > 0."""
        pk = _unique_key()
        unique_word = f"scoretest{uuid.uuid4().hex[:6]}"
        data = _make_create(pk, title=f"Scored decision {unique_word}")
        await decision_repo.create(data)

        results = await decision_repo.search_fts(unique_word, project_key=pk)

        assert len(results) >= 1
        for decision, score in results:
            assert isinstance(score, float)
            assert score > 0.0
            assert decision.title is not None


# ---------------------------------------------------------------------------
# Vector search tests
# ---------------------------------------------------------------------------


class TestDecisionVectorSearch:
    """Vector similarity search on decisions — uses pgvector cosine distance."""

    async def test_search_vector_returns_results_with_embedding(
        self, decision_repo: PgDecisionRepo
    ) -> None:
        """Decision with embedding appears in vector search results with similarity."""
        pk = _unique_key()
        embedding = [0.1] * 1536
        data = _make_create(pk, title="Vector searchable decision")

        created = await decision_repo.create(data, embedding=embedding)
        assert created.embedding is not None

        query_vec = [0.1] * 1536
        results = await decision_repo.search_vector(query_vec, project_key=pk)

        assert len(results) >= 1
        ids = [d.id for d, _ in results]
        assert created.id in ids

        for _, similarity in results:
            assert isinstance(similarity, float)
            assert 0.0 <= similarity <= 1.0

    async def test_search_vector_empty_when_no_embeddings(
        self, decision_repo: PgDecisionRepo
    ) -> None:
        """Decision without embedding is not returned by search_vector."""
        pk = _unique_key()
        data = _make_create(pk, title="No embedding decision")
        # Create without embedding
        await decision_repo.create(data)

        query_vec = [0.1] * 1536
        results = await decision_repo.search_vector(query_vec, project_key=pk)

        # Only rows with embedding IS NOT NULL are returned
        assert results == []


# ---------------------------------------------------------------------------
# Supersession chain tests
# ---------------------------------------------------------------------------


class TestDecisionSupersession:
    """Supersession chain and atomic supersede operation."""

    async def test_supersession_chain_single_decision(self, decision_repo: PgDecisionRepo) -> None:
        """A standalone decision has a supersession chain of length 1."""
        pk = _unique_key()
        data = _make_create(pk, title="Standalone decision")
        created = await decision_repo.create(data)

        chain = await decision_repo.get_supersession_chain(created.id)

        assert len(chain) == 1
        assert chain[0].id == created.id

    async def test_supersede_creates_chain(self, decision_repo: PgDecisionRepo) -> None:
        """supersede() creates a new Decision and marks old as superseded."""
        pk = _unique_key()
        old_data = _make_create(pk, title="Old decision")
        old = await decision_repo.create(old_data)

        new_data = _make_create(pk, title="New superseding decision")
        new = await decision_repo.supersede(old.id, new_data)

        assert new.id != old.id
        assert new.title == "New superseding decision"

        # Old decision should now be superseded
        refreshed_old = await decision_repo.get_by_id(old.id)
        assert refreshed_old is not None
        assert refreshed_old.status == "superseded"
        assert refreshed_old.superseded_by == new.id

    async def test_supersede_chain_ordered_old_to_new(self, decision_repo: PgDecisionRepo) -> None:
        """Supersession chain is ordered: chain[0] is old, chain[1] is new."""
        pk = _unique_key()
        old_data = _make_create(pk, title="Chain old decision")
        old = await decision_repo.create(old_data)

        new_data = _make_create(pk, title="Chain new decision")
        new = await decision_repo.supersede(old.id, new_data)

        chain = await decision_repo.get_supersession_chain(old.id)

        assert len(chain) == 2
        assert chain[0].id == old.id
        assert chain[1].id == new.id
