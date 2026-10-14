"""The v25 mint accepts only the declared 067 delta on disposable databases."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/mint_recovery_contract_v25.py"


@pytest.fixture(scope="module")
def mint() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mint_recovery_contract_v25", SCRIPT)
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
        "postgresql://u:p@h:5433/brain_v25_x?dbname=brain",
        "postgresql://u:p@h:5433/brain_v25_x?host=/tmp",
        "postgresql://u:p@h:5433/brain_v25_x/extra",
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
    mint.assert_disposable_dsn("postgresql://u:p@h:5433/brain_v25_base_0123456789")


def test_the_connection_itself_is_verified_before_any_use(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = AsyncMock()
    connection.fetchval.return_value = "brain"
    monkeypatch.setattr(mint.asyncpg, "connect", AsyncMock(return_value=connection))
    with pytest.raises(SystemExit):
        asyncio.run(mint.connect("postgresql://u:p@h:5433/brain_v25_x"))
    connection.close.assert_awaited_once()
    connection.fetch.assert_not_called()


@pytest.mark.parametrize("heads", [[("066",)], [("066",), ("067",)]])
def test_measurement_requires_exactly_head_067(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, heads: list[tuple[str]]
) -> None:
    connection = AsyncMock()
    connection.fetch.return_value = heads
    monkeypatch.setattr(mint, "connect", AsyncMock(return_value=connection))
    measure = AsyncMock()
    monkeypatch.setattr(mint, "measure", measure)
    with pytest.raises(SystemExit, match="single head 067"):
        asyncio.run(
            mint.main(
                "postgresql://u:p@h:5433/brain_v25_x",
                mint.RECOVERY / "brain-v42-v24.sql",
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


def test_only_the_new_scope_check_can_be_added(mint: ModuleType) -> None:
    row = (*mint.NEW_CONSTRAINT, "0" * 32)
    assert mint.split_delta("expected_table_constraints", [row], []) == ([row], [])


@pytest.mark.parametrize(
    "name,key",
    [
        ("expected_table_columns", ("brain_client_credentials",)),
        (
            "expected_table_constraints",
            ("brain_credential_audit", "brain_credential_audit_event_valid"),
        ),
    ],
)
def test_only_declared_existing_fingerprints_can_move(
    mint: ModuleType, name: str, key: tuple[str, ...]
) -> None:
    old, new = (*key, "0" * 32), (*key, "1" * 32)
    assert mint.split_delta(name, [new], [old]) == ([], [new])
    lines = (mint.RECOVERY / "brain-v42-v24.sql").read_text().split("\n")
    updated = mint.rewrite_row(lines, name, new)
    head, close, _ = mint.find_block(updated, name)
    assert mint.render(name, new)[-2] in updated[head:close]


def test_no_row_is_exempt_from_the_067_delta(mint: ModuleType) -> None:
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
    asyncio.run(mint.replay_written("postgresql://u:p@h:5433/brain_v25_x", asset, restored=False))


@pytest.mark.parametrize("version", ["0.8.4", "0.8.5"])
def test_a_proven_vector_build_passes_the_live_replay(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str
) -> None:
    asset = tmp_path / "brain-v42-v25.sql"
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
    asset = tmp_path / "brain-v42-v25.sql"
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
        (tmp_path / f"brain-v42-v25{suffix}").write_text("{}")
    with pytest.raises(FileNotFoundError):
        mint.bind()
    assert (tmp_path / "current.json").read_text() == "unchanged"


def test_manifest_preserves_the_strict_v24_shape_and_grants(
    mint: ModuleType, tmp_path: Path
) -> None:
    source = mint.RECOVERY / "brain-v42-v24.json"
    target = tmp_path / "v25.json"
    mint.mint_manifest(source, target)
    old, new = json.loads(source.read_text()), json.loads(target.read_text())
    assert {**new, "contract_id": old["contract_id"], "schema_version": 24} == old
    assert new["contract_id"] == "brain-v42/postgresql-recovery/v25"
    assert new["schema_version"] == 25


def test_catalog_counts_are_recounted_including_invalid_objects(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = AsyncMock(return_value=[(json.dumps(mint.CATALOG),)])
    monkeypatch.setattr(mint, "scalar_rows", rows)
    lines = (mint.RECOVERY / "brain-v42-v24.sql").read_text().split("\n")
    assert asyncio.run(mint.observed_catalog_counts("unused", lines)) == mint.CATALOG
    assert "SELECT observed::text FROM catalog_counts" in rows.await_args.args[1]


def _declared_delta(mint: ModuleType) -> dict[str, dict[str, list[tuple[str, ...]]]]:
    """A synthetic 067 footprint lets the mint guards run without a database."""
    delta = {name: {"missing": [], "stale": []} for name, _ in mint.PAIRS}
    for name, key in mint.REWRITABLE_ROWS:
        delta[name]["missing"].append((*key, "1" * 32))
        delta[name]["stale"].append((*key, "0" * 32))
    delta["expected_table_constraints"]["missing"].append((*mint.NEW_CONSTRAINT, "2" * 32))
    return delta


def test_incomplete_declared_footprint_is_refused_before_writing(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = AsyncMock()
    connection.fetch.return_value = [("067",)]
    monkeypatch.setattr(mint, "connect", AsyncMock(return_value=connection))
    delta = _declared_delta(mint)
    delta["expected_table_columns"] = {"missing": [], "stale": []}
    monkeypatch.setattr(mint, "measure", AsyncMock(return_value=delta))
    target = tmp_path / "v25.sql"
    with pytest.raises(SystemExit, match="incomplete 067 rewrites"):
        asyncio.run(
            mint.main(
                "postgresql://u:p@h:5433/brain_v25_x", mint.RECOVERY / "brain-v42-v24.sql", target
            )
        )
    assert not target.exists()


def test_changed_catalog_counts_are_refused_before_writing(
    mint: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = AsyncMock()
    connection.fetch.return_value = [("067",)]
    monkeypatch.setattr(mint, "connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(mint, "measure", AsyncMock(return_value=_declared_delta(mint)))
    monkeypatch.setattr(
        mint, "observed_catalog_counts", AsyncMock(return_value={**mint.CATALOG, "indexes": 216})
    )
    target = tmp_path / "v25.sql"
    with pytest.raises(SystemExit, match="catalog counts measured"):
        asyncio.run(
            mint.main(
                "postgresql://u:p@h:5433/brain_v25_x", mint.RECOVERY / "brain-v42-v24.sql", target
            )
        )
    assert not target.exists()
