"""Real-PostgreSQL proofs for bounded, isolated plan-index repair transactions.

The shared integration fixture accepts only ``BRAIN_V42_TEST_DB_URL`` and skips
before creating an engine when it is absent.  Every row below has a randomized
``integ-plan-repair-*`` owner, which the shared cleanup fixture may delete safely.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    feature_artifacts,
    features,
    indexed_plan_chunks,
    indexed_plans,
    project_contexts,
)
from brain_v42.maintenance import plan_index_repair as repair
from brain_v42.maintenance import plan_index_repair_store as repair_store

pytestmark = pytest.mark.integration

_PROJECT_COUNT = 7


@dataclass(frozen=True, slots=True)
class _SeededRows:
    project_keys: tuple[str, ...]
    polluted_ids: dict[str, UUID]
    feature_ids: dict[str, UUID]
    control_project_key: str
    control_plan_id: UUID
    control_feature_id: UUID


@dataclass(frozen=True, slots=True)
class _DatabaseState:
    """Complete deterministic state for every test-owned row and control row."""

    contexts: tuple[dict[str, object], ...]
    plans: tuple[dict[str, object], ...]
    chunks: tuple[dict[str, object], ...]
    links: tuple[dict[str, object], ...]
    counts: tuple[int, int, int, int]


def _normalize_snapshot_value(value: object) -> object:
    """Make database values deterministic and safe for equality assertions."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _normalize_snapshot_value(value[key]) for key in sorted(value, key=str)}
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return _normalize_snapshot_value(tolist())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_normalize_snapshot_value(item) for item in value)
    return value


def _normalized_row(row: Mapping[str, object]) -> dict[str, object]:
    """Normalize every column and assert the local equality oracle is idempotent."""
    normalized = _normalize_snapshot_value(row)
    assert isinstance(normalized, dict)
    assert _normalize_snapshot_value(normalized) == normalized
    return normalized


def _test_projects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[repair.RepairManifest, tuple[repair.LocalPlanFile, ...]]:
    """Create seven test-only roots and make both repair modules own only them."""
    run_token = uuid4().hex[:8]
    project_keys = tuple(
        f"integ-plan-repair-{run_token}-{index}" for index in range(_PROJECT_COUNT)
    )
    payload_projects: list[dict[str, object]] = []
    trusted_projects: dict[str, repair.ProjectTarget] = {}
    for project_key in project_keys:
        project_root = tmp_path / project_key
        scan_path = project_root / "docs" / "plans"
        scan_path.mkdir(parents=True)
        (scan_path / f"{project_key}-plan.md").write_text(
            f"# Canonical {project_key}\n",
            encoding="utf-8",
        )
        trusted_projects[project_key] = repair.ProjectTarget(
            project_key=project_key,
            project_root=project_root,
            scan_paths=(scan_path,),
        )
        payload_projects.append(
            {
                "project_key": project_key,
                "project_root": str(project_root),
                "scan_paths": [str(scan_path)],
            }
        )

    allowed_project_keys = frozenset(project_keys)
    monkeypatch.setattr(repair, "TARGET_PROJECTS", trusted_projects)
    monkeypatch.setattr(repair, "TARGET_PROJECT_KEYS", allowed_project_keys)
    monkeypatch.setattr(repair_store, "TARGET_PROJECT_KEYS", allowed_project_keys)
    assert set(repair.TARGET_PROJECTS) == repair.TARGET_PROJECT_KEYS
    assert repair.TARGET_PROJECT_KEYS == repair_store.TARGET_PROJECT_KEYS == allowed_project_keys
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"version": 1, "projects": payload_projects}),
        encoding="utf-8",
    )
    manifest = repair.load_manifest(
        manifest_path,
        allowed_project_keys=allowed_project_keys,
    )
    return manifest, repair.discover_local_files(manifest)


async def _seed_polluted_rows(
    session_factory: async_sessionmaker[AsyncSession],
    manifest: repair.RepairManifest,
) -> _SeededRows:
    project_keys = tuple(project.project_key for project in manifest.projects)
    polluted_ids = {project_key: uuid4() for project_key in project_keys}
    feature_ids = {project_key: uuid4() for project_key in project_keys}
    control_project_key = f"integ-control-{uuid4().hex[:8]}"
    control_plan_id = uuid4()
    control_feature_id = uuid4()
    control_file_path = (
        manifest.projects[0].project_root.parent / f"{control_project_key}-control.md"
    )
    control_file_path.write_text("# Unrelated control\n", encoding="utf-8")
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert(),
            [
                {
                    "project_key": project_key,
                    "name": f"Repair {project_key}",
                    "description": "transactional repair integration proof",
                    "plan_scan_paths": ["docs/plans"],
                    "metadata": {"owner": project_key, "sentinel": "untouched"},
                }
                for project_key in project_keys
            ],
        )
        await session.execute(
            project_contexts.insert(),
            [
                {
                    "project_key": control_project_key,
                    "name": "Control context",
                    "description": "unrelated integration control",
                    "plan_scan_paths": ["docs/control"],
                    "metadata": {"owner": control_project_key, "sentinel": "control"},
                }
            ],
        )
        await session.execute(
            features.insert(),
            [
                {
                    "id": feature_ids[project_key],
                    "project_key": project_key,
                    "name": f"Feature {project_key}",
                    "description": "plan artifact integration proof",
                }
                for project_key in project_keys
            ]
            + [
                {
                    "id": control_feature_id,
                    "project_key": control_project_key,
                    "name": "Control feature",
                    "description": "unrelated integration control",
                }
            ],
        )
        await session.execute(
            indexed_plans.insert(),
            [
                {
                    "id": polluted_ids[project_key],
                    "project_key": project_key,
                    "file_path": f"docs/plans/{project_key}-legacy-plan.md",
                    "title": f"Legacy {project_key}",
                    "plan_type": "plan",
                    "content_hash": hashlib.sha256(f"legacy:{project_key}".encode()).hexdigest(),
                    "content": f"# Legacy {project_key}\n",
                    "status": "active",
                    "freshness_status": "fresh",
                    "chunk_count": 1,
                }
                for project_key in project_keys
            ]
            + [
                {
                    "id": control_plan_id,
                    "project_key": control_project_key,
                    "file_path": str(control_file_path),
                    "title": "Unrelated control",
                    "plan_type": "plan",
                    "content_hash": hashlib.sha256(b"unrelated-control").hexdigest(),
                    "content": "# Unrelated control\n",
                    "status": "active",
                    "freshness_status": "fresh",
                    "chunk_count": 1,
                }
            ],
        )
        await session.execute(
            indexed_plan_chunks.insert(),
            [
                {
                    "plan_id": polluted_ids[project_key],
                    "section_title": "Legacy",
                    "section_path": "Legacy",
                    "content": f"Legacy chunk {project_key}",
                    "section_order": 0,
                    "word_count": 3,
                    "embedding": [0.0] * 1536,
                    "project_key": project_key,
                    "plan_type": "plan",
                    "status": "active",
                }
                for project_key in project_keys
            ]
            + [
                {
                    "plan_id": control_plan_id,
                    "section_title": "Control",
                    "section_path": "Control",
                    "content": "Unrelated control chunk",
                    "section_order": 0,
                    "word_count": 3,
                    "embedding": [0.0] * 1536,
                    "project_key": control_project_key,
                    "plan_type": "plan",
                    "status": "active",
                }
            ],
        )
        await session.execute(
            feature_artifacts.insert(),
            [
                {
                    "feature_id": feature_ids[project_key],
                    "artifact_type": "plan",
                    "artifact_id": polluted_ids[project_key],
                    "similarity_score": 0.85,
                }
                for project_key in project_keys
            ]
            + [
                {
                    "feature_id": control_feature_id,
                    "artifact_type": "plan",
                    "artifact_id": control_plan_id,
                    "similarity_score": 0.5,
                }
            ],
        )
    return _SeededRows(
        project_keys,
        polluted_ids,
        feature_ids,
        control_project_key,
        control_plan_id,
        control_feature_id,
    )


async def _seed_canonical_rows(
    session_factory: async_sessionmaker[AsyncSession],
    local_files: tuple[repair.LocalPlanFile, ...],
    seeded: _SeededRows,
) -> dict[str, UUID]:
    canonical_ids = {item.project_key: uuid4() for item in local_files}
    async with session_factory.begin() as session:
        await session.execute(
            indexed_plans.insert(),
            [
                {
                    "id": canonical_ids[item.project_key],
                    "project_key": item.project_key,
                    "file_path": item.file_path,
                    "title": f"Canonical {item.project_key}",
                    "plan_type": "plan",
                    "content_hash": item.content_hash,
                    "content": Path(item.file_path).read_text(encoding="utf-8"),
                    "status": "active",
                    "freshness_status": "fresh",
                    "chunk_count": 1,
                }
                for item in local_files
            ],
        )
        await session.execute(
            indexed_plan_chunks.insert(),
            [
                {
                    "plan_id": canonical_ids[item.project_key],
                    "section_title": "Canonical",
                    "section_path": "Canonical",
                    "content": f"Canonical chunk {item.project_key}",
                    "section_order": 0,
                    "word_count": 3,
                    "embedding": [0.0] * 1536,
                    "project_key": item.project_key,
                    "plan_type": "plan",
                    "status": "active",
                }
                for item in local_files
            ],
        )
        await session.execute(
            feature_artifacts.insert(),
            [
                {
                    "feature_id": seeded.feature_ids[item.project_key],
                    "artifact_type": "plan",
                    "artifact_id": canonical_ids[item.project_key],
                    "similarity_score": 0.9,
                }
                for item in local_files
            ],
        )
    return canonical_ids


def _proof(snapshot: repair.RepairSnapshot) -> repair.MutationProof:
    return repair.MutationProof(
        snapshot_sha256=repair.sha256_json(snapshot.to_dict()),
        backup_receipt_sha256="b" * 64,
        postgres_restore_tested=True,
        writers_off_confirmed=True,
    )


def _evidence(
    snapshot: repair.RepairSnapshot,
    project_keys: tuple[str, ...],
) -> repair.ReindexEvidence:
    return repair.ReindexEvidence(
        version=1,
        snapshot_sha256=repair.sha256_json(snapshot.to_dict()),
        projects=tuple(
            repair.ProjectReindexStats(
                project_key=project_key,
                indexed=1,
                skipped=0,
                linked=1,
                errors=0,
                chunks_created=1,
            )
            for project_key in sorted(project_keys)
        ),
    )


def _scoped_project_keys(seeded: _SeededRows) -> tuple[str, ...]:
    return (*seeded.project_keys, seeded.control_project_key)


async def _database_state(
    session_factory: async_sessionmaker[AsyncSession],
    seeded: _SeededRows,
) -> _DatabaseState:
    project_keys = _scoped_project_keys(seeded)
    async with session_factory() as session:
        contexts = tuple(
            _normalized_row(dict(row))
            for row in (
                await session.execute(
                    sa.select(*project_contexts.c)
                    .where(project_contexts.c.project_key.in_(project_keys))
                    .order_by(project_contexts.c.project_key)
                )
            )
            .mappings()
            .all()
        )
        plans = tuple(
            _normalized_row(dict(row))
            for row in (
                await session.execute(
                    sa.select(*indexed_plans.c)
                    .where(indexed_plans.c.project_key.in_(project_keys))
                    .order_by(indexed_plans.c.id)
                )
            )
            .mappings()
            .all()
        )
        plan_ids = sa.select(indexed_plans.c.id).where(
            indexed_plans.c.project_key.in_(project_keys)
        )
        chunks = tuple(
            _normalized_row(dict(row))
            for row in (
                await session.execute(
                    sa.select(*indexed_plan_chunks.c)
                    .where(indexed_plan_chunks.c.plan_id.in_(plan_ids))
                    .order_by(indexed_plan_chunks.c.id)
                )
            )
            .mappings()
            .all()
        )
        feature_ids = sa.select(features.c.id).where(features.c.project_key.in_(project_keys))
        links = tuple(
            _normalized_row(dict(row))
            for row in (
                await session.execute(
                    sa.select(*feature_artifacts.c)
                    .where(
                        sa.or_(
                            feature_artifacts.c.artifact_id.in_(plan_ids),
                            feature_artifacts.c.feature_id.in_(feature_ids),
                        )
                    )
                    .order_by(feature_artifacts.c.artifact_id, feature_artifacts.c.feature_id)
                )
            )
            .mappings()
            .all()
        )
    return _DatabaseState(
        contexts=contexts,
        plans=plans,
        chunks=chunks,
        links=links,
        counts=(len(contexts), len(plans), len(chunks), len(links)),
    )


async def _cleanup_seeded_rows(
    session_factory: async_sessionmaker[AsyncSession],
    seeded: _SeededRows,
) -> None:
    project_keys = _scoped_project_keys(seeded)
    async with session_factory.begin() as session:
        plan_ids = sa.select(indexed_plans.c.id).where(
            indexed_plans.c.project_key.in_(project_keys)
        )
        feature_ids = sa.select(features.c.id).where(features.c.project_key.in_(project_keys))
        await session.execute(
            feature_artifacts.delete().where(
                sa.or_(
                    feature_artifacts.c.artifact_id.in_(plan_ids),
                    feature_artifacts.c.feature_id.in_(feature_ids),
                )
            )
        )
        await session.execute(
            indexed_plans.delete().where(indexed_plans.c.project_key.in_(project_keys))
        )
        await session.execute(features.delete().where(features.c.project_key.in_(project_keys)))
        await session.execute(
            project_contexts.delete().where(project_contexts.c.project_key.in_(project_keys))
        )


@pytest.fixture
async def repair_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[list[_SeededRows]]:
    seeded_rows: list[_SeededRows] = []
    try:
        yield seeded_rows
    finally:
        for seeded in reversed(seeded_rows):
            await _cleanup_seeded_rows(session_factory, seeded)


def _without_polluted_rows(
    state: _DatabaseState,
    polluted_ids: dict[str, UUID],
) -> _DatabaseState:
    polluted = frozenset(str(plan_id) for plan_id in polluted_ids.values())
    plans = tuple(row for row in state.plans if row["id"] not in polluted)
    chunks = tuple(row for row in state.chunks if row["plan_id"] not in polluted)
    links = tuple(row for row in state.links if row["artifact_id"] not in polluted)
    return _DatabaseState(
        contexts=state.contexts,
        plans=plans,
        chunks=chunks,
        links=links,
        counts=(len(state.contexts), len(plans), len(chunks), len(links)),
    )


@pytest.mark.asyncio
async def test_inventory_is_read_only(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_cleanup: list[_SeededRows],
) -> None:
    manifest, local_files = _test_projects(monkeypatch, tmp_path)
    seeded = await _seed_polluted_rows(session_factory, manifest)
    repair_cleanup.append(seeded)
    before = await _database_state(session_factory, seeded)
    # The property under test is "read-only", not "revision equals a fixed
    # string" — a hardcoded head recasts every future migration as a test
    # failure here (observed: "041" broke silently at head 045, four
    # migrations later, with nothing in this file pointing at why). Read the
    # actual head straight from the same database inventory() reads from.
    async with session_factory() as session:
        expected_revision = (
            await session.execute(sa.text("SELECT version_num FROM alembic_version"))
        ).scalar_one()

    snapshot = await repair_store.RepairStore(session_factory).inventory(manifest, local_files)

    assert before.counts == (8, 8, 8, 8)
    assert await _database_state(session_factory, seeded) == before
    assert snapshot.alembic_revision == expected_revision
    assert len(snapshot.polluted_plan_ids) == _PROJECT_COUNT


@pytest.mark.asyncio
async def test_apply_cas_drift_rolls_back(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_cleanup: list[_SeededRows],
) -> None:
    manifest, local_files = _test_projects(monkeypatch, tmp_path)
    seeded = await _seed_polluted_rows(session_factory, manifest)
    repair_cleanup.append(seeded)
    store = repair_store.RepairStore(session_factory)
    snapshot = await store.inventory(manifest, local_files)
    drifted_key = seeded.project_keys[-1]
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.update()
            .where(project_contexts.c.project_key == drifted_key)
            .values(metadata={"owner": drifted_key, "sentinel": "concurrent-drift"})
        )
    drifted_state = await _database_state(session_factory, seeded)

    with pytest.raises(repair.RepairSafetyError, match="^context_cas_conflict$"):
        await store.apply_paths(snapshot, _proof(snapshot))

    assert await _database_state(session_factory, seeded) == drifted_state


@pytest.mark.asyncio
async def test_apply_changes_only_paths_and_timestamp(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_cleanup: list[_SeededRows],
) -> None:
    manifest, local_files = _test_projects(monkeypatch, tmp_path)
    seeded = await _seed_polluted_rows(session_factory, manifest)
    repair_cleanup.append(seeded)
    store = repair_store.RepairStore(session_factory)
    before = await _database_state(session_factory, seeded)
    snapshot = await store.inventory(manifest, local_files)

    result = await store.apply_paths(snapshot, _proof(snapshot))

    assert result.status == "applied"
    assert result.affected_rows == _PROJECT_COUNT
    expected_paths = {
        project.project_key: tuple(str(path) for path in project.scan_paths)
        for project in manifest.projects
    }
    after = await _database_state(session_factory, seeded)
    assert after.plans == before.plans
    assert after.chunks == before.chunks
    assert after.links == before.links
    assert after.counts == before.counts
    before_contexts = {str(row["project_key"]): row for row in before.contexts}
    for row in after.contexts:
        project_key = str(row["project_key"])
        original = before_contexts[project_key]
        if project_key == seeded.control_project_key:
            assert row == original
            continue
        assert row["plan_scan_paths"] == expected_paths[project_key]
        assert {
            key: value for key, value in row.items() if key not in {"plan_scan_paths", "updated_at"}
        } == {
            key: value
            for key, value in original.items()
            if key not in {"plan_scan_paths", "updated_at"}
        }
        assert row["updated_at"] == snapshot.mutation_timestamp


@pytest.mark.asyncio
async def test_finalize_removes_exactly_polluted_rows_with_chunk_cascade(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_cleanup: list[_SeededRows],
) -> None:
    manifest, local_files = _test_projects(monkeypatch, tmp_path)
    seeded = await _seed_polluted_rows(session_factory, manifest)
    repair_cleanup.append(seeded)
    store = repair_store.RepairStore(session_factory)
    snapshot = await store.inventory(manifest, local_files)
    await store.apply_paths(snapshot, _proof(snapshot))
    canonical_ids = await _seed_canonical_rows(session_factory, local_files, seeded)
    report = await store.verify(snapshot, _evidence(snapshot, seeded.project_keys))
    before_finalize = await _database_state(session_factory, seeded)

    result = await store.finalize(snapshot, _proof(snapshot), report)

    assert result.status == "finalized"
    assert result.affected_rows == _PROJECT_COUNT
    assert set(canonical_ids) == set(seeded.project_keys)
    after_finalize = await _database_state(session_factory, seeded)
    assert after_finalize == _without_polluted_rows(before_finalize, seeded.polluted_ids)
    assert after_finalize.counts == (8, 8, 8, 8)


@pytest.mark.asyncio
async def test_seventh_update_failure_rolls_back_every_context(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair_cleanup: list[_SeededRows],
) -> None:
    manifest, local_files = _test_projects(monkeypatch, tmp_path)
    seeded = await _seed_polluted_rows(session_factory, manifest)
    repair_cleanup.append(seeded)
    store = repair_store.RepairStore(session_factory)
    snapshot = await store.inventory(manifest, local_files)
    before = await _database_state(session_factory, seeded)
    original_execute = AsyncSession.execute
    delegated_execute = cast(Any, original_execute)
    updates = 0

    async def fail_seventh_update(
        session: AsyncSession,
        statement: sa.Executable,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal updates
        if isinstance(statement, sa.sql.dml.Update) and statement.table is project_contexts:
            updates += 1
            if updates == _PROJECT_COUNT:
                raise RuntimeError("test seventh context update failure")
        return await delegated_execute(session, statement, *args, **kwargs)

    with monkeypatch.context() as patched_execute:
        patched_execute.setattr(AsyncSession, "execute", fail_seventh_update)
        with pytest.raises(
            repair.RepairSafetyError,
            match="^apply_paths_transaction_failed$",
        ):
            await store.apply_paths(snapshot, _proof(snapshot))

    assert updates == _PROJECT_COUNT
    assert await _database_state(session_factory, seeded) == before
