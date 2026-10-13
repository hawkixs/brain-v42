"""Prove v23 on live 065 and on both restore flows its twin must hold for.

- A source at 065, dumped and restored with --no-owner --no-acl: red-backup's drill.
- A source at 064, dumped, restored, then migrated to 065: a restore-marked
  production (red-base since the cutover) once `brain-v42-migrate` applies 065,
  where migrate proves the restored variant (R1-F1).

Dump and restore rewrite some CHECK texts, so the 065 tables must read the same
either way. Migration 064 may create brain_app NOLOGIN, never LOGIN or a password
here. v22's ACL delta is inherited; one mutation pair proves it covers the new
tables.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.disposable_db import (
    asyncpg_dsn,
    create_database,
    drop_database,
    replay_attestation,
    run_sql,
    swap_database,
)

pytestmark = pytest.mark.integration
PROJECT_ROOT = Path(__file__).parents[3]
CONTRACT_HEAD = "065"  # Each recovery contract must be checked against its own schema head.
V23_SQL = PROJECT_ROOT / "ops/recovery/brain-v42-v23.sql"
V23_JSON = PROJECT_ROOT / "ops/recovery/brain-v42-v23.json"
V23_PGRESTORE = PROJECT_ROOT / "ops/recovery/brain-v42-v23-pgrestore.sql"
DATA_CHECK_KINDS = frozenset({"row_count_sum_min"})
RESTORE_BUILD_VECTOR_VERSIONS = frozenset(
    json.loads(
        (PROJECT_ROOT / "ops/recovery/proven_vector_versions.json").read_text(encoding="utf-8")
    )["versions"]
)
PINNED_DISABLED_TRIGGER_DRIFT: dict[str, Any] = {
    "view_column_mismatches": 0,
    "view_option_mismatches": 0,
    "artifact_index_mismatches": 0,
    "ended_snapshot_mismatches": 0,
    "focus_revision_violations": 0,
    "session_column_mismatches": 0,
    "artifact_column_mismatches": 0,
    "runtime_trigger_mismatches": 1,
    "view_definition_mismatches": 0,
    "artifact_project_mismatches": 0,
    "session_constraint_mismatches": 0,
    "artifact_constraint_mismatches": 0,
}


def _database_url_or_skip() -> str:
    url = os.environ.get("BRAIN_V42_TEST_DB_URL")
    if not url:
        pytest.skip("BRAIN_V42_TEST_DB_URL is not set")
    return url


def _require_pg_tools() -> None:
    for tool in ("pg_dump", "pg_restore"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not available on this host")


def _upgrade(db_url: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        env={**os.environ, "POSTGRES_URL": db_url},
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(f"alembic upgrade {revision} failed:\n{result.stderr}\n{result.stdout}")


def _dump(source_url: str, archive: Path) -> None:
    result = subprocess.run(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--file",
            str(archive),
            asyncpg_dsn(source_url),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(f"pg_dump failed:\n{result.stderr}\n{result.stdout}")


def _restore(target_url: str, archive: Path) -> None:
    result = subprocess.run(
        [
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            asyncpg_dsn(target_url),
            str(archive),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(f"pg_restore failed:\n{result.stderr}\n{result.stdout}")


@pytest.fixture(scope="module")
def fresh_head_db_url() -> Iterator[str]:
    admin_url = _database_url_or_skip()
    name = f"brain_v23_fresh_{uuid.uuid4().hex[:12]}"
    create_database(admin_url, name)
    url = swap_database(admin_url, name)
    try:
        _upgrade(url, CONTRACT_HEAD)
        yield url
    finally:
        drop_database(admin_url, name)


def _restored_head_database(*, migrate_after: bool) -> Iterator[str]:
    """Dump a 064 source and restore it without owner or ACL replay.

    Without `migrate_after`, the source reaches 065 before the dump, as red-backup
    dumps a production already at 065. With it, the RESTORED database is migrated
    to 065, as `brain-v42-migrate` does on a restore-marked production.
    """
    _require_pg_tools()
    admin_url = _database_url_or_skip()
    archive = PROJECT_ROOT / f"brain_v23_restore_{uuid.uuid4().hex}.dump"
    source = f"brain_v23_src_{uuid.uuid4().hex[:12]}"
    target = f"brain_v23_tgt_{uuid.uuid4().hex[:12]}"
    create_database(admin_url, source)
    create_database(admin_url, target)
    source_url, target_url = swap_database(admin_url, source), swap_database(admin_url, target)
    try:
        _upgrade(source_url, "064")
        if not migrate_after:
            _upgrade(source_url, CONTRACT_HEAD)
        _dump(source_url, archive)
        run_sql(asyncpg_dsn(target_url), ["CREATE EXTENSION IF NOT EXISTS vector"])
        _restore(target_url, archive)
        # Keep the restored dump without reapplying owner ACLs: red-backup uses --no-acl.
        if migrate_after:
            _upgrade(target_url, CONTRACT_HEAD)
        yield target_url
    finally:
        archive.unlink(missing_ok=True)
        drop_database(admin_url, source)
        drop_database(admin_url, target)


@pytest.fixture(scope="module")
def restored_at_065_db_url() -> Iterator[str]:
    yield from _restored_head_database(migrate_after=False)


@pytest.fixture(scope="module")
def restored_064_migrated_db_url() -> Iterator[str]:
    yield from _restored_head_database(migrate_after=True)


def _assert_contract(failures: dict[str, dict[str, Any]], *, restored: bool) -> None:
    contract = json.loads(V23_JSON.read_text(encoding="utf-8"))
    kinds = {check["id"]: check.get("kind") for check in contract["checks"]}
    unexplained = {
        key: value
        for key, value in failures.items()
        if kinds.get(key) not in DATA_CHECK_KINDS
        and key not in {"brain_runtime_032_036_037", "extension_versions"}
    }
    assert not unexplained, json.dumps(unexplained, indent=2, default=str)
    runtime = failures.get("brain_runtime_032_036_037")
    assert runtime is not None
    assert runtime["observed"] == PINNED_DISABLED_TRIGGER_DRIFT
    if restored:
        assert "extension_versions" not in failures
    else:
        extensions = failures.get("extension_versions")
        assert extensions is not None
        assert extensions["expected"] == "pg_stat_statements 1.10, plpgsql 1.0, vector 0.8.2"
        assert extensions["observed"] in {
            f"pg_stat_statements 1.10, plpgsql 1.0, vector {version}"
            for version in RESTORE_BUILD_VECTOR_VERSIONS
        }


@pytest.mark.asyncio
async def test_recovery_contract_v23_matches_fresh_and_both_restore_flows(
    fresh_head_db_url: str, restored_at_065_db_url: str, restored_064_migrated_db_url: str
) -> None:
    _assert_contract(await replay_attestation(fresh_head_db_url, V23_SQL), restored=False)
    for restored_url in (restored_at_065_db_url, restored_064_migrated_db_url):
        _assert_contract(await replay_attestation(restored_url, V23_PGRESTORE), restored=True)


@pytest.mark.parametrize(
    "damage,repair,counter",
    [
        # The two 065 tables are under v22's inherited runtime ACL check.
        (
            "REVOKE INSERT ON public.programme_gates FROM brain_app",
            "GRANT INSERT ON public.programme_gates TO brain_app",
            "relation_mismatches",
        ),
        (
            "GRANT TRUNCATE ON public.programmes TO brain_app",
            "REVOKE TRUNCATE ON public.programmes FROM brain_app",
            "relation_mismatches",
        ),
    ],
)
@pytest.mark.asyncio
async def test_live_acl_delta_detects_real_database_scoped_damage(
    fresh_head_db_url: str, damage: str, repair: str, counter: str
) -> None:
    """Change only this disposable database's ACLs, never cluster role attributes."""
    baseline = await replay_attestation(fresh_head_db_url, V23_SQL)
    assert "runtime_acl_064" not in baseline
    engine = create_async_engine(fresh_head_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text(damage))
        try:
            failures = await replay_attestation(fresh_head_db_url, V23_SQL)
            assert failures["runtime_acl_064"]["observed"][counter] > 0
        finally:
            async with engine.begin() as connection:
                await connection.execute(sa.text(repair))
        assert "runtime_acl_064" not in await replay_attestation(fresh_head_db_url, V23_SQL)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_restored_contract_does_not_require_application_acls(
    restored_at_065_db_url: str,
) -> None:
    failures = await replay_attestation(restored_at_065_db_url, V23_SQL)
    assert "runtime_acl_064" in failures, "--no-acl must actually remove the runtime grants"
    restored = await replay_attestation(restored_at_065_db_url, V23_PGRESTORE)
    _assert_contract(restored, restored=True)
