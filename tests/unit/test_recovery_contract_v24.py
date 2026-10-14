"""Recovery contract v24 preserves v23 and declares only the 066 lifecycle delta.

Generated assets are deliberately required: missing assets must fail this suite
until the coordinator measures them on disposable PostgreSQL databases.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECOVERY = ROOT / "ops/recovery"
SPEC = importlib.util.spec_from_file_location(
    "mint_recovery_contract_v24", ROOT / "scripts/mint_recovery_contract_v24.py"
)
assert SPEC is not None and SPEC.loader is not None
mint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mint)

V23_ASSETS = {
    "brain-v42-v23.json": "140d4de9cee2d9e5f87880bf543322d1edcef6642648ad925c0461876bb319ec",
    "brain-v42-v23.sql": "e59850438487828fc9aaae6cbe9c64077e258dff843216b837b91542dd782004",
    "brain-v42-v23-pgrestore.sql": "c714e423dd4128154790cb026f108cd1e582061e760a25401583e2e6719c346e",
}
VARIANTS = ("brain-v42-v24.sql", "brain-v42-v24-pgrestore.sql")


def test_v23_assets_remain_byte_identical() -> None:
    for name, digest in V23_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == digest


def test_current_binding_has_moved_past_v24() -> None:
    """v24 stays frozen for 066; the current 067 contract is v25."""
    current = json.loads((RECOVERY / "current.json").read_text())
    assert current["contract_version"] > 24
    assert current["schema_head"] != "066"
    assert "brain-v42-v24" not in json.dumps(current)


def test_v24_identity_and_counts_move_in_both_variants() -> None:
    for name in VARIANTS:
        text = (RECOVERY / name).read_text()
        assert " 'contract_id', 'brain-v42/postgresql-recovery/v24'," in text
        assert " 'schema_version', 24" in text
        assert "         'foreign_keys', 74," in text
        assert "         'indexes', 215," in text


def test_v24_covers_every_declared_constraint_and_index() -> None:
    for name in VARIANTS:
        text = (RECOVERY / name).read_text()
        for table in mint.NEW_TABLES:
            assert f"     ('{table}')" in text
        for table, suffixes in mint.CONSTRAINTS.items():
            for suffix in suffixes:
                assert f"'{table}',\n         '{table}_{suffix}'," in text
        for table, index in mint.INDEXES:
            assert f"'{table}',\n         '{index}'," in text


def test_v24_attests_sequence_ownership_shape_and_high_water() -> None:
    for name in VARIANTS:
        text = (RECOVERY / name).read_text()
        for row in mint.declared_sequences():
            assert mint.render("expected_sequences", row)[0].rstrip(",") in text
            assert f"('{row[0]}', (SELECT max(id) FROM public.{row[1]}))" in text


def test_v24_keeps_v23_fingerprints_except_the_declared_footprint() -> None:
    for name in VARIANTS:
        old = (RECOVERY / name.replace("v24", "v23")).read_text()
        new = (RECOVERY / name).read_text()
        # Identity, counts and live ACL declarations are independently reproduced.
        expected = mint.patch_runtime_acl(mint.patch_scalars(old))
        removed = [
            line[2:]
            for line in difflib.ndiff(expected.splitlines(), new.splitlines())
            if line.startswith("- ")
        ]
        # Appending VALUES/UNION rows may only add a trailing comma to an old last row.
        assert all(line + "," in new.splitlines() for line in removed), (name, removed)
        for block in (
            "expected_trigger_functions",
            "runtime_observed_triggers",
            "expected_view_columns",
        ):
            if not any(line.startswith(block) for line in old.splitlines()):
                continue
            old_lines, new_lines = old.splitlines(), new.splitlines()
            start, end, _ = mint.find_block(old_lines, block)
            new_start, new_end, _ = mint.find_block(new_lines, block)
            assert mint.body(old_lines, start, end) == mint.body(new_lines, new_start, new_end)


def test_v24_manifest_is_exactly_the_declared_v23_delta(tmp_path: Path) -> None:
    generated = tmp_path / "manifest.json"
    mint.mint_manifest(RECOVERY / "brain-v42-v23.json", generated)
    assert (RECOVERY / "brain-v42-v24.json").read_bytes() == generated.read_bytes()
    document = json.loads(generated.read_text())
    acl = next(check for check in document["checks"] if check["id"] == "runtime_acl_064")
    assert acl["revision"] == "066"
    # red-backup's strict model refuses any other field for this check kind.
    assert sorted(acl) == ["grants", "id", "kind", "proof_scope", "restore_rule", "revision"]
    v23 = json.loads((RECOVERY / "brain-v42-v23.json").read_text())
    v23_acl = next(check for check in v23["checks"] if check["id"] == "runtime_acl_064")
    assert {**acl, "revision": "064"} == v23_acl
    counts = next(check for check in document["checks"] if check["id"] == "catalog_counts")
    assert (counts["foreign_keys"], counts["indexes"]) == (74, 215)


def test_v24_live_acl_asset_is_the_declared_v22_delta() -> None:
    before = (RECOVERY / "brain-v42-v22-acl.sql").read_text()
    expected = (
        mint.patch_runtime_acl(before)
        .replace(
            "'brain-v42/postgresql-recovery/v22-acl'", "'brain-v42/postgresql-recovery/v24-acl'"
        )
        .replace(" 'schema_version', 22", " 'schema_version', 24")
    )
    assert (RECOVERY / "brain-v42-v24-acl.sql").read_text() == expected
    document = json.loads((RECOVERY / "brain-v42-v22-acl.json").read_text())
    document.update(contract_id="brain-v42/postgresql-recovery/v24-acl", schema_version=24)
    mint.move_acl_manifest(document)
    assert json.loads((RECOVERY / "brain-v42-v24-acl.json").read_text()) == document


def test_restored_variant_has_no_runtime_acl_requirement() -> None:
    text = (RECOVERY / "brain-v42-v24-pgrestore.sql").read_text()
    assert "runtime_lifecycle_grants" not in text
    assert "runtime_role AS (" not in text
    assert "not_applicable: pg_restore --no-owner --no-acl" in text
    for suffix in ("-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v24{suffix}").exists()


def test_declared_footprint_matches_the_migration() -> None:
    """Compare declarations without executing either upgrade or its ACL writes."""
    import ast
    import re

    tree = ast.parse((ROOT / "alembic/versions/066_knowledge_lifecycle.py").read_text())
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"_HISTORY", "_MUTABLE"}
    }
    assert constants["_HISTORY"] == mint.HISTORY
    assert constants["_MUTABLE"] == mint.MUTABLE
    sql = "\n".join(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    assert set(re.findall(r"CREATE TABLE IF NOT EXISTS public\.(\w+)", sql)) == set(mint.NEW_TABLES)
    assert sql.count("BIGSERIAL PRIMARY KEY") == 4
    assert len(mint.INDEXES) == 16
    assert len(mint.NEW_TABLES) == 7
