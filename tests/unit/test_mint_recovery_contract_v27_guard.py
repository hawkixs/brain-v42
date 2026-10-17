"""The 069 mint permits exactly six column rewrites and six new author checks."""

import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
TABLES = ("decisions", "learnings", "snippets", "runbooks", "adrs", "focus_slots")


def load_mint():
    spec = importlib.util.spec_from_file_location(
        "mint_v27", ROOT / "scripts/mint_recovery_contract_v27.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mint():
    return load_mint()


def delta(mint):
    result = {name: {"missing": [], "stale": []} for name, _ in mint.PAIRS}
    for table in TABLES:
        result["expected_table_columns"]["missing"].append((table, "1" * 32))
        result["expected_table_columns"]["stale"].append((table, "0" * 32))
        result["expected_table_constraints"]["missing"].append(
            (table, f"{table}_author_client_id_format", "2" * 32)
        )
    return result


def test_exact_declared_footprint(mint):
    assert mint.REWRITABLE_ROWS == {("expected_table_columns", (table,)) for table in TABLES}
    assert mint.DECLARED_ADDITIONS == {
        "expected_table_constraints": {
            (table, f"{table}_author_client_id_format") for table in TABLES
        }
    }
    assert mint.TWIN_DIVERGENCE_BY_DESIGN == set()
    for name, sides in delta(mint).items():
        additions, rewrites = mint.split_delta(name, sides["missing"], sides["stale"])
        assert len(additions) == (6 if name == "expected_table_constraints" else 0)
        assert len(rewrites) == (6 if name == "expected_table_columns" else 0)


@pytest.mark.parametrize(
    "name,key",
    [
        ("expected_table_columns", ("features",)),
        ("expected_table_constraints", ("decisions", "decisions_merge_chain_guard")),
        ("expected_table_indexes", ("decisions", "idx_decisions_merged_into")),
        ("expected_trigger_functions", ("refuse_merge_chain",)),
        ("expected_sequences", ("adrs_number_seq",)),
    ],
)
def test_unrelated_additions_rewrites_and_removals_are_refused(mint, name, key):
    old, new = (*key, "0" * 32), (*key, "1" * 32)
    for missing, stale in [([new], []), ([new], [old]), ([], [old])]:
        with pytest.raises(SystemExit):
            mint.split_delta(name, missing, stale)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@h/brain",
        "postgresql://u:p@h/brain_test",
        "postgresql://u:p@h/%62rain",
        "postgresql://u:p@h/brain_v27?dbname=brain",
        "postgresql://u:p@h/other",
    ],
)
def test_mint_refuses_non_disposable_databases(mint, dsn):
    with pytest.raises(SystemExit):
        mint.assert_disposable_dsn(dsn)


@pytest.mark.parametrize(
    "damage", ["head", "incomplete_additions", "incomplete_rewrites", "catalog", "remaining"]
)
def test_mint_refuses_bad_measurements_before_writing(mint, monkeypatch, tmp_path, damage):
    connection = AsyncMock()
    connection.fetch.return_value = [("068" if damage == "head" else "069",)]
    monkeypatch.setattr(mint, "connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(mint, "validate_inherited_columns", AsyncMock())
    measured = delta(mint)
    if damage == "incomplete_additions":
        measured["expected_table_constraints"]["missing"].pop()
    if damage == "incomplete_rewrites":
        measured["expected_table_columns"]["missing"].pop()
        measured["expected_table_columns"]["stale"].pop()
    empty = {name: {"missing": [], "stale": []} for name, _ in mint.PAIRS}
    monkeypatch.setattr(
        mint,
        "measure",
        AsyncMock(side_effect=[measured, measured if damage == "remaining" else empty]),
    )
    counts = {key: new for key, (_, new) in mint.CATALOG.items()}
    if damage == "catalog":
        counts["indexes"] += 1
    monkeypatch.setattr(mint, "observed_catalog_counts", AsyncMock(return_value=counts))
    target = tmp_path / "new.sql"
    with pytest.raises(SystemExit):
        asyncio.run(
            mint.main("postgresql://u:p@h/brain_v27_x", mint.RECOVERY / "brain-v42-v26.sql", target)
        )
    assert not target.exists()


def test_complete_measurement_is_written_and_replayed(mint, monkeypatch, tmp_path):
    connection = AsyncMock()
    connection.fetch.return_value = [("069",)]
    monkeypatch.setattr(mint, "connect", AsyncMock(return_value=connection))
    inherited = AsyncMock()
    monkeypatch.setattr(mint, "validate_inherited_columns", inherited)
    empty = {name: {"missing": [], "stale": []} for name, _ in mint.PAIRS}
    monkeypatch.setattr(mint, "measure", AsyncMock(side_effect=[delta(mint), empty]))
    monkeypatch.setattr(
        mint,
        "observed_catalog_counts",
        AsyncMock(return_value={key: new for key, (_, new) in mint.CATALOG.items()}),
    )
    replay = AsyncMock()
    monkeypatch.setattr(mint, "replay_written", replay)
    target = tmp_path / "brain-v42-v27-pgrestore.sql"
    dsn = "postgresql://u:p@h/brain_v27_x"
    assert asyncio.run(mint.main(dsn, mint.RECOVERY / "brain-v42-v26-pgrestore.sql", target)) == 0
    replay.assert_awaited_once_with(dsn, target, restored=True)
    assert "postgresql-recovery/v27" in target.read_text()
    inherited.assert_awaited_once()


@pytest.mark.parametrize("drift", [[], [("missing", "decisions", "changed-existing-column")]])
def test_author_rewrite_cannot_hide_changes_to_existing_columns(mint, monkeypatch, drift):
    observed = AsyncMock(return_value=drift)
    monkeypatch.setattr(mint, "scalar_rows", observed)
    lines = (mint.RECOVERY / "brain-v42-v26.sql").read_text().split("\n")
    if drift:
        with pytest.raises(SystemExit, match="inherited columns"):
            asyncio.run(mint.validate_inherited_columns("unused", lines))
    else:
        asyncio.run(mint.validate_inherited_columns("unused", lines))
    query = observed.await_args.args[1]
    assert "observed_column.column_name = 'author_client_id'" in query
    for table in TABLES:
        assert f"'{table}'" in query
    assert "row_number() OVER" in query


def test_manifest_changes_only_identity_and_bind_waits_for_all_assets(mint, monkeypatch, tmp_path):
    target = tmp_path / "manifest.json"
    source = mint.RECOVERY / "brain-v42-v26.json"
    mint.mint_manifest(source, target)
    old, new = json.loads(source.read_text()), json.loads(target.read_text())
    assert {**new, "contract_id": old["contract_id"], "schema_version": 26} == old
    assert new["schema_version"] == 27
    monkeypatch.setattr(mint, "ROOT", tmp_path)
    monkeypatch.setattr(mint, "RECOVERY", tmp_path)
    binding = tmp_path / "current.json"
    binding.write_text("unchanged")
    for suffix in (".sql", "-pgrestore.sql", ".json"):
        (tmp_path / f"brain-v42-v27{suffix}").write_text("{}")
    with pytest.raises(FileNotFoundError):
        mint.bind()
    assert binding.read_text() == "unchanged"
    for suffix in ("-acl.sql", "-acl.json"):
        (tmp_path / f"brain-v42-v27{suffix}").write_text("{}")
    mint.bind()
    assert json.loads(binding.read_text())["schema_head"] == "069"
