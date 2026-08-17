"""Unit tests for ConsolidationJob — detect near-duplicate entities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.services.consolidation import ConsolidationJob


@pytest.fixture
def session() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def session_factory(session: AsyncMock) -> MagicMock:
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


@pytest.fixture
def log_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.get_handled_pairs = AsyncMock(return_value=set())
    return repo


@pytest.fixture
def job(session_factory: MagicMock, log_repo: AsyncMock) -> ConsolidationJob:
    return ConsolidationJob(
        session_factory=session_factory,
        consolidation_log_repo=log_repo,
        threshold=0.92,
    )


class TestFindCandidates:
    @pytest.mark.asyncio
    async def test_returns_list_of_dicts(
        self, job: ConsolidationJob, session: AsyncMock, log_repo: AsyncMock
    ) -> None:
        """find_candidates returns list of dicts with expected structure."""
        id_a, id_b = uuid4(), uuid4()

        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [
            {
                "id_a": id_a,
                "id_b": id_b,
                "similarity": 0.9501,
                "title_a": "Title A",
                "title_b": "Title B",
            }
        ]
        session.execute = AsyncMock(return_value=mock_result)

        candidates = await job.find_candidates(entity_type="decision")

        assert len(candidates) == 1
        c = candidates[0]
        assert c["entity_type"] == "decision"
        assert c["id_a"] == str(id_a)
        assert c["id_b"] == str(id_b)
        assert c["similarity"] == 0.9501
        assert c["title_a"] == "Title A"
        assert c["title_b"] == "Title B"

    @pytest.mark.asyncio
    async def test_excludes_handled_pairs(
        self, job: ConsolidationJob, session: AsyncMock, log_repo: AsyncMock
    ) -> None:
        """find_candidates excludes already-handled pairs."""
        id_a, id_b = uuid4(), uuid4()
        id_c, id_d = uuid4(), uuid4()

        # Mark (id_a, id_b) as already handled
        log_repo.get_handled_pairs.return_value = {(id_a, id_b), (id_b, id_a)}

        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = [
            {
                "id_a": id_a,
                "id_b": id_b,
                "similarity": 0.96,
                "title_a": "A",
                "title_b": "B",
            },
            {
                "id_a": id_c,
                "id_b": id_d,
                "similarity": 0.93,
                "title_a": "C",
                "title_b": "D",
            },
        ]
        session.execute = AsyncMock(return_value=mock_result)

        candidates = await job.find_candidates(entity_type="decision")

        assert len(candidates) == 1
        assert candidates[0]["id_a"] == str(id_c)

    @pytest.mark.asyncio
    async def test_respects_limit(
        self, job: ConsolidationJob, session: AsyncMock, log_repo: AsyncMock
    ) -> None:
        """find_candidates stops at the limit."""
        pairs = [
            {
                "id_a": uuid4(),
                "id_b": uuid4(),
                "similarity": 0.95 - i * 0.001,
                "title_a": f"A{i}",
                "title_b": f"B{i}",
            }
            for i in range(10)
        ]

        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = pairs
        session.execute = AsyncMock(return_value=mock_result)

        candidates = await job.find_candidates(entity_type="decision", limit=3)

        assert len(candidates) == 3

    @pytest.mark.asyncio
    async def test_filters_to_one_type(
        self, job: ConsolidationJob, session: AsyncMock, log_repo: AsyncMock
    ) -> None:
        """find_candidates(entity_type='decision') only checks decisions."""
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=mock_result)

        candidates = await job.find_candidates(entity_type="decision")

        assert candidates == []
        # Only one get_handled_pairs call (for 'decision')
        log_repo.get_handled_pairs.assert_called_once_with("decision")

    @pytest.mark.asyncio
    async def test_all_types_when_none(
        self, job: ConsolidationJob, session: AsyncMock, log_repo: AsyncMock
    ) -> None:
        """find_candidates(entity_type=None) checks all 5 types."""
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=mock_result)

        candidates = await job.find_candidates(entity_type=None)

        assert candidates == []
        # Should be called for each of the 5 types
        assert log_repo.get_handled_pairs.call_count == 5

    @pytest.mark.asyncio
    async def test_unknown_type_returns_empty(
        self, job: ConsolidationJob, session: AsyncMock, log_repo: AsyncMock
    ) -> None:
        """find_candidates with unknown entity_type returns empty list."""
        candidates = await job.find_candidates(entity_type="nonexistent")

        assert candidates == []
