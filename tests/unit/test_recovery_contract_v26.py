"""Require measured v26 assets while preserving v25's fingerprints and strict shape.

Missing generated assets fail deliberately until both disposable database forms
have been measured and the coordinator has bound the final files.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECOVERY = ROOT / "ops/recovery"
SPEC = importlib.util.spec_from_file_location(
    "mint_recovery_contract_v26", ROOT / "scripts/mint_recovery_contract_v26.py"
)
assert SPEC is not None and SPEC.loader is not None
mint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mint)

V25_ASSETS = {
    "brain-v42-v25.json": "75a8f080db824511098adc42a15c6498325c34208bd3311fb56a7b883d786bd7",
    "brain-v42-v25.sql": "19dc0f3cce37110a98d64c16b0e6525f19bf5dcf8c0f03d9d40de5db480b9675",
    "brain-v42-v25-pgrestore.sql": "8b6f5852fd33193c85c92eb4b9920ad4d44c94b155ce048fa80f7f9b15802859",
    "brain-v42-v25-acl.json": "2764820ab3edc3c32c9d173a31e895832ab0cfe90399c0fe3171119ac1e4aa3c",
    "brain-v42-v25-acl.sql": "f95b0d569a8270602a17076bdeff60735e26f12bd98fc96a6cd9854255848ebc",
}
VARIANTS = ("brain-v42-v26.sql", "brain-v42-v26-pgrestore.sql")


def test_v25_assets_remain_byte_identical() -> None:
    for name, digest in V25_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == digest


def test_current_binding_names_v26_and_pins_each_asset() -> None:
    current = json.loads((RECOVERY / "current.json").read_text())
    assert current["contract_id"] == "brain-v42/postgresql-recovery/v26"
    assert current["contract_version"] == 26
    assert current["schema_head"] == "068"
    for key, suffix in (
        ("attestation_sql", ".sql"),
        ("restored_attestation_sql", "-pgrestore.sql"),
        ("manifest", ".json"),
    ):
        path = f"ops/recovery/brain-v42-v26{suffix}"
        assert current[key]["path"] == path
        assert current[key]["sha256"] == hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def test_v26_identity_and_recounted_catalog_counts_in_both_variants() -> None:
    for name in VARIANTS:
        text = (RECOVERY / name).read_text()
        assert " 'contract_id', 'brain-v42/postgresql-recovery/v26'," in text
        assert " 'schema_version', 26" in text
        for key, (_, value) in mint.CATALOG.items():
            assert f"         '{key}', {value}" in text


def _fingerprints(text: str, name: str) -> dict[tuple[str, ...], str]:
    lines = text.splitlines()
    head, close, _ = mint.find_block(lines, name)
    width = mint.KEY_WIDTH[name]
    if name == "expected_trigger_functions":
        return {
            (function,): f"{digest}:{octets}"
            for function, digest, octets in re.findall(
                r"\('([^']*)', '([0-9a-f]{64})', (\d+)\)", mint.body(lines, head, close)
            )
        }
    values = re.findall(r"'([^']*)'", mint.body(lines, head, close))
    rows = [values[i : i + width + 1] for i in range(0, len(values), width + 1)]
    assert all(len(row) == width + 1 for row in rows)
    return {tuple(row[:width]): row[width] for row in rows}


def test_v26_adds_exactly_the_thirteen_declared_rows() -> None:
    tables = ("adrs", "decisions", "features", "learnings", "runbooks", "snippets")
    declared = {
        "expected_table_constraints": {(table, f"{table}_merge_chain_guard") for table in tables},
        "expected_table_indexes": {(table, f"idx_{table}_merged_into") for table in tables},
        "expected_trigger_functions": {("refuse_merge_chain",)},
    }
    for variant in VARIANTS:
        old = (RECOVERY / variant.replace("v26", "v25")).read_text()
        new = (RECOVERY / variant).read_text()
        for name, additions in declared.items():
            before, after = _fingerprints(old, name), _fingerprints(new, name)
            assert after.keys() - before.keys() == additions
            assert before.keys() - after.keys() == set()
            assert all(before[key] == after[key] for key in before)
            if name == "expected_table_constraints":
                assert {after[key] for key in additions} == {"40b04baf4eb12bc74afe359f88bfeaaa"}
        expected = mint.patch_scalars(old)
        removed = [
            line[2:]
            for line in difflib.ndiff(expected.splitlines(), new.splitlines())
            if line.startswith("- ")
        ]
        assert all(line + "," in new.splitlines() for line in removed), (variant, removed)
        for name in (
            "expected_tables",
            "expected_table_columns",
            "expected_sequences",
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


def test_v26_manifest_keeps_every_v25_check_and_field(tmp_path: Path) -> None:
    generated = tmp_path / "manifest.json"
    mint.mint_manifest(RECOVERY / "brain-v42-v25.json", generated)
    assert (RECOVERY / "brain-v42-v26.json").read_bytes() == generated.read_bytes()
    old = json.loads((RECOVERY / "brain-v42-v25.json").read_text())
    new = json.loads(generated.read_text())
    assert new["contract_id"] == "brain-v42/postgresql-recovery/v26"
    assert new["schema_version"] == 26
    counts = next(check for check in new["checks"] if check["id"] == "catalog_counts")
    assert counts["indexes"] == 221
    counts["indexes"] = 215
    assert {**new, "contract_id": old["contract_id"], "schema_version": 25} == old
    acl = next(check for check in new["checks"] if check["id"] == "runtime_acl_064")
    assert sorted(acl) == ["grants", "id", "kind", "proof_scope", "restore_rule", "revision"]
    assert acl["revision"] == "066"
    assert "public.brain_credential_projects_valid(text[])" not in acl["grants"]["functions"]


def test_v26_live_acl_assets_change_only_identity() -> None:
    old_sql = (RECOVERY / "brain-v42-v25-acl.sql").read_text()
    assert (RECOVERY / "brain-v42-v26-acl.sql").read_text() == old_sql.replace(
        "'brain-v42/postgresql-recovery/v25-acl'", "'brain-v42/postgresql-recovery/v26-acl'"
    ).replace(" 'schema_version', 25", " 'schema_version', 26")
    old = json.loads((RECOVERY / "brain-v42-v25-acl.json").read_text())
    new = json.loads((RECOVERY / "brain-v42-v26-acl.json").read_text())
    assert {**new, "contract_id": old["contract_id"], "schema_version": 25} == old


def test_restored_variant_has_no_runtime_acl_requirement() -> None:
    text = (RECOVERY / "brain-v42-v26-pgrestore.sql").read_text()
    assert "runtime_lifecycle_grants" not in text
    assert "runtime_role AS (" not in text
    assert "not_applicable: pg_restore --no-owner --no-acl" in text
    for suffix in ("-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v26{suffix}").exists()


def test_v26_asset_hashes_are_filled_from_the_mint() -> None:
    assets = {
        "brain-v42-v26.json": "a60d6ab4c8e254d4df64c9dc0b5401448bb54639d5b25e7f2a6912521cf5a128",
        "brain-v42-v26.sql": "028055e0c9cc034bc7aeac11cb04b82500c32866e673d7a70acdc99b6def8e2a",
        "brain-v42-v26-pgrestore.sql": "3247a5de2ea05629276e4dc1619094290d353c36cdd528619506c9072528f334",
        "brain-v42-v26-acl.json": "e6c568bb12011fb1b77f404dc31d88db180f21cd8cfee88f3dda8d86c7af34fb",
        "brain-v42-v26-acl.sql": "1e7c113ffd5d1c59d311cf6d23af45bcf55f8ebc019fc8c697528219ca2ac543",
    }
    for name, digest in assets.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == digest
