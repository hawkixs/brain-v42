"""Execute both v4 recovery attestations in a read-only PostgreSQL transaction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[3]
RECOVERY = ROOT / "ops" / "recovery"

V3_DATA_RELATIONS = (
    "adrs",
    "alembic_version",
    "brain_entities",
    "brain_session_artifacts",
    "brain_sessions",
    "decisions",
    "entity_relations",
    "feature_artifacts",
    "features",
    "gitlab_events",
    "graph_outbox",
    "graph_projection_leases",
    "indexed_plan_chunks",
    "indexed_plans",
    "learnings",
    "project_contexts",
    "projects",
    "runbooks",
    "snippets",
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


async def _database_snapshot(connection: Any) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for relation in V3_DATA_RELATIONS:
        snapshot[f"table:{relation}"] = str(
            await connection.scalar(
                sa.text(
                    "SELECT COALESCE(jsonb_agg(to_jsonb(observed) "
                    "ORDER BY to_jsonb(observed)::text), '[]'::jsonb)::text "
                    f"FROM public.{relation} AS observed"
                )
            )
        )
    catalog_queries = {
        "catalog:functions": """
            SELECT COALESCE(jsonb_agg(to_jsonb(observed) ORDER BY to_jsonb(observed)::text), '[]'::jsonb)::text
            FROM (
                SELECT namespace_record.nspname, function_record.*
                FROM pg_catalog.pg_proc AS function_record
                JOIN pg_catalog.pg_namespace AS namespace_record
                  ON namespace_record.oid = function_record.pronamespace
                WHERE namespace_record.nspname = 'public'
                  AND function_record.proname IN (
                      'set_project_context_updated_at', 'update_updated_at', 'vector_dims'
                  )
            ) AS observed
        """,
        "catalog:namespaces": """
            SELECT jsonb_agg(to_jsonb(observed) ORDER BY to_jsonb(observed)::text)::text
            FROM (SELECT * FROM pg_catalog.pg_namespace WHERE nspname = 'public') AS observed
        """,
        "catalog:default_acl": """
            SELECT COALESCE(jsonb_agg(to_jsonb(observed) ORDER BY to_jsonb(observed)::text), '[]'::jsonb)::text
            FROM pg_catalog.pg_default_acl AS observed
        """,
        "catalog:triggers": """
            SELECT COALESCE(jsonb_agg(to_jsonb(observed) ORDER BY to_jsonb(observed)::text), '[]'::jsonb)::text
            FROM (
                SELECT trigger_record.*
                FROM pg_catalog.pg_trigger AS trigger_record
                JOIN pg_catalog.pg_class AS table_record
                  ON table_record.oid = trigger_record.tgrelid
                JOIN pg_catalog.pg_namespace AS namespace_record
                  ON namespace_record.oid = table_record.relnamespace
                WHERE namespace_record.nspname = 'public'
            ) AS observed
        """,
        "catalog:languages": """
            SELECT jsonb_agg(to_jsonb(observed) ORDER BY to_jsonb(observed)::text)::text
            FROM (SELECT * FROM pg_catalog.pg_language WHERE lanname = 'plpgsql') AS observed
        """,
    }
    for name, query in catalog_queries.items():
        snapshot[name] = str(await connection.scalar(sa.text(query)))
    return snapshot


@pytest.mark.parametrize("asset", ["brain-v42-v4.sql", "brain-v42-v4-pgrestore.sql"])
@pytest.mark.asyncio
async def test_v4_assets_execute_read_only_without_mutation(
    engine: AsyncEngine, asset: str
) -> None:
    contract_path = RECOVERY / "brain-v42-v4.json"
    sql_path = RECOVERY / asset
    if not contract_path.is_file() or not sql_path.is_file():
        pytest.fail("recovery_v4_asset_missing")
    expected = json.loads(
        contract_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
    )
    sql = sql_path.read_text(encoding="utf-8")

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(sa.text("SET TRANSACTION READ ONLY"))
            assert await connection.scalar(sa.text("SHOW transaction_read_only")) == "on"
            before = await _database_snapshot(connection)
            raw = await connection.scalar(sa.text(sql))
            result = json.loads(str(raw), object_pairs_hook=_reject_duplicate_keys)
            after = await _database_snapshot(connection)
            assert set(result) == {"checks", "contract_id", "schema_version"}
            assert result["contract_id"] == "brain-v42/postgresql-recovery/v4"
            assert result["schema_version"] == 4
            observed_ids = [check["id"] for check in result["checks"]]
            expected_ids = [check["id"] for check in expected["checks"]]
            assert len(observed_ids) == len(set(observed_ids)) == 25
            assert set(observed_ids) == set(expected_ids)
            assert all(
                set(check) == {"expected", "id", "observed", "status"}
                and isinstance(check["id"], str)
                and check["id"]
                and check["status"] in {"pass", "fail"}
                for check in result["checks"]
            )
            assert after == before
        finally:
            await transaction.rollback()
