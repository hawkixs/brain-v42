"""Replay measured v27 against fresh 069 and real restores from 068 and 069."""

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from tests.integration.db.test_recovery_contract_v26 import (
    DATA_CHECK_KINDS,
    PINNED_DISABLED_TRIGGER_DRIFT,
    PROJECT_ROOT,
    RESTORE_BUILD_VECTOR_VERSIONS,
    _database_url_or_skip,
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


@pytest.fixture(scope="module")
def fresh_069_db_url() -> Iterator[str]:
    admin = _database_url_or_skip()
    database = f"brain_v27_fresh_{uuid4().hex[:12]}"
    create_database(admin, database)
    try:
        url = swap_database(admin, database)
        _upgrade(url, "069")
        yield url
    finally:
        drop_database(admin, database)


@pytest.fixture(params=["068", "069"])
def restored_069_db_url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    _require_pg_tools()
    admin = _database_url_or_skip()
    source_revision = request.param
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
            yield target_url
        finally:
            drop_database(admin, target)
    finally:
        drop_database(admin, source)


@pytest.mark.asyncio
async def test_v27_matches_a_fresh_069_database(fresh_069_db_url: str) -> None:
    assert_contract(
        await replay_attestation(fresh_069_db_url, RECOVERY / "brain-v42-v27.sql"),
        restored=False,
    )


@pytest.mark.asyncio
async def test_v27_matches_real_pgrestore_then_migration(restored_069_db_url: str) -> None:
    assert_contract(
        await replay_attestation(restored_069_db_url, RECOVERY / "brain-v42-v27-pgrestore.sql"),
        restored=True,
    )
