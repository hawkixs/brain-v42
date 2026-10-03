"""The v15 contract passes against a fresh 058 head AND a real restore of it.

`test_fresh_head_is_the_yardstick.py` proves the v14 candidate against the
alembic chain alone, and says so: what it still does not prove is the
`pg_dump`/`pg_restore` round-trip itself. This module closes that gap for v15,
and it has to, because 058 changes a constraint whose `IN (...)` definition
`pg_restore` re-serialises — the exact failure class ticket `1ec33903` caught
on `knowledge_claim_current` (055).

Two targets, two receipts, both measured here rather than assumed:

* the BASE asset, replayed against a disposable database the alembic chain
  built at head 058;
* the `-pgrestore` twin, replayed against a database built the same way the
  mint built its twin: a custom-format `pg_dump` of a disposable 057 source,
  restored into an isolated disposable target, then migrated to 058 with the
  installed wheel.

The only non-data failures a fresh or restored-but-empty database can carry are
the disabled-trigger pin inherited from 050 (`project_contexts_focus_history_required`
ships disabled, production armed it, no chain-built or chain-sourced database
ever arms it) and, for the BASE only, the extension-version pin (a fresh head
reports the hosting server's `vector` build, not production's `0.8.2`). Everything
else must pass — zero unexplained structural mismatches.
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
CONTRACT_HEAD = "058"  # Each recovery contract must be checked against its own schema head.
V15_SQL = PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v15.sql"
V15_JSON = PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v15.json"
V15_PGRESTORE = PROJECT_ROOT / "ops" / "recovery" / "brain-v42-v15-pgrestore.sql"

#: The contract checks that attest the DATA carried by a restoration. A fresh
#: database is empty by construction: they cannot pass here and that is not a
#: drift.
DATA_CHECK_KINDS = frozenset({"row_count_sum_min"})

#: 050 ships `project_contexts_focus_history_required` DISABLED at birth and
#: arming it is a separate operator gesture (performed 2026-09-02). The contract
#: demands the ARMED form, so every chain-built or chain-sourced database reads
#: one runtime-trigger mismatch. Pinned at its exact value, not absorbed.
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

#: The BASE asset pins the inventory production DECLARES (`vector 0.8.2`); any
#: fresh database declares its image's build. The observed value therefore
#: depends on the server hosting the disposable database, not on the alembic
#: chain: a closed band, not a single value.
RESTORE_BUILD_VECTOR_VERSIONS = frozenset(
    json.loads(
        (PROJECT_ROOT / "ops" / "recovery" / "proven_vector_versions.json").read_text(
            encoding="utf-8"
        )
    )["versions"]
)


def _database_url_or_skip() -> str:
    url = os.environ.get("BRAIN_V42_TEST_DB_URL")
    if not url:
        pytest.skip("BRAIN_V42_TEST_DB_URL is not set")
    return url


def _require_pg_tools() -> None:
    for tool in ("pg_dump", "pg_restore"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not available on this host")


def _alembic_upgrade_to(db_url: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        env={**os.environ, "POSTGRES_URL": db_url},
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade {revision} failed:\n{result.stderr}\n{result.stdout}")


def _pg_dump(source_url: str, archive: Path) -> None:
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
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed:\n{result.stderr}\n{result.stdout}")


def _pg_restore(target_url: str, archive: Path) -> None:
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
    if result.returncode != 0:
        raise RuntimeError(f"pg_restore failed:\n{result.stderr}\n{result.stdout}")


@pytest.fixture(scope="module")
def fresh_head_db_url() -> Iterator[str]:
    """A pristine disposable database upgraded to this contract's 058 head."""
    admin_url = _database_url_or_skip()
    name = f"brain_v15_fresh_{uuid.uuid4().hex[:12]}"
    create_database(admin_url, name)
    url = swap_database(admin_url, name)
    try:
        _alembic_upgrade_to(url, CONTRACT_HEAD)
        yield url
    finally:
        drop_database(admin_url, name)


@pytest.fixture(scope="module")
def restored_head_db_url() -> Iterator[str]:
    """A real `pg_dump`/`pg_restore` of a 057 source, then migrated to 058.

    The same recipe the v15 mint uses for its twin: a custom-format dump of a
    chain-built 057 database, restored into an isolated target, then upgraded to
    058 — so the fingerprints measured here match the ones the mint measured.
    """
    _require_pg_tools()
    admin_url = _database_url_or_skip()
    archive = PROJECT_ROOT / f"brain_v15_restore_{uuid.uuid4().hex}.dump"
    source = f"brain_v15_src_{uuid.uuid4().hex[:12]}"
    target = f"brain_v15_tgt_{uuid.uuid4().hex[:12]}"
    create_database(admin_url, source)
    create_database(admin_url, target)
    source_url = swap_database(admin_url, source)
    target_url = swap_database(admin_url, target)
    try:
        _alembic_upgrade_to(source_url, "057")
        _pg_dump(source_url, archive)
        # The vector type must exist before the restore emits the columns that
        # use it; `pg_dump` may or may not carry `CREATE EXTENSION` itself.
        run_sql(asyncpg_dsn(target_url), ["CREATE EXTENSION IF NOT EXISTS vector"])
        _pg_restore(target_url, archive)
        _alembic_upgrade_to(target_url, CONTRACT_HEAD)
        yield target_url
    finally:
        archive.unlink(missing_ok=True)
        drop_database(admin_url, source)
        drop_database(admin_url, target)


def _kinds(contract: dict[str, Any]) -> dict[str, str]:
    return {check["id"]: check.get("kind") for check in contract["checks"]}


def _assert_no_unexplained(
    failures: dict[str, dict[str, Any]],
    kinds: dict[str, str],
    *,
    exempt_extension: bool,
    target: str,
) -> None:
    unexplained = {
        check_id: failure
        for check_id, failure in failures.items()
        if kinds.get(check_id) not in DATA_CHECK_KINDS
        and check_id not in {"brain_runtime_032_036_037", "extension_versions"}
    }
    assert not unexplained, (
        f"the v15 {target} receipt disagrees with its database beyond the pinned "
        f"drift:\n{json.dumps(unexplained, indent=2, default=str)}"
    )
    runtime = failures.get("brain_runtime_032_036_037")
    assert runtime is not None, (
        f"the v15 {target} receipt now matches on the runtime triggers: either 050 "
        "stopped shipping project_contexts_focus_history_required disabled, or the "
        "pin was minted wrong — re-measure before removing it"
    )
    assert runtime["observed"] == PINNED_DISABLED_TRIGGER_DRIFT, (
        f"the v15 {target} runtime divergence from a fresh head MOVED:\n"
        f"pinned:   {json.dumps(PINNED_DISABLED_TRIGGER_DRIFT, sort_keys=True)}\n"
        f"observed: {json.dumps(runtime['observed'], sort_keys=True)}"
    )
    if exempt_extension:
        extensions = failures.get("extension_versions")
        assert extensions is not None, (
            f"the v15 {target} receipt now passes extension_versions against a fresh "
            "head — re-measure before removing its pin"
        )
        assert extensions["expected"] == "plpgsql 1.0, vector 0.8.2"
        assert extensions["observed"] in {
            f"plpgsql 1.0, vector {version}" for version in RESTORE_BUILD_VECTOR_VERSIONS
        }


@pytest.mark.asyncio
async def test_recovery_contract_v15_matches_fresh_and_restored_head(
    fresh_head_db_url: str, restored_head_db_url: str
) -> None:
    """Base on a fresh 058 head; twin on a real restored-then-migrated 058."""
    contract = json.loads(V15_JSON.read_text(encoding="utf-8"))
    kinds = _kinds(contract)

    base_failures = await replay_attestation(fresh_head_db_url, V15_SQL)
    _assert_no_unexplained(base_failures, kinds, exempt_extension=True, target="base")

    twin_failures = await replay_attestation(restored_head_db_url, V15_PGRESTORE)
    _assert_no_unexplained(twin_failures, kinds, exempt_extension=False, target="-pgrestore")

    # The twin only requires the extension NAMES: unlike the base asset, it MUST
    # pass this check on a restored database.
    assert "extension_versions" not in twin_failures, (
        "the twin now judges extension VERSIONS — the names-only rule was lost"
    )
