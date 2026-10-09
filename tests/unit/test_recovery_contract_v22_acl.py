"""Head-built ACL mutation tests need the live 064 proof, not an older era."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from tests.integration.db import test_acl_contract_negative_mutations as mutations

ROOT = Path(__file__).resolve().parents[2]
RECOVERY = ROOT / "ops/recovery"


def _mint():
    spec = importlib.util.spec_from_file_location(
        "mint_recovery_contract_v22", ROOT / "scripts/mint_recovery_contract_v22.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_head_built_negative_mutations_use_the_064_acl_era() -> None:
    assert mutations.ACL_ASSET == RECOVERY / "brain-v42-v22-acl.sql"


def test_v22_acl_mint_preserves_every_historical_negative_witness() -> None:
    mint = _mint()
    before = (RECOVERY / "brain-v42-v11-acl.sql").read_text()
    after = mint.mint_acl_sql(before)
    assert after == (RECOVERY / "brain-v42-v22-acl.sql").read_text()
    assert "VALUES ('brain'), ('codex_ro'), ('brain_app')" in after
    assert "'contract_id', 'brain-v42/postgresql-recovery/v22-acl'" in after
    assert "'schema_version', 22" in after
    # The missing-grant witness must still remove exactly the original term.
    assert after.count(mutations.MISSING_GRANT_TERM) == 1
    for *_, counter in mutations.MUTATIONS:
        assert f"'{counter}', 0" in after
        assert f"AND {counter}.value = 0" in after or f"     {counter}.value = 0" in after
    # Keep the full cluster-role enumeration, and require the runtime ACL delta.
    enumeration = before.split("role_privilege_mismatches AS (", 1)[1].split(
        "check_rows(id, expected, observed, passed) AS (", 1
    )[0]
    assert enumeration in after
    assert mint.live_delta() in after
    assert mint.check_delta(restored=False) in after
    assert "FROM runtime_expected_relations" in after
    assert "('brain', 'codex_ro', 'brain_app')" in after


def test_v22_acl_manifest_is_reproducible_and_live_only(tmp_path: Path) -> None:
    mint = _mint()
    generated = tmp_path / "acl.json"
    mint.mint_acl_manifest(RECOVERY / "brain-v42-v11-acl.json", generated)
    assert generated.read_bytes() == (RECOVERY / "brain-v42-v22-acl.json").read_bytes()
    document = json.loads(generated.read_text())
    assert document["contract_id"] == "brain-v42/postgresql-recovery/v22-acl"
    assert document["schema_version"] == 22
    checks = {check["id"]: check for check in document["checks"]}
    old = json.loads((RECOVERY / "brain-v42-v11-acl.json").read_text())
    old["checks"][0]["roles"].append("brain_app")
    assert checks["acl_and_ownership"] == old["checks"][0]
    assert checks["runtime_acl_064"]["grants"] == mint.runtime_grants()
    assert checks["runtime_acl_064"]["proof_scope"] == "live-only"
    assert checks["runtime_acl_064"]["restore_rule"] == "not-applicable-no-owner-no-acl"
    assert not (RECOVERY / "brain-v42-v22-acl-pgrestore.sql").exists()


def test_mint_all_emits_the_live_acl_assets(tmp_path: Path, monkeypatch) -> None:
    mint = _mint()
    for name in (
        "brain-v42-v21.sql",
        "brain-v42-v21-pgrestore.sql",
        "brain-v42-v21.json",
        "brain-v42-v11-acl.sql",
        "brain-v42-v11-acl.json",
    ):
        (tmp_path / name).write_bytes((RECOVERY / name).read_bytes())
    declaration = mint.runtime_grants()
    monkeypatch.setattr(mint, "ROOT", tmp_path)
    monkeypatch.setattr(mint, "RECOVERY", tmp_path)
    monkeypatch.setattr(mint, "runtime_grants", lambda: declaration)
    mint.mint_all()
    for suffix in (".sql", ".json"):
        name = f"brain-v42-v22-acl{suffix}"
        assert (tmp_path / name).read_bytes() == (RECOVERY / name).read_bytes()
