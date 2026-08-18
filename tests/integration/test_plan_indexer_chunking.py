"""Integration: PlanIndexer chunks plans end-to-end."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo
from brain_v42.services.plan_indexer import PlanIndexer

# ---------------------------------------------------------------------------
# Fake fixtures for embedding_service and cluster_guard
# ---------------------------------------------------------------------------


class _FakeEmbeddingService:
    """Fake embedding service that returns deterministic 1536-dim vectors."""

    async def embed(self, text: str) -> list[float]:
        return [0.1] * 1536

    async def embed_query(self, text: str) -> list[float]:
        return [0.1] * 1536

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1536 for _ in texts]


class _FakeClusterGuard:
    """Fake ClusterGuard that returns a dummy feature."""

    async def resolve(
        self,
        text: str,
        embedding: list[float],
        project_key: str,
        signal_type: str,
    ) -> tuple:
        feature = MagicMock()
        feature.id = uuid.uuid4()
        feature.name = "Fake Feature"
        return feature, "linked"


@pytest.fixture
def embedding_service() -> _FakeEmbeddingService:
    return _FakeEmbeddingService()


@pytest.fixture
def cluster_guard() -> _FakeClusterGuard:
    return _FakeClusterGuard()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_indexer_creates_chunks(
    tmp_path: Path,
    session_factory,
    embedding_service,
    cluster_guard,
    db_session,
):
    # Build a markdown file with 2 sections, each large enough to survive
    # the min-chunk rule (>=50 words).
    md = tmp_path / "sample-design.md"
    md.write_text(
        "# My Design\n\nIntro.\n\n## Architecture\n\n"
        + ("architecture words with enough content to pass the threshold " * 15)
        + "\n\n## Tests\n\n"
        + ("test section words with enough content to pass the threshold " * 15)
    )

    indexer = PlanIndexer(
        session_factory=session_factory,
        embedding_svc=embedding_service,
        cluster_guard=cluster_guard,
    )

    stats = await indexer.index_path(str(tmp_path), project_key="brain-v42")
    assert stats["indexed"] == 1
    assert stats["chunks_created"] == 2

    # Fetch back and verify
    plan_id = (
        await db_session.execute(
            text("SELECT id FROM indexed_plans WHERE file_path = :fp"),
            {"fp": str(md)},
        )
    ).scalar_one()

    repo = PgIndexedPlanRepo(db_session)
    result = await repo.get_with_chunks(plan_id)
    assert result is not None
    plan, chunks = result
    assert plan.chunk_count == 2
    titles = [c.section_title for c in chunks]
    assert titles == ["Architecture", "Tests"]

    # Cleanup
    await repo.delete(plan_id)


@pytest.mark.asyncio
async def test_plan_indexer_persists_planned_frontmatter_as_active(
    tmp_path: Path,
    session_factory,
    embedding_service,
    cluster_guard,
    db_session,
):
    md = tmp_path / "future-work-plan.md"
    md.write_text(
        """---
title: Future Work
status: planned
---

# Future Work

## Scope

Implementation details.
"""
    )
    indexer = PlanIndexer(
        session_factory=session_factory,
        embedding_svc=embedding_service,
        cluster_guard=cluster_guard,
    )

    stats = await indexer.index_path(str(tmp_path), project_key="brain-v42")

    assert stats["indexed"] == 1
    assert stats["chunks_created"] == 1
    plan_id = (
        await db_session.execute(
            text("SELECT id FROM indexed_plans WHERE file_path = :fp"),
            {"fp": str(md)},
        )
    ).scalar_one()
    repo = PgIndexedPlanRepo(db_session)
    result = await repo.get_with_chunks(plan_id)
    assert result is not None
    plan, chunks = result
    assert plan.status == "active"
    assert [chunk.status for chunk in chunks] == ["active"]

    await repo.delete(plan_id)


@pytest.mark.asyncio
async def test_plan_indexer_skips_unchanged_file(
    tmp_path: Path,
    session_factory,
    embedding_service,
    cluster_guard,
    db_session,
):
    md = tmp_path / "stable-plan.md"
    md.write_text("# Stable\n\n## A\n\n" + ("word with enough content to pass the threshold " * 15))

    indexer = PlanIndexer(
        session_factory=session_factory,
        embedding_svc=embedding_service,
        cluster_guard=cluster_guard,
    )

    first = await indexer.index_path(str(tmp_path), project_key="brain-v42")
    second = await indexer.index_path(str(tmp_path), project_key="brain-v42")

    assert first["indexed"] == 1
    assert second["indexed"] == 0
    assert second["skipped"] == 1

    # Cleanup
    plan_id = (
        await db_session.execute(
            text("SELECT id FROM indexed_plans WHERE file_path = :fp"),
            {"fp": str(md)},
        )
    ).scalar_one()
    repo = PgIndexedPlanRepo(db_session)
    await repo.delete(plan_id)


@pytest.mark.asyncio
async def test_plan_indexer_no_chunks_when_no_h2(
    tmp_path: Path,
    session_factory,
    embedding_service,
    cluster_guard,
    db_session,
):
    """A file with no H2 sections produces chunk_count=0 and chunks_created=0."""
    md = tmp_path / "minimal-design.md"
    md.write_text("# No Sections\n\nJust a flat document with no H2 headers at all.")

    indexer = PlanIndexer(
        session_factory=session_factory,
        embedding_svc=embedding_service,
        cluster_guard=cluster_guard,
    )

    stats = await indexer.index_path(str(tmp_path), project_key="brain-v42")
    assert stats["indexed"] == 1
    assert stats["chunks_created"] == 0

    plan_id = (
        await db_session.execute(
            text("SELECT id FROM indexed_plans WHERE file_path = :fp"),
            {"fp": str(md)},
        )
    ).scalar_one()

    repo = PgIndexedPlanRepo(db_session)
    result = await repo.get_with_chunks(plan_id)
    assert result is not None
    plan, chunks = result
    assert plan.chunk_count == 0
    assert chunks == []

    # Cleanup
    await repo.delete(plan_id)


@pytest.mark.asyncio
async def test_plan_indexer_skips_mirror_path_duplicate(
    tmp_path: Path,
    session_factory,
    embedding_service,
    cluster_guard,
    db_session,
):
    """Two scan paths that hold identical files should not produce two rows.

    Reproduces the brain_v42 mirror-path bug: a primary repo and a monorepo
    mirror both contain the same -design.md file, leading to duplicate
    indexed_plans rows. After the dedup-by-hash guard, the second scan must
    skip the file.
    """
    primary = tmp_path / "primary"
    mirror = tmp_path / "mirror"
    primary.mkdir()
    mirror.mkdir()

    body = "# Mirror Test\n\n## Section A\n\n" + (
        "words to push past the min chunk size threshold " * 15
    )
    (primary / "mirror-test-design.md").write_text(body)
    (mirror / "mirror-test-design.md").write_text(body)

    indexer = PlanIndexer(
        session_factory=session_factory,
        embedding_svc=embedding_service,
        cluster_guard=cluster_guard,
    )

    first = await indexer.index_path(str(primary), project_key="brain-v42-mirror")
    second = await indexer.index_path(str(mirror), project_key="brain-v42-mirror")

    assert first["indexed"] == 1
    assert second["indexed"] == 0
    assert second["skipped"] == 1

    rows = (
        await db_session.execute(
            text("SELECT id FROM indexed_plans WHERE project_key = :pk"),
            {"pk": "brain-v42-mirror"},
        )
    ).all()
    assert len(rows) == 1, f"expected 1 row after dedup, got {len(rows)}"

    # Cleanup
    await db_session.execute(
        text("DELETE FROM indexed_plans WHERE project_key = :pk"),
        {"pk": "brain-v42-mirror"},
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_dedupe_plans_consolidates_existing_duplicates(
    tmp_path: Path,
    session_factory,
    embedding_service,
    cluster_guard,
    db_session,
):
    """dedupe_plans collapses pre-existing rows that share a content_hash.

    Simulates the post-bug state: two indexed_plans rows with identical
    content_hash inserted directly (bypassing the new guard). After
    dedupe_plans runs, only one row should remain — the one whose file is
    still on disk.
    """
    pk = "brain-v42-dedupe-test"
    real_file = tmp_path / "real-design.md"
    real_file.write_text("# Real\n\nSome content")
    vanished_path = "/tmp/this/path/does/not/exist-design.md"
    shared_hash = "abc123" * 10 + "abcd"  # 64 hex chars
    embedding = "[" + ",".join(["0.1"] * 1536) + "]"

    insert_sql = text("""
        INSERT INTO indexed_plans (
            file_path, title, plan_type, project_key, content_hash,
            embedding, content, status, tags, metadata,
            chunk_count, word_count, freshness_status, indexed_at
        ) VALUES (
            :file_path, :title, 'spec', :pk, :hash,
            :embedding, '# x', 'active', '{}', '{}'::jsonb,
            0, 1, 'fresh', :indexed_at
        )
        RETURNING id
    """)

    from datetime import UTC, datetime, timedelta

    keeper_id = (
        await db_session.execute(
            insert_sql,
            {
                "file_path": str(real_file),
                "title": "Real",
                "pk": pk,
                "hash": shared_hash,
                "embedding": embedding,
                "indexed_at": datetime.now(UTC),
            },
        )
    ).scalar_one()
    loser_id = (
        await db_session.execute(
            insert_sql,
            {
                "file_path": vanished_path,
                "title": "Vanished",
                "pk": pk,
                "hash": shared_hash,
                "embedding": embedding,
                "indexed_at": datetime.now(UTC) - timedelta(days=1),
            },
        )
    ).scalar_one()
    await db_session.commit()

    indexer = PlanIndexer(
        session_factory=session_factory,
        embedding_svc=embedding_service,
        cluster_guard=cluster_guard,
    )
    deleted = await indexer.dedupe_plans(pk)
    assert deleted == 1

    survivors = (
        (
            await db_session.execute(
                text("SELECT id FROM indexed_plans WHERE project_key = :pk"),
                {"pk": pk},
            )
        )
        .scalars()
        .all()
    )
    assert survivors == [keeper_id], (
        f"expected only the on-disk row to survive; got {survivors} "
        f"(keeper={keeper_id}, loser={loser_id})"
    )

    # Cleanup
    await db_session.execute(
        text("DELETE FROM indexed_plans WHERE project_key = :pk"),
        {"pk": pk},
    )
    await db_session.commit()
