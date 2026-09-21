"""Migration 055 — immutable claim definitions, occurrences, and verdicts.

These assertions exercise PostgreSQL itself.  In particular, ``brain`` is a
superuser in the measured local fixture, so an ACL-only test would prove no
immutability at all; the asserted messages originate in the trigger guards.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DIGEST = "a" * 64


async def _scene(conn: AsyncConnection) -> tuple[str, UUID]:
    """Create a valid project anchor and one definition inside the caller's transaction."""
    project_key = f"migration-055-{uuid4().hex[:12]}"
    await conn.execute(
        sa.text(
            "INSERT INTO project_contexts (project_key, name, description) "
            "VALUES (:project_key, :project_key, 'migration 055 scene')"
        ),
        {"project_key": project_key},
    )
    entity_id = await conn.scalar(
        sa.text(
            "INSERT INTO brain_entities "
            "(entity_type, entity_key, project_key, scope_kind, lifecycle) "
            "VALUES ('learning', :entity_key, :project_key, 'project', 'active') RETURNING id"
        ),
        {"entity_key": f"migration-055-entity-{uuid4()}", "project_key": project_key},
    )
    await conn.execute(
        sa.text(
            "INSERT INTO knowledge_fact_definitions "
            "(fact_name, definition_version, target, ttl_seconds, timeout_seconds, policies, "
            "value_schema, digest) VALUES (:fact_name, 1, 'production', 60, 10, "
            "'{}'::jsonb, '{}'::jsonb, :digest)"
        ),
        {"fact_name": f"migration_055_fact_{uuid4().hex}", "digest": _DIGEST},
    )
    fact_name = await conn.scalar(
        sa.text(
            "SELECT fact_name FROM knowledge_fact_definitions "
            "ORDER BY registered_at DESC, fact_name DESC LIMIT 1"
        )
    )
    assert isinstance(entity_id, UUID)
    assert isinstance(fact_name, str)
    return fact_name, entity_id


async def _claim(
    conn: AsyncConnection,
    *,
    fact_name: str,
    entity_id: UUID,
    project_key: str,
    claim_key: str | None = None,
    replaces_id: UUID | None = None,
) -> UUID:
    """Insert one valid occurrence; callers vary only the guard under test."""
    claim_id = await conn.scalar(
        sa.text(
            "INSERT INTO knowledge_claims "
            "(entity_ref_id, entity_type, project_key, claim_key, statement, fact_name, "
            "definition_version, target, expected, expected_resolved, validity_seconds, "
            "provenance, declared_by, declared_at, replaces_id) "
            "VALUES (:entity_id, 'learning', :project_key, :claim_key, 'the claim holds', "
            ":fact_name, 1, 'production', '{}'::jsonb, '{}'::jsonb, 60, 'declared', "
            "'migration-055', now(), :replaces_id) RETURNING id"
        ),
        {
            "entity_id": entity_id,
            "project_key": project_key,
            "claim_key": claim_key or uuid4().hex * 2,
            "fact_name": fact_name,
            "replaces_id": replaces_id,
        },
    )
    assert isinstance(claim_id, UUID)
    return claim_id


async def _refused(
    conn: AsyncConnection, statement: str, params: dict[str, object], message: str
) -> None:
    """Assert a database guard's message and keep the surrounding scene usable."""
    with pytest.raises(DBAPIError) as excinfo:
        async with conn.begin_nested():
            await conn.execute(sa.text(statement), params)
    assert message in str(excinfo.value)


async def test_upgrade_creates_claim_objects_and_downgrade_counts_each_table(
    engine: AsyncEngine, migration_downgrade_fence
) -> None:
    """A populated ledger cannot disappear silently, but its named opt-in works."""
    async with engine.begin() as conn:
        for name in (
            "knowledge_fact_definitions",
            "knowledge_claims",
            "knowledge_claim_verdicts",
            "knowledge_claim_current",
        ):
            assert await conn.scalar(sa.text("SELECT to_regclass(:name)"), {"name": name}) == name
        fact_name, entity_id = await _scene(conn)
        project_key = await conn.scalar(
            sa.text("SELECT project_key FROM brain_entities WHERE id = :id"), {"id": entity_id}
        )
        assert isinstance(project_key, str)
        claim_id = await _claim(
            conn, fact_name=fact_name, entity_id=entity_id, project_key=project_key
        )
        await conn.execute(
            sa.text(
                "INSERT INTO knowledge_claim_verdicts "
                "(claim_id, verdict, measurement, observation_id, issuer_identity, issuer_kind, "
                "request_fingerprint, outcome_fingerprint, idempotency_key, emitted_at) "
                "VALUES (:claim_id, 'holds', '{}'::jsonb, gen_random_uuid(), 'migration-055', "
                "'robot', :digest, :digest, 'downgrade', now())"
            ),
            {"claim_id": claim_id, "digest": _DIGEST},
        )

    migration_downgrade_fence("054")
    database_url = os.environ["BRAIN_V42_TEST_DB_URL"]
    refused = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "054"],
        env={**os.environ, "POSTGRES_URL": database_url},
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert refused.returncode != 0
    for name in ("knowledge_claims", "knowledge_claim_verdicts", "knowledge_fact_definitions"):
        assert name in refused.stderr
    assert "allow_claims_downgrade=yes" in refused.stderr

    accepted = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            "allow_claims_downgrade=yes",
            "downgrade",
            "054",
        ],
        env={**os.environ, "POSTGRES_URL": database_url},
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert accepted.returncode == 0, accepted.stderr
    restored = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "POSTGRES_URL": database_url},
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert restored.returncode == 0, restored.stderr


@pytest.mark.parametrize(
    "table, key, update",
    [
        (
            "knowledge_fact_definitions",
            "fact_name = :fact_name AND definition_version = 1",
            "ttl_seconds = 61",
        ),
        ("knowledge_claim_verdicts", "id = :id", "reason = 'rewritten'"),
    ],
)
async def test_definition_and_verdict_update_guards_name_their_table(
    engine: AsyncEngine, table: str, key: str, update: str
) -> None:
    """Removing either append-only trigger would make this mutation succeed."""
    async with engine.connect() as conn:
        async with conn.begin():
            fact_name, entity_id = await _scene(conn)
            project_key = await conn.scalar(
                sa.text("SELECT project_key FROM brain_entities WHERE id = :id"), {"id": entity_id}
            )
            assert isinstance(project_key, str)
            params: dict[str, object] = {"fact_name": fact_name}
            if table == "knowledge_claim_verdicts":
                claim_id = await _claim(
                    conn, fact_name=fact_name, entity_id=entity_id, project_key=project_key
                )
                verdict_id = await conn.scalar(
                    sa.text(
                        "INSERT INTO knowledge_claim_verdicts (claim_id, verdict, measurement, observation_id, issuer_identity, issuer_kind, request_fingerprint, outcome_fingerprint, idempotency_key, emitted_at) VALUES (:claim_id, 'holds', '{}'::jsonb, gen_random_uuid(), 'migration-055', 'robot', :digest, :digest, 'append-only', now()) RETURNING id"
                    ),
                    {"claim_id": claim_id, "digest": _DIGEST},
                )
                params = {"id": verdict_id}
            await _refused(conn, f"UPDATE {table} SET {update} WHERE {key}", params, table)


@pytest.mark.parametrize(
    "table, key",
    [
        ("knowledge_fact_definitions", "fact_name = :fact_name AND definition_version = 1"),
        ("knowledge_claim_verdicts", "id = :id"),
    ],
)
async def test_definition_and_verdict_delete_guards_name_their_table(
    engine: AsyncEngine, table: str, key: str
) -> None:
    """The guard, rather than the superuser-bypassed ACL, refuses deletion."""
    async with engine.connect() as conn:
        async with conn.begin():
            fact_name, entity_id = await _scene(conn)
            project_key = await conn.scalar(
                sa.text("SELECT project_key FROM brain_entities WHERE id = :id"), {"id": entity_id}
            )
            assert isinstance(project_key, str)
            params: dict[str, object] = {"fact_name": fact_name}
            if table == "knowledge_claim_verdicts":
                claim_id = await _claim(
                    conn, fact_name=fact_name, entity_id=entity_id, project_key=project_key
                )
                verdict_id = await conn.scalar(
                    sa.text(
                        "INSERT INTO knowledge_claim_verdicts (claim_id, verdict, measurement, observation_id, issuer_identity, issuer_kind, request_fingerprint, outcome_fingerprint, idempotency_key, emitted_at) VALUES (:claim_id, 'holds', '{}'::jsonb, gen_random_uuid(), 'migration-055', 'robot', :digest, :digest, 'delete-guard', now()) RETURNING id"
                    ),
                    {"claim_id": claim_id, "digest": _DIGEST},
                )
                params = {"id": verdict_id}
            await _refused(conn, f"DELETE FROM {table} WHERE {key}", params, table)


async def test_claim_update_gate_allows_one_retirement_and_refuses_other_changes(
    engine: AsyncEngine,
) -> None:
    """The only legal occurrence update is the one-way retirement transition."""
    async with engine.connect() as conn:
        async with conn.begin():
            fact_name, entity_id = await _scene(conn)
            project_key = await conn.scalar(
                sa.text("SELECT project_key FROM brain_entities WHERE id = :id"), {"id": entity_id}
            )
            assert isinstance(project_key, str)
            claim_id = await _claim(
                conn, fact_name=fact_name, entity_id=entity_id, project_key=project_key
            )
            await _refused(
                conn,
                "UPDATE knowledge_claims SET statement = 'rewritten' WHERE id = :id",
                {"id": claim_id},
                "knowledge_claims",
            )
            await conn.execute(
                sa.text("UPDATE knowledge_claims SET retired_at = now() WHERE id = :id"),
                {"id": claim_id},
            )
            await _refused(
                conn,
                "UPDATE knowledge_claims SET retired_at = now() + interval '1 minute' WHERE id = :id",
                {"id": claim_id},
                "knowledge_claims",
            )
            await _refused(
                conn,
                "UPDATE knowledge_claims SET retired_at = NULL WHERE id = :id",
                {"id": claim_id},
                "knowledge_claims",
            )


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("entity_type", "decision", "entity_type"),
        ("project_key", "wrong-project", "project_key"),
        ("scope_kind", "global", "scope_kind"),
        ("lifecycle", "archived", "lifecycle"),
    ],
)
async def test_anchor_gate_names_each_invalid_anchor_property(
    engine: AsyncEngine, column: str, value: str, message: str
) -> None:
    """Each anchor failure has a distinct diagnosis for the writer's debugger."""
    async with engine.connect() as conn:
        async with conn.begin():
            fact_name, entity_id = await _scene(conn)
            project_key = await conn.scalar(
                sa.text("SELECT project_key FROM brain_entities WHERE id = :id"), {"id": entity_id}
            )
            assert isinstance(project_key, str)
            if column == "scope_kind":
                await conn.execute(
                    sa.text(
                        "UPDATE brain_entities SET scope_kind = 'global', project_key = NULL "
                        "WHERE id = :id"
                    ),
                    {"id": entity_id},
                )
            elif column == "lifecycle":
                await conn.execute(
                    sa.text("UPDATE brain_entities SET lifecycle = :value WHERE id = :id"),
                    {"value": value, "id": entity_id},
                )
            payload = {"entity_type": "learning", "project_key": project_key}
            if column in payload:
                payload[column] = value
            await _refused(
                conn,
                "INSERT INTO knowledge_claims (entity_ref_id, entity_type, project_key, claim_key, statement, fact_name, definition_version, target, expected, expected_resolved, validity_seconds, provenance, declared_by, declared_at) VALUES (:entity_id, :entity_type, :project_key, :claim_key, 'bad anchor', :fact_name, 1, 'production', '{}'::jsonb, '{}'::jsonb, 60, 'declared', 'migration-055', now())",
                {
                    "entity_id": entity_id,
                    "fact_name": fact_name,
                    "claim_key": uuid4().hex * 2,
                    **payload,
                },
                message,
            )


async def test_chain_gate_requires_the_latest_retired_predecessor(engine: AsyncEngine) -> None:
    """A chain cannot fork, skip a retired occurrence, or cross entity ownership."""
    async with engine.connect() as conn:
        async with conn.begin():
            fact_name, entity_id = await _scene(conn)
            project_key = await conn.scalar(
                sa.text("SELECT project_key FROM brain_entities WHERE id = :id"), {"id": entity_id}
            )
            assert isinstance(project_key, str)
            key = "b" * 64
            first = await _claim(
                conn,
                fact_name=fact_name,
                entity_id=entity_id,
                project_key=project_key,
                claim_key=key,
            )
            await conn.execute(
                sa.text("UPDATE knowledge_claims SET retired_at = now() WHERE id = :id"),
                {"id": first},
            )
            second = await _claim(
                conn,
                fact_name=fact_name,
                entity_id=entity_id,
                project_key=project_key,
                claim_key=key,
                replaces_id=first,
            )
            await conn.execute(
                sa.text("UPDATE knowledge_claims SET retired_at = now() WHERE id = :id"),
                {"id": second},
            )
            await _refused(
                conn,
                "INSERT INTO knowledge_claims (entity_ref_id, entity_type, project_key, claim_key, statement, fact_name, definition_version, target, expected, expected_resolved, validity_seconds, provenance, declared_by, declared_at, replaces_id) VALUES (:entity_id, 'learning', :project_key, :claim_key, 'skips', :fact_name, 1, 'production', '{}'::jsonb, '{}'::jsonb, 60, 'declared', 'migration-055', now(), :replaces_id)",
                {
                    "entity_id": entity_id,
                    "project_key": project_key,
                    "claim_key": key,
                    "fact_name": fact_name,
                    "replaces_id": first,
                },
                "latest retired",
            )
            active_key = "c" * 64
            active = await _claim(
                conn,
                fact_name=fact_name,
                entity_id=entity_id,
                project_key=project_key,
                claim_key=active_key,
            )
            await _refused(
                conn,
                "INSERT INTO knowledge_claims (entity_ref_id, entity_type, project_key, claim_key, statement, fact_name, definition_version, target, expected, expected_resolved, validity_seconds, provenance, declared_by, declared_at, replaces_id) VALUES (:entity_id, 'learning', :project_key, :claim_key, 'active predecessor', :fact_name, 1, 'production', '{}'::jsonb, '{}'::jsonb, 60, 'declared', 'migration-055', now(), :replaces_id)",
                {
                    "entity_id": entity_id,
                    "project_key": project_key,
                    "claim_key": active_key,
                    "fact_name": fact_name,
                    "replaces_id": active,
                },
                "retired",
            )
            other_entity = await conn.scalar(
                sa.text(
                    "INSERT INTO brain_entities (entity_type, entity_key, project_key, scope_kind, lifecycle) VALUES ('learning', :key, :project_key, 'project', 'active') RETURNING id"
                ),
                {"key": f"migration-055-other-{uuid4()}", "project_key": project_key},
            )
            assert isinstance(other_entity, UUID)
            await _refused(
                conn,
                "INSERT INTO knowledge_claims (entity_ref_id, entity_type, project_key, claim_key, statement, fact_name, definition_version, target, expected, expected_resolved, validity_seconds, provenance, declared_by, declared_at, replaces_id) VALUES (:entity_id, 'learning', :project_key, :claim_key, 'other entity', :fact_name, 1, 'production', '{}'::jsonb, '{}'::jsonb, 60, 'declared', 'migration-055', now(), :replaces_id)",
                {
                    "entity_id": other_entity,
                    "project_key": project_key,
                    "claim_key": key,
                    "fact_name": fact_name,
                    "replaces_id": second,
                },
                "entity_ref_id",
            )
            await _refused(
                conn,
                "INSERT INTO knowledge_claims (entity_ref_id, entity_type, project_key, claim_key, statement, fact_name, definition_version, target, expected, expected_resolved, validity_seconds, provenance, declared_by, declared_at) VALUES (:entity_id, 'learning', :project_key, :claim_key, 'unnamed predecessor', :fact_name, 1, 'production', '{}'::jsonb, '{}'::jsonb, 60, 'declared', 'migration-055', now())",
                {
                    "entity_id": entity_id,
                    "project_key": project_key,
                    "claim_key": key,
                    "fact_name": fact_name,
                },
                "replacement_required",
            )


async def test_active_partial_unique_index_allows_replacement_only_after_retirement(
    engine: AsyncEngine,
) -> None:
    """The partial key permits history but forbids two simultaneously active assertions."""
    async with engine.connect() as conn:
        async with conn.begin():
            fact_name, entity_id = await _scene(conn)
            project_key = await conn.scalar(
                sa.text("SELECT project_key FROM brain_entities WHERE id = :id"), {"id": entity_id}
            )
            assert isinstance(project_key, str)
            key = "d" * 64
            first = await _claim(
                conn,
                fact_name=fact_name,
                entity_id=entity_id,
                project_key=project_key,
                claim_key=key,
            )
            with pytest.raises(IntegrityError, match="uq_knowledge_claims_active_entity_key"):
                async with conn.begin_nested():
                    await _claim(
                        conn,
                        fact_name=fact_name,
                        entity_id=entity_id,
                        project_key=project_key,
                        claim_key=key,
                    )
            await conn.execute(
                sa.text("UPDATE knowledge_claims SET retired_at = now() WHERE id = :id"),
                {"id": first},
            )
            await _claim(
                conn,
                fact_name=fact_name,
                entity_id=entity_id,
                project_key=project_key,
                claim_key=key,
                replaces_id=first,
            )


async def test_view_separates_latest_attempt_from_latest_conclusive_verdict(
    engine: AsyncEngine,
) -> None:
    """An unreadable retry must not erase the preceding conclusive reading."""
    async with engine.connect() as conn:
        async with conn.begin():
            fact_name, entity_id = await _scene(conn)
            project_key = await conn.scalar(
                sa.text("SELECT project_key FROM brain_entities WHERE id = :id"), {"id": entity_id}
            )
            assert isinstance(project_key, str)
            claim_id = await _claim(
                conn, fact_name=fact_name, entity_id=entity_id, project_key=project_key
            )
            only_unreadable = await _claim(
                conn, fact_name=fact_name, entity_id=entity_id, project_key=project_key
            )
            for current_id, verdict, key in (
                (claim_id, "holds", "view-holds"),
                (claim_id, "unreadable", "view-unreadable"),
                (only_unreadable, "unreadable", "view-only-unreadable"),
            ):
                await conn.execute(
                    sa.text(
                        "INSERT INTO knowledge_claim_verdicts (claim_id, verdict, reason, measurement, observation_id, issuer_identity, issuer_kind, request_fingerprint, outcome_fingerprint, idempotency_key, emitted_at) VALUES (:claim_id, :verdict, CASE WHEN CAST(:verdict AS varchar(16)) = 'unreadable' THEN 'path_absent' ELSE NULL END, '{}'::jsonb, gen_random_uuid(), 'migration-055', 'robot', :digest, :digest, :key, now())"
                    ),
                    {"claim_id": current_id, "verdict": verdict, "key": key, "digest": _DIGEST},
                )
            row = (
                await conn.execute(
                    sa.text(
                        "SELECT latest_verdict, conclusive_verdict, conclusive_seq, conclusive_recorded_at FROM knowledge_claim_current WHERE claim_id = :id"
                    ),
                    {"id": claim_id},
                )
            ).one()
            assert row.latest_verdict == "unreadable"
            assert row.conclusive_verdict == "holds"
            assert row.conclusive_seq is not None
            assert row.conclusive_recorded_at is not None
            empty = (
                await conn.execute(
                    sa.text(
                        "SELECT conclusive_seq, conclusive_verdict, conclusive_recorded_at, conclusive_observation_id FROM knowledge_claim_current WHERE claim_id = :id"
                    ),
                    {"id": only_unreadable},
                )
            ).one()
            assert empty == (None, None, None, None)


async def test_ledger_seq_is_server_assigned_and_cannot_be_supplied(engine: AsyncEngine) -> None:
    """Changing either identity to a client-overridable serial would forge its order."""
    async with engine.connect() as conn:
        async with conn.begin():
            fact_name, entity_id = await _scene(conn)
            project_key = await conn.scalar(
                sa.text("SELECT project_key FROM brain_entities WHERE id = :id"), {"id": entity_id}
            )
            assert isinstance(project_key, str)
            claim_id = await _claim(
                conn, fact_name=fact_name, entity_id=entity_id, project_key=project_key
            )
            with pytest.raises(DBAPIError, match="GENERATED ALWAYS"):
                async with conn.begin_nested():
                    await conn.execute(
                        sa.text(
                            "INSERT INTO knowledge_claims (seq, entity_ref_id, entity_type, project_key, claim_key, statement, fact_name, definition_version, target, expected, expected_resolved, validity_seconds, provenance, declared_by, declared_at) VALUES (1, :entity_id, 'learning', :project_key, :claim_key, 'forged claim sequence', :fact_name, 1, 'production', '{}'::jsonb, '{}'::jsonb, 60, 'declared', 'migration-055', now())"
                        ),
                        {
                            "entity_id": entity_id,
                            "project_key": project_key,
                            "claim_key": uuid4().hex * 2,
                            "fact_name": fact_name,
                        },
                    )
            with pytest.raises(DBAPIError, match="GENERATED ALWAYS"):
                async with conn.begin_nested():
                    await conn.execute(
                        sa.text(
                            "INSERT INTO knowledge_claim_verdicts (seq, claim_id, verdict, measurement, observation_id, issuer_identity, issuer_kind, request_fingerprint, outcome_fingerprint, idempotency_key, emitted_at) VALUES (1, :claim_id, 'holds', '{}'::jsonb, gen_random_uuid(), 'migration-055', 'robot', :digest, :digest, 'forged-seq', now())"
                        ),
                        {"claim_id": claim_id, "digest": _DIGEST},
                    )
