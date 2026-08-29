"""Integration tests for PgIndexedPlanRepo."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

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


async def test_upsert_declares_its_provenance_on_both_branches(db_session):
    """Ticket 55a21fb8, fermé par la 049 : l'upsert posait `fresh` SANS source,
    le trigger de la 043 remettait la provenance à NULL — un plan ARCHIVÉ dont
    le fichier est réédité repassait fresh par ici, désarchivage légitime mais
    INVISIBLE (ni compté, ni attribué). Les deux branches (INSERT et ON
    CONFLICT UPDATE) déclarent désormais `plan_reindex`, un mot que le
    vocabulaire de la 049 admet."""
    repo = PgIndexedPlanRepo(db_session)
    base = IndexedPlanCreate(
        file_path="tests/fixtures/provenance_test.md",
        title="Provenance",
        plan_type="plan",
        project_key="brain-v42",
        content_hash="p" * 64,
        content="# P\n\n## S\n\nBody.",
        status="active",
        tags=[],
        word_count=3,
        chunk_count=1,
    )
    chunk = IndexedPlanChunkCreate(
        section_title="S",
        section_path="S",
        content="## S\n\nBody.",
        section_order=0,
        word_count=2,
        project_key="brain-v42",
        plan_type="plan",
        tags=[],
    )

    plan_id = await repo.upsert_plan_with_chunks(base, [0.01] * 1536, [chunk], [[0.02] * 1536])
    row = (
        (
            await db_session.execute(
                sa.text(
                    "SELECT freshness_status, freshness_source FROM indexed_plans WHERE id = :i"
                ),
                {"i": plan_id},
            )
        )
        .mappings()
        .one()
    )
    assert row["freshness_status"] == "fresh"
    assert row["freshness_source"] == "plan_reindex", "branche INSERT"

    # Simuler le cas du ticket : plan archivé, provenance effacée, fichier réédité.
    await db_session.execute(
        sa.text(
            "UPDATE indexed_plans SET freshness_status='archived', freshness_source=NULL "
            "WHERE id = :i"
        ),
        {"i": plan_id},
    )
    edited = base.model_copy(update={"content_hash": "q" * 64})
    plan_id_2 = await repo.upsert_plan_with_chunks(edited, [0.01] * 1536, [chunk], [[0.02] * 1536])
    assert plan_id_2 == plan_id
    row = (
        (
            await db_session.execute(
                sa.text(
                    "SELECT freshness_status, freshness_source FROM indexed_plans WHERE id = :i"
                ),
                {"i": plan_id},
            )
        )
        .mappings()
        .one()
    )
    assert row["freshness_status"] == "fresh"
    assert row["freshness_source"] == "plan_reindex", (
        "branche ON CONFLICT : le désarchivage par réédition se déclare"
    )

    # Auto-nettoyage : une ligne laissée avec le vocabulaire de la 049 ferait
    # tirer le refus fail-closed du downgrade dans les round-trips voisins —
    # mesuré sur le banc jetable : le garde a mordu son propre banc.
    await db_session.execute(sa.text("DELETE FROM indexed_plans WHERE id = :i"), {"i": plan_id})
    await db_session.commit()
