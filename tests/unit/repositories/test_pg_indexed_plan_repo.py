"""Unit tests for PgIndexedPlanRepo — mocked async session, no real DB.

Covers:
- TDD RED: executemany batch insert (1 execute call for N chunks, not N calls)
- TDD RED: transactional safety (rollback on exception mid-upsert)
- Behavioral equivalence (same chunks, same param values)
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

PLAN_ID = uuid.uuid4()


def _make_plan() -> IndexedPlanCreate:
    return IndexedPlanCreate(
        file_path="docs/plans/my_spec.md",
        title="My Spec",
        plan_type="spec",
        project_key="brain-v42",
        content_hash="a" * 64,
        content="# My Spec\n\n## Intro\n\nHello.\n\n## Body\n\nWorld.",
        summary="A test spec",
        status="active",
        tags=["test"],
        metadata={"source": "unit"},
        chunk_count=2,
        word_count=10,
    )


def _make_chunks(n: int = 2) -> list[IndexedPlanChunkCreate]:
    return [
        IndexedPlanChunkCreate(
            section_title=f"Section {i}",
            section_path=f"section_{i}",
            content=f"Content of section {i}.",
            section_order=i,
            word_count=4,
            project_key="brain-v42",
            plan_type="spec",
            status="active",
            tags=["test"],
        )
        for i in range(n)
    ]


def _make_embeddings(n: int, dim: int = 4) -> list[list[float]]:
    """Return n small dummy embeddings (dim=4 to keep assertions readable)."""
    return [[float(i)] * dim for i in range(n)]


def _make_session(plan_id: uuid.UUID = PLAN_ID) -> AsyncMock:
    """Build a mock AsyncSession whose execute() returns sensible results.

    Call sequence during upsert_plan_with_chunks:
      1. upsert plan → scalar_one() = plan_id
      2. DELETE chunks → rowcount (ignored)
      3. INSERT chunks (executemany) → ignored result

    The mock tracks all execute() calls so tests can inspect call_count / args.
    """
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    # Default result
    default_result = MagicMock()
    default_result.scalar_one.return_value = plan_id
    default_result.rowcount = 0

    session.execute = AsyncMock(return_value=default_result)
    return session


# ===========================================================================
# 1. Executemany batch: single execute() call for all chunks
# ===========================================================================


class TestExecutemanyBatchInsert:
    """The chunk INSERT must be a single executemany call, not N calls."""

    @pytest.mark.asyncio
    async def test_two_chunks_produce_one_execute_call_for_inserts(self):
        """With 2 chunks, exactly 1 execute() call for the INSERT (executemany)."""
        session = _make_session()
        repo = PgIndexedPlanRepo(session)

        plan = _make_plan()
        chunks = _make_chunks(2)
        chunk_embs = _make_embeddings(2)
        plan_emb = _make_embeddings(1)[0]

        await repo.upsert_plan_with_chunks(plan, plan_emb, chunks, chunk_embs)

        # execute() calls: 1 (upsert plan) + 1 (DELETE) + 1 (INSERT executemany) = 3
        assert session.execute.call_count == 3, (
            f"Expected 3 execute() calls (upsert + delete + executemany insert), "
            f"got {session.execute.call_count}. "
            f"Old code loops N times; new code uses a single executemany."
        )

    @pytest.mark.asyncio
    async def test_five_chunks_produce_one_execute_call_for_inserts(self):
        """With 5 chunks, exactly 1 execute() call for the INSERT (executemany)."""
        session = _make_session()
        repo = PgIndexedPlanRepo(session)

        plan = _make_plan()
        chunks = _make_chunks(5)
        chunk_embs = _make_embeddings(5)
        plan_emb = _make_embeddings(1)[0]

        await repo.upsert_plan_with_chunks(plan, plan_emb, chunks, chunk_embs)

        assert session.execute.call_count == 3, (
            f"Expected 3 execute() calls regardless of chunk count, "
            f"got {session.execute.call_count}."
        )

    @pytest.mark.asyncio
    async def test_zero_chunks_skips_insert_execute(self):
        """With 0 chunks, only 2 execute() calls (upsert + delete, no insert)."""
        session = _make_session()
        repo = PgIndexedPlanRepo(session)

        plan = _make_plan()
        plan_emb = _make_embeddings(1)[0]

        await repo.upsert_plan_with_chunks(plan, plan_emb, [], [])

        # upsert + delete, no insert
        assert session.execute.call_count == 2, (
            f"Expected 2 execute() calls with no chunks, got {session.execute.call_count}."
        )

    @pytest.mark.asyncio
    async def test_executemany_passes_list_of_dicts(self):
        """The INSERT execute() call must receive a list (not a single dict) as params."""
        session = _make_session()
        repo = PgIndexedPlanRepo(session)

        plan = _make_plan()
        chunks = _make_chunks(3)
        chunk_embs = _make_embeddings(3)
        plan_emb = _make_embeddings(1)[0]

        await repo.upsert_plan_with_chunks(plan, plan_emb, chunks, chunk_embs)

        # The 3rd execute call (index 2) is the INSERT executemany
        insert_call = session.execute.call_args_list[2]
        params_arg = (
            insert_call.args[1] if len(insert_call.args) > 1 else insert_call.kwargs.get("params")
        )
        assert isinstance(params_arg, list), (
            f"The INSERT execute() params must be a list of dicts for executemany, "
            f"got {type(params_arg)}."
        )
        assert len(params_arg) == 3, (
            f"Expected 3 param dicts (one per chunk), got {len(params_arg)}."
        )

    @pytest.mark.asyncio
    async def test_executemany_each_dict_has_plan_id(self):
        """Each param dict in the executemany call must include plan_id."""
        session = _make_session(PLAN_ID)
        repo = PgIndexedPlanRepo(session)

        plan = _make_plan()
        chunks = _make_chunks(2)
        chunk_embs = _make_embeddings(2)
        plan_emb = _make_embeddings(1)[0]

        await repo.upsert_plan_with_chunks(plan, plan_emb, chunks, chunk_embs)

        insert_call = session.execute.call_args_list[2]
        params_arg = (
            insert_call.args[1] if len(insert_call.args) > 1 else insert_call.kwargs.get("params")
        )
        for i, param_dict in enumerate(params_arg):
            assert "plan_id" in param_dict, f"Param dict {i} missing 'plan_id' key."
            assert param_dict["plan_id"] == PLAN_ID, (
                f"Param dict {i} plan_id={param_dict['plan_id']!r} != {PLAN_ID!r}."
            )


# ===========================================================================
# 2. Transactional safety: rollback on exception at insert
# ===========================================================================


class TestTransactionalSafety:
    """An exception during the chunk INSERT must trigger session.rollback()."""

    @pytest.mark.asyncio
    async def test_rollback_called_on_insert_exception(self):
        """If execute() raises during chunk INSERT, rollback() must be called."""
        plan_id = uuid.uuid4()
        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()

        call_counter = {"n": 0}

        async def execute_with_failure(stmt: Any, params: Any = None) -> MagicMock:
            call_counter["n"] += 1
            result = MagicMock()
            result.scalar_one.return_value = plan_id
            result.rowcount = 0
            if call_counter["n"] == 3:
                # 3rd call = chunk INSERT — simulate DB error
                raise RuntimeError("DB error during insert")
            return result

        session.execute = execute_with_failure

        repo = PgIndexedPlanRepo(session)
        plan = _make_plan()
        chunks = _make_chunks(2)
        chunk_embs = _make_embeddings(2)
        plan_emb = _make_embeddings(1)[0]

        with pytest.raises(RuntimeError, match="DB error during insert"):
            await repo.upsert_plan_with_chunks(plan, plan_emb, chunks, chunk_embs)

        (
            session.rollback.assert_called_once(),
            ("session.rollback() must be called when an exception occurs during upsert."),
        )

    @pytest.mark.asyncio
    async def test_commit_not_called_on_exception(self):
        """If the INSERT raises, commit() must NOT be called (dirty session not committed)."""
        plan_id = uuid.uuid4()
        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()

        call_counter = {"n": 0}

        async def execute_with_failure(stmt: Any, params: Any = None) -> MagicMock:
            call_counter["n"] += 1
            result = MagicMock()
            result.scalar_one.return_value = plan_id
            result.rowcount = 0
            if call_counter["n"] == 3:
                raise RuntimeError("DB error")
            return result

        session.execute = execute_with_failure

        repo = PgIndexedPlanRepo(session)
        plan = _make_plan()
        chunks = _make_chunks(2)
        chunk_embs = _make_embeddings(2)
        plan_emb = _make_embeddings(1)[0]

        with pytest.raises(RuntimeError):
            await repo.upsert_plan_with_chunks(plan, plan_emb, chunks, chunk_embs)

        (
            session.commit.assert_not_called(),
            ("session.commit() must NOT be called when an exception raised during upsert."),
        )

    @pytest.mark.asyncio
    async def test_rollback_called_on_upsert_plan_exception(self):
        """If the plan upsert (1st execute) raises, rollback() must be called."""
        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()

        async def execute_fail_first(stmt: Any, params: Any = None) -> MagicMock:
            raise RuntimeError("plan upsert failed")

        session.execute = execute_fail_first

        repo = PgIndexedPlanRepo(session)
        plan = _make_plan()
        plan_emb = _make_embeddings(1)[0]

        with pytest.raises(RuntimeError, match="plan upsert failed"):
            await repo.upsert_plan_with_chunks(plan, plan_emb, [], [])

        session.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_happy_path_commits_once(self):
        """On success, commit() is called exactly once and rollback() is never called."""
        session = _make_session()
        repo = PgIndexedPlanRepo(session)

        plan = _make_plan()
        chunks = _make_chunks(2)
        chunk_embs = _make_embeddings(2)
        plan_emb = _make_embeddings(1)[0]

        await repo.upsert_plan_with_chunks(plan, plan_emb, chunks, chunk_embs)

        session.commit.assert_called_once()
        session.rollback.assert_not_called()


# ===========================================================================
# 3. Behavioral equivalence: chunk fields are preserved
# ===========================================================================


class TestBehavioralEquivalence:
    """The executemany params must contain the same field values as the old per-chunk loop."""

    @pytest.mark.asyncio
    async def test_chunk_section_title_in_params(self):
        """section_title of each chunk is correctly placed in executemany param dicts."""
        session = _make_session()
        repo = PgIndexedPlanRepo(session)

        plan = _make_plan()
        chunks = _make_chunks(2)
        chunk_embs = _make_embeddings(2)
        plan_emb = _make_embeddings(1)[0]

        await repo.upsert_plan_with_chunks(plan, plan_emb, chunks, chunk_embs)

        insert_call = session.execute.call_args_list[2]
        params = (
            insert_call.args[1] if len(insert_call.args) > 1 else insert_call.kwargs.get("params")
        )

        assert params[0]["section_title"] == "Section 0"
        assert params[1]["section_title"] == "Section 1"

    @pytest.mark.asyncio
    async def test_chunk_section_order_in_params(self):
        """section_order is preserved in the executemany param dicts."""
        session = _make_session()
        repo = PgIndexedPlanRepo(session)

        plan = _make_plan()
        chunks = _make_chunks(3)
        chunk_embs = _make_embeddings(3)
        plan_emb = _make_embeddings(1)[0]

        await repo.upsert_plan_with_chunks(plan, plan_emb, chunks, chunk_embs)

        insert_call = session.execute.call_args_list[2]
        params = (
            insert_call.args[1] if len(insert_call.args) > 1 else insert_call.kwargs.get("params")
        )

        for i, pd in enumerate(params):
            assert pd["section_order"] == i, (
                f"Expected section_order={i}, got {pd['section_order']}."
            )

    @pytest.mark.asyncio
    async def test_chunk_embedding_in_params(self):
        """Each chunk embedding is correctly serialised in executemany param dicts."""
        session = _make_session()
        repo = PgIndexedPlanRepo(session)

        plan = _make_plan()
        chunks = _make_chunks(2)
        embs = [[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]]
        plan_emb = [0.0, 0.0, 0.0, 0.0]

        await repo.upsert_plan_with_chunks(plan, plan_emb, chunks, embs)

        insert_call = session.execute.call_args_list[2]
        params = (
            insert_call.args[1] if len(insert_call.args) > 1 else insert_call.kwargs.get("params")
        )

        assert params[0]["embedding"] == str([1.0, 2.0, 3.0, 4.0])
        assert params[1]["embedding"] == str([5.0, 6.0, 7.0, 8.0])

    @pytest.mark.asyncio
    async def test_returns_plan_id_uuid(self):
        """upsert_plan_with_chunks returns the UUID from scalar_one()."""
        expected_id = uuid.uuid4()
        session = _make_session(expected_id)
        repo = PgIndexedPlanRepo(session)

        plan = _make_plan()
        plan_emb = _make_embeddings(1)[0]

        result = await repo.upsert_plan_with_chunks(plan, plan_emb, [], [])

        assert result == expected_id
