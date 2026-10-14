"""Require measured v25 assets while preserving v24's fingerprints and strict shape.

Missing generated assets fail deliberately until both disposable database forms
have been measured and the coordinator has bound the final files.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECOVERY = ROOT / "ops/recovery"
SPEC = importlib.util.spec_from_file_location(
    "mint_recovery_contract_v25", ROOT / "scripts/mint_recovery_contract_v25.py"
)
assert SPEC is not None and SPEC.loader is not None
mint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mint)

V24_ASSETS = {
    "brain-v42-v24.json": "58ee4f701d2e35bf7d7390aa54f0c61d5c01d83d003e71e783e26eaf03bfa4c9",
    "brain-v42-v24.sql": "b9e0577e92dd2ab4f24b2b2e6005f4c96d69e98b63ba930396a63bb2742d9aea",
    "brain-v42-v24-pgrestore.sql": "8efef79111339bb18411139a572f98e0add558c6b5e8298e0dfa4ead00b955b7",
    "brain-v42-v24-acl.json": "efcd13939ff6ca3bdcaaa689e0506e154450a7c5dfa9860800c83a44aa0911e8",
    "brain-v42-v24-acl.sql": "a461c42e79b2461f10176dbeaad34fbd55e5987d13d54b2bedc1eeb4e1598cfc",
}
VARIANTS = ("brain-v42-v25.sql", "brain-v42-v25-pgrestore.sql")


def test_v24_assets_remain_byte_identical() -> None:
    for name, digest in V24_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == digest


def test_current_binding_names_v25_and_pins_each_asset() -> None:
    current = json.loads((RECOVERY / "current.json").read_text())
    assert current["contract_id"] == "brain-v42/postgresql-recovery/v25"
    assert current["contract_version"] == 25
    assert current["schema_head"] == "067"
    for key, suffix in (
        ("attestation_sql", ".sql"),
        ("restored_attestation_sql", "-pgrestore.sql"),
        ("manifest", ".json"),
    ):
        path = f"ops/recovery/brain-v42-v25{suffix}"
        assert current[key]["path"] == path
        assert current[key]["sha256"] == hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def test_v25_identity_and_recounted_catalog_counts_in_both_variants() -> None:
    for name in VARIANTS:
        text = (RECOVERY / name).read_text()
        assert " 'contract_id', 'brain-v42/postgresql-recovery/v25'," in text
        assert " 'schema_version', 25" in text
        for key, value in mint.CATALOG.items():
            assert f"         '{key}', {value}" in text


def _fingerprints(text: str, name: str) -> dict[tuple[str, ...], str]:
    lines = text.splitlines()
    head, close, _ = mint.find_block(lines, name)
    width = mint.KEY_WIDTH[name]
    values = re.findall(r"'([^']*)'", mint.body(lines, head, close))
    rows = [values[i : i + width + 1] for i in range(0, len(values), width + 1)]
    assert all(len(row) == width + 1 for row in rows)
    return {tuple(row[:width]): row[width] for row in rows}


def test_v25_changes_exactly_the_three_declared_fingerprint_rows() -> None:
    for variant in VARIANTS:
        old = (RECOVERY / variant.replace("v25", "v24")).read_text()
        new = (RECOVERY / variant).read_text()
        for name in ("expected_table_columns", "expected_table_constraints"):
            before, after = _fingerprints(old, name), _fingerprints(new, name)
            added = {mint.NEW_CONSTRAINT} if name == "expected_table_constraints" else set()
            assert after.keys() - before.keys() == added
            assert before.keys() - after.keys() == set()
            changed = {key for key in before if before[key] != after[key]}
            assert changed == {key for block, key in mint.REWRITABLE_ROWS if block == name}
        for name in (
            "expected_tables",
            "expected_table_indexes",
            "expected_sequences",
            "expected_trigger_functions",
            "runtime_expected_functions",
            "runtime_lifecycle_grants",
            "runtime_observed_triggers",
            "expected_view_columns",
        ):
            if not any(line.startswith(name) for line in old.splitlines()):
                continue
            old_lines, new_lines = old.splitlines(), new.splitlines()
            old_head, old_close, _ = mint.find_block(old_lines, name)
            new_head, new_close, _ = mint.find_block(new_lines, name)
            assert mint.body(old_lines, old_head, old_close) == mint.body(
                new_lines, new_head, new_close
            )


def test_v25_manifest_keeps_every_v24_check_and_field(tmp_path: Path) -> None:
    generated = tmp_path / "manifest.json"
    mint.mint_manifest(RECOVERY / "brain-v42-v24.json", generated)
    assert (RECOVERY / "brain-v42-v25.json").read_bytes() == generated.read_bytes()
    old = json.loads((RECOVERY / "brain-v42-v24.json").read_text())
    new = json.loads(generated.read_text())
    assert {**new, "contract_id": old["contract_id"], "schema_version": 24} == old
    acl = next(check for check in new["checks"] if check["id"] == "runtime_acl_064")
    assert sorted(acl) == ["grants", "id", "kind", "proof_scope", "restore_rule", "revision"]
    assert acl["revision"] == "066"
    assert "public.brain_credential_projects_valid(text[])" not in acl["grants"]["functions"]


def test_v25_live_acl_assets_change_only_identity() -> None:
    old_sql = (RECOVERY / "brain-v42-v24-acl.sql").read_text()
    assert (RECOVERY / "brain-v42-v25-acl.sql").read_text() == old_sql.replace(
        "'brain-v42/postgresql-recovery/v24-acl'", "'brain-v42/postgresql-recovery/v25-acl'"
    ).replace(" 'schema_version', 24", " 'schema_version', 25")
    old = json.loads((RECOVERY / "brain-v42-v24-acl.json").read_text())
    new = json.loads((RECOVERY / "brain-v42-v25-acl.json").read_text())
    assert {**new, "contract_id": old["contract_id"], "schema_version": 24} == old


def test_restored_variant_has_no_runtime_acl_requirement() -> None:
    text = (RECOVERY / "brain-v42-v25-pgrestore.sql").read_text()
    assert "runtime_lifecycle_grants" not in text
    assert "runtime_role AS (" not in text
    assert "not_applicable: pg_restore --no-owner --no-acl" in text
    for suffix in ("-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v25{suffix}").exists()
