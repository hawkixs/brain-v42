"""The v26 mint accepts only the declared 068 delta on disposable databases."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

TABLES = ("adrs", "decisions", "features", "learnings", "runbooks", "snippets")
DECLARED_ADDITIONS = {
    "expected_table_constraints": {(table, f"{table}_merge_chain_guard") for table in TABLES},
    "expected_table_indexes": {(table, f"idx_{table}_merged_into") for table in TABLES},
    "expected_trigger_functions": {("refuse_merge_chain",)},
}

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/mint_recovery_contract_v26.py"


@pytest.fixture(scope="module")
def mint() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mint_recovery_contract_v26", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@h:5433/brain",
        "postgresql://u:p@h:5433/brain_test",
        "postgresql://u:p@h:5433/brain?application_name=x/y",
        "postgresql://u:p@h:5433/%62rain",
        "postgresql://u:p@h:5433/brain_v26_x?dbname=brain",
        "postgresql://u:p@h:5433/brain_v26_x?host=/tmp",
        "postgresql://u:p@h:5433/brain_v26_x/extra",
        "postgresql://u:p@h:5433/",
        "postgresql://u:p@h:5433/other",
    ],
)
def test_the_dsn_guard_refuses_what_is_not_a_disposable_database(
    mint: ModuleType, dsn: str
) -> None:
    with pytest.raises(SystemExit):
        mint.assert_disposable_dsn(dsn)


def test_the_dsn_guard_accepts_a_disposable_database(mint: ModuleType) -> None:
    mint.assert_disposable_dsn("postgresql://u:p@h:5433/brain_v26_base_0123456789")


def test_the_connection_itself_is_verified_before_any_use(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = AsyncMock()
    connection.fetchval.return_value = "brain"
    monkeypatch.setattr(mint.asyncpg, "connect", AsyncMock(return_value=connection))
    with pytest.raises(SystemExit):
        asyncio.run(mint.connect("postgresql://u:p@h:5433/brain_v26_x"))
    connection.close.assert_awaited_once()
    connection.fetch.assert_not_called()


@pytest.mark.parametrize("heads", [[("067",)], [("067",), ("068",)]])
def test_measurement_requires_exactly_head_068(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, heads: list[tuple[str]]
) -> None:
    connection = AsyncMock()
    connection.fetch.return_value = heads
    monkeypatch.setattr(mint, "connect", AsyncMock(return_value=connection))
    measure = AsyncMock()
    monkeypatch.setattr(mint, "measure", measure)
    with pytest.raises(SystemExit, match="single head 068"):
        asyncio.run(
            mint.main(
                "postgresql://u:p@h:5433/brain_v26_x",
                mint.RECOVERY / "brain-v42-v25.sql",
                tmp_path / "new.sql",
            )
        )
    measure.assert_not_awaited()
    connection.close.assert_awaited_once()


@pytest.mark.parametrize(
    "name,key",
    [
        ("expected_table_columns", ("programmes",)),
        ("expected_table_constraints", ("brain_client_credentials", "surprise_check")),
        ("expected_table_indexes", ("brain_client_credentials", "surprise_idx")),
        ("expected_trigger_functions", ("brain_credential_projects_valid",)),
        ("expected_sequences", ("brain_client_credentials_id_seq",)),
    ],
)
def test_rows_outside_the_declared_footprint_stop_the_mint(
    mint: ModuleType, name: str, key: tuple[str, ...]
) -> None:
    with pytest.raises(SystemExit):
        mint.split_delta(name, [(*key, "unexpected")], [])


@pytest.mark.parametrize(
    "name,key",
    [(name, key) for name, keys in DECLARED_ADDITIONS.items() for key in sorted(keys)],
)
def test_only_the_thirteen_declared_rows_can_be_added(
    mint: ModuleType, name: str, key: tuple[str, ...]
) -> None:
    row = (*key, "0" * 32)
    assert mint.allowed_addition(name, key)
    assert mint.split_delta(name, [row], []) == ([row], [])


@pytest.mark.parametrize(
    "name,key",
    [
        ("expected_table_columns", ("brain_client_credentials",)),
        (
            "expected_table_constraints",
            ("brain_credential_audit", "brain_credential_audit_event_valid"),
        ),
        ("expected_table_indexes", ("learnings", "idx_learnings_merged_into")),
        ("expected_trigger_functions", ("refuse_merge_chain",)),
        ("expected_sequences", ("knowledge_extractions_id_seq",)),
    ],
)
def test_no_existing_fingerprint_can_move(
    mint: ModuleType, name: str, key: tuple[str, ...]
) -> None:
    old, new = (*key, "0" * 32), (*key, "1" * 32)
    assert mint.REWRITABLE_ROWS == set()
    with pytest.raises(SystemExit):
        mint.split_delta(name, [new], [old])
    with pytest.raises(SystemExit, match="undeclared fingerprint rewrite"):
        mint.rewrite_row([], name, new)


def test_no_row_is_exempt_from_the_068_delta(mint: ModuleType) -> None:
    assert mint.TWIN_DIVERGENCE_BY_DESIGN == set()
    with pytest.raises(SystemExit):
        mint.split_delta(
            "expected_table_indexes",
            [("dream_promotions", "idx_dream_promotions_source_materialized", "0" * 32)],
            [("dream_promotions", "idx_dream_promotions_source_materialized", "1" * 32)],
        )


def test_a_declared_row_cannot_disappear(mint: ModuleType) -> None:
    with pytest.raises(SystemExit):
        mint.split_delta("expected_table_columns", [], [("brain_client_credentials", "0" * 32)])


def test_every_written_asset_is_replayed_from_disk(mint: ModuleType) -> None:
    source = SCRIPT.read_text()
    main = source[source.index("async def main(") : source.index("async def replay_written(")]
    assert main.index("dst.write_text(") < main.index("await replay_written(dsn, dst")
    assert "asset.read_text(" in source[source.index("async def replay_written(") :]


def _replay(mint: ModuleType, monkeypatch: pytest.MonkeyPatch, asset: Path, observed: str) -> None:
    receipt = {
        "checks": [
            {
                "id": "extension_versions",
                "status": "fail",
                "expected": "pg_stat_statements 1.10, plpgsql 1.0, vector 0.8.2",
                "observed": observed,
            }
        ]
    }
    connection = MagicMock()
    connection.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
    connection.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    connection.fetchval = AsyncMock(return_value=json.dumps(receipt))
    connection.close = AsyncMock()
    monkeypatch.setattr(mint, "connect", AsyncMock(return_value=connection))
    asset.write_text("SELECT 1", encoding="utf-8")
    asyncio.run(mint.replay_written("postgresql://u:p@h:5433/brain_v26_x", asset, restored=False))


@pytest.mark.parametrize("version", ["0.8.4", "0.8.5"])
def test_a_proven_vector_build_passes_the_live_replay(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str
) -> None:
    asset = tmp_path / "brain-v42-v26.sql"
    _replay(mint, monkeypatch, asset, f"pg_stat_statements 1.10, plpgsql 1.0, vector {version}")
    assert asset.exists()


@pytest.mark.parametrize(
    "observed",
    [
        "pg_stat_statements 1.10, pgcrypto 1.3, plpgsql 1.0, vector 0.8.5",
        "pg_stat_statements 1.10, plpgsql 1.0, vector 0.9.0",
        "plpgsql 1.0, vector 0.8.5",
    ],
)
def test_any_other_extension_inventory_fails_the_live_replay(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, observed: str
) -> None:
    asset = tmp_path / "brain-v42-v26.sql"
    with pytest.raises(SystemExit):
        _replay(mint, monkeypatch, asset, observed)
    assert not asset.exists()


def test_bind_requires_acl_assets_before_writing_current(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mint, "RECOVERY", tmp_path)
    monkeypatch.setattr(mint, "ROOT", tmp_path)
    (tmp_path / "current.json").write_text("unchanged")
    for suffix in (".sql", "-pgrestore.sql", ".json"):
        (tmp_path / f"brain-v42-v26{suffix}").write_text("{}")
    with pytest.raises(FileNotFoundError):
        mint.bind()
    assert (tmp_path / "current.json").read_text() == "unchanged"


def test_manifest_preserves_the_strict_v25_shape_and_grants(
    mint: ModuleType, tmp_path: Path
) -> None:
    source = mint.RECOVERY / "brain-v42-v25.json"
    target = tmp_path / "v26.json"
    mint.mint_manifest(source, target)
    old, new = json.loads(source.read_text()), json.loads(target.read_text())
    counts = next(check for check in new["checks"] if check["id"] == "catalog_counts")
    assert counts["indexes"] == 221
    counts["indexes"] = 215
    assert {**new, "contract_id": old["contract_id"], "schema_version": 25} == old
    assert new["contract_id"] == "brain-v42/postgresql-recovery/v26"
    assert new["schema_version"] == 26


def test_catalog_counts_are_recounted_including_invalid_objects(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = {
        "foreign_keys": 74,
        "indexes": 221,
        "invalid_indexes": 0,
        "unvalidated_constraints": 0,
    }
    rows = AsyncMock(return_value=[(json.dumps(expected),)])
    monkeypatch.setattr(mint, "scalar_rows", rows)
    lines = (mint.RECOVERY / "brain-v42-v25.sql").read_text().split("\n")
    assert asyncio.run(mint.observed_catalog_counts("unused", lines)) == expected
    assert "SELECT observed::text FROM catalog_counts" in rows.await_args.args[1]


def _declared_delta(mint: ModuleType) -> dict[str, dict[str, list[tuple[str, ...]]]]:
    """A synthetic 068 footprint lets the mint guards run without a database."""
    delta = {name: {"missing": [], "stale": []} for name, _ in mint.PAIRS}
    for name, keys in DECLARED_ADDITIONS.items():
        for key in sorted(keys):
            row = (
                (*key, "2" * 32, 123) if name == "expected_trigger_functions" else (*key, "2" * 32)
            )
            delta[name]["missing"].append(row)
    return delta


def test_incomplete_declared_footprint_is_refused_before_writing(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = AsyncMock()
    connection.fetch.return_value = [("068",)]
    monkeypatch.setattr(mint, "connect", AsyncMock(return_value=connection))
    delta = _declared_delta(mint)
    delta["expected_table_constraints"]["missing"].pop()
    monkeypatch.setattr(mint, "measure", AsyncMock(return_value=delta))
    target = tmp_path / "v26.sql"
    with pytest.raises(SystemExit, match="incomplete 068 additions"):
        asyncio.run(
            mint.main(
                "postgresql://u:p@h:5433/brain_v26_x", mint.RECOVERY / "brain-v42-v25.sql", target
            )
        )
    assert not target.exists()


def test_changed_catalog_counts_are_refused_before_writing(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = AsyncMock()
    connection.fetch.return_value = [("068",)]
    monkeypatch.setattr(mint, "connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(mint, "measure", AsyncMock(return_value=_declared_delta(mint)))
    monkeypatch.setattr(
        mint,
        "observed_catalog_counts",
        AsyncMock(
            return_value={
                "foreign_keys": 74,
                "indexes": 215,
                "invalid_indexes": 0,
                "unvalidated_constraints": 0,
            }
        ),
    )
    target = tmp_path / "v26.sql"
    with pytest.raises(SystemExit, match="catalog counts measured"):
        asyncio.run(
            mint.main(
                "postgresql://u:p@h:5433/brain_v26_x", mint.RECOVERY / "brain-v42-v25.sql", target
            )
        )
    assert not target.exists()


def test_scalar_patch_moves_identity_and_only_the_index_count(mint: ModuleType) -> None:
    source = (mint.RECOVERY / "brain-v42-v25.sql").read_text()
    patched = mint.patch_scalars(source)
    assert "'contract_id', 'brain-v42/postgresql-recovery/v26'" in patched
    assert "'schema_version', 26" in patched
    assert "'indexes', 221," in patched
    assert (
        patched.replace("postgresql-recovery/v26", "postgresql-recovery/v25")
        .replace("'schema_version', 26", "'schema_version', 25")
        .replace("'indexes', 221,", "'indexes', 215,")
        == source
    )


@pytest.mark.parametrize(
    "contract_id,schema_version",
    [
        ("brain-v42/postgresql-recovery/v24", 24),
        ("brain-v42/postgresql-recovery/v26", 26),
        ("brain-v42/postgresql-recovery/v25", 24),
    ],
)
def test_manifest_refuses_a_non_v25_source(
    mint: ModuleType, tmp_path: Path, contract_id: str, schema_version: int
) -> None:
    document = json.loads((mint.RECOVERY / "brain-v42-v25.json").read_text())
    document.update(contract_id=contract_id, schema_version=schema_version)
    source, target = tmp_path / "source.json", tmp_path / "target.json"
    source.write_text(json.dumps(document))
    with pytest.raises(SystemExit, match="v25 manifest"):
        mint.mint_manifest(source, target)
    assert not target.exists()


@pytest.mark.parametrize("indexes", [214, 216, 221])
def test_manifest_refuses_an_unexpected_source_index_count(
    mint: ModuleType, tmp_path: Path, indexes: int
) -> None:
    document = json.loads((mint.RECOVERY / "brain-v42-v25.json").read_text())
    next(check for check in document["checks"] if check["id"] == "catalog_counts")["indexes"] = (
        indexes
    )
    source, target = tmp_path / "source.json", tmp_path / "target.json"
    source.write_text(json.dumps(document))
    with pytest.raises(SystemExit, match="v25 catalog counts"):
        mint.mint_manifest(source, target)
    assert not target.exists()


def test_bind_pins_v26_assets_to_head_068(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hashlib

    recovery = tmp_path / "ops/recovery"
    recovery.mkdir(parents=True)
    monkeypatch.setattr(mint, "ROOT", tmp_path)
    monkeypatch.setattr(mint, "RECOVERY", recovery)
    for suffix in (".sql", "-pgrestore.sql", ".json", "-acl.sql", "-acl.json"):
        (recovery / f"brain-v42-v26{suffix}").write_text(suffix)
    mint.bind()
    binding = json.loads((recovery / "current.json").read_text())
    assert binding["contract_id"] == "brain-v42/postgresql-recovery/v26"
    assert binding["contract_version"] == 26
    assert binding["schema_head"] == "068"
    for key, suffix in (
        ("attestation_sql", ".sql"),
        ("restored_attestation_sql", "-pgrestore.sql"),
        ("manifest", ".json"),
    ):
        assert binding[key] == {
            "path": f"ops/recovery/brain-v42-v26{suffix}",
            "sha256": hashlib.sha256(suffix.encode()).hexdigest(),
        }


def test_acl_advances_only_identity(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for suffix in ("-acl.sql", "-acl.json"):
        (tmp_path / f"brain-v42-v25{suffix}").write_bytes(
            (mint.RECOVERY / f"brain-v42-v25{suffix}").read_bytes()
        )
    monkeypatch.setattr(mint, "RECOVERY", tmp_path)
    mint.mint_acl_assets()
    old_sql = (tmp_path / "brain-v42-v25-acl.sql").read_text()
    assert (tmp_path / "brain-v42-v26-acl.sql").read_text() == old_sql.replace(
        "postgresql-recovery/v25-acl", "postgresql-recovery/v26-acl"
    ).replace("'schema_version', 25", "'schema_version', 26")
    old = json.loads((tmp_path / "brain-v42-v25-acl.json").read_text())
    new = json.loads((tmp_path / "brain-v42-v26-acl.json").read_text())
    assert {**new, "contract_id": old["contract_id"], "schema_version": 25} == old


def test_complete_delta_is_written_and_replayed(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = AsyncMock()
    connection.fetch.return_value = [("068",)]
    monkeypatch.setattr(mint, "connect", AsyncMock(return_value=connection))
    empty = {name: {"missing": [], "stale": []} for name, _ in mint.PAIRS}
    monkeypatch.setattr(mint, "measure", AsyncMock(side_effect=[_declared_delta(mint), empty]))
    monkeypatch.setattr(
        mint,
        "observed_catalog_counts",
        AsyncMock(
            return_value={
                "foreign_keys": 74,
                "indexes": 221,
                "invalid_indexes": 0,
                "unvalidated_constraints": 0,
            }
        ),
    )
    replay = AsyncMock()
    monkeypatch.setattr(mint, "replay_written", replay)
    target = tmp_path / "brain-v42-v26-pgrestore.sql"
    assert (
        asyncio.run(
            mint.main(
                "postgresql://u:p@h:5433/brain_v26_x",
                mint.RECOVERY / "brain-v42-v25-pgrestore.sql",
                target,
            )
        )
        == 0
    )
    assert "'indexes', 221," in target.read_text()
    replay.assert_awaited_once_with("postgresql://u:p@h:5433/brain_v26_x", target, restored=True)
