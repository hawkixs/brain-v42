"""Integration tests for PgIndexedPlanRepo."""

from __future__ import annotations

import pytest

from brain_v42.models.indexed_plan import IndexedPlanCreate
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunkCreate
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo


@pytest.mark.asyncio
async def test_upsert_plan_with_chunks_and_get(db_session):
    repo = PgIndexedPlanRepo(db_session)
    embedding = [0.01] * 1536

    plan_data = IndexedPlanCreate(
        file_path="tests/fixtures/repo_test.md",
        title="Repo Test Plan",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# Repo Test Plan\n\n## A\n\nBody A.\n\n## B\n\nBody B.",
        status="active",
        tags=["test"],
        word_count=20,
        chunk_count=2,
    )
    chunks_data = [
        IndexedPlanChunkCreate(
            section_title="A",
            section_path="A",
            content="## A\n\nBody A.",
            section_order=0,
            word_count=3,
            project_key="brain-v42",
            plan_type="plan",
            tags=["test"],
        ),
        IndexedPlanChunkCreate(
            section_title="B",
            section_path="B",
            content="## B\n\nBody B.",
            section_order=1,
            word_count=3,
            project_key="brain-v42",
            plan_type="plan",
            tags=["test"],
        ),
    ]
    chunk_embeddings = [[0.02] * 1536, [0.03] * 1536]

    plan_id = await repo.upsert_plan_with_chunks(
        plan_data, embedding, chunks_data, chunk_embeddings
    )
    assert plan_id is not None

    result = await repo.get_with_chunks(plan_id)
    assert result is not None
    fetched, chunks = result
    assert fetched.title == "Repo Test Plan"
    assert fetched.chunk_count == 2
    assert [c.section_order for c in chunks] == [0, 1]
    assert chunks[0].section_title == "A"

    # Cleanup
    await repo.delete(plan_id)


@pytest.mark.asyncio
async def test_upsert_replaces_existing_chunks(db_session):
    repo = PgIndexedPlanRepo(db_session)

    base = IndexedPlanCreate(
        file_path="tests/fixtures/replace_test.md",
        title="Replace Test",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# T\n\n## A\n\nA body",
        chunk_count=1,
        word_count=5,
    )
    chunk_a = IndexedPlanChunkCreate(
        section_title="A",
        section_path="A",
        content="## A\n\nA body",
        section_order=0,
        word_count=3,
        project_key="brain-v42",
        plan_type="plan",
    )
    plan_id = await repo.upsert_plan_with_chunks(base, [0.01] * 1536, [chunk_a], [[0.02] * 1536])

    # Re-upsert with one different chunk
    base2 = base.model_copy(update={"content_hash": "i" * 64, "chunk_count": 1})
    chunk_b = IndexedPlanChunkCreate(
        section_title="B",
        section_path="B",
        content="## B\n\nB body",
        section_order=0,
        word_count=3,
        project_key="brain-v42",
        plan_type="plan",
    )
    plan_id_2 = await repo.upsert_plan_with_chunks(base2, [0.01] * 1536, [chunk_b], [[0.03] * 1536])
    assert plan_id_2 == plan_id  # same file_path -> same row

    result = await repo.get_with_chunks(plan_id)
    assert result is not None
    _, chunks = result
    assert len(chunks) == 1
    assert chunks[0].section_title == "B"

    await repo.delete(plan_id)


@pytest.mark.asyncio
async def test_delete_cascades_to_chunks(db_session):
    repo = PgIndexedPlanRepo(db_session)

    base = IndexedPlanCreate(
        file_path="tests/fixtures/del_test.md",
        title="Del",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="h" * 64,
        content="# Del\n\n## A\n\nA body",
        chunk_count=1,
        word_count=5,
    )
    chunk = IndexedPlanChunkCreate(
        section_title="A",
        section_path="A",
        content="## A\n\nA body",
        section_order=0,
        word_count=3,
        project_key="brain-v42",
        plan_type="plan",
    )
    plan_id = await repo.upsert_plan_with_chunks(base, [0.01] * 1536, [chunk], [[0.02] * 1536])

    deleted = await repo.delete(plan_id)
    assert deleted is True

    result = await repo.get_with_chunks(plan_id)
    assert result is None
