"""Disposable-PostgreSQL atomicity checks for the vector refresh boundary."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "refresh_plan_embeddings.py"
DIMENSION = 1536


def _cli():
    spec = importlib.util.spec_from_file_location("refresh_plan_embeddings_transaction_cli", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _vector(value: float) -> str:
    return "[" + ",".join(str(value) for _ in range(DIMENSION)) + "]"


async def _seed(conn: asyncpg.Connection, project_key: str) -> tuple[UUID, UUID, UUID]:
    plan_id, chunk_id, feature_id = uuid4(), uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO indexed_plans (id, file_path, title, plan_type, project_key, content_hash, "
        "embedding, content, summary, freshness_status, freshness_source) "
        "VALUES ($1, $2, 'Recovery', 'plan', $3, $4, $5::vector, 'Source body', "
        "'Summary', 'archived', 'manual_update')",
        plan_id,
        f"/private/{plan_id}-plan.md",
        project_key,
        "a" * 64,
        _vector(0.1),
    )
    await conn.execute(
        "INSERT INTO indexed_plan_chunks (id, plan_id, section_title, section_path, content, "
        "section_order, embedding, project_key, plan_type, status) "
        "VALUES ($1, $2, 'Section', 'Section', 'Chunk body', 0, $3::vector, $4, "
        "'plan', 'archived')",
        chunk_id,
        plan_id,
        _vector(0.1),
        project_key,
    )
    await conn.execute(
        "INSERT INTO features (id, project_key, name, description) VALUES ($1, $2, 'f', 'f')",
        feature_id,
        project_key,
    )
    await conn.execute(
        "INSERT INTO feature_artifacts (feature_id, artifact_type, artifact_id, similarity_score) "
        "VALUES ($1, 'plan', $2, 0.9)",
        feature_id,
        plan_id,
    )
    return plan_id, chunk_id, feature_id


async def _cleanup(conn: asyncpg.Connection, plan_id: UUID, feature_id: UUID) -> None:
    """Remove only this test's rows from the disposable database."""
    await conn.execute("DELETE FROM feature_artifacts WHERE artifact_id = $1", plan_id)
    await conn.execute("DELETE FROM features WHERE id = $1", feature_id)
    await conn.execute("DELETE FROM indexed_plan_chunks WHERE plan_id = $1", plan_id)
    await conn.execute("DELETE FROM indexed_plans WHERE id = $1", plan_id)


@pytest.mark.asyncio
async def test_refresh_updates_only_vectors_and_preserves_archived_cohort(
    migration_database_url: str, tmp_path: Path
) -> None:
    """A real refresh retains every parent/chunk source field and feature link."""
    cli = _cli()
    conn = await asyncpg.connect(migration_database_url.replace("+asyncpg", ""))
    plan_id: UUID | None = None
    feature_id: UUID | None = None
    try:
        project_key = f"refresh-{uuid4().hex[:12]}"
        plan_id, chunk_id, feature_id = await _seed(conn, project_key)
        snapshot = await cli.read_snapshot(
            conn, database="brain_migration", model="codestral", project_key=project_key
        )
        before_parent = snapshot.parents[0]
        before_chunk = snapshot.chunks[0]
        changed = [0.2] * DIMENSION
        plans, chunks, _ = await cli.apply_refresh(
            conn,
            snapshot,
            recovery_file=(tmp_path / "vector-recovery.json").absolute(),
            parent_vectors={str(plan_id): changed},
            chunk_vectors={str(chunk_id): changed},
        )

        parent = await conn.fetchrow(
            "SELECT id, project_key, content, summary, freshness_status, freshness_source, "
            "embedding::text AS embedding FROM indexed_plans WHERE id = $1",
            plan_id,
        )
        chunk = await conn.fetchrow(
            "SELECT id, plan_id, project_key, content, status, embedding::text AS embedding "
            "FROM indexed_plan_chunks WHERE id = $1",
            chunk_id,
        )
        linked = await conn.fetchval(
            "SELECT count(*) FROM feature_artifacts WHERE feature_id = $1 AND artifact_id = $2",
            feature_id,
            plan_id,
        )
        assert (plans, chunks) == (1, 1)
        assert parent["id"] == plan_id and parent["project_key"] == project_key
        assert parent["content"] == "Source body" and parent["summary"] == "Summary"
        assert (
            parent["freshness_status"] == "archived"
            and parent["freshness_source"] == "manual_update"
        )
        assert parent["embedding"] == _vector(0.2)
        assert chunk["id"] == chunk_id and chunk["plan_id"] == plan_id
        assert chunk["project_key"] == project_key and chunk["content"] == "Chunk body"
        assert chunk["status"] == "archived" and chunk["embedding"] == _vector(0.2)
        assert linked == 1
        after = await cli.read_snapshot(
            conn, database="brain_migration", model="codestral", project_key=project_key
        )
        assert (
            after.parents[0]
            | {"embedding": before_parent["embedding"], "updated_at": before_parent["updated_at"]}
            == before_parent
        )
        assert after.chunks[0] | {"embedding": before_chunk["embedding"]} == before_chunk
        assert after.parents[0]["updated_at"] > before_parent["updated_at"]
    finally:
        if plan_id is not None and feature_id is not None:
            await _cleanup(conn, plan_id, feature_id)
        await conn.close()


@pytest.mark.asyncio
async def test_refresh_rolls_back_both_tables_when_chunk_write_fails(
    migration_database_url: str, tmp_path: Path
) -> None:
    """A fault after the parent update cannot leave a mixed-model cohort."""
    cli = _cli()
    conn = await asyncpg.connect(migration_database_url.replace("+asyncpg", ""))
    plan_id: UUID | None = None
    feature_id: UUID | None = None
    trigger_created = False
    try:
        project_key = f"refresh-{uuid4().hex[:12]}"
        plan_id, chunk_id, feature_id = await _seed(conn, project_key)
        snapshot = await cli.read_snapshot(
            conn, database="brain_migration", model="codestral", project_key=project_key
        )
        await conn.execute(
            "CREATE FUNCTION fail_refresh_chunk_update() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'forced'; END; $$"
        )
        await conn.execute(
            "CREATE TRIGGER fail_refresh_chunk_update BEFORE UPDATE ON indexed_plan_chunks "
            "FOR EACH ROW EXECUTE FUNCTION fail_refresh_chunk_update()"
        )
        trigger_created = True
        with pytest.raises(asyncpg.PostgresError, match="forced"):
            await cli.apply_refresh(
                conn,
                snapshot,
                recovery_file=(tmp_path / "rollback-recovery.json").absolute(),
                parent_vectors={str(plan_id): [0.2] * DIMENSION},
                chunk_vectors={str(chunk_id): [0.2] * DIMENSION},
            )
        stored = await conn.fetch(
            "SELECT embedding::text AS embedding FROM indexed_plans WHERE id = $1 "
            "UNION ALL SELECT embedding::text AS embedding FROM indexed_plan_chunks WHERE id = $2",
            plan_id,
            chunk_id,
        )
        assert [row["embedding"] for row in stored] == [_vector(0.1), _vector(0.1)]
    finally:
        if trigger_created:
            await conn.execute(
                "DROP TRIGGER IF EXISTS fail_refresh_chunk_update ON indexed_plan_chunks"
            )
            await conn.execute("DROP FUNCTION IF EXISTS fail_refresh_chunk_update()")
        if plan_id is not None and feature_id is not None:
            await _cleanup(conn, plan_id, feature_id)
        await conn.close()


@pytest.mark.asyncio
async def test_refresh_refuses_a_concurrent_vector_only_change(
    migration_database_url: str, tmp_path: Path
) -> None:
    """CAS includes original vectors, not only source text and cohort IDs."""
    cli = _cli()
    conn = await asyncpg.connect(migration_database_url.replace("+asyncpg", ""))
    plan_id: UUID | None = None
    feature_id: UUID | None = None
    try:
        project_key = f"refresh-{uuid4().hex[:12]}"
        plan_id, chunk_id, feature_id = await _seed(conn, project_key)
        snapshot = await cli.read_snapshot(
            conn, database="brain_migration", model="codestral", project_key=project_key
        )
        await conn.execute(
            "UPDATE indexed_plan_chunks SET embedding = $2::vector WHERE id = $1",
            chunk_id,
            _vector(0.3),
        )
        with pytest.raises(cli.RefreshRefusal, match="cohort_state_changed"):
            await cli.apply_refresh(
                conn,
                snapshot,
                recovery_file=(tmp_path / "concurrent-recovery.json").absolute(),
                parent_vectors={str(plan_id): [0.2] * DIMENSION},
                chunk_vectors={str(chunk_id): [0.2] * DIMENSION},
            )
        assert await conn.fetchval(
            "SELECT embedding::text FROM indexed_plan_chunks WHERE id = $1", chunk_id
        ) == _vector(0.3)
    finally:
        if plan_id is not None and feature_id is not None:
            await _cleanup(conn, plan_id, feature_id)
        await conn.close()
