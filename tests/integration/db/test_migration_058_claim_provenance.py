"""Exercise revision 058 against disposable PostgreSQL databases."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.disposable_db import fresh_head_database

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]


def _alembic(db_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "POSTGRES_URL": db_url},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.fixture
def migration_058_databases(migration_database_url: str) -> Iterator[tuple[str, str]]:
    """Build both downgrade targets before entering the async test loop."""
    with ExitStack() as stack:
        populated = stack.enter_context(
            fresh_head_database(migration_database_url, prefix="brain_migration_058")
        )
        clean = stack.enter_context(
            fresh_head_database(migration_database_url, prefix="brain_migration_058")
        )
        yield populated, clean


async def _insert_claim(conn: AsyncConnection, provenance: str) -> UUID:
    """Give the CHECK a valid claim so only provenance decides acceptance."""
    project_key = f"migration-058-{uuid4().hex[:12]}"
    fact_name = f"migration_058_fact_{uuid4().hex}"
    await conn.execute(
        sa.text(
            "INSERT INTO project_contexts (project_key, name, description) "
            "VALUES (:key, :key, 'migration 058 scene')"
        ),
        {"key": project_key},
    )
    entity_id = await conn.scalar(
        sa.text(
            "INSERT INTO brain_entities "
            "(entity_type, entity_key, project_key, scope_kind, lifecycle) "
            "VALUES ('learning', :entity_key, :project_key, 'project', 'active') RETURNING id"
        ),
        {"entity_key": f"migration-058-entity-{uuid4()}", "project_key": project_key},
    )
    await conn.execute(
        sa.text(
            "INSERT INTO knowledge_fact_definitions "
            "(fact_name, definition_version, target, ttl_seconds, timeout_seconds, "
            "policies, value_schema, digest) "
            "VALUES (:fact_name, 1, 'production', 60, 10, '{}'::jsonb, '{}'::jsonb, :digest)"
        ),
        {"fact_name": fact_name, "digest": "a" * 64},
    )
    claim_id = await conn.scalar(
        sa.text(
            "INSERT INTO knowledge_claims "
            "(entity_ref_id, entity_type, project_key, claim_key, statement, fact_name, "
            "definition_version, target, expected, expected_resolved, validity_seconds, "
            "provenance, declared_by, declared_at) "
            "VALUES (:entity_id, 'learning', :project_key, :claim_key, 'head is current', "
            ":fact_name, 1, 'production', '{}'::jsonb, '{}'::jsonb, 60, "
            ":provenance, 'migration-058', now()) RETURNING id"
        ),
        {
            "entity_id": entity_id,
            "project_key": project_key,
            "claim_key": uuid4().hex * 2,
            "fact_name": fact_name,
            "provenance": provenance,
        },
    )
    assert isinstance(claim_id, UUID)
    return claim_id


@pytest.mark.asyncio
async def test_migration_058_accepts_extracted_and_rejects_unknown_provenance(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            claim_id = await _insert_claim(conn, "extracted")
            assert (
                await conn.scalar(
                    sa.text("SELECT provenance FROM knowledge_claims WHERE id = :id"),
                    {"id": claim_id},
                )
                == "extracted"
            )
            with pytest.raises(IntegrityError, match="knowledge_claims_provenance_valid"):
                async with conn.begin_nested():
                    await _insert_claim(conn, "unknown")
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_migration_058_downgrade_refuses_extracted_rows(
    migration_058_databases: tuple[str, str],
    migration_downgrade_fence: Callable[..., None],
) -> None:
    """A refused downgrade preserves head; a separate clean database can downgrade.

    Both downgrades target disposable databases, never the shared one; the fence
    is requested anyway, as in test_delivery_contracts, so the residue guard's
    declared list stays exact and an interruption stays attributable.
    """
    migration_downgrade_fence(downgraded_to="057")
    populated_url, clean_url = migration_058_databases
    probe = create_async_engine(populated_url, poolclass=NullPool)
    try:
        async with probe.connect() as conn:
            original_head = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
    finally:
        await probe.dispose()

    engine = create_async_engine(populated_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await _insert_claim(conn, "extracted")
    finally:
        await engine.dispose()

    refused = _alembic(populated_url, "downgrade", "057")
    assert refused.returncode != 0
    assert "knowledge_claims_provenance_downgrade_refused" in refused.stderr
    probe = create_async_engine(populated_url, poolclass=NullPool)
    try:
        async with probe.connect() as conn:
            assert (
                await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == original_head
            )
    finally:
        await probe.dispose()

    accepted = _alembic(clean_url, "downgrade", "057")
    assert accepted.returncode == 0, accepted.stderr
    probe = create_async_engine(clean_url, poolclass=NullPool)
    try:
        async with probe.connect() as conn:
            assert await conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "057"
    finally:
        await probe.dispose()
