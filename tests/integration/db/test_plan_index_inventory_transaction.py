"""Disposable-PostgreSQL checks for the inventory repair transaction."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from brain_v42.maintenance.plan_index_inventory import RowMutation

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "plan_index_inventory.py"


def _cli():
    spec = importlib.util.spec_from_file_location("plan_index_inventory_transaction_cli", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_locked_revalidation_rejects_a_concurrent_parent_change(
    migration_database_url: str, tmp_path: Path
) -> None:
    """A stale approved before-state cannot overwrite a changed parent row."""
    cli = _cli()
    conn = await asyncpg.connect(migration_database_url.replace("+asyncpg", ""))
    plan_id = uuid4()
    try:
        await conn.execute(
            "INSERT INTO indexed_plans (id, file_path, title, plan_type, project_key, "
            "content_hash, content, freshness_status) VALUES ($1, $2, 'p', 'plan', $3, $4, 'x', 'fresh')",
            plan_id,
            f"/tmp/{plan_id}-plan.md",
            "red-games",
            "a" * 64,
        )
        before = await conn.fetchrow(
            "SELECT file_path, project_key, content_hash, freshness_status, "
            "freshness_source, indexed_at, updated_at "
            "FROM indexed_plans WHERE id = $1",
            plan_id,
        )
        assert before is not None
        mutation = RowMutation(
            row_id=str(plan_id),
            kind="rewrite",
            before={
                "file_path": before["file_path"],
                "project_key": before["project_key"],
                "freshness_status": before["freshness_status"],
                "content_hash": before["content_hash"],
                "indexed_at": before["indexed_at"].isoformat(),
                "updated_at": before["updated_at"].isoformat(),
                "freshness_source": before["freshness_source"],
            },
            after={
                "file_path": f"/tmp/{plan_id}-canonical-plan.md",
                "project_key": "red-writer",
                "freshness_status": "fresh",
            },
        )
        await conn.execute(
            "UPDATE indexed_plans SET content_hash = $2 WHERE id = $1", plan_id, "b" * 64
        )

        with pytest.raises(cli.InventoryApplyError, match="plan_state_changed"):
            await cli.apply_with_recovery(
                conn,
                (mutation,),
                tmp_path / "recovery.json",
                expected_dependents={"chunks": [], "feature_edges": []},
                detach_cross_project_links=False,
            )

        row = await conn.fetchrow(
            "SELECT content_hash, project_key FROM indexed_plans WHERE id = $1", plan_id
        )
        assert row["content_hash"] == "b" * 64
        assert row["project_key"] == "red-games"
        assert not (tmp_path / "recovery.json").exists()
    finally:
        await conn.close()


async def _mutation_for(conn: asyncpg.Connection, plan_id) -> RowMutation:
    before = await conn.fetchrow(
        "SELECT file_path, project_key, content_hash, freshness_status, "
        "freshness_source, indexed_at, updated_at FROM indexed_plans WHERE id = $1",
        plan_id,
    )
    assert before is not None
    return RowMutation(
        row_id=str(plan_id),
        kind="rewrite",
        before={
            "file_path": before["file_path"],
            "project_key": before["project_key"],
            "freshness_status": before["freshness_status"],
            "content_hash": before["content_hash"],
            "indexed_at": before["indexed_at"].isoformat(),
            "updated_at": before["updated_at"].isoformat(),
            "freshness_source": before["freshness_source"],
        },
        after={
            "file_path": f"/tmp/{plan_id}-canonical-plan.md",
            "project_key": "red-writer",
            "freshness_status": "fresh",
        },
    )


@pytest.mark.asyncio
async def test_locked_apply_reattributes_chunk_and_detaches_only_foreign_edge(
    migration_database_url: str, tmp_path: Path
) -> None:
    cli = _cli()
    conn = await asyncpg.connect(migration_database_url.replace("+asyncpg", ""))
    plan_id, chunk_id, feature_id = uuid4(), uuid4(), uuid4()
    try:
        await conn.execute(
            "INSERT INTO indexed_plans (id, file_path, title, plan_type, project_key, content_hash, content) "
            "VALUES ($1, $2, 'p', 'plan', 'red-games', $3, 'x')",
            plan_id,
            f"/tmp/{plan_id}-plan.md",
            "a" * 64,
        )
        await conn.execute(
            "INSERT INTO indexed_plan_chunks (id, plan_id, section_title, section_path, content, section_order, embedding, project_key, plan_type) "
            "VALUES ($1, $2, 's', 's', 'x', 0, array_fill(0::real, ARRAY[1536])::vector, 'red-games', 'plan')",
            chunk_id,
            plan_id,
        )
        await conn.execute(
            "INSERT INTO features (id, project_key, name, description) VALUES ($1, 'red-other', 'f', 'f')",
            feature_id,
        )
        await conn.execute(
            "INSERT INTO feature_artifacts (feature_id, artifact_type, artifact_id, similarity_score) "
            "VALUES ($1, 'plan', $2, 0.9)",
            feature_id,
            plan_id,
        )

        _written, _digest = await cli.apply_with_recovery(
            conn,
            (await _mutation_for(conn, plan_id),),
            tmp_path / "recovery.json",
            expected_dependents=await cli.read_dependent_state(conn, [str(plan_id)]),
            detach_cross_project_links=True,
        )

        parent = await conn.fetchrow("SELECT project_key FROM indexed_plans WHERE id = $1", plan_id)
        chunk = await conn.fetchrow(
            "SELECT project_key FROM indexed_plan_chunks WHERE id = $1", chunk_id
        )
        edge = await conn.fetchrow(
            "SELECT 1 FROM feature_artifacts WHERE feature_id = $1", feature_id
        )
        assert parent["project_key"] == chunk["project_key"] == "red-writer"
        assert edge is None
        recovery = json.loads((tmp_path / "recovery.json").read_text())
        assert recovery["chunks"][0]["id"] == str(chunk_id)
        assert recovery["feature_edges"][0]["feature_id"] == str(feature_id)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_chunk_update_failure_rolls_back_the_parent_rewrite(
    migration_database_url: str, tmp_path: Path
) -> None:
    cli = _cli()
    conn = await asyncpg.connect(migration_database_url.replace("+asyncpg", ""))
    plan_id, chunk_id = uuid4(), uuid4()
    try:
        await conn.execute(
            "INSERT INTO indexed_plans (id, file_path, title, plan_type, project_key, content_hash, content) "
            "VALUES ($1, $2, 'p', 'plan', 'red-games', $3, 'x')",
            plan_id,
            f"/tmp/{plan_id}-plan.md",
            "a" * 64,
        )
        await conn.execute(
            "INSERT INTO indexed_plan_chunks (id, plan_id, section_title, section_path, content, section_order, embedding, project_key, plan_type) "
            "VALUES ($1, $2, 's', 's', 'x', 0, array_fill(0::real, ARRAY[1536])::vector, 'red-games', 'plan')",
            chunk_id,
            plan_id,
        )
        await conn.execute(
            "CREATE FUNCTION fail_inventory_chunk_update() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced'; END; $$"
        )
        await conn.execute(
            "CREATE TRIGGER fail_inventory_chunk_update BEFORE UPDATE ON indexed_plan_chunks FOR EACH ROW EXECUTE FUNCTION fail_inventory_chunk_update()"
        )
        with pytest.raises(asyncpg.PostgresError):
            await cli.apply_with_recovery(
                conn,
                (await _mutation_for(conn, plan_id),),
                tmp_path / "recovery.json",
                expected_dependents=await cli.read_dependent_state(conn, [str(plan_id)]),
                detach_cross_project_links=False,
            )
        parent = await conn.fetchrow("SELECT project_key FROM indexed_plans WHERE id = $1", plan_id)
        assert parent["project_key"] == "red-games"
    finally:
        await conn.execute(
            "DROP TRIGGER IF EXISTS fail_inventory_chunk_update ON indexed_plan_chunks"
        )
        await conn.execute("DROP FUNCTION IF EXISTS fail_inventory_chunk_update()")
        await conn.close()
