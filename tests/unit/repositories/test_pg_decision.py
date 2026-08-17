"""Unit tests for PgDecisionRepo — no real DB required.

All sessions are mocked. Tests cover:
1. create — creates a new decision
2. get_by_id (not found) — returns None when row doesn't exist
3. get_by_id (found) — returns Decision when row exists
4. update (not found) — returns None when ID doesn't exist
5. delete (True) — returns True when row deleted
6. delete (False) — returns False when row not found
7. search_fts — returns (Decision, float) tuples
8. search_vector — returns (Decision, float) tuples
9. supersession_chain — returns list of decisions via recursive CTE
10. supersede — creates new decision and marks old as superseded

Mock mechanism adaptations (vague 3 re-platform):
- Write operations (create, update, delete, supersede) now use
  ``async with sess.begin()`` (base pattern) instead of explicit
  ``session.commit()``.  Assertions that previously checked
  ``session.commit.assert_called_once()`` are adapted to check
  ``session.begin.return_value.__aexit__.assert_awaited_once()``.
- update() NOW() ecart: updated_at is stamped client-side (datetime.now(UTC))
  instead of server-side NOW() — no assertion change needed (value not inspected).
- get_by_id lambda in empty-update test updated to accept session kwarg.
- No behavioral assertions are weakened: return-type checks, value checks, and
  None-guard checks are all preserved verbatim.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.decision import Decision, DecisionCreate, DecisionUpdate
from brain_v42.repositories.pg_decision import PgDecisionRepo

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
OLD_UUID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NEW_UUID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

NOW = datetime.now(UTC)


def _make_decision_row(
    id: uuid.UUID | None = None,
    title: str = "Test Decision",
    description: str = "A description",
    reasoning: str = "Because it matters",
    project_key: str | None = "test-proj",
    status: str = "active",
    superseded_by: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Return a dict mimicking a DB row for decisions table."""
    return {
        "id": id or FIXED_UUID,
        "title": title,
        "description": description,
        "reasoning": reasoning,
        "alternatives": [],
        "consequences": None,
        "project_key": project_key,
        "tags": [],
        "status": status,
        "superseded_by": superseded_by,
        "embedding": None,
        "metadata": {},
        "created_at": NOW,
        "updated_at": NOW,
    }


def _make_mock_session(
    row: dict[str, Any] | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> AsyncMock:
    """Create an AsyncMock mimicking SQLAlchemy AsyncSession.

    If row is provided, .mappings().one() and .mappings().one_or_none() return it.
    If rows is provided, .mappings().all() returns them.
    """
    session = AsyncMock()

    mock_result = MagicMock()
    mock_mappings = MagicMock()
    mock_result.mappings.return_value = mock_mappings

    mock_mappings.one.return_value = row or {}
    mock_mappings.one_or_none.return_value = row
    mock_mappings.all.return_value = rows or []

    mock_result.rowcount = 1 if row is not None else 0
    mock_result.one_or_none.return_value = row

    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()

    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)

    return session


@asynccontextmanager
async def _mock_get_session(session: AsyncMock):  # type: ignore[misc]
    """Async context manager that yields the given mock session."""
    yield session


# ---------------------------------------------------------------------------
# Test 1: create
# ---------------------------------------------------------------------------


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_decision(self) -> None:
        """create() inserts a row and returns a Decision model."""
        row = _make_decision_row()
        session = _make_mock_session(row=row)
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            data = DecisionCreate(
                title="Test Decision",
                description="A description",
                reasoning="Because it matters",
                project_key="test-proj",
            )
            result = await repo.create(data)

        assert isinstance(result, Decision)
        assert result.title == "Test Decision"
        assert result.project_key == "test-proj"
        session.execute.assert_called_once()
        # Write path: begin() context manager must have been entered and exited.
        session.begin.return_value.__aexit__.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 2: get_by_id — not found
# ---------------------------------------------------------------------------


class TestGetByIdNotFound:
    @pytest.mark.asyncio
    async def test_get_by_id_returns_none_when_not_found(self) -> None:
        """get_by_id() returns None when the decision doesn't exist."""
        session = _make_mock_session(row=None)
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.get_by_id(FIXED_UUID)

        assert result is None
        session.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: get_by_id — found
# ---------------------------------------------------------------------------


class TestGetByIdFound:
    @pytest.mark.asyncio
    async def test_get_by_id_returns_decision_when_found(self) -> None:
        """get_by_id() returns a Decision when row exists."""
        row = _make_decision_row(id=FIXED_UUID)
        session = _make_mock_session(row=row)
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.get_by_id(FIXED_UUID)

        assert isinstance(result, Decision)
        assert result.id == FIXED_UUID
        assert result.title == "Test Decision"


# ---------------------------------------------------------------------------
# Test 4: update — not found
# ---------------------------------------------------------------------------


class TestUpdateNotFound:
    @pytest.mark.asyncio
    async def test_update_returns_none_when_not_found(self) -> None:
        """update() returns None when the decision ID doesn't exist."""
        session = _make_mock_session(row=None)
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.update(FIXED_UUID, DecisionUpdate(title="New Title"))

        assert result is None


# ---------------------------------------------------------------------------
# Test 4b: update — success
# ---------------------------------------------------------------------------


class TestUpdateSuccess:
    @pytest.mark.asyncio
    async def test_update_returns_decision_when_found(self) -> None:
        """update() returns updated Decision on success."""
        row = _make_decision_row(title="Updated Title")
        session = _make_mock_session(row=row)
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.update(FIXED_UUID, DecisionUpdate(title="Updated Title"))

        assert isinstance(result, Decision)
        assert result.title == "Updated Title"
        session.execute.assert_called_once()
        # Write path: begin() context manager must have been entered and exited.
        session.begin.return_value.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_with_embedding(self) -> None:
        """update() includes embedding in the UPDATE when provided."""
        row = _make_decision_row()
        session = _make_mock_session(row=row)
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            embedding = [0.1] * 1536
            result = await repo.update(FIXED_UUID, DecisionUpdate(title="New"), embedding=embedding)

        assert isinstance(result, Decision)
        # Write path: begin() context manager must have been entered and exited.
        session.begin.return_value.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_empty_data_falls_back_to_get_by_id(self) -> None:
        """update() with no fields set calls get_by_id (no-op path)."""
        row = _make_decision_row()
        session = _make_mock_session(row=row)
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            # Patch get_by_id to verify it's called (accepts session kwarg after re-platform)
            m.setattr(
                repo,
                "get_by_id",
                lambda _id, *, session=None: _mock_get_session(session).__aenter__(),
            )

            await repo.update(FIXED_UUID, DecisionUpdate())

        # No commit — it's a read-only fallback (begin() must not have been called)
        session.begin.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: delete — returns True
# ---------------------------------------------------------------------------


class TestDeleteTrue:
    @pytest.mark.asyncio
    async def test_delete_returns_true_when_row_deleted(self) -> None:
        """delete() returns True when the row existed and was deleted."""
        row = _make_decision_row(id=FIXED_UUID)
        session = _make_mock_session(row=row)
        session.execute.return_value.rowcount = 1
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.delete(FIXED_UUID)

        assert result is True
        # 2 execute calls: UPDATE (clear superseded_by refs) + DELETE
        assert session.execute.call_count == 2
        # Write path: transaction() uses begin() context manager — no explicit commit.
        session.begin.return_value.__aexit__.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 6: delete — returns False
# ---------------------------------------------------------------------------


class TestDeleteFalse:
    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self) -> None:
        """delete() returns False when the row didn't exist."""
        session = _make_mock_session(row=None)
        session.execute.return_value.rowcount = 0
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.delete(FIXED_UUID)

        assert result is False


# ---------------------------------------------------------------------------
# Test 6c: delete — clears superseded_by references before deleting
# ---------------------------------------------------------------------------

SUPERSEDING_UUID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


class TestDeleteClearsSupersededBy:
    """delete() must NULL out superseded_by refs and reactivate old decisions."""

    @pytest.mark.asyncio
    async def test_delete_issues_update_before_delete(self) -> None:
        """delete() runs UPDATE to clear superseded_by before the DELETE."""
        row = _make_decision_row(id=SUPERSEDING_UUID)
        session = _make_mock_session(row=row)
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.delete(SUPERSEDING_UUID)

        assert result is True
        # Must have 2 execute calls: UPDATE (clear refs) + DELETE
        assert session.execute.call_count == 2
        # Write path: transaction() uses begin() context manager — no explicit commit.
        session.begin.return_value.__aexit__.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 6b: list_all
# ---------------------------------------------------------------------------


class TestListAll:
    @pytest.mark.asyncio
    async def test_list_all_returns_decisions(self) -> None:
        """list_all() returns a list of Decision models."""
        row = _make_decision_row()
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.list_all()

        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], Decision)

    @pytest.mark.asyncio
    async def test_list_all_with_project_key_filter(self) -> None:
        """list_all() filters by project_key when provided."""
        row = _make_decision_row(project_key="brain_v42")
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.list_all(project_key="brain_v42")

        assert len(result) == 1
        session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_all_with_status_filter(self) -> None:
        """list_all() filters by status when provided."""
        row = _make_decision_row(status="active")
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.list_all(status="active")

        assert len(result) == 1
        session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_all_with_tags_filter(self) -> None:
        """list_all() filters by tags overlap when provided."""
        row = _make_decision_row()
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.list_all(tags=["architecture"])

        assert len(result) == 1
        session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_all_empty_result(self) -> None:
        """list_all() returns empty list when no rows match."""
        session = _make_mock_session(rows=[])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.list_all()

        assert result == []

    @pytest.mark.asyncio
    async def test_list_all_uses_pagination(self) -> None:
        """list_all() accepts limit and offset parameters."""
        session = _make_mock_session(rows=[])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.list_all(limit=5, offset=10)

        assert result == []
        session.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7: search_fts
# ---------------------------------------------------------------------------


class TestSearchFts:
    @pytest.mark.asyncio
    async def test_search_fts_returns_decision_score_tuples(self) -> None:
        """search_fts() returns list of (Decision, float) tuples."""
        row: dict[str, Any] = {**_make_decision_row(), "rank": 0.85, "search_vector": None}
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            results = await repo.search_fts("test query")

        assert len(results) == 1
        decision, score = results[0]
        assert isinstance(decision, Decision)
        assert isinstance(score, float)
        assert score == pytest.approx(0.85)

    @pytest.mark.asyncio
    async def test_search_fts_empty_results(self) -> None:
        """search_fts() returns empty list when no matches."""
        session = _make_mock_session(rows=[])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            results = await repo.search_fts("no match query")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_fts_with_project_key(self) -> None:
        """search_fts() applies project_key filter."""
        row: dict[str, Any] = {**_make_decision_row(), "rank": 0.7, "search_vector": None}
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            results = await repo.search_fts("test", project_key="brain_v42")

        assert len(results) == 1
        session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_fts_with_status(self) -> None:
        """search_fts() applies status filter."""
        row: dict[str, Any] = {**_make_decision_row(), "rank": 0.6, "search_vector": None}
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            results = await repo.search_fts("test", status="active")

        assert len(results) == 1
        session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_fts_with_tags(self) -> None:
        """search_fts() applies tags overlap filter."""
        row: dict[str, Any] = {**_make_decision_row(), "rank": 0.5, "search_vector": None}
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            results = await repo.search_fts("test", tags=["architecture"])

        assert len(results) == 1
        session.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Test 8: search_vector
# ---------------------------------------------------------------------------


class TestSearchVector:
    @pytest.mark.asyncio
    async def test_search_vector_returns_decision_similarity_tuples(self) -> None:
        """search_vector() returns list of (Decision, float) tuples."""
        row: dict[str, Any] = {**_make_decision_row(), "similarity": 0.92, "search_vector": None}
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            query_embedding = [0.1] * 1536
            results = await repo.search_vector(query_embedding)

        assert len(results) == 1
        decision, sim = results[0]
        assert isinstance(decision, Decision)
        assert isinstance(sim, float)
        assert sim == pytest.approx(0.92)

    @pytest.mark.asyncio
    async def test_search_vector_empty_results(self) -> None:
        """search_vector() returns empty list when no matching embeddings."""
        session = _make_mock_session(rows=[])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            results = await repo.search_vector([0.0] * 1536)

        assert results == []

    @pytest.mark.asyncio
    async def test_search_vector_with_project_key(self) -> None:
        """search_vector() applies project_key filter."""
        row: dict[str, Any] = {**_make_decision_row(), "similarity": 0.88, "search_vector": None}
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            results = await repo.search_vector([0.1] * 1536, project_key="brain_v42")

        assert len(results) == 1
        session.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Test 9: get_supersession_chain
# ---------------------------------------------------------------------------


class TestSupersessionChain:
    @pytest.mark.asyncio
    async def test_get_supersession_chain_returns_decisions(self) -> None:
        """get_supersession_chain() returns list of Decisions starting from given id."""
        row1: dict[str, Any] = {
            **_make_decision_row(id=OLD_UUID, status="superseded", superseded_by=NEW_UUID),
            "search_vector": None,
        }
        row2: dict[str, Any] = {
            **_make_decision_row(id=NEW_UUID, status="active"),
            "search_vector": None,
        }
        session = _make_mock_session(rows=[row1, row2])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            results = await repo.get_supersession_chain(OLD_UUID)

        assert len(results) == 2
        assert all(isinstance(d, Decision) for d in results)
        assert results[0].id == OLD_UUID
        assert results[1].id == NEW_UUID

    @pytest.mark.asyncio
    async def test_get_supersession_chain_single_decision(self) -> None:
        """get_supersession_chain() returns single decision when no supersession."""
        row: dict[str, Any] = {**_make_decision_row(id=FIXED_UUID), "search_vector": None}
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            results = await repo.get_supersession_chain(FIXED_UUID)

        assert len(results) == 1
        assert results[0].id == FIXED_UUID

    @pytest.mark.asyncio
    async def test_get_supersession_chain_bidirectional_from_new(self) -> None:
        """get_supersession_chain() returns full chain even when called from the NEW decision.

        The CTE must walk backward (new→old) to find the root, then forward
        to return the complete chain ordered oldest→newest.
        """
        row_old: dict[str, Any] = {
            **_make_decision_row(id=OLD_UUID, status="superseded", superseded_by=NEW_UUID),
            "search_vector": None,
        }
        row_new: dict[str, Any] = {
            **_make_decision_row(id=NEW_UUID, status="active"),
            "search_vector": None,
        }
        # Mock returns full chain even when starting from NEW_UUID
        session = _make_mock_session(rows=[row_old, row_new])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            results = await repo.get_supersession_chain(NEW_UUID)

        assert len(results) == 2
        assert results[0].id == OLD_UUID  # oldest first
        assert results[1].id == NEW_UUID  # newest last

    @pytest.mark.asyncio
    async def test_get_supersession_chain_three_decisions(self) -> None:
        """get_supersession_chain() handles a 3-deep chain from any entry point."""
        oldest = uuid.UUID("11111111-1111-1111-1111-111111111111")
        middle = uuid.UUID("22222222-2222-2222-2222-222222222222")
        newest = uuid.UUID("33333333-3333-3333-3333-333333333333")
        row1: dict[str, Any] = {
            **_make_decision_row(id=oldest, status="superseded", superseded_by=middle),
            "search_vector": None,
        }
        row2: dict[str, Any] = {
            **_make_decision_row(id=middle, status="superseded", superseded_by=newest),
            "search_vector": None,
        }
        row3: dict[str, Any] = {
            **_make_decision_row(id=newest, status="active"),
            "search_vector": None,
        }
        session = _make_mock_session(rows=[row1, row2, row3])
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            # Call from the middle of the chain
            results = await repo.get_supersession_chain(middle)

        assert len(results) == 3
        assert results[0].id == oldest
        assert results[1].id == middle
        assert results[2].id == newest


# ---------------------------------------------------------------------------
# Test 10: supersede
# ---------------------------------------------------------------------------


class TestSupersede:
    @pytest.mark.asyncio
    async def test_supersede_returns_new_decision(self) -> None:
        """supersede() creates new decision and marks old as superseded; returns new Decision."""
        new_row = _make_decision_row(id=NEW_UUID, title="New Decision", status="active")
        session = _make_mock_session(row=new_row)
        repo = PgDecisionRepo()

        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            new_data = DecisionCreate(
                title="New Decision",
                description="Updated description",
                reasoning="Better reasoning",
                project_key="test-proj",
            )
            result = await repo.supersede(OLD_UUID, new_data)

        assert isinstance(result, Decision)
        assert result.title == "New Decision"
        # Two SQL executions: INSERT (new) + UPDATE (old)
        assert session.execute.call_count == 2
        # Write path: transaction() uses begin() context manager — no explicit commit.
        session.begin.return_value.__aexit__.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 11: fetch_cross_project_resonance_pairs
# ---------------------------------------------------------------------------


class TestFetchCrossProjectResonancePairs:
    @pytest.mark.asyncio
    async def test_sql_excludes_intra_project_and_nulls(self):
        session = _make_mock_session(rows=[])
        repo = PgDecisionRepo()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            await repo.fetch_cross_project_resonance_pairs(
                ids=[str(uuid4()), str(uuid4())], threshold=0.80
            )
        sql = str(session.execute.call_args[0][0])
        assert "a.project_key <> b.project_key" in sql
        assert "a.embedding IS NOT NULL" in sql
        assert "a.id < b.id" in sql
        assert "ANY(CAST(:ids AS uuid[]))" in sql

    @pytest.mark.asyncio
    async def test_returns_row_dicts(self):
        row = {
            "a_id": uuid4(),
            "b_id": uuid4(),
            "a_project": "brain-v42",
            "b_project": "red-shrik",
            "a_title": "t1",
            "b_title": "t2",
            "a_created_at": date(2026, 4, 1),
            "b_created_at": date(2026, 4, 2),
            "cosine": 0.91,
        }
        session = _make_mock_session(rows=[row])
        repo = PgDecisionRepo()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            result = await repo.fetch_cross_project_resonance_pairs(
                ids=[str(uuid4())], threshold=0.8
            )
        assert result[0]["cosine"] == 0.91

    @pytest.mark.asyncio
    async def test_empty_ids_short_circuits_without_query(self):
        session = _make_mock_session(rows=[])
        repo = PgDecisionRepo()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(repo, "get_session", lambda: _mock_get_session(session))
            assert await repo.fetch_cross_project_resonance_pairs(ids=[], threshold=0.8) == []
        session.execute.assert_not_called()
