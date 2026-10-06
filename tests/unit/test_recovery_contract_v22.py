"""v22 is v21's schema plus migration-derived 064 ACLs on the live target only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECOVERY = ROOT / "ops/recovery"
SCRIPT = ROOT / "scripts/mint_recovery_contract_v22.py"
V21_ASSETS = {
    "brain-v42-v21.json": "4c7c4523a0e4c47d44962ca865cd8e84c2c14c6251a90cc84049a39ed2ceae8b",
    "brain-v42-v21.sql": "47d0aa50afadd3a946362b860aee4cd575fc342f60ad05399a8b2241766720f0",
    "brain-v42-v21-pgrestore.sql": "baac61ab1a2148d8d373b3afd78553046430fc098da9e92868d00f577a656a16",
}
V22_ASSETS = {
    "brain-v42-v22.json": "68d8f4bdcdac452e0b43e7d4862b736e03167e4c832c79956d2a8e1d49beb7af",
    "brain-v42-v22.sql": "72446e66b24d476aaac98c1f05026810532d5920d754d84a7a7fb9e918c9f498",
    "brain-v42-v22-pgrestore.sql": "fdc2ce6c2b5f089830b1e7593eef490d20ef6fcd94d1f023e877fe42de053b85",
}


@pytest.fixture
def mint():
    assert SCRIPT.is_file(), "v22 must ship its reproducible mint"
    spec = importlib.util.spec_from_file_location("mint_recovery_contract_v22", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v21_assets_remain_byte_identical():
    for name, digest in V21_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == digest


def test_v22_assets_are_pinned():
    for name, digest in V22_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("restored", [False, True])
def test_v22_sql_is_reproduced_by_its_mint(mint, restored):
    suffix = "-pgrestore.sql" if restored else ".sql"
    before = (RECOVERY / f"brain-v42-v21{suffix}").read_text()
    after = (RECOVERY / f"brain-v42-v22{suffix}").read_text()
    assert after == mint.mint_sql(before, restored=restored)
    assert " 'contract_id', 'brain-v42/postgresql-recovery/v22'," in after
    assert " 'schema_version', 22" in after
    # Removing the declared delta must recover every byte of v21.
    assert mint.remove_delta(after, restored=restored) == before


def test_v22_manifest_keeps_all_v21_checks_and_declares_live_only_acl(mint, tmp_path):
    generated = tmp_path / "manifest.json"
    mint.mint_manifest(RECOVERY / "brain-v42-v21.json", generated)
    assert generated.read_bytes() == (RECOVERY / "brain-v42-v22.json").read_bytes()
    old = json.loads((RECOVERY / "brain-v42-v21.json").read_text())
    new = json.loads(generated.read_text())
    added = [check for check in new["checks"] if check["id"] == "runtime_acl_064"]
    assert len(added) == 1
    assert added[0]["restore_rule"] == "not-applicable-no-owner-no-acl"
    new["checks"].remove(added[0])
    new.update(contract_id=old["contract_id"], schema_version=old["schema_version"])
    assert new == old


def test_runtime_acl_expectations_are_derived_from_064(mint):
    declaration = mint.runtime_grants()
    assert declaration["schemas"] == ["public", "monitoring"]
    assert declaration["tables"] == ["SELECT", "INSERT", "UPDATE", "DELETE"]
    assert declaration["sequences"] == ["USAGE", "SELECT"]
    assert declaration["functions"] == [
        "public.register_referenced_project(text)",
        "public.vector_dims(public.vector)",
        "public.vector_norm(public.vector)",
    ]
    source = (ROOT / "alembic/versions/064_brain_app_runtime_role.py").read_text()
    assert mint.runtime_grants(source.replace("SELECT, INSERT, UPDATE, DELETE", "SELECT"))[
        "tables"
    ] == ["SELECT"]
    sql = (RECOVERY / "brain-v42-v22.sql").read_text()
    for function in declaration["functions"]:
        assert f"('{function}')" in sql
    for counter in (
        "role_mismatches",
        "schema_mismatches",
        "relation_mismatches",
        "function_mismatches",
        "default_mismatches",
        "ownership_mismatches",
    ):
        assert f"'{counter}'" in sql


def test_restore_contract_has_no_application_role_or_acl_requirement():
    sql = (RECOVERY / "brain-v42-v22-pgrestore.sql").read_text()
    assert "brain_app" not in sql
    assert "runtime_acl_064" in sql
    assert "not_applicable: pg_restore --no-owner --no-acl" in sql
    assert "runtime_expected_" not in sql


def test_current_binding_names_v22_and_pins_each_asset():
    current = json.loads((RECOVERY / "current.json").read_text())
    assert current["contract_id"] == "brain-v42/postgresql-recovery/v22"
    assert current["contract_version"] == 22
    assert current["schema_head"] == "064"
    for key, name in (
        ("attestation_sql", "brain-v42-v22.sql"),
        ("restored_attestation_sql", "brain-v42-v22-pgrestore.sql"),
        ("manifest", "brain-v42-v22.json"),
    ):
        assert current[key]["path"] == f"ops/recovery/{name}"
        assert current[key]["sha256"] == hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()
