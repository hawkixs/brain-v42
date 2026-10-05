"""Recovery contract v20 — v19 plus the 062 footprint, head 062.

062 adds two partial indexes on `delivery_confirmations` and the `pg_stat_statements`
extension in schema `monitoring`. No table, no trigger, no grant in `public`.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V19_ASSETS = {
    "brain-v42-v19.json": "8c5d510fde49d402199d797937c3fd815d930c8d41a24e21009cf5524fe41e5f",
    "brain-v42-v19.sql": "0a47cd0f50af94728ad503ab61fd62775ebbbe78e6b9a6749c8764fb036c6088",
    "brain-v42-v19-pgrestore.sql": "58162494975b704d81e2519f84e176bcbd25e0aa9c0690d039b8de27757e0940",
}
V19_SQL = RECOVERY / "brain-v42-v19.sql"
V19_PGRESTORE = RECOVERY / "brain-v42-v19-pgrestore.sql"
V19_JSON = RECOVERY / "brain-v42-v19.json"
V20_SQL = RECOVERY / "brain-v42-v20.sql"
V20_PGRESTORE = RECOVERY / "brain-v42-v20-pgrestore.sql"
V20_JSON = RECOVERY / "brain-v42-v20.json"
V20_ASSETS = {
    "brain-v42-v20.json": "a0736e760d02777ae081ccfd726bb56325b8ee1b5e7f387cb39c62368e539518",
    "brain-v42-v20.sql": "0229a57125021a51a20d5cde59b223abfb3e57c51cbc1a9e0d9ada457fefe976",
    "brain-v42-v20-pgrestore.sql": "f3e0198c341b27b3e76fbe0c8046bab3c29b019a9f3286e4092e7c23dda08d66",
}

INDEX_ROWS_ADDED = [
    "     ),",
    "     (",
    "         'delivery_confirmations',",
    "         'idx_delivery_confirmations_binding_errors',",
    "         '5efff88e31c49e4c293dc0ef0bf641eb'",
    "     ),",
    "     (",
    "         'delivery_confirmations',",
    "         'idx_delivery_confirmations_context_errors',",
    "         'd9732b50f781d03d0ebd1353ea6c0565'",
]
# The two variants differ only in how
# they pin the extension inventory (the twin judges names, never versions).
COUNT_REMOVED = ["         'indexes', 184,"]
COUNT_ADDED = ["         'indexes', 186,"]
IDENTITY_REMOVED = [
    " 'contract_id', 'brain-v42/postgresql-recovery/v19',",
    " 'schema_version', 19",
]
IDENTITY_ADDED = [
    " 'contract_id', 'brain-v42/postgresql-recovery/v20',",
    " 'schema_version', 20",
]
INVENTORY = "pg_stat_statements 1.10, plpgsql 1.0, vector 0.8.2"
BASE_REMOVED = [
    "     to_jsonb('plpgsql 1.0, vector 0.8.2'::text),",
    "     extension_observation.inventory = 'plpgsql 1.0, vector 0.8.2'",
]
BASE_ADDED = [
    f"     to_jsonb('{INVENTORY}'::text),",
    f"     extension_observation.inventory = '{INVENTORY}'",
]
TWIN_REMOVED = [
    "         'names', '[\"plpgsql\", \"vector\"]'::jsonb,",
    "         'origin_inventory', 'plpgsql 1.0, vector 0.8.2'",
    '     extension_observation.names = \'["plpgsql", "vector"]\'::jsonb',
]
TWIN_ADDED = [
    '         \'names\', \'["pg_stat_statements", "plpgsql", "vector"]\'::jsonb,',
    f"         'origin_inventory', '{INVENTORY}'",
    '     extension_observation.names = \'["pg_stat_statements", "plpgsql", "vector"]\'::jsonb',
]

INDEX_ROW = re.compile(r"^         '(idx_delivery_confirmations_(?:binding|context)_errors)',$")


def _sha256(name: str) -> str:
    return hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()


def _added_and_removed(before: Path, after: Path) -> tuple[list[str], list[str]]:
    diff = list(difflib.ndiff(before.read_text().splitlines(), after.read_text().splitlines()))
    return (
        [line[2:] for line in diff if line.startswith("+ ")],
        [line[2:] for line in diff if line.startswith("- ")],
    )


def test_v19_assets_remain_byte_identical() -> None:
    for name, expected in V19_ASSETS.items():
        assert _sha256(name) == expected, name


def test_v20_assets_are_pinned() -> None:
    for name, expected in V20_ASSETS.items():
        assert _sha256(name) == expected, name


def test_v20_sql_is_v19_plus_exactly_the_measured_delta_in_both_variants() -> None:
    for before, after, removed, added in (
        (V19_SQL, V20_SQL, BASE_REMOVED, BASE_ADDED),
        (V19_PGRESTORE, V20_PGRESTORE, TWIN_REMOVED, TWIN_ADDED),
    ):
        got_added, got_removed = _added_and_removed(before, after)
        # Diff order is file order: index rows, index count, extension pins, identity.
        assert got_removed == [*COUNT_REMOVED, *removed, *IDENTITY_REMOVED], after.name
        assert got_added == [*INDEX_ROWS_ADDED, *COUNT_ADDED, *added, *IDENTITY_ADDED], after.name


def test_v20_adds_only_the_two_confirmation_indexes() -> None:
    """The one new row family is the pair of partial indexes, in both variants."""
    for before, after in ((V19_SQL, V20_SQL), (V19_PGRESTORE, V20_PGRESTORE)):
        added, _ = _added_and_removed(before, after)
        named = [m.group(1) for line in added if (m := INDEX_ROW.match(line))]
        assert named == [
            "idx_delivery_confirmations_binding_errors",
            "idx_delivery_confirmations_context_errors",
        ], after.name
        assert added.count("         'delivery_confirmations',") == 2, after.name
        # No new table and no trigger: 062 creates neither.
        assert not any("expected_tables" in line or "trigger" in line for line in added), after.name


def test_v20_moves_the_extension_inventory() -> None:
    base = V20_SQL.read_text()
    twin = V20_PGRESTORE.read_text()
    inventory = re.search(r"'(pg_stat_statements \d+\.\d+, plpgsql 1\.0, vector 0\.8\.2)'", base)
    assert inventory is not None
    assert base.count(f"'{inventory.group(1)}'") == 2
    assert "'plpgsql 1.0, vector 0.8.2'" not in base
    names = '\'["pg_stat_statements", "plpgsql", "vector"]\'::jsonb'
    assert twin.count(names) == 2
    assert f"'origin_inventory', '{inventory.group(1)}'" in twin
    assert '["plpgsql", "vector"]' not in twin


def test_v20_manifest_is_v19_with_identity_index_count_and_inventory_moved() -> None:
    v19 = json.loads(V19_JSON.read_text())
    v20 = json.loads(V20_JSON.read_text())
    assert v20["contract_id"] == "brain-v42/postgresql-recovery/v20"
    assert v20["schema_version"] == 20
    counts = next(check for check in v20["checks"] if check["id"] == "catalog_counts")
    assert (counts["foreign_keys"], counts["indexes"]) == (61, 186)
    extensions = next(check for check in v20["checks"] if check["id"] == "extension_versions")
    assert re.fullmatch(
        r"pg_stat_statements \d+\.\d+, plpgsql 1\.0, vector 0\.8\.2", extensions["inventory"]
    )
    old_counts = next(check for check in v19["checks"] if check["id"] == "catalog_counts")
    old_counts.update(indexes=186)
    old_extensions = next(check for check in v19["checks"] if check["id"] == "extension_versions")
    old_extensions["inventory"] = extensions["inventory"]
    v19["contract_id"], v19["schema_version"] = v20["contract_id"], v20["schema_version"]
    assert v20 == v19
    assert V20_JSON.read_text() == json.dumps(v20, sort_keys=True, separators=(",", ":")) + "\n"


def test_v20_ships_no_acl_asset_because_062_grants_nothing_in_public() -> None:
    for suffix in ("-acl.sql", "-acl.json", "-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v20{suffix}").exists()
        assert (RECOVERY / f"brain-v42-v11{suffix}").is_file()


def test_current_binding_names_v20() -> None:
    current = json.loads((RECOVERY / "current.json").read_text())
    assert current["contract_id"] == "brain-v42/postgresql-recovery/v20"
    assert current["contract_version"] == 20
    assert current["schema_head"] == "062"
    for key, name in (
        ("attestation_sql", "brain-v42-v20.sql"),
        ("restored_attestation_sql", "brain-v42-v20-pgrestore.sql"),
        ("manifest", "brain-v42-v20.json"),
    ):
        assert current[key]["path"] == f"ops/recovery/{name}"
        assert current[key]["sha256"] == _sha256(name)
