"""Replay measured v27 against fresh 069 and real restores from 068 and 069."""

import json
import os
from uuid import uuid4

import pytest

from tests.integration.db.test_recovery_contract_v26 import (
    DATA_CHECK_KINDS,
    PINNED_DISABLED_TRIGGER_DRIFT,
    PROJECT_ROOT,
    RESTORE_BUILD_VECTOR_VERSIONS,
    _dump,
    _require_pg_tools,
    _restore,
    _upgrade,
)
from tests.integration.disposable_db import (
    asyncpg_dsn,
    create_database,
    drop_database,
    replay_attestation,
    run_sql,
    swap_database,
)

pytestmark = pytest.mark.integration
RECOVERY = PROJECT_ROOT / "ops/recovery"


def assert_contract(failures, *, restored):
    manifest = json.loads((RECOVERY / "brain-v42-v27.json").read_text())
    kinds = {check["id"]: check["kind"] for check in manifest["checks"]}
    non_data = {
        key: value for key, value in failures.items() if kinds.get(key) not in DATA_CHECK_KINDS
    }
    expected = {"brain_runtime_032_036_037"}
    if not restored and "extension_versions" in non_data:
        expected.add("extension_versions")
        assert (
            non_data["extension_versions"]["expected"]
            == "pg_stat_statements 1.10, plpgsql 1.0, vector 0.8.2"
        )
        assert non_data["extension_versions"]["observed"] in {
            f"pg_stat_statements 1.10, plpgsql 1.0, vector {v}"
            for v in RESTORE_BUILD_VECTOR_VERSIONS
        }
    assert set(non_data) == expected
    assert non_data["brain_runtime_032_036_037"]["observed"] == PINNED_DISABLED_TRIGGER_DRIFT


async def test_v27_matches_a_fresh_069_database():
    admin = os.environ["BRAIN_V42_TEST_DB_URL"]
    database = f"brain_v27_fresh_{uuid4().hex[:12]}"
    create_database(admin, database)
    try:
        url = swap_database(admin, database)
        _upgrade(url, "069")
        assert_contract(
            await replay_attestation(url, RECOVERY / "brain-v42-v27.sql"), restored=False
        )
    finally:
        drop_database(admin, database)


@pytest.mark.parametrize("source_revision", ["068", "069"])
async def test_v27_matches_real_pgrestore_then_migration(source_revision, tmp_path):
    _require_pg_tools()
    admin = os.environ["BRAIN_V42_TEST_DB_URL"]
    source, target = (f"brain_v27_{tag}_{uuid4().hex[:12]}" for tag in ("src", "dst"))
    create_database(admin, source)
    try:
        create_database(admin, target)
        try:
            source_url, target_url = swap_database(admin, source), swap_database(admin, target)
            _upgrade(source_url, source_revision)
            archive = tmp_path / "source.dump"
            _dump(source_url, archive)
            run_sql(asyncpg_dsn(target_url), ["CREATE EXTENSION IF NOT EXISTS vector"])
            _restore(target_url, archive)
            if source_revision != "069":
                _upgrade(target_url, "069")
            assert_contract(
                await replay_attestation(target_url, RECOVERY / "brain-v42-v27-pgrestore.sql"),
                restored=True,
            )
        finally:
            drop_database(admin, target)
    finally:
        drop_database(admin, source)
