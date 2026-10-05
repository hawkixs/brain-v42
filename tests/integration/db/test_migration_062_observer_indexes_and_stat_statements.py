"""Migration 062: observer error indexes and pg_stat_statements in schema `monitoring`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from asyncpg.exceptions import ObjectNotInPrerequisiteStateError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]

BINDING_INDEX = "idx_delivery_confirmations_binding_errors"
CONTEXT_INDEX = "idx_delivery_confirmations_context_errors"

# The literal predicate SQL the observer's failure count uses. Written out here, on
# purpose and independent of the repository code: a partial index is usable only when
# the query's own clauses imply its predicate, and CHECK constraints do not count.
BINDING_QUERY = """
    SELECT count(*) FROM (
      SELECT 1 FROM delivery_confirmations
      WHERE outcome = 'error' AND subject_kind = 'artifact_binding'
        AND binding_id = '{binding}' AND collection_finished_at > now()
      LIMIT 6) AS failures
"""
CONTEXT_QUERY = """
    SELECT count(*) FROM (
      SELECT 1 FROM delivery_confirmations
      WHERE outcome = 'error' AND subject_kind = 'repository_context'
        AND ticket_id = '{ticket}' AND contract_revision = 1 AND attempt = 1
        AND context_set_digest = '{digest}' AND collection_finished_at > now()
      LIMIT 6) AS failures
"""


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "POSTGRES_URL": os.environ["BRAIN_V42_TEST_DB_URL"]},
        timeout=120,
    )


async def _scalar(engine: AsyncEngine, sql: str) -> object:
    async with engine.connect() as connection:
        return await connection.scalar(sa.text(sql))


async def test_partial_error_indexes_exist_and_are_valid(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT c.relname, i.indisvalid, pg_get_indexdef(i.indexrelid) AS definition "
                    "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE i.indrelid = 'public.delivery_confirmations'::regclass "
                    "AND c.relname = ANY(:names)"
                ),
                {"names": [BINDING_INDEX, CONTEXT_INDEX]},
            )
        ).all()
    found = {row.relname: row for row in rows}
    assert set(found) == {BINDING_INDEX, CONTEXT_INDEX}
    assert all(row.indisvalid for row in rows)
    binding = found[BINDING_INDEX].definition
    assert "(binding_id, collection_finished_at)" in binding
    assert "'error'" in binding and "'artifact_binding'" in binding
    context = found[CONTEXT_INDEX].definition
    assert (
        "(ticket_id, contract_revision, attempt, context_set_digest, collection_finished_at)"
        in (context)
    )
    assert "'error'" in context and "'repository_context'" in context


async def _plan_index_names(engine: AsyncEngine, query: str) -> set[str]:
    async with engine.connect() as connection:
        async with connection.begin():
            await connection.execute(sa.text("SET LOCAL enable_seqscan = off"))
            raw = await connection.scalar(sa.text(f"EXPLAIN (FORMAT JSON) {query}"))
    plan = raw if isinstance(raw, list) else json.loads(raw)
    names: set[str] = set()

    def walk(node: dict[str, object]) -> None:
        if "Index Name" in node:
            names.add(str(node["Index Name"]))
        for child in node.get("Plans", []):  # type: ignore[attr-defined]
            walk(child)

    walk(plan[0]["Plan"])
    return names


async def test_binding_and_context_error_lookups_use_the_partial_indexes(
    engine: AsyncEngine,
) -> None:
    binding = await _plan_index_names(engine, BINDING_QUERY.format(binding=uuid4()))
    context = await _plan_index_names(engine, CONTEXT_QUERY.format(ticket=uuid4(), digest="d" * 64))
    assert BINDING_INDEX in binding
    assert CONTEXT_INDEX in context


async def test_pg_stat_statements_lives_in_monitoring_not_public(engine: AsyncEngine) -> None:
    """Nothing lands in `public`: the ACL contract and the schema fingerprint stay valid."""
    assert (
        await _scalar(
            engine,
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname = 'pg_stat_statements'",
        )
        == "monitoring"
    )
    assert await _scalar(engine, "SELECT to_regclass('public.pg_stat_statements')") is None
    assert await _scalar(engine, "SELECT to_regclass('monitoring.pg_stat_statements')") is not None
    assert (
        await _scalar(
            engine,
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname LIKE 'pg_stat_statements%'",
        )
        == 0
    )


async def test_unloaded_library_is_tolerated(engine: AsyncEngine) -> None:
    """Creating the extension never needed the preload; only reading the view does."""
    preload = await _scalar(engine, "SELECT current_setting('shared_preload_libraries')")
    if "pg_stat_statements" in str(preload):
        pytest.skip("pg_stat_statements is preloaded on this server")
    with pytest.raises(DBAPIError) as excinfo:
        await _scalar(engine, "SELECT 1 FROM monitoring.pg_stat_statements")
    assert isinstance(excinfo.value.orig.__cause__, ObjectNotInPrerequisiteStateError)  # type: ignore[union-attr]


async def test_downgrade_drops_indexes_extension_and_schema_then_reupgrade_restores(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None]
) -> None:
    migration_downgrade_fence(downgraded_to="061")
    down = _run_alembic("downgrade", "061")
    assert down.returncode == 0, down.stderr
    assert await _scalar(engine, f"SELECT to_regclass('public.{BINDING_INDEX}')") is None
    assert await _scalar(engine, f"SELECT to_regclass('public.{CONTEXT_INDEX}')") is None
    assert (
        await _scalar(
            engine, "SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'"
        )
        == 0
    )
    assert await _scalar(engine, "SELECT to_regnamespace('monitoring')") is None
    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert await _scalar(engine, f"SELECT to_regclass('public.{BINDING_INDEX}')") is not None
    assert await _scalar(engine, "SELECT to_regclass('monitoring.pg_stat_statements')") is not None


async def test_downgrade_refuses_foreign_objects_in_monitoring(
    engine: AsyncEngine, migration_downgrade_fence: Callable[..., None]
) -> None:
    """RESTRICT makes an operator's own object in `monitoring` stop the downgrade, loudly."""
    migration_downgrade_fence(downgraded_to="061")
    async with engine.begin() as connection:
        await connection.execute(sa.text("CREATE TABLE monitoring.operator_probe (x int)"))
    try:
        down = _run_alembic("downgrade", "061")
        assert down.returncode != 0
        assert "operator_probe" in down.stderr or "monitoring" in down.stderr
        revision = await _scalar(engine, "SELECT version_num FROM alembic_version")
        assert revision == "062"
        assert await _scalar(engine, f"SELECT to_regclass('public.{BINDING_INDEX}')") is not None
    finally:
        async with engine.begin() as connection:
            await connection.execute(sa.text("DROP TABLE IF EXISTS monitoring.operator_probe"))
