"""Integration tests for cross-entity search — Learning FTS, vector search, FTS ranking.

These tests run against a real PostgreSQL+pgvector instance.
PgLearningRepo accepts session_factory injection in __init__, so no patching needed.
All tests use unique project_keys per test for isolation.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.models.learning import LearningCreate
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _unique_key() -> str:
    """Return a unique project_key per test call."""
    return f"integ-{uuid.uuid4().hex[:8]}"


def _make_learning(
    project_key: str,
    topic: str = "Test topic",
    insight: str = "Test insight",
    tags: list[str] | None = None,
    embedding: list[float] | None = None,
) -> LearningCreate:
    return LearningCreate(
        topic=topic,
        insight=insight,
        project_key=project_key,
        tags=tags or [],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def learning_repo(
    session_factory: async_sessionmaker[AsyncSession],
) -> PgLearningRepo:
    """PgLearningRepo instantiated with the test session_factory (Pattern B)."""
    return PgLearningRepo(session_factory=session_factory)


@pytest_asyncio.fixture
async def decision_repo_patched(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> PgDecisionRepo:
    """PgDecisionRepo with global session factory patched to test engine."""
    monkeypatch.setattr(
        "brain_v42.repositories.pg_base.get_session_factory",
        lambda: session_factory,
    )
    return PgDecisionRepo()


# ---------------------------------------------------------------------------
# Learning FTS tests
# ---------------------------------------------------------------------------


class TestLearningFTS:
    """Full-text search on learnings table."""

    async def test_learning_fts_finds_by_topic(self, learning_repo: PgLearningRepo) -> None:
        """FTS finds a learning by a unique keyword in its topic."""
        pk = _unique_key()
        unique_word = f"topicfts{uuid.uuid4().hex[:6]}"
        data = _make_learning(pk, topic=f"Learning about {unique_word} patterns")
        await learning_repo.create(data)

        results = await learning_repo.search_fts(unique_word, project_key=pk)

        assert len(results) >= 1
        topics = [lr.topic for lr in results]
        assert any(unique_word in t for t in topics)

    async def test_learning_fts_multiple_results_ordered_by_rank(
        self, learning_repo: PgLearningRepo
    ) -> None:
        """Multiple learnings are returned, ordered by FTS rank."""
        pk = _unique_key()
        unique_word = f"rankword{uuid.uuid4().hex[:6]}"

        # Learning 1: keyword appears in topic (weight A — highest)
        data1 = _make_learning(pk, topic=f"{unique_word} in topic", insight="Some insight")
        await learning_repo.create(data1)

        # Learning 2: keyword appears in insight only (weight B)
        data2 = _make_learning(
            pk, topic="Different topic", insight=f"Insight mentioning {unique_word}"
        )
        await learning_repo.create(data2)

        results = await learning_repo.search_fts(unique_word, project_key=pk)

        assert len(results) >= 2
        # The topic match (weight A) should appear before the insight match (weight B)
        assert unique_word in results[0].topic


# ---------------------------------------------------------------------------
# Learning vector search tests
# ---------------------------------------------------------------------------


class TestLearningVectorSearch:
    """Semantic vector search on learnings table."""

    async def test_learning_vector_search_returns_similar_first(
        self, learning_repo: PgLearningRepo
    ) -> None:
        """The learning with embedding closest to query appears first in results."""
        pk = _unique_key()

        # Embedding 1: very close to query [0.1] * 1536
        close_embedding = [0.1] * 1536
        data1 = _make_learning(pk, topic="Close embedding learning", insight="Near vector")
        learning1 = await learning_repo.create(data1, embedding=close_embedding)

        # Embedding 2: different — all zeros except first element
        far_embedding = [1.0] + [0.0] * 1535
        data2 = _make_learning(pk, topic="Far embedding learning", insight="Far vector")
        learning2 = await learning_repo.create(data2, embedding=far_embedding)

        query_vec = [0.1] * 1536
        results = await learning_repo.search_vector(query_vec, project_key=pk)

        assert len(results) >= 2
        ids = [lr.id for lr, _ in results]
        assert learning1.id in ids
        assert learning2.id in ids

        # First result should be the closest to query
        assert results[0][0].id == learning1.id

    async def test_learning_search_vector_returns_similarity_tuples(
        self, learning_repo: PgLearningRepo
    ) -> None:
        """Results are (Learning, float) tuples where float is similarity in [0.0, 1.0]."""
        pk = _unique_key()
        embedding = [0.5] * 1536
        data = _make_learning(pk, topic="Similarity tuple test", insight="Check similarity score")
        await learning_repo.create(data, embedding=embedding)

        query_vec = [0.5] * 1536
        results = await learning_repo.search_vector(query_vec, project_key=pk)

        assert len(results) >= 1
        for learning, similarity in results:
            assert learning.id is not None
            assert isinstance(similarity, float)
            assert 0.0 <= similarity <= 1.0


# ---------------------------------------------------------------------------
# FTS ranking — title weight higher than description
# ---------------------------------------------------------------------------


class TestDecisionFTSRanking:
    """FTS ranking: title (weight A) ranks higher than description (weight B)."""

    async def test_fts_title_weighted_higher_than_description(
        self, decision_repo_patched: PgDecisionRepo
    ) -> None:
        """A keyword in title ranks higher than the same keyword in description only.

        The FTS weight hierarchy (from migration):
        - Title → weight 'A' (highest)
        - Description → weight 'B'

        ts_rank with default weights [0.1, 0.2, 0.4, 1.0] (D/C/B/A) ensures
        that a match in title (A) outranks a match in description only (B).
        """
        pk = _unique_key()
        keyword = f"ranktest{uuid.uuid4().hex[:6]}"

        from brain_v42.models.decision import DecisionCreate

        # Decision A: keyword in title (weight A)
        data_title = DecisionCreate(
            title=f"Title contains {keyword} word",
            description="Description without the keyword",
            reasoning="Standard reasoning",
            project_key=pk,
        )
        await decision_repo_patched.create(data_title)

        # Decision B: keyword only in description (weight B)
        data_desc = DecisionCreate(
            title="Title without special word",
            description=f"Description contains {keyword} word",
            reasoning="Standard reasoning",
            project_key=pk,
        )
        await decision_repo_patched.create(data_desc)

        results = await decision_repo_patched.search_fts(keyword, project_key=pk)

        assert len(results) >= 2

        scores = {d.title: score for d, score in results}
        title_match_title = f"Title contains {keyword} word"
        desc_match_title = "Title without special word"

        assert title_match_title in scores
        assert desc_match_title in scores

        # Title match should have higher ts_rank score
        assert scores[title_match_title] > scores[desc_match_title]
