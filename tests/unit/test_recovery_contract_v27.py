"""Require measured v27 assets; missing measurements deliberately fail this gate."""

import hashlib
import json

from tests.unit.test_recovery_contract_v26 import RECOVERY, ROOT, _fingerprints

TABLES = ("decisions", "learnings", "snippets", "runbooks", "adrs", "focus_slots")


def test_current_binding_names_v27_head_069_and_pins_every_asset():
    current = json.loads((RECOVERY / "current.json").read_text())
    assert (current["contract_id"], current["contract_version"], current["schema_head"]) == (
        "brain-v42/postgresql-recovery/v27",
        27,
        "069",
    )
    for key, suffix in [
        ("attestation_sql", ".sql"),
        ("restored_attestation_sql", "-pgrestore.sql"),
        ("manifest", ".json"),
    ]:
        path = f"ops/recovery/brain-v42-v27{suffix}"
        assert current[key] == {
            "path": path,
            "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest(),
        }


def test_both_sql_variants_change_only_the_declared_fingerprints_and_identity():
    from tests.unit.test_mint_recovery_contract_v27_guard import load_mint

    module = load_mint()
    for suffix in (".sql", "-pgrestore.sql"):
        old = (RECOVERY / f"brain-v42-v26{suffix}").read_text()
        new = (RECOVERY / f"brain-v42-v27{suffix}").read_text()
        for name, _ in module.PAIRS:
            before, after = _fingerprints(old, name), _fingerprints(new, name)
            assert before.keys() <= after.keys()
            changed = {key for key in before if before[key] != after[key]}
            assert changed == (
                {(table,) for table in TABLES} if name == "expected_table_columns" else set()
            )
            assert after.keys() - before.keys() == module.DECLARED_ADDITIONS.get(name, set())
        # Rebuild from the measured rows, then require all other SQL bytes to agree.
        lines = old.split("\n")
        for name, _ in module.PAIRS:
            before, after = _fingerprints(old, name), _fingerprints(new, name)
            for key in before:
                if before[key] != after[key]:
                    lines = module.rewrite_row(lines, name, (*key, after[key]))
            lines = module.append_rows(
                lines, name, [(*key, after[key]) for key in after if key not in before]
            )
        assert new == module.patch_scalars("\n".join(lines))


def test_v27_manifest_and_acl_keep_the_inherited_checks_and_grants():
    old = json.loads((RECOVERY / "brain-v42-v26.json").read_text())
    new = json.loads((RECOVERY / "brain-v42-v27.json").read_text())
    assert {**new, "contract_id": old["contract_id"], "schema_version": 26} == old
    assert new["contract_id"] == "brain-v42/postgresql-recovery/v27"
    assert new["schema_version"] == 27
    for suffix in ("-acl.json",):
        old = json.loads((RECOVERY / f"brain-v42-v26{suffix}").read_text())
        new = json.loads((RECOVERY / f"brain-v42-v27{suffix}").read_text())
        assert {**new, "contract_id": old["contract_id"], "schema_version": 26} == old
        assert new["contract_id"] == "brain-v42/postgresql-recovery/v27-acl"
    old_sql = (RECOVERY / "brain-v42-v26-acl.sql").read_text()
    assert (RECOVERY / "brain-v42-v27-acl.sql").read_text() == old_sql.replace(
        "postgresql-recovery/v26-acl", "postgresql-recovery/v27-acl"
    ).replace("'schema_version', 26", "'schema_version', 27")
