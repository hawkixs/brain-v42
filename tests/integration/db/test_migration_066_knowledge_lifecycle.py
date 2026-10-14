"""Lifecycle history keeps entry revisions and observations inseparable.

All writes are rolled back on the disposable migration database. Runtime ACLs
are exercised with SET ROLE, without altering cluster-wide login attributes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from brain_v42.db.tables import METADATA

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]
TABLES = (
    "knowledge_lifecycle_runs",
    "knowledge_lifecycle_requests",
    "knowledge_extractions",
    "knowledge_evaluations",
    "knowledge_justifications",
    "knowledge_lifecycle_events",
    "knowledge_lifecycle_state",
)
NOW = datetime(2026, 10, 14, tzinfo=UTC)
DIGEST = "a" * 64


@pytest_asyncio.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


async def _insert(conn: AsyncConnection, name: str, **values: Any) -> Any:
    table = METADATA.tables[name]
    if "id" in table.c:
        key = table.c.id
    elif "entity_id" in table.c:
        key = table.c.entity_id
    else:
        key = next(iter(table.primary_key.columns))
    return await conn.scalar(table.insert().values(**values).returning(key))


async def _run(conn: AsyncConnection) -> str:
    run_id = f"integ-066-{uuid4().hex}"
    await _insert(
        conn,
        TABLES[0],
        run_id=run_id,
        mode="apply",
        scope={},
        status="pending",
        manifest_digest=DIGEST,
    )
    return run_id


async def _extraction(conn: AsyncConnection, run_id: str, **overrides: Any) -> int:
    values = {
        "run_id": run_id,
        "entity_type": "decisions",
        "entity_id": uuid4(),
        "entry_digest": DIGEST,
        "model": "test-extractor",
        "prompt_version": "v1",
        "assertions": [],
    }
    values.update(overrides)
    return await _insert(conn, TABLES[2], **values)


async def _evaluation(conn: AsyncConnection, run_id: str, **overrides: Any) -> int:
    values = {
        "run_id": run_id,
        "entity_type": "decisions",
        "entity_id": uuid4(),
        "entry_digest": DIGEST,
        "expected_cache_revision": 0,
        "rules_version": "v1",
        "ranks_version": "v1",
        "registry_version": "v1",
        "origin_main_sha": "b" * 40,
        "observed_day": date(2026, 10, 14),
        "complete": True,
        "state": "valid",
        "reason": "declaration_only",
        "fingerprint": "c" * 64,
    }
    values.update(overrides)
    return await _insert(conn, TABLES[3], **values)


async def _observation(conn: AsyncConnection, evaluation: int, **overrides: Any) -> int:
    entry = (
        await conn.execute(
            sa.text("SELECT entity_type, entity_id FROM knowledge_evaluations WHERE id = :id"),
            {"id": evaluation},
        )
    ).one()
    values = {
        "evaluation_id": evaluation,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "assertion_key": "core",
        "polarity": "supports",
        "source_kind": "code",
        "source_rank": 4,
        "source_ref": "origin/main:src/example.py",
        "observed_value": {},
        "valid_from_precision": "unknown",
    }
    values.update(overrides)
    return await _insert(conn, TABLES[4], **values)


async def _event(conn: AsyncConnection, evaluation: int, **overrides: Any) -> int:
    entry = (
        await conn.execute(
            sa.text("SELECT entity_type, entity_id FROM knowledge_evaluations WHERE id = :id"),
            {"id": evaluation},
        )
    ).one()
    values = {
        "evaluation_id": evaluation,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "kind": "transition",
        "from_state": "valid",
        "to_state": "contested",
        "reason": "contradiction",
        "valid_from_precision": "unknown",
    }
    values.update(overrides)
    return await _insert(conn, TABLES[5], **values)


async def _refused(conn: AsyncConnection, action: Callable[[], Any], constraint: str) -> None:
    savepoint = await conn.begin_nested()
    try:
        with pytest.raises(IntegrityError, match=constraint):
            await action()
    finally:
        await savepoint.rollback()


@pytest.mark.parametrize("table", TABLES)
async def test_upgrade_creates_table(engine: AsyncEngine, table: str) -> None:
    async with engine.connect() as conn:
        assert await conn.scalar(sa.text(f"SELECT to_regclass('public.{table}')")) == table


async def test_wrong_entity_type_is_rejected(connection: AsyncConnection) -> None:
    run_id = await _run(connection)
    await _refused(
        connection,
        lambda: _evaluation(connection, run_id, entity_type="tickets"),
        "knowledge_evaluations_entity_type_valid",
    )


@pytest.mark.parametrize(
    "kind, rank",
    [
        ("structure", 7),
        ("measured", 6),
        ("config", 5),
        ("code", 4),
        ("pr", 3),
        ("ticket", 3),
        ("decision", 3),
        ("propagation", 2),
        ("decay", 1),
    ],
)
async def test_rank_must_match_source_kind(
    connection: AsyncConnection, kind: str, rank: int
) -> None:
    evaluation = await _evaluation(connection, await _run(connection))
    await _refused(
        connection,
        lambda: _observation(connection, evaluation, source_kind=kind, source_rank=rank % 7 + 1),
        "knowledge_justifications_rank_valid",
    )
    await _observation(connection, evaluation, source_kind=kind, source_rank=rank)


async def test_observation_cannot_belong_to_another_entry(connection: AsyncConnection) -> None:
    evaluation = await _evaluation(connection, await _run(connection))
    await _refused(
        connection,
        lambda: _observation(connection, evaluation, entity_id=uuid4()),
        "knowledge_justifications_evaluation_entry_fkey",
    )


@pytest.mark.parametrize("mismatch", ["entity_id", "entity_type", "entry_digest"])
async def test_extraction_must_match_entry_revision(
    connection: AsyncConnection, mismatch: str
) -> None:
    run_id = await _run(connection)
    entry_id = uuid4()
    extraction = await _extraction(connection, run_id, entity_id=entry_id)
    values = {"extraction_id": extraction, "entity_id": entry_id}
    values[mismatch] = {"entity_id": uuid4(), "entity_type": "learnings", "entry_digest": "d" * 64}[
        mismatch
    ]
    await _refused(
        connection,
        lambda: _evaluation(connection, run_id, **values),
        "knowledge_evaluations_extraction_entry_fkey",
    )


async def test_obsolete_event_requires_end_date(connection: AsyncConnection) -> None:
    evaluation = await _evaluation(connection, await _run(connection))
    await _refused(
        connection,
        lambda: _event(connection, evaluation, to_state="obsolete"),
        "knowledge_lifecycle_events_obsolete_date_valid",
    )


async def test_run_evaluates_each_entry_once(connection: AsyncConnection) -> None:
    run_id = await _run(connection)
    entry_id = uuid4()
    await _evaluation(connection, run_id, entity_id=entry_id)
    await _refused(
        connection,
        lambda: _evaluation(connection, run_id, entity_id=entry_id),
        "knowledge_evaluations_run_entry_key",
    )


async def test_event_must_match_evaluation_entry(connection: AsyncConnection) -> None:
    evaluation = await _evaluation(connection, await _run(connection))
    await _refused(
        connection,
        lambda: _event(connection, evaluation, entity_id=uuid4()),
        "knowledge_lifecycle_events_evaluation_entry_fkey",
    )


async def _cache(conn: AsyncConnection, event: int, **overrides: Any) -> Any:
    row = (
        await conn.execute(
            sa.text("SELECT entity_type, entity_id FROM knowledge_lifecycle_events WHERE id = :id"),
            {"id": event},
        )
    ).one()
    values = {
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "state": "contested",
        "reason": "contradiction",
        "since": NOW,
        "entry_content_updated_at": NOW,
        "event_id": event,
        "revision": 1,
    }
    values.update(overrides)
    return await _insert(conn, TABLES[6], **values)


async def test_cache_must_match_event_entry(connection: AsyncConnection) -> None:
    event = await _event(connection, await _evaluation(connection, await _run(connection)))
    await _refused(
        connection,
        lambda: _cache(connection, event, entity_id=uuid4()),
        "knowledge_lifecycle_state_event_entry_fkey",
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("polarity", "agrees"),
        ("source_kind", "rumour"),
        ("valid_from_precision", "approximate"),
    ],
)
async def test_observation_vocabulary_is_closed(
    connection: AsyncConnection, field: str, value: str
) -> None:
    evaluation = await _evaluation(connection, await _run(connection))
    await _refused(
        connection,
        lambda: _observation(connection, evaluation, **{field: value}),
        "knowledge_justifications_",
    )


@pytest_asyncio.fixture
async def runtime_connection(connection: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    superuser = await connection.scalar(
        sa.text("SELECT rolsuper FROM pg_roles WHERE rolname = 'brain_app'")
    )
    if superuser:
        pytest.skip("brain_app must be non-superuser for permission checks")
    can_assume_role = await connection.scalar(
        sa.text(
            "SELECT rolsuper OR pg_has_role(current_user, 'brain_app', 'USAGE') "
            "FROM pg_roles WHERE rolname = current_user"
        )
    )
    if not can_assume_role:
        pytest.skip("current user must be superuser or have USAGE of brain_app")
    await connection.execute(sa.text("SET LOCAL ROLE brain_app"))
    assert await connection.scalar(sa.text("SELECT current_user")) == "brain_app"
    yield connection


async def test_runtime_inserts_and_reads_history(runtime_connection: AsyncConnection) -> None:
    conn = runtime_connection
    run_id = await _run(conn)
    entry_id = uuid4()
    extraction = await _extraction(conn, run_id, entity_id=entry_id)
    evaluation = await _evaluation(conn, run_id, entity_id=entry_id, extraction_id=extraction)
    await _observation(conn, evaluation)
    await _event(conn, evaluation)
    for name in TABLES[2:6]:
        assert await conn.scalar(sa.select(sa.func.count()).select_from(METADATA.tables[name]))


@pytest.mark.parametrize("table", TABLES[4:6])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
async def test_runtime_cannot_rewrite_history(
    runtime_connection: AsyncConnection,
    table: str,
    operation: str,
) -> None:
    conn = runtime_connection
    evaluation = await _evaluation(conn, await _run(conn))
    if table == TABLES[4]:
        await _observation(conn, evaluation)
    else:
        await _event(conn, evaluation)
    sql = {
        "UPDATE": f"UPDATE {table} SET entity_type = entity_type",
        "DELETE": f"DELETE FROM {table}",
        "TRUNCATE": f"TRUNCATE {table}",
    }[operation]
    savepoint = await conn.begin_nested()
    try:
        with pytest.raises(DBAPIError, match="permission denied") as error:
            await conn.execute(sa.text(sql))
        assert getattr(error.value.orig, "sqlstate", None) == "42501"
    finally:
        await savepoint.rollback()


async def test_runtime_can_update_cache(runtime_connection: AsyncConnection) -> None:
    conn = runtime_connection
    event = await _event(conn, await _evaluation(conn, await _run(conn)))
    entry_id = await _cache(conn, event)
    revision = await conn.scalar(
        sa.text(
            "UPDATE knowledge_lifecycle_state SET revision = revision + 1 "
            "WHERE entity_type = 'decisions' AND entity_id = :id RETURNING revision"
        ),
        {"id": entry_id},
    )
    assert revision == 2


async def test_runtime_can_update_operational_records(runtime_connection: AsyncConnection) -> None:
    conn = runtime_connection
    run_id = await _run(conn)
    await _insert(
        conn, TABLES[1], run_id=run_id, custom_id="request-1", manifest_entry={}, status="pending"
    )
    assert (
        await conn.scalar(
            sa.text(
                "UPDATE knowledge_lifecycle_runs SET counts = '{}' WHERE run_id = :id RETURNING run_id"
            ),
            {"id": run_id},
        )
        == run_id
    )
    assert (
        await conn.scalar(
            sa.text(
                "UPDATE knowledge_lifecycle_requests SET status = 'missing' "
                "WHERE run_id = :id RETURNING status"
            ),
            {"id": run_id},
        )
        == "missing"
    )


async def test_downgrade_removes_all_tables(
    engine: AsyncEngine,
    migration_downgrade_fence: Callable[..., None],
) -> None:
    migration_downgrade_fence(downgraded_to="065")

    def migrate(action: str, target: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", action, target],
            cwd=ROOT,
            env={**os.environ, "POSTGRES_URL": os.environ["BRAIN_V42_TEST_DB_URL"]},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

    try:
        migrate("downgrade", "065")
        async with engine.connect() as conn:
            for name in TABLES:
                assert await conn.scalar(sa.text(f"SELECT to_regclass('public.{name}')")) is None
    finally:
        migrate("upgrade", "head")


async def _stored_graph(conn: AsyncConnection) -> None:
    run_id = await _run(conn)
    entry_id = uuid4()
    extraction = await _extraction(conn, run_id, entity_id=entry_id)
    evaluation = await _evaluation(conn, run_id, entity_id=entry_id, extraction_id=extraction)
    await _observation(conn, evaluation)
    event = await _event(conn, evaluation)
    await _cache(conn, event)
    await _insert(conn, TABLES[1], run_id=run_id, custom_id="request-1", manifest_entry={})


@pytest.mark.parametrize("table", TABLES[2:])
async def test_each_entry_identity_rejects_unknown_types(
    connection: AsyncConnection,
    table: str,
) -> None:
    await _stored_graph(connection)
    await _refused(
        connection,
        lambda: connection.execute(METADATA.tables[table].update().values(entity_type="tickets")),
        f"{table}_entity_type_valid",
    )


@pytest.mark.parametrize(
    "table,field,value,constraint",
    [
        (TABLES[0], "mode", "write", "mode_valid"),
        (TABLES[0], "batch_ids", {}, "batch_ids_valid"),
        (TABLES[0], "invalidated_at", NOW, "invalidation_valid"),
        (TABLES[0], "invalidation_reason", "incorrect run", "invalidation_valid"),
        (TABLES[1], "manifest_entry", [], "manifest_valid"),
        (TABLES[1], "status", "lost", "status_valid"),
        (TABLES[2], "assertions", {}, "assertions_valid"),
        (TABLES[3], "state", "pending", "state_valid"),
        (TABLES[3], "expected_cache_revision", -1, "revision_valid"),
        (TABLES[4], "valid_from_precision", "estimated", "precision_valid"),
        (TABLES[5], "kind", "rewrite", "(kind_valid|compensation_valid)"),
        (TABLES[5], "from_state", "pending", "from_state_valid"),
        (TABLES[5], "to_state", "pending", "to_state_valid"),
        (TABLES[5], "valid_from_precision", "estimated", "precision_valid"),
        (TABLES[5], "invalidated_run_id", "orphan", "compensation_valid"),
        (TABLES[6], "state", "pending", "state_valid"),
        (TABLES[6], "revision", -1, "revision_valid"),
    ],
)
async def test_invalid_shapes_are_refused(
    connection: AsyncConnection,
    table: str,
    field: str,
    value: Any,
    constraint: str,
) -> None:
    await _stored_graph(connection)
    await _refused(
        connection,
        lambda: connection.execute(METADATA.tables[table].update().values(**{field: value})),
        f"{table}_{constraint}",
    )


async def test_structural_observations_are_unique_with_null_assertion_keys(
    connection: AsyncConnection,
) -> None:
    evaluation = await _evaluation(connection, await _run(connection))
    await _observation(
        connection,
        evaluation,
        assertion_key=None,
        source_kind="structure",
        source_rank=7,
        source_ref="merged_into:entry-1",
    )
    await _observation(
        connection,
        evaluation,
        assertion_key=None,
        source_kind="structure",
        source_rank=7,
        source_ref="superseded_by:entry-2",
    )
    await _refused(
        connection,
        lambda: _observation(
            connection,
            evaluation,
            assertion_key=None,
            source_kind="structure",
            source_rank=7,
            source_ref="merged_into:entry-1",
        ),
        "knowledge_justifications_observation_key",
    )


async def test_only_one_transition_is_recorded_per_evaluation(connection: AsyncConnection) -> None:
    evaluation = await _evaluation(connection, await _run(connection))
    await _event(connection, evaluation)
    await _refused(
        connection,
        lambda: _event(connection, evaluation),
        "knowledge_lifecycle_events_transition_key",
    )


async def test_compensation_is_idempotent_per_invalidated_run_and_entry(
    connection: AsyncConnection,
) -> None:
    invalidated_run = await _run(connection)
    entry_id = uuid4()
    evaluation = await _evaluation(connection, invalidated_run, entity_id=entry_id)
    original = await _event(connection, evaluation)
    compensation = {
        "kind": "compensation",
        "compensates_event_id": original,
        "invalidated_run_id": invalidated_run,
        "from_state": "contested",
        "to_state": "valid",
    }
    await _event(connection, evaluation, **compensation)
    later = await _evaluation(connection, await _run(connection), entity_id=entry_id)
    await _refused(
        connection,
        lambda: _event(connection, later, **compensation),
        "knowledge_lifecycle_events_invalidation_entry_key",
    )
    # A later invalidation is a separate compensation, even for this evaluation.
    compensation["invalidated_run_id"] = await _run(connection)
    await _event(connection, evaluation, **compensation)


async def test_compensation_cannot_point_to_another_entry(connection: AsyncConnection) -> None:
    run_id = await _run(connection)
    first = await _event(connection, await _evaluation(connection, run_id))
    second = await _evaluation(connection, run_id)
    await _refused(
        connection,
        lambda: _event(
            connection,
            second,
            kind="compensation",
            compensates_event_id=first,
            invalidated_run_id=run_id,
        ),
        "knowledge_lifecycle_events_compensated_entry_fkey",
    )


async def test_extraction_revision_is_reused_across_runs(connection: AsyncConnection) -> None:
    entry_id = uuid4()
    first_run = await _run(connection)
    extraction = await _extraction(connection, first_run, entity_id=entry_id)
    later_run = await _run(connection)
    await _refused(
        connection,
        lambda: _extraction(connection, later_run, entity_id=entry_id),
        "knowledge_extractions_revision_key",
    )
    await _evaluation(connection, later_run, entity_id=entry_id, extraction_id=extraction)


@pytest.mark.parametrize("table", TABLES)
async def test_runtime_acl_is_exact(connection: AsyncConnection, table: str) -> None:
    mutable = table in (TABLES[0], TABLES[1], TABLES[6])
    for privilege in ("INSERT", "SELECT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        allowed = privilege in ("INSERT", "SELECT") or (mutable and privilege == "UPDATE")
        assert (
            await connection.scalar(
                sa.text("SELECT has_table_privilege('brain_app', :table, :privilege)"),
                {"table": f"public.{table}", "privilege": privilege},
            )
            is allowed
        )
