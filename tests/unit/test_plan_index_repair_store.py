"""Unit tests for read-only PostgreSQL plan-index repair inventory."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update

from brain_v42.db.tables import project_contexts
from brain_v42.maintenance import plan_index_repair, plan_index_repair_store
from brain_v42.maintenance.plan_index_repair import (
    ContextRecord,
    LocalPlanFile,
    ProjectTarget,
    RepairManifest,
    RepairSafetyError,
    RepairSnapshot,
    database_identity_fingerprint,
    sha256_json,
)
from brain_v42.maintenance.plan_index_repair_store import (
    _REQUIRED_ALEMBIC_HEAD,
    RepairStore,
)


def _mapping_result(rows: list[dict[str, object]]) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    return result


def _one_mapping_result(row: dict[str, object]) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.one.return_value = row
    return result


def _session_factory(execute_results: list[MagicMock]) -> tuple[MagicMock, MagicMock]:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute_results)
    session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


def _update_result(*returned_timestamps: datetime) -> MagicMock:
    result = MagicMock()
    # Keep the legacy rowcount contract until the following production commit
    # switches the Store to the richer RETURNING contract.
    result.rowcount = len(returned_timestamps)
    result.scalars.return_value.all.return_value = list(returned_timestamps)
    return result


@pytest.mark.parametrize(
    "returned_timestamps",
    [
        (),
        (datetime(2026, 7, 1, tzinfo=UTC),),
        (
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
        ),
        (datetime(2025, 12, 31, tzinfo=UTC),),
    ],
)
def test_update_result_double_exposes_exact_returned_timestamps(
    returned_timestamps: tuple[datetime, ...],
) -> None:
    """Dropping, duplicating, or replacing a RETURNING row must remain observable."""
    result = _update_result(*returned_timestamps)

    assert result.scalars().all() == list(returned_timestamps)


def _context_row() -> dict[str, object]:
    return {
        column.name: (
            "red-phone"
            if column.name == "project_key"
            else UUID("11111111-1111-1111-1111-111111111111")
            if column.name == "id"
            else ["docs/plans"]
            if column.name == "plan_scan_paths"
            else []
            if column.name
            in {"languages", "frameworks", "databases", "blockers", "related_projects"}
            else {}
            if column.name == "metadata"
            else 0
            if column.name.endswith("_count") or column.name == "focus_revision"
            else "value"
        )
        for column in project_contexts.c
    }


def _manifest(tmp_path: Path) -> tuple[RepairManifest, tuple[LocalPlanFile, ...]]:
    root = tmp_path / "red-phone"
    scan = root / "docs" / "plans"
    scan.mkdir(parents=True)
    plan_path = scan / "one-plan.md"
    plan_path.write_text("# One", encoding="utf-8")
    return (
        RepairManifest(
            version=1,
            projects=(ProjectTarget("red-phone", root.resolve(), (scan.resolve(),)),),
        ),
        (
            LocalPlanFile(
                project_key="red-phone",
                file_path=str(plan_path.resolve()),
                content_hash="a" * 64,
                size_bytes=5,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_inventory_is_read_only_complete_and_content_safe(tmp_path: Path) -> None:
    """Dropping a control query or selecting secret-bearing columns must make this fail."""
    manifest, local_files = _manifest(tmp_path)
    plan_id = UUID("22222222-2222-2222-2222-222222222222")
    context_result = _mapping_result([_context_row()])
    plan_result = _mapping_result(
        [
            {
                "id": plan_id,
                "project_key": "red-phone",
                "file_path": "docs/plans/legacy-plan.md",
                "content_hash": "b" * 64,
                "status": "active",
                "freshness_status": "fresh",
                "chunk_count": 2,
            }
        ]
    )
    chunk_result = _mapping_result([{"plan_id": plan_id, "observed_chunk_count": 2}])
    link_result = _mapping_result(
        [
            {
                "feature_id": UUID("33333333-3333-3333-3333-333333333333"),
                "artifact_id": plan_id,
                "similarity_score": 0.85,
                "created_at": "2026-07-01T00:00:00+00:00",
            }
        ]
    )
    alembic_result = MagicMock()
    alembic_result.scalar_one.return_value = "037"
    identity = {
        "database_name": "SENSITIVE_DATABASE_NAME",
        "server_address": "SENSITIVE_SERVER_ADDRESS",
        "server_port": 5432,
    }
    identity_result = _one_mapping_result(identity)
    factory, session = _session_factory(
        [
            MagicMock(),
            context_result,
            plan_result,
            chunk_result,
            link_result,
            alembic_result,
            identity_result,
        ]
    )
    store = RepairStore(factory)

    snapshot = await store.inventory(manifest, local_files)

    session.begin.assert_called_once_with()
    sql = [str(call.args[0]) for call in session.execute.await_args_list]
    assert sql[0] == "SET TRANSACTION READ ONLY"
    assert all(column.name in sql[1] for column in project_contexts.c)
    plan_stmt = session.execute.await_args_list[2].args[0]
    assert "content" not in plan_stmt.selected_columns.keys()
    assert "embedding" not in plan_stmt.selected_columns.keys()
    assert "project_key" in sql[2]
    assert "file_path" in sql[2]
    chunk_stmt = session.execute.await_args_list[3].args[0]
    assert "content" not in chunk_stmt.selected_columns.keys()
    assert "embedding" not in chunk_stmt.selected_columns.keys()
    assert "feature_artifacts" in sql[4]
    assert "alembic_version" in sql[5]
    assert snapshot.alembic_revision == "037"
    assert "current_database" in sql[6]
    assert snapshot.database_identity_hash == database_identity_fingerprint(identity)
    assert set(snapshot.contexts[0].values) == {column.name for column in project_contexts.c}
    assert snapshot.contexts[0].proposed_plan_scan_paths == (
        str(manifest.projects[0].scan_paths[0]),
    )
    assert datetime.fromisoformat(snapshot.mutation_timestamp).tzinfo is not None
    assert "SENSITIVE" not in repr(snapshot.to_dict())
    assert snapshot.polluted_plan_ids == (str(plan_id),)


@pytest.mark.asyncio
async def test_inventory_requires_every_manifest_context(tmp_path: Path) -> None:
    """Producing a snapshot without a targeted context must make this test fail."""
    manifest, local_files = _manifest(tmp_path)
    factory, _session = _session_factory([MagicMock(), _mapping_result([])])
    store = RepairStore(factory)

    with pytest.raises(RepairSafetyError) as exc:
        await store.inventory(manifest, local_files)

    assert exc.value.reason_code == "context_set_mismatch"


_PROJECT_KEYS = (
    "red-games",
    "red-gift",
    "red-phone",
    "red-quant",
    "red-shrik",
    "red-viewer",
    "red-writer",
)
_DATABASE_IDENTITY = {
    "database_name": "brain_v42_test",
    "server_address": "127.0.0.1",
    "server_port": 5432,
}
# Dérivé, jamais recopié : la duplication de cette constante est précisément
# ce qui a laissé la 041 atterrir sans que rien ne le signale. Sa justesse
# est gardée par tests/unit/test_plan_index_repair_head_pin.py.
_ALEMBIC_HEAD = _REQUIRED_ALEMBIC_HEAD


def _context_row_for(project_key: str, ordinal: int) -> dict[str, object]:
    row = _context_row()
    row.update(
        {
            "id": UUID(int=ordinal + 1),
            "project_key": project_key,
            "name": project_key,
            "metadata": {"ordinal": ordinal},
            "plan_scan_paths": [f"legacy/{project_key}"],
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 7, 1, tzinfo=UTC),
        }
    )
    return row


def _apply_snapshot() -> RepairSnapshot:
    contexts = tuple(
        ContextRecord.from_values(
            _context_row_for(project_key, ordinal),
            proposed_plan_scan_paths=(f"/srv/{project_key}/docs/plans",),
        )
        for ordinal, project_key in enumerate(_PROJECT_KEYS)
    )
    return RepairSnapshot(
        version=1,
        mutation_timestamp="2026-07-28T01:02:03+00:00",
        database_identity_hash=database_identity_fingerprint(_DATABASE_IDENTITY),
        alembic_revision=_ALEMBIC_HEAD,
        contexts=contexts,
        local_files=(),
        indexed_plans=(),
        feature_links=(),
        polluted_plan_ids=(),
        missing_canonical_files=(),
        collisions=(),
    )


def _mutation_proof(snapshot: RepairSnapshot, *, snapshot_sha256: str | None = None) -> object:
    return plan_index_repair.MutationProof(
        snapshot_sha256=snapshot_sha256 or sha256_json(snapshot.to_dict()),
        backup_receipt_sha256="b" * 64,
        postgres_restore_tested=True,
        writers_off_confirmed=True,
    )


def _post_update_rows(snapshot: RepairSnapshot) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for context in snapshot.contexts:
        row = dict(context.values)
        row["plan_scan_paths"] = list(context.proposed_plan_scan_paths)
        row["updated_at"] = datetime.fromisoformat(snapshot.mutation_timestamp)
        rows.append(row)
    return rows


def _scalar_result(value: str) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = value
    return result


def _apply_session_factory(
    snapshot: RepairSnapshot,
    *,
    locked_rows: list[dict[str, object]] | None = None,
    database_identity: dict[str, object] | None = None,
    alembic_head: str = _ALEMBIC_HEAD,
    update_results: list[object] | None = None,
) -> tuple[MagicMock, MagicMock]:
    rows = locked_rows or [
        _context_row_for(project_key, ordinal) for ordinal, project_key in enumerate(_PROJECT_KEYS)
    ]
    mutation_timestamp = datetime.fromisoformat(snapshot.mutation_timestamp)
    updates = (
        update_results
        if update_results is not None
        else [_update_result(mutation_timestamp) for _ in _PROJECT_KEYS]
    )
    factory, session = _session_factory(
        [
            MagicMock(),
            _one_mapping_result(database_identity or _DATABASE_IDENTITY),
            _scalar_result(alembic_head),
            _mapping_result(rows),
            MagicMock(),
            *updates,
        ]
    )
    return factory, session


def _update_calls(session: MagicMock) -> list[object]:
    return [call for call in session.execute.await_args_list if isinstance(call.args[0], Update)]


@pytest.mark.parametrize("invalid_context_set", ["missing", "dream_extra"])
@pytest.mark.asyncio
async def test_apply_paths_rejects_noncanonical_project_set_before_opening_session(
    invalid_context_set: str,
) -> None:
    """Opening a DB session for fewer/more than the seven ticket projects must make this fail."""
    snapshot = _apply_snapshot()
    if invalid_context_set == "missing":
        snapshot = replace(snapshot, contexts=snapshot.contexts[:-1])
    else:
        dream = ContextRecord.from_values(
            _context_row_for("Dream", len(_PROJECT_KEYS)),
            proposed_plan_scan_paths=("/srv/Dream/docs/plans",),
        )
        snapshot = replace(snapshot, contexts=(*snapshot.contexts, dream))
    factory = MagicMock()
    store = RepairStore(factory)

    with pytest.raises(RepairSafetyError) as exc:
        await store.apply_paths(snapshot, _mutation_proof(snapshot))

    assert exc.value.reason_code == "context_set_mismatch"
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_apply_paths_updates_all_original_rows_with_signed_values_and_partial_sql() -> None:
    """A full upsert, unsigned timestamp, or omitted context update must make this fail."""
    snapshot = _apply_snapshot()
    factory, session = _apply_session_factory(snapshot)
    store = RepairStore(factory)

    result = await store.apply_paths(snapshot, _mutation_proof(snapshot))

    assert result.status == "applied"
    assert result.affected_rows == 7
    updates = _update_calls(session)
    assert len(updates) == 7
    for call, context in zip(updates, snapshot.contexts, strict=True):
        statement = call.args[0]
        compiled = statement.compile(dialect=postgresql.dialect())
        set_clause = compiled.string.partition(" SET ")[2].partition(" WHERE ")[0]
        updated_columns = {
            assignment.partition("=")[0].strip().split(".")[-1]
            for assignment in set_clause.split(", ")
        }
        assert updated_columns == {"plan_scan_paths", "updated_at"}
        assert "project_key" in compiled.string.partition(" WHERE ")[2]
        assert context.project_key in compiled.params.values()
        assert list(context.proposed_plan_scan_paths) in compiled.params.values()
        assert datetime.fromisoformat(snapshot.mutation_timestamp) in compiled.params.values()


@pytest.mark.asyncio
async def test_apply_paths_uses_serializable_transaction_and_locks_exactly_seven_rows() -> None:
    """Dropping serializable isolation or FOR UPDATE on any target must make this fail."""
    snapshot = _apply_snapshot()
    factory, session = _apply_session_factory(snapshot)
    store = RepairStore(factory)

    await store.apply_paths(snapshot, _mutation_proof(snapshot))

    session.begin.assert_called_once_with()
    assert str(session.execute.await_args_list[0].args[0]) == (
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
    )
    selects = [
        call.args[0]
        for call in session.execute.await_args_list
        if isinstance(call.args[0], sa.sql.Select)
    ]
    assert len(selects) == 1
    lock_sql = str(selects[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in lock_sql
    assert set(selects[0].selected_columns.keys()) == {column.name for column in project_contexts.c}
    lock_params = tuple(selects[0].compile().params.values())
    assert len(lock_params) == 1
    assert tuple(lock_params[0]) == _PROJECT_KEYS


@pytest.mark.asyncio
async def test_apply_paths_replays_only_an_exact_all_row_post_update_state() -> None:
    """Rewriting an exact post-update state instead of replaying safely must make this fail."""
    snapshot = _apply_snapshot()
    factory, session = _apply_session_factory(snapshot, locked_rows=_post_update_rows(snapshot))
    store = RepairStore(factory)

    result = await store.apply_paths(snapshot, _mutation_proof(snapshot))

    assert result.status == "already_applied"
    assert result.affected_rows == 0
    assert _update_calls(session) == []


@pytest.mark.asyncio
async def test_apply_paths_conflict_compares_every_fingerprint_before_any_update() -> None:
    """Updating early rows before discovering later drift must make this fail."""
    snapshot = _apply_snapshot()
    rows = [_context_row_for(key, ordinal) for ordinal, key in enumerate(_PROJECT_KEYS)]
    rows[-1]["metadata"] = {"ordinal": 999}
    factory, session = _apply_session_factory(snapshot, locked_rows=rows, update_results=[])
    store = RepairStore(factory)

    with pytest.raises(RepairSafetyError) as exc:
        await store.apply_paths(snapshot, _mutation_proof(snapshot))

    assert exc.value.reason_code == "context_cas_conflict"
    assert _update_calls(session) == []


@pytest.mark.asyncio
async def test_apply_paths_rejects_mixed_original_and_post_update_rows() -> None:
    """Accepting a partial prior apply as a replay-safe state must make this fail."""
    snapshot = _apply_snapshot()
    rows = [_context_row_for(key, ordinal) for ordinal, key in enumerate(_PROJECT_KEYS)]
    rows[0] = _post_update_rows(snapshot)[0]
    factory, session = _apply_session_factory(snapshot, locked_rows=rows, update_results=[])
    store = RepairStore(factory)

    with pytest.raises(RepairSafetyError) as exc:
        await store.apply_paths(snapshot, _mutation_proof(snapshot))

    assert exc.value.reason_code == "context_cas_conflict"
    assert _update_calls(session) == []


@pytest.mark.asyncio
async def test_apply_paths_blocks_wrong_database_identity_inside_mutation_transaction() -> None:
    """Mutating a database other than the signed inventory target must make this fail."""
    snapshot = _apply_snapshot()
    factory, session = _apply_session_factory(
        snapshot,
        database_identity={**_DATABASE_IDENTITY, "database_name": "wrong_database"},
        update_results=[],
    )
    store = RepairStore(factory)

    with pytest.raises(RepairSafetyError) as exc:
        await store.apply_paths(snapshot, _mutation_proof(snapshot))

    assert exc.value.reason_code == "database_identity_mismatch"
    session.begin.return_value.__aenter__.assert_awaited_once_with()
    assert "current_database" in str(session.execute.await_args_list[1].args[0])
    assert _update_calls(session) == []


@pytest.mark.asyncio
async def test_apply_paths_blocks_noncanonical_alembic_head_inside_mutation_transaction() -> None:
    """Mutating on the Dream-only 038 head must fail before context reads or DML."""
    snapshot = _apply_snapshot()
    factory, session = _apply_session_factory(
        snapshot,
        alembic_head="038",
        update_results=[],
    )
    store = RepairStore(factory)

    with pytest.raises(RepairSafetyError) as exc:
        await store.apply_paths(snapshot, _mutation_proof(snapshot))

    assert exc.value.reason_code == "alembic_head_mismatch"
    session.begin.return_value.__aenter__.assert_awaited_once_with()
    assert "alembic_version" in str(session.execute.await_args_list[2].args[0])
    assert _update_calls(session) == []


@pytest.mark.asyncio
async def test_apply_paths_rejects_migration_filename_instead_of_revision() -> None:
    """Treating an Alembic filename as revision 037 must make this test fail."""
    descriptive_head = "037_session_lifecycle_v4"
    snapshot = replace(_apply_snapshot(), alembic_revision=descriptive_head)
    factory, session = _apply_session_factory(
        snapshot,
        alembic_head=descriptive_head,
        update_results=[],
    )

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).apply_paths(snapshot, _mutation_proof(snapshot))

    assert exc.value.reason_code == "alembic_head_mismatch"
    assert _update_calls(session) == []


@pytest.mark.asyncio
async def test_apply_paths_rejects_unbound_proof_before_opening_session() -> None:
    """Opening a mutating session for a proof bound to another snapshot must make this fail."""
    snapshot = _apply_snapshot()
    factory = MagicMock()
    store = RepairStore(factory)

    with pytest.raises(RepairSafetyError) as exc:
        await store.apply_paths(snapshot, _mutation_proof(snapshot, snapshot_sha256="0" * 64))

    assert exc.value.reason_code == "snapshot_proof_mismatch"
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_apply_paths_rolls_back_when_seventh_update_affects_no_row() -> None:
    """Reporting success after a zero-row seventh UPDATE must make this fail."""
    snapshot = _apply_snapshot()
    factory, session = _apply_session_factory(
        snapshot,
        update_results=[
            *[
                _update_result(datetime.fromisoformat(snapshot.mutation_timestamp))
                for _ in range(6)
            ],
            _update_result(),
        ],
    )
    store = RepairStore(factory)

    with pytest.raises(RepairSafetyError) as exc:
        await store.apply_paths(snapshot, _mutation_proof(snapshot))

    assert exc.value.reason_code == "context_update_count_mismatch"
    assert len(_update_calls(session)) == 7
    exit_args = session.begin.return_value.__aexit__.await_args.args
    assert exit_args[0] is RepairSafetyError
    assert exit_args[1] is exc.value


@pytest.mark.asyncio
async def test_apply_paths_rejects_returned_timestamp_mismatch() -> None:
    snapshot = _apply_snapshot()
    signed = datetime.fromisoformat(snapshot.mutation_timestamp)
    factory, _session = _apply_session_factory(
        snapshot,
        update_results=[_update_result(signed) for _ in range(6)]
        + [_update_result(datetime(2025, 1, 1, tzinfo=UTC))],
    )

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).apply_paths(snapshot, _mutation_proof(snapshot))

    assert exc.value.reason_code == "context_cas_conflict"


@pytest.mark.asyncio
async def test_apply_paths_rolls_back_when_seventh_update_fails() -> None:
    """Leaking a raw seventh-update failure after rollback must make this fail."""
    snapshot = _apply_snapshot()
    secret = "SENSITIVE_DB_EXCEPTION_DETAIL"
    failure = RuntimeError(f"seventh update failed: {secret}")
    factory, session = _apply_session_factory(
        snapshot,
        update_results=[
            *[
                _update_result(datetime.fromisoformat(snapshot.mutation_timestamp))
                for _ in range(6)
            ],
            failure,
        ],
    )
    store = RepairStore(factory)

    with pytest.raises(RepairSafetyError) as exc:
        await store.apply_paths(snapshot, _mutation_proof(snapshot))

    assert exc.value.reason_code == "apply_paths_transaction_failed"
    assert secret not in str(exc.value)
    assert secret not in repr(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert len(_update_calls(session)) == 7
    exit_args = session.begin.return_value.__aexit__.await_args.args
    assert exit_args[0] is RuntimeError
    assert exit_args[1] is failure


def _task5_snapshot(
    tmp_path: Path,
    *,
    preexisting_project_keys: frozenset[str] = frozenset(),
    polluted_count: int = 1,
) -> RepairSnapshot:
    local_files: list[LocalPlanFile] = []
    existing_plans: list[plan_index_repair.IndexedPlanRecord] = []
    for ordinal, project_key in enumerate(_PROJECT_KEYS):
        plan_path = tmp_path / project_key / "docs" / "plans" / "canonical-plan.md"
        plan_path.parent.mkdir(parents=True)
        content = f"# {project_key}\n".encode()
        plan_path.write_bytes(content)
        local_file = LocalPlanFile(
            project_key=project_key,
            file_path=str(plan_path.resolve()),
            content_hash=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )
        local_files.append(local_file)
        if project_key in preexisting_project_keys:
            existing_plans.append(
                plan_index_repair.IndexedPlanRecord(
                    id=str(UUID(int=100 + ordinal)),
                    project_key=project_key,
                    file_path=local_file.file_path,
                    content_hash=local_file.content_hash,
                    status="active",
                    freshness_status="fresh",
                    declared_chunk_count=1,
                    observed_chunk_count=1,
                )
            )

    polluted_plans = tuple(
        plan_index_repair.IndexedPlanRecord(
            id=str(UUID(int=500 + ordinal)),
            project_key=_PROJECT_KEYS[ordinal],
            file_path=str((tmp_path / f"legacy-{ordinal}-plan.md").resolve()),
            content_hash="d" * 64,
            status="active",
            freshness_status="stale",
            declared_chunk_count=1,
            observed_chunk_count=1,
        )
        for ordinal in range(polluted_count)
    )
    feature_links = tuple(
        plan_index_repair.FeatureLinkRecord(
            feature_id=str(UUID(int=700 + ordinal)),
            plan_id=plan.id,
            similarity_score=0.75,
            created_at="2026-07-01T00:00:00+00:00",
        )
        for ordinal, plan in enumerate(polluted_plans)
    )
    base = _apply_snapshot()
    missing = tuple(
        local_file
        for local_file in local_files
        if local_file.project_key not in preexisting_project_keys
    )
    indexed_plans = (*existing_plans, *polluted_plans)
    return replace(
        base,
        local_files=tuple(local_files),
        indexed_plans=indexed_plans,
        feature_links=feature_links,
        polluted_plan_ids=tuple(plan.id for plan in polluted_plans),
        missing_canonical_files=missing,
    )


def _current_plan_rows(snapshot: RepairSnapshot) -> list[dict[str, object]]:
    existing_by_path = {
        plan.file_path: plan
        for plan in snapshot.indexed_plans
        if plan.file_path in {item.file_path for item in snapshot.local_files}
    }
    rows: list[dict[str, object]] = []
    for ordinal, local_file in enumerate(snapshot.local_files):
        existing = existing_by_path.get(local_file.file_path)
        rows.append(
            {
                "id": UUID(existing.id) if existing else UUID(int=200 + ordinal),
                "project_key": local_file.project_key,
                "file_path": local_file.file_path,
                "content_hash": local_file.content_hash,
                "status": existing.status if existing else "active",
                "freshness_status": existing.freshness_status if existing else "fresh",
                "chunk_count": existing.declared_chunk_count if existing else 1,
            }
        )
    for plan in snapshot.indexed_plans:
        if plan.id in snapshot.polluted_plan_ids:
            rows.append(
                {
                    "id": UUID(plan.id),
                    "project_key": plan.project_key,
                    "file_path": plan.file_path,
                    "content_hash": plan.content_hash,
                    "status": plan.status,
                    "freshness_status": plan.freshness_status,
                    "chunk_count": plan.declared_chunk_count,
                }
            )
    return rows


def _task5_evidence(snapshot: RepairSnapshot) -> plan_index_repair.ReindexEvidence:
    missing_keys = {item.project_key for item in snapshot.missing_canonical_files}
    existing_keys = {
        plan.project_key
        for plan in snapshot.indexed_plans
        if plan.file_path in {item.file_path for item in snapshot.local_files}
    }
    return plan_index_repair.ReindexEvidence(
        version=1,
        snapshot_sha256=sha256_json(snapshot.to_dict()),
        projects=tuple(
            plan_index_repair.ProjectReindexStats(
                project_key=project_key,
                indexed=int(project_key in missing_keys),
                skipped=int(project_key in existing_keys),
                linked=int(project_key in missing_keys),
                errors=0,
                chunks_created=int(project_key in missing_keys),
            )
            for project_key in _PROJECT_KEYS
        ),
    )


def _task5_report(
    snapshot: RepairSnapshot,
    rows: list[dict[str, object]],
) -> plan_index_repair.VerificationReport:
    canonical_paths = {item.file_path for item in snapshot.local_files}
    evidence = _task5_evidence(snapshot)
    return plan_index_repair.VerificationReport(
        version=1,
        snapshot_sha256=sha256_json(snapshot.to_dict()),
        evidence_sha256=sha256_json(evidence.to_dict()),
        evidence=evidence,
        canonical_plans=tuple(
            plan_index_repair.VerifiedPlanRecord(
                id=str(row["id"]),
                project_key=str(row["project_key"]),
                file_path=str(row["file_path"]),
                content_hash=str(row["content_hash"]),
            )
            for row in rows
            if row["file_path"] in canonical_paths
        ),
    )


def _verification_results(
    snapshot: RepairSnapshot,
    rows: list[dict[str, object]],
) -> list[MagicMock]:
    missing_paths = {item.file_path for item in snapshot.missing_canonical_files}
    missing_ids = [row["id"] for row in rows if row["file_path"] in missing_paths]
    return [
        MagicMock(),
        _one_mapping_result(_DATABASE_IDENTITY),
        _scalar_result(_ALEMBIC_HEAD),
        _mapping_result(rows),
        _mapping_result([{"plan_id": row["id"], "observed_chunk_count": 1} for row in rows]),
        _mapping_result(
            [
                {
                    "feature_id": UUID(int=800 + ordinal),
                    "artifact_id": plan_id,
                }
                for ordinal, plan_id in enumerate(missing_ids)
            ]
        ),
    ]


@pytest.mark.asyncio
async def test_verify_recomputes_files_and_proves_exact_canonical_database_rows(
    tmp_path: Path,
) -> None:
    """Skipping filesystem, row, chunk, or link checks must make this test fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    factory, session = _session_factory(_verification_results(snapshot, rows))
    store = RepairStore(factory)

    report = await store.verify(snapshot, _task5_evidence(snapshot))

    assert report.snapshot_sha256 == sha256_json(snapshot.to_dict())
    assert report.evidence_sha256 == sha256_json(_task5_evidence(snapshot).to_dict())
    assert {plan.file_path for plan in report.canonical_plans} == {
        item.file_path for item in snapshot.local_files
    }
    assert str(session.execute.await_args_list[0].args[0]) == (
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY"
    )
    plan_statement = session.execute.await_args_list[3].args[0]
    assert "content" not in plan_statement.selected_columns.keys()
    assert "embedding" not in plan_statement.selected_columns.keys()
    assert "project_key" in str(plan_statement)
    assert "file_path" in str(plan_statement)
    assert "indexed_plan_chunks" in str(session.execute.await_args_list[4].args[0])
    assert "feature_artifacts" in str(session.execute.await_args_list[5].args[0])


@pytest.mark.asyncio
async def test_verify_rejects_wrong_evidence_digest_before_opening_session(
    tmp_path: Path,
) -> None:
    """Opening a session for evidence bound to another snapshot must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    evidence = replace(_task5_evidence(snapshot), snapshot_sha256="0" * 64)
    factory = MagicMock()

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).verify(snapshot, evidence)

    assert exc.value.reason_code == "snapshot_evidence_mismatch"
    factory.assert_not_called()


@pytest.mark.parametrize(
    ("drift", "reason_code"),
    [
        ("missing", "canonical_plan_set_mismatch"),
        ("extra", "canonical_plan_set_mismatch"),
        ("owner", "canonical_plan_owner_mismatch"),
        ("hash", "canonical_plan_hash_mismatch"),
        ("polluted", "polluted_plan_changed"),
        ("polluted_chunks", "polluted_plan_changed"),
    ],
)
@pytest.mark.asyncio
async def test_verify_rejects_database_drift(
    tmp_path: Path,
    drift: str,
    reason_code: str,
) -> None:
    """Accepting missing, extra, misowned, rehashed, or changed rows must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    if drift == "missing":
        rows.pop(0)
    elif drift == "extra":
        rows.insert(1, {**rows[0], "id": UUID(int=999)})
    elif drift == "owner":
        rows[0]["project_key"] = "red-gift"
    elif drift == "hash":
        rows[0]["content_hash"] = "e" * 64
    elif drift == "polluted":
        rows[-1]["status"] = "archived"
    else:
        rows[-1]["chunk_count"] = 2
    factory, _session = _session_factory(_verification_results(snapshot, rows))

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).verify(snapshot, _task5_evidence(snapshot))

    assert exc.value.reason_code == reason_code


@pytest.mark.parametrize("mutation", ["changed", "missing"])
@pytest.mark.asyncio
async def test_verify_rejects_a_changed_local_file_without_exposing_content(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Trusting an inventory-time hash after local mutation must make this test fail."""
    snapshot = _task5_snapshot(tmp_path)
    secret = "SENSITIVE_CHANGED_PLAN_CONTENT"
    changed_path = Path(snapshot.local_files[0].file_path)
    if mutation == "changed":
        changed_path.write_text(secret, encoding="utf-8")
    else:
        changed_path.unlink()
    factory = MagicMock()

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).verify(snapshot, _task5_evidence(snapshot))

    assert exc.value.reason_code == "local_file_changed"
    assert secret not in str(exc.value)
    assert secret not in repr(exc.value)
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_verify_rejects_stats_not_proven_by_chunks_and_links(
    tmp_path: Path,
) -> None:
    """Trusting claimed counters instead of database aggregates must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    evidence = _task5_evidence(snapshot)
    stats = list(evidence.projects)
    phone_index = next(index for index, stat in enumerate(stats) if stat.project_key == "red-phone")
    stats[phone_index] = replace(
        stats[phone_index],
        linked=stats[phone_index].linked + 1,
        chunks_created=stats[phone_index].chunks_created + 1,
    )
    evidence = replace(evidence, projects=tuple(stats))
    factory, _session = _session_factory(_verification_results(snapshot, rows))

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).verify(snapshot, evidence)

    assert exc.value.reason_code == "reindex_stats_mismatch"


@pytest.mark.asyncio
async def test_verify_masks_unexpected_database_errors_without_chaining(
    tmp_path: Path,
) -> None:
    """Leaking a driver exception or its context from verification must make this fail."""
    snapshot = _task5_snapshot(tmp_path, polluted_count=2)
    snapshot = replace(
        snapshot,
        feature_links=(
            *snapshot.feature_links,
            plan_index_repair.FeatureLinkRecord(
                feature_id=str(UUID(int=799)),
                plan_id=snapshot.polluted_plan_ids[0],
                similarity_score=0.5,
                created_at="2026-07-02T00:00:00+00:00",
            ),
        ),
    )
    secret = "SENSITIVE_VERIFY_DB_DETAIL"
    factory, _session = _session_factory([RuntimeError(secret)])

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).verify(snapshot, _task5_evidence(snapshot))

    assert exc.value.reason_code == "verify_transaction_failed"
    assert secret not in repr(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


@pytest.mark.asyncio
async def test_verify_does_not_capture_cancellation(tmp_path: Path) -> None:
    """Converting cancellation into a repair result must make this test fail."""
    snapshot = _task5_snapshot(tmp_path)
    factory, _session = _session_factory([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await RepairStore(factory).verify(snapshot, _task5_evidence(snapshot))


def _finalize_factory(
    snapshot: RepairSnapshot,
    rows: list[dict[str, object]],
    *,
    contexts: list[dict[str, object]] | None = None,
    identity: dict[str, object] | None = None,
    alembic_head: str = _ALEMBIC_HEAD,
    link_rowcount: int | None = None,
    plan_rowcount: int | None = None,
    chunk_rows: list[dict[str, object]] | None = None,
    link_rows: list[dict[str, object]] | None = None,
) -> tuple[MagicMock, MagicMock]:
    polluted_count = len(snapshot.polluted_plan_ids)
    targeted_link_count = sum(
        link.plan_id in snapshot.polluted_plan_ids for link in snapshot.feature_links
    )
    polluted_plans = [
        plan for plan in snapshot.indexed_plans if plan.id in snapshot.polluted_plan_ids
    ]
    expected_chunk_rows = [
        {
            "plan_id": UUID(plan.id),
            "observed_chunk_count": plan.observed_chunk_count,
        }
        for plan in polluted_plans
    ]
    expected_link_rows = [
        {
            "feature_id": UUID(link.feature_id),
            "artifact_id": UUID(link.plan_id),
            "similarity_score": link.similarity_score,
            "created_at": datetime.fromisoformat(link.created_at),
        }
        for link in snapshot.feature_links
        if link.plan_id in snapshot.polluted_plan_ids
    ]
    return _session_factory(
        [
            MagicMock(),
            _one_mapping_result(identity if identity is not None else _DATABASE_IDENTITY),
            _scalar_result(alembic_head),
            _mapping_result(contexts if contexts is not None else _post_update_rows(snapshot)),
            _mapping_result(rows),
            _mapping_result(chunk_rows if chunk_rows is not None else expected_chunk_rows),
            _mapping_result(link_rows if link_rows is not None else expected_link_rows),
            MagicMock(rowcount=(targeted_link_count if link_rowcount is None else link_rowcount)),
            MagicMock(rowcount=polluted_count if plan_rowcount is None else plan_rowcount),
        ]
    )


@pytest.mark.asyncio
async def test_finalize_revalidates_every_cas_and_deletes_links_before_exact_plans(
    tmp_path: Path,
) -> None:
    """A broad delete, wrong order, or omitted transactional recheck must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    report = _task5_report(snapshot, rows)
    factory, session = _finalize_factory(snapshot, rows)

    result = await RepairStore(factory).finalize(snapshot, _mutation_proof(snapshot), report)

    assert result.status == "finalized"
    assert result.affected_rows == len(snapshot.polluted_plan_ids)
    assert str(session.execute.await_args_list[0].args[0]) == (
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
    )
    assert "current_database" in str(session.execute.await_args_list[1].args[0])
    assert "alembic_version" in str(session.execute.await_args_list[2].args[0])
    context_select = session.execute.await_args_list[3].args[0]
    plan_select = session.execute.await_args_list[4].args[0]
    assert "FOR UPDATE" in str(context_select.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in str(plan_select.compile(dialect=postgresql.dialect()))
    deletes = [
        call.args[0]
        for call in session.execute.await_args_list
        if isinstance(call.args[0], sa.sql.dml.Delete)
    ]
    assert [statement.table.name for statement in deletes] == [
        "feature_artifacts",
        "indexed_plans",
    ]
    link_sql = str(deletes[0].compile(dialect=postgresql.dialect()))
    plan_compiled = deletes[1].compile(dialect=postgresql.dialect())
    assert "artifact_type" in link_sql
    assert "artifact_id" in link_sql
    assert "id" in plan_compiled.string
    assert "project_key" in plan_compiled.string
    assert "file_path" in plan_compiled.string
    assert "content_hash" in plan_compiled.string
    assert not any(statement.table.name == "indexed_plan_chunks" for statement in deletes)


@pytest.mark.parametrize("drift", ["status", "freshness", "declared_chunks", "chunks", "link"])
@pytest.mark.asyncio
async def test_finalize_rejects_full_polluted_row_or_link_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    current_rows = [dict(row) for row in rows]
    chunk_rows = None
    link_rows = None
    polluted = next(row for row in current_rows if str(row["id"]) in snapshot.polluted_plan_ids)
    if drift == "status":
        polluted["status"] = "archived"
    elif drift == "freshness":
        polluted["freshness_status"] = "fresh"
    elif drift == "declared_chunks":
        polluted["chunk_count"] = 2
    elif drift == "chunks":
        chunk_rows = [
            {
                "plan_id": UUID(snapshot.polluted_plan_ids[0]),
                "observed_chunk_count": 2,
            }
        ]
    else:
        original_link = snapshot.feature_links[0]
        link_rows = [
            {
                "feature_id": UUID(int=999),
                "artifact_id": UUID(original_link.plan_id),
                "similarity_score": original_link.similarity_score,
                "created_at": datetime.fromisoformat(original_link.created_at),
            }
        ]
    factory, session = _finalize_factory(
        snapshot,
        current_rows,
        chunk_rows=chunk_rows,
        link_rows=link_rows,
    )

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).finalize(
            snapshot,
            _mutation_proof(snapshot),
            _task5_report(snapshot, rows),
        )

    assert exc.value.reason_code == "finalize_plan_cas_conflict"
    assert not any(
        isinstance(call.args[0], sa.sql.dml.Delete) for call in session.execute.await_args_list
    )


@pytest.mark.parametrize(
    ("target", "rowcount", "reason_code"),
    [
        ("links", 0, "feature_link_delete_count_mismatch"),
        ("links", 2, "feature_link_delete_count_mismatch"),
        ("plans", 0, "polluted_plan_delete_count_mismatch"),
        ("plans", 2, "polluted_plan_delete_count_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_finalize_rolls_back_on_zero_or_excess_delete_counts(
    tmp_path: Path,
    target: str,
    rowcount: int,
    reason_code: str,
) -> None:
    """Committing after a zero or excess bounded delete must make this test fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    factory, session = _finalize_factory(
        snapshot,
        rows,
        link_rowcount=rowcount if target == "links" else None,
        plan_rowcount=rowcount if target == "plans" else None,
    )

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).finalize(
            snapshot,
            _mutation_proof(snapshot),
            _task5_report(snapshot, rows),
        )

    assert exc.value.reason_code == reason_code
    exit_args = session.begin.return_value.__aexit__.await_args.args
    assert exit_args[0] is RepairSafetyError
    deletes = [
        call.args[0]
        for call in session.execute.await_args_list
        if isinstance(call.args[0], sa.sql.dml.Delete)
    ]
    assert len(deletes) == (1 if target == "links" else 2)


@pytest.mark.parametrize(
    ("gate", "reason_code"),
    [
        ("identity", "database_identity_mismatch"),
        ("schema", "alembic_head_mismatch"),
        ("context", "context_cas_conflict"),
        ("plan", "finalize_plan_cas_conflict"),
    ],
)
@pytest.mark.asyncio
async def test_finalize_rejects_transactional_cas_drift(
    tmp_path: Path,
    gate: str,
    reason_code: str,
) -> None:
    """Mutating after identity, schema, context, or plan drift must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    current_rows = [dict(row) for row in rows]
    if gate == "plan":
        current_rows[-1]["content_hash"] = "e" * 64
    factory, session = _finalize_factory(
        snapshot,
        current_rows,
        contexts=(
            [_context_row_for(key, ordinal) for ordinal, key in enumerate(_PROJECT_KEYS)]
            if gate == "context"
            else None
        ),
        identity=({**_DATABASE_IDENTITY, "database_name": "wrong"} if gate == "identity" else None),
        alembic_head="038" if gate == "schema" else _ALEMBIC_HEAD,
    )

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).finalize(
            snapshot,
            _mutation_proof(snapshot),
            _task5_report(snapshot, rows),
        )

    assert exc.value.reason_code == reason_code
    assert not any(
        isinstance(call.args[0], sa.sql.dml.Delete) for call in session.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_finalize_rejects_report_or_filesystem_drift(
    tmp_path: Path,
) -> None:
    """Trusting a report from another proof or a changed local file must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    factory = MagicMock()
    report = _task5_report(snapshot, rows)
    wrong_evidence = replace(report.evidence, snapshot_sha256="0" * 64)
    report = replace(
        report,
        snapshot_sha256="0" * 64,
        evidence_sha256=sha256_json(wrong_evidence.to_dict()),
        evidence=wrong_evidence,
    )
    with pytest.raises(RepairSafetyError) as report_exc:
        await RepairStore(factory).finalize(snapshot, _mutation_proof(snapshot), report)
    assert report_exc.value.reason_code == "snapshot_report_mismatch"
    factory.assert_not_called()

    Path(snapshot.local_files[0].file_path).write_text("changed", encoding="utf-8")
    factory, session = _finalize_factory(snapshot, rows)
    with pytest.raises(RepairSafetyError) as file_exc:
        await RepairStore(factory).finalize(
            snapshot,
            _mutation_proof(snapshot),
            _task5_report(snapshot, rows),
        )
    assert file_exc.value.reason_code == "local_file_changed"
    session.begin.return_value.__aenter__.assert_awaited_once_with()
    assert len(session.execute.await_args_list) == 7
    assert not any(
        isinstance(call.args[0], sa.sql.dml.Delete) for call in session.execute.await_args_list
    )


@pytest.mark.parametrize("gate", ["proof", "evidence", "canonical"])
@pytest.mark.asyncio
async def test_finalize_rejects_altered_private_bindings_before_mutation(
    tmp_path: Path,
    gate: str,
) -> None:
    """Accepting altered proof, report evidence, or canonical tuples must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    proof = _mutation_proof(snapshot)
    report = _task5_report(snapshot, rows)
    if gate == "proof":
        proof = _mutation_proof(snapshot, snapshot_sha256="0" * 64)
    elif gate == "evidence":
        object.__setattr__(report, "evidence_sha256", "0" * 64)
    else:
        first = report.canonical_plans[0]
        report = replace(
            report,
            canonical_plans=(
                replace(first, content_hash="0" * 64),
                *report.canonical_plans[1:],
            ),
        )
    factory, session = _finalize_factory(snapshot, rows)

    with pytest.raises(RepairSafetyError):
        await RepairStore(factory).finalize(snapshot, proof, report)

    assert not any(
        isinstance(call.args[0], sa.sql.dml.Delete) for call in session.execute.await_args_list
    )


def _rollback_factory(
    snapshot: RepairSnapshot,
    rows: list[dict[str, object]],
    *,
    contexts: list[dict[str, object]] | None = None,
    alembic_head: str = _ALEMBIC_HEAD,
    links: list[dict[str, object]] | None = None,
    update_results: list[object] | None = None,
    link_rowcount: int | None = None,
    plan_rowcount: int | None = None,
) -> tuple[MagicMock, MagicMock]:
    missing_paths = {item.file_path for item in snapshot.missing_canonical_files}
    new_ids = [row["id"] for row in rows if row["file_path"] in missing_paths]
    selected_links = (
        links
        if links is not None
        else [
            {
                "feature_id": UUID(int=900 + ordinal),
                "artifact_id": plan_id,
            }
            for ordinal, plan_id in enumerate(new_ids)
        ]
    )
    updates = (
        update_results
        if update_results is not None
        else [
            _update_result(datetime.fromisoformat(str(context.values["updated_at"])))
            for context in sorted(snapshot.contexts, key=lambda item: item.project_key)
        ]
    )
    return _session_factory(
        [
            MagicMock(),
            _one_mapping_result(_DATABASE_IDENTITY),
            _scalar_result(alembic_head),
            _mapping_result(contexts if contexts is not None else _post_update_rows(snapshot)),
            _mapping_result(rows),
            _mapping_result(selected_links),
            MagicMock(),
            *updates,
            MagicMock(rowcount=len(selected_links) if link_rowcount is None else link_rowcount),
            MagicMock(rowcount=len(new_ids) if plan_rowcount is None else plan_rowcount),
        ]
    )


@pytest.mark.asyncio
async def test_rollback_rejects_dream_only_head_before_update_or_delete(
    tmp_path: Path,
) -> None:
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    factory, session = _rollback_factory(snapshot, rows, alembic_head="038")

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).rollback_before_finalize(
            snapshot,
            _mutation_proof(snapshot),
        )

    assert exc.value.reason_code == "alembic_head_mismatch"
    assert len(session.execute.await_args_list) == 3
    assert not any(
        isinstance(call.args[0], (sa.sql.dml.Update, sa.sql.dml.Delete))
        for call in session.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_rollback_restores_only_changed_context_fields_and_new_canonical_rows(
    tmp_path: Path,
) -> None:
    """Rewriting untouched context fields or deleting original plans must make this fail."""
    snapshot = _task5_snapshot(
        tmp_path,
        preexisting_project_keys=frozenset({"red-games"}),
    )
    rows = _current_plan_rows(snapshot)
    factory, session = _rollback_factory(snapshot, rows)

    result = await RepairStore(factory).rollback_before_finalize(
        snapshot,
        _mutation_proof(snapshot),
    )

    assert result.status == "rolled_back"
    assert result.affected_rows == 13
    updates = _update_calls(session)
    assert len(updates) == 7
    for statement in (call.args[0] for call in updates):
        compiled = statement.compile(dialect=postgresql.dialect())
        assignments = compiled.string.partition(" SET ")[2].partition(" WHERE ")[0]
        assert {
            item.partition("=")[0].strip().split(".")[-1] for item in assignments.split(", ")
        } == {
            "plan_scan_paths",
            "updated_at",
        }
    for call, context in zip(
        updates,
        sorted(snapshot.contexts, key=lambda item: item.project_key),
        strict=True,
    ):
        params = tuple(call.args[0].compile().params.values())
        assert list(context.values["plan_scan_paths"]) in params
        assert datetime.fromisoformat(str(context.values["updated_at"])) in params
    deletes = [
        call.args[0]
        for call in session.execute.await_args_list
        if isinstance(call.args[0], sa.sql.dml.Delete)
    ]
    assert [statement.table.name for statement in deletes] == [
        "feature_artifacts",
        "indexed_plans",
    ]
    plan_params = tuple(deletes[1].compile().params.values())
    original_id = UUID(
        next(plan.id for plan in snapshot.indexed_plans if plan.project_key == "red-games")
    )
    assert original_id not in plan_params
    expected_new_ids = {
        str(row["id"])
        for row in rows
        if row["file_path"] in {item.file_path for item in snapshot.missing_canonical_files}
    }
    assert expected_new_ids.issubset({str(value) for value in plan_params})
    assert not any(statement.table.name == "indexed_plan_chunks" for statement in deletes)


@pytest.mark.asyncio
async def test_rollback_is_idempotent_after_complete_pre_finalize_rollback(
    tmp_path: Path,
) -> None:
    """Rewriting or failing a fully restored state must make this test fail."""
    snapshot = _task5_snapshot(
        tmp_path,
        preexisting_project_keys=frozenset({"red-games"}),
    )
    rows = [
        row
        for row in _current_plan_rows(snapshot)
        if row["file_path"] not in {item.file_path for item in snapshot.missing_canonical_files}
    ]
    factory, session = _rollback_factory(
        snapshot,
        rows,
        contexts=[
            _context_row_for(project_key, ordinal)
            for ordinal, project_key in enumerate(_PROJECT_KEYS)
        ],
        links=[],
        update_results=[],
    )

    result = await RepairStore(factory).rollback_before_finalize(
        snapshot,
        _mutation_proof(snapshot),
    )

    assert result.status == "already_rolled_back"
    assert result.affected_rows == 0
    assert _update_calls(session) == []
    assert not any(
        isinstance(call.args[0], sa.sql.dml.Delete) for call in session.execute.await_args_list
    )
    assert len(session.execute.await_args_list) == 5


@pytest.mark.asyncio
async def test_rollback_returns_only_backup_digest_after_finalize(tmp_path: Path) -> None:
    """Attempting local reconstruction after every polluted row vanished must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = [
        row
        for row in _current_plan_rows(snapshot)
        if str(row["id"]) not in snapshot.polluted_plan_ids
    ]
    factory, session = _rollback_factory(
        snapshot,
        rows,
        links=[],
        update_results=[],
    )

    result = await RepairStore(factory).rollback_before_finalize(
        snapshot,
        _mutation_proof(snapshot),
    )

    assert result.to_dict() == {
        "status": "backup_restore_required",
        "backup_receipt_sha256": "b" * 64,
    }
    assert _update_calls(session) == []
    assert not any(
        isinstance(call.args[0], sa.sql.dml.Delete) for call in session.execute.await_args_list
    )
    assert len(session.execute.await_args_list) == 5


@pytest.mark.asyncio
async def test_rollback_refuses_when_only_one_polluted_row_disappeared(
    tmp_path: Path,
) -> None:
    """Treating partial polluted-row disappearance as finalized must make this fail."""
    snapshot = _task5_snapshot(tmp_path, polluted_count=2)
    rows = _current_plan_rows(snapshot)
    rows = [row for row in rows if str(row["id"]) != snapshot.polluted_plan_ids[0]]
    factory, session = _rollback_factory(snapshot, rows)

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).rollback_before_finalize(
            snapshot,
            _mutation_proof(snapshot),
        )

    assert exc.value.reason_code == "polluted_plan_missing"
    assert _update_calls(session) == []


@pytest.mark.asyncio
async def test_rollback_rejects_drift_in_any_untouched_context_column(
    tmp_path: Path,
) -> None:
    """Comparing only plan paths and timestamps before restoration must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    contexts = _post_update_rows(snapshot)
    contexts[-1]["metadata"] = {"ordinal": 999}
    rows = _current_plan_rows(snapshot)
    factory, session = _rollback_factory(snapshot, rows, contexts=contexts)

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).rollback_before_finalize(
            snapshot,
            _mutation_proof(snapshot),
        )

    assert exc.value.reason_code == "context_cas_conflict"
    assert _update_calls(session) == []
    assert not any(
        isinstance(call.args[0], sa.sql.dml.Delete) for call in session.execute.await_args_list
    )


@pytest.mark.parametrize("mutation", ["changed", "missing"])
@pytest.mark.asyncio
async def test_rollback_rejects_changed_or_missing_new_canonical_file(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Deleting database rows after a local-file CAS failure must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    path = Path(snapshot.missing_canonical_files[0].file_path)
    if mutation == "changed":
        path.write_text("changed", encoding="utf-8")
    else:
        path.unlink()
    factory, session = _rollback_factory(snapshot, rows)

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).rollback_before_finalize(
            snapshot,
            _mutation_proof(snapshot),
        )

    assert exc.value.reason_code == "local_file_changed"
    assert _update_calls(session) == []
    assert not any(
        isinstance(call.args[0], sa.sql.dml.Delete) for call in session.execute.await_args_list
    )
    assert len(session.execute.await_args_list) == 5


@pytest.mark.parametrize(
    ("target", "rowcount", "reason_code"),
    [
        ("context", 0, "context_update_count_mismatch"),
        ("context", 2, "context_update_count_mismatch"),
        ("links", 0, "feature_link_delete_count_mismatch"),
        ("links", 8, "feature_link_delete_count_mismatch"),
        ("plans", 0, "canonical_plan_delete_count_mismatch"),
        ("plans", 8, "canonical_plan_delete_count_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_rollback_requires_exact_dml_rowcounts(
    tmp_path: Path,
    target: str,
    rowcount: int,
    reason_code: str,
) -> None:
    """Committing any incomplete rollback DML must make this test fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    updates = [
        _update_result(datetime.fromisoformat(str(context.values["updated_at"])))
        for context in sorted(snapshot.contexts, key=lambda item: item.project_key)
    ]
    if target == "context":
        original = datetime.fromisoformat(
            str(
                sorted(snapshot.contexts, key=lambda item: item.project_key)[-1].values[
                    "updated_at"
                ]
            )
        )
        updates[-1] = _update_result(*([original] * rowcount))
    factory, _session = _rollback_factory(
        snapshot,
        rows,
        update_results=updates,
        link_rowcount=rowcount if target == "links" else None,
        plan_rowcount=rowcount if target == "plans" else None,
    )

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).rollback_before_finalize(
            snapshot,
            _mutation_proof(snapshot),
        )

    assert exc.value.reason_code == reason_code
    deletes = [
        call.args[0]
        for call in _session.execute.await_args_list
        if isinstance(call.args[0], sa.sql.dml.Delete)
    ]
    assert len(deletes) == {"context": 0, "links": 1, "plans": 2}[target]


@pytest.mark.asyncio
async def test_rollback_rejects_returned_timestamp_mismatch(tmp_path: Path) -> None:
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    updates = [
        _update_result(datetime.fromisoformat(str(context.values["updated_at"])))
        for context in sorted(snapshot.contexts, key=lambda item: item.project_key)
    ]
    updates[-1] = _update_result(datetime(2025, 1, 1, tzinfo=UTC))
    factory, _session = _rollback_factory(snapshot, rows, update_results=updates)

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).rollback_before_finalize(snapshot, _mutation_proof(snapshot))

    assert exc.value.reason_code == "context_cas_conflict"


@pytest.mark.asyncio
async def test_finalize_and_rollback_mask_db_errors_but_not_cancellation(
    tmp_path: Path,
) -> None:
    """Leaking DB details or swallowing cancellation in mutation phases must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    for method_name, args, reason_code in (
        (
            "finalize",
            (snapshot, _mutation_proof(snapshot), _task5_report(snapshot, rows)),
            "finalize_transaction_failed",
        ),
        (
            "rollback_before_finalize",
            (snapshot, _mutation_proof(snapshot)),
            "rollback_transaction_failed",
        ),
    ):
        secret = f"SENSITIVE_{method_name}_DB_DETAIL"
        factory, _session = _session_factory([RuntimeError(secret)])
        with pytest.raises(RepairSafetyError) as exc:
            await getattr(RepairStore(factory), method_name)(*args)
        assert exc.value.reason_code == reason_code
        assert secret not in repr(exc.value)
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None

        factory, _session = _session_factory([asyncio.CancelledError()])
        with pytest.raises(asyncio.CancelledError):
            await getattr(RepairStore(factory), method_name)(*args)


def _verification_results_with_target(
    snapshot: RepairSnapshot,
    rows: list[dict[str, object]],
    *,
    identity: dict[str, object] = _DATABASE_IDENTITY,
    alembic_head: str = _ALEMBIC_HEAD,
) -> list[MagicMock]:
    results = _verification_results(snapshot, rows)
    results[1] = _one_mapping_result(identity)
    results[2] = _scalar_result(alembic_head)
    return results


@pytest.mark.asyncio
async def test_verify_rejects_database_identity_drift_inside_read_transaction(
    tmp_path: Path,
) -> None:
    """Verifying rows from a database other than the snapshot target must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    factory, session = _session_factory(
        _verification_results_with_target(
            snapshot,
            rows,
            identity={**_DATABASE_IDENTITY, "database_name": "wrong"},
        )
    )

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).verify(snapshot, _task5_evidence(snapshot))

    assert exc.value.reason_code == "database_identity_mismatch"
    assert "current_database" in str(session.execute.await_args_list[1].args[0])
    assert len(session.execute.await_args_list) == 2


@pytest.mark.asyncio
async def test_verify_rejects_schema_drift_inside_read_transaction(
    tmp_path: Path,
) -> None:
    """Verifying on a schema head other than the snapshotted head must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    factory, session = _session_factory(
        _verification_results_with_target(
            snapshot,
            rows,
            alembic_head="038",
        )
    )

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).verify(snapshot, _task5_evidence(snapshot))

    assert exc.value.reason_code == "alembic_head_mismatch"
    assert "alembic_version" in str(session.execute.await_args_list[2].args[0])
    assert len(session.execute.await_args_list) == 3


@pytest.mark.asyncio
async def test_verify_rejects_migration_filename_instead_of_revision(
    tmp_path: Path,
) -> None:
    """Accepting the descriptive migration filename as head must make this test fail."""
    descriptive_head = "037_session_lifecycle_v4"
    snapshot = replace(
        _task5_snapshot(tmp_path),
        alembic_revision=descriptive_head,
    )
    rows = _current_plan_rows(snapshot)
    factory, session = _session_factory(
        _verification_results_with_target(
            snapshot,
            rows,
            alembic_head=descriptive_head,
        )
    )

    with pytest.raises(RepairSafetyError) as exc:
        await RepairStore(factory).verify(snapshot, _task5_evidence(snapshot))

    assert exc.value.reason_code == "alembic_head_mismatch"
    assert len(session.execute.await_args_list) == 3


@pytest.mark.asyncio
async def test_finalize_revalidates_report_again_inside_transaction_before_plan_cas(
    tmp_path: Path,
) -> None:
    """Relying only on the pre-session report validation must make this test fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    report = _task5_report(snapshot, rows)
    factory, session = _finalize_factory(snapshot, rows)
    original = plan_index_repair_store._validate_verification_report
    execute_counts: list[int] = []

    def tracked_validation(
        current_snapshot: RepairSnapshot,
        current_report: plan_index_repair.VerificationReport,
    ) -> None:
        execute_counts.append(len(session.execute.await_args_list))
        original(current_snapshot, current_report)

    with patch.object(
        plan_index_repair_store,
        "_validate_verification_report",
        side_effect=tracked_validation,
    ):
        result = await RepairStore(factory).finalize(
            snapshot,
            _mutation_proof(snapshot),
            report,
        )

    assert result.status == "finalized"
    assert execute_counts == [0, 4]


def _flatten_sql_parameter(value: object) -> list[object]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for nested in value for item in _flatten_sql_parameter(nested)]
    return [value]


def _compiled_parameter_values(statement: object) -> list[object]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return [item for value in compiled.params.values() for item in _flatten_sql_parameter(value)]


def _compiled_values_for_column(statement: object, column_name: str) -> list[object]:
    compiled = statement.compile(dialect=postgresql.dialect())
    prefix = f"{column_name}_"
    return [
        item
        for key, value in compiled.params.items()
        if key.startswith(prefix)
        for item in _flatten_sql_parameter(value)
    ]


def _compiled_exact_plan_tuples(
    statement: object,
) -> set[tuple[object, object, object, object]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    columns = ("id", "project_key", "file_path", "content_hash")
    grouped: dict[str, dict[str, object]] = {}
    for column_name in columns:
        prefix = f"{column_name}_"
        for key, value in compiled.params.items():
            if key.startswith(prefix):
                values = _flatten_sql_parameter(value)
                assert len(values) == 1
                grouped.setdefault(key.removeprefix(prefix), {})[column_name] = values[0]
    assert all(set(values) == set(columns) for values in grouped.values())
    return {
        (
            values["id"],
            values["project_key"],
            values["file_path"],
            values["content_hash"],
        )
        for values in grouped.values()
    }


@pytest.mark.asyncio
async def test_finalize_binds_exact_uuid_plan_ids_and_multi_link_predicates(
    tmp_path: Path,
) -> None:
    """Binding snapshot IDs as strings or broadening either delete must make this fail."""
    snapshot = _task5_snapshot(tmp_path, polluted_count=2)
    snapshot = replace(
        snapshot,
        feature_links=(
            *snapshot.feature_links,
            plan_index_repair.FeatureLinkRecord(
                feature_id=str(UUID(int=799)),
                plan_id=snapshot.polluted_plan_ids[0],
                similarity_score=0.5,
                created_at="2026-07-02T00:00:00+00:00",
            ),
        ),
    )
    assert len(snapshot.feature_links) == 3
    assert (
        sum(link.plan_id == snapshot.polluted_plan_ids[0] for link in snapshot.feature_links) == 2
    )
    rows = _current_plan_rows(snapshot)
    factory, session = _finalize_factory(snapshot, rows)

    result = await RepairStore(factory).finalize(
        snapshot,
        _mutation_proof(snapshot),
        _task5_report(snapshot, rows),
    )

    assert result.affected_rows == 2
    expected_ids = {UUID(plan_id) for plan_id in snapshot.polluted_plan_ids}
    plan_select = session.execute.await_args_list[4].args[0]
    assert {
        value for value in _compiled_parameter_values(plan_select) if isinstance(value, UUID)
    } == expected_ids
    deletes = [
        call.args[0]
        for call in session.execute.await_args_list
        if isinstance(call.args[0], sa.sql.dml.Delete)
    ]
    assert [statement.table.name for statement in deletes] == [
        "feature_artifacts",
        "indexed_plans",
    ]
    link_values = _compiled_parameter_values(deletes[0])
    assert {value for value in link_values if isinstance(value, UUID)} == expected_ids
    assert "plan" in link_values
    assert set(_compiled_values_for_column(deletes[0], "artifact_id")) == expected_ids
    assert _compiled_values_for_column(deletes[0], "artifact_type") == ["plan"]
    plan_values = _compiled_parameter_values(deletes[1])
    assert {value for value in plan_values if isinstance(value, UUID)} == expected_ids
    assert not {str(plan_id) for plan_id in expected_ids}.intersection(
        value for value in plan_values if isinstance(value, str)
    )
    plan_sql = str(deletes[1].compile(dialect=postgresql.dialect()))
    assert plan_sql.count("indexed_plans.id =") == 2
    assert plan_sql.count("indexed_plans.project_key =") == 2
    assert plan_sql.count("indexed_plans.file_path =") == 2
    assert plan_sql.count("indexed_plans.content_hash =") == 2
    expected_plan_tuples = {
        (UUID(plan.id), plan.project_key, plan.file_path, plan.content_hash)
        for plan in snapshot.indexed_plans
        if plan.id in snapshot.polluted_plan_ids
    }
    assert _compiled_exact_plan_tuples(deletes[1]) == expected_plan_tuples


@pytest.mark.asyncio
async def test_rollback_binds_snapshot_and_new_plan_ids_as_uuid(tmp_path: Path) -> None:
    """Binding rollback UUID columns from string snapshot values must make this fail."""
    snapshot = _task5_snapshot(tmp_path)
    rows = _current_plan_rows(snapshot)
    factory, session = _rollback_factory(snapshot, rows)

    await RepairStore(factory).rollback_before_finalize(
        snapshot,
        _mutation_proof(snapshot),
    )

    expected_polluted = {UUID(plan_id) for plan_id in snapshot.polluted_plan_ids}
    plan_select = session.execute.await_args_list[4].args[0]
    assert {
        value for value in _compiled_parameter_values(plan_select) if isinstance(value, UUID)
    } == expected_polluted
    deletes = [
        call.args[0]
        for call in session.execute.await_args_list
        if isinstance(call.args[0], sa.sql.dml.Delete)
    ]
    expected_new = {
        row["id"]
        for row in rows
        if row["file_path"] in {item.file_path for item in snapshot.missing_canonical_files}
    }
    assert {
        value for value in _compiled_parameter_values(deletes[0]) if isinstance(value, UUID)
    } == (expected_new)
    assert {
        value for value in _compiled_parameter_values(deletes[1]) if isinstance(value, UUID)
    } == (expected_new)
    assert set(_compiled_values_for_column(deletes[0], "artifact_id")) == expected_new
    assert _compiled_values_for_column(deletes[0], "artifact_type") == ["plan"]
    missing_paths = {item.file_path for item in snapshot.missing_canonical_files}
    expected_new_tuples = {
        (
            row["id"],
            row["project_key"],
            row["file_path"],
            row["content_hash"],
        )
        for row in rows
        if row["file_path"] in missing_paths
    }
    assert _compiled_exact_plan_tuples(deletes[1]) == expected_new_tuples


def _empty_new_plan_rollback_results(
    snapshot: RepairSnapshot,
    *,
    contexts: list[dict[str, object]],
    include_updates: bool,
) -> list[MagicMock]:
    results = [
        MagicMock(),
        _one_mapping_result(_DATABASE_IDENTITY),
        _scalar_result(_ALEMBIC_HEAD),
        _mapping_result(contexts),
        _mapping_result(_current_plan_rows(snapshot)),
    ]
    if include_updates:
        results.append(MagicMock())
        results.extend(
            _update_result(datetime.fromisoformat(str(context.values["updated_at"])))
            for context in sorted(snapshot.contexts, key=lambda item: item.project_key)
        )
    return results


@pytest.mark.asyncio
async def test_rollback_with_no_new_plans_restores_contexts_without_deletes(
    tmp_path: Path,
) -> None:
    """Issuing empty deletes or rejecting the first context-only rollback must make this fail."""
    snapshot = _task5_snapshot(
        tmp_path,
        preexisting_project_keys=frozenset(_PROJECT_KEYS),
    )
    assert snapshot.missing_canonical_files == ()
    factory, session = _session_factory(
        _empty_new_plan_rollback_results(
            snapshot,
            contexts=_post_update_rows(snapshot),
            include_updates=True,
        )
    )

    result = await RepairStore(factory).rollback_before_finalize(
        snapshot,
        _mutation_proof(snapshot),
    )

    assert result.status == "rolled_back"
    assert result.affected_rows == 7
    assert len(_update_calls(session)) == 7
    assert not any(
        isinstance(call.args[0], sa.sql.dml.Delete) for call in session.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_rollback_with_no_new_plans_replays_original_contexts_idempotently(
    tmp_path: Path,
) -> None:
    """Treating an empty-plan replay as a rollback conflict must make this fail."""
    snapshot = _task5_snapshot(
        tmp_path,
        preexisting_project_keys=frozenset(_PROJECT_KEYS),
    )
    original_contexts = [
        _context_row_for(project_key, ordinal) for ordinal, project_key in enumerate(_PROJECT_KEYS)
    ]
    factory, session = _session_factory(
        _empty_new_plan_rollback_results(
            snapshot,
            contexts=original_contexts,
            include_updates=False,
        )
    )

    result = await RepairStore(factory).rollback_before_finalize(
        snapshot,
        _mutation_proof(snapshot),
    )

    assert result.status == "already_rolled_back"
    assert result.affected_rows == 0
    assert _update_calls(session) == []
    assert not any(
        isinstance(call.args[0], sa.sql.dml.Delete) for call in session.execute.await_args_list
    )
