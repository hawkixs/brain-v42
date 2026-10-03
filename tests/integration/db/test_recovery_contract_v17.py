"""Prove v17 against a fresh 059 head and a real restored 058 source."""

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
CONTRACT_HEAD = "059"  # Each recovery contract must be checked against its own schema head.
V17_SQL = PROJECT_ROOT / "ops/recovery/brain-v42-v17.sql"
V17_JSON = PROJECT_ROOT / "ops/recovery/brain-v42-v17.json"
V17_PGRESTORE = PROJECT_ROOT / "ops/recovery/brain-v42-v17-pgrestore.sql"
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
            "--no-privileges",
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
    name = f"brain_v17_fresh_{uuid.uuid4().hex[:12]}"
    create_database(admin_url, name)
    url = swap_database(admin_url, name)
    try:
        _upgrade(url, CONTRACT_HEAD)
        yield url
    finally:
        drop_database(admin_url, name)


@pytest.fixture(scope="module")
def restored_head_db_url() -> Iterator[str]:
    _require_pg_tools()
    admin_url = _database_url_or_skip()
    archive = PROJECT_ROOT / f"brain_v17_restore_{uuid.uuid4().hex}.dump"
    source = f"brain_v17_src_{uuid.uuid4().hex[:12]}"
    target = f"brain_v17_tgt_{uuid.uuid4().hex[:12]}"
    create_database(admin_url, source)
    create_database(admin_url, target)
    source_url, target_url = swap_database(admin_url, source), swap_database(admin_url, target)
    try:
        _upgrade(source_url, "058")
        _dump(source_url, archive)
        run_sql(asyncpg_dsn(target_url), ["CREATE EXTENSION IF NOT EXISTS vector"])
        _restore(target_url, archive)
        _upgrade(target_url, CONTRACT_HEAD)
        yield target_url
    finally:
        archive.unlink(missing_ok=True)
        drop_database(admin_url, source)
        drop_database(admin_url, target)


def _assert_contract(failures: dict[str, dict[str, Any]], *, restored: bool) -> None:
    contract = json.loads(V17_JSON.read_text(encoding="utf-8"))
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
        assert extensions["expected"] == "plpgsql 1.0, vector 0.8.2"
        assert extensions["observed"] in {
            f"plpgsql 1.0, vector {version}" for version in RESTORE_BUILD_VECTOR_VERSIONS
        }


@pytest.mark.asyncio
async def test_recovery_contract_v17_matches_fresh_and_restored_head(
    fresh_head_db_url: str, restored_head_db_url: str
) -> None:
    _assert_contract(await replay_attestation(fresh_head_db_url, V17_SQL), restored=False)
    _assert_contract(await replay_attestation(restored_head_db_url, V17_PGRESTORE), restored=True)
