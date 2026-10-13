"""Recovery contract v23 — v22 plus the 065 footprint, head 065.

065 adds two tables, `programmes` and `programme_gates`, with their 23 constraints,
four indexes (two primary keys, two UNIQUE constraints) and three foreign keys. It
adds no trigger, trigger function, sequence, view or extension, changes no existing
table and grants nothing beyond what 064's default privileges already give.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V22_ASSETS = {
    "brain-v42-v22.json": "68d8f4bdcdac452e0b43e7d4862b736e03167e4c832c79956d2a8e1d49beb7af",
    "brain-v42-v22.sql": "72446e66b24d476aaac98c1f05026810532d5920d754d84a7a7fb9e918c9f498",
    "brain-v42-v22-pgrestore.sql": "fdc2ce6c2b5f089830b1e7593eef490d20ef6fcd94d1f023e877fe42de053b85",
}
V23_ASSETS = {
    "brain-v42-v23.json": "140d4de9cee2d9e5f87880bf543322d1edcef6642648ad925c0461876bb319ec",
    "brain-v42-v23.sql": "214581f7f7440315c1283e3503dc18979658fe99288803eae7ac6b1da06e686b",
    "brain-v42-v23-pgrestore.sql": "32bcf735af16456359dcf4b5906b482b5ebfbc7cbc7bbb3fe357b2a5136c5e85",
}
VARIANTS = ("brain-v42-v23.sql", "brain-v42-v23-pgrestore.sql")
NEW_TABLES = ["programme_gates", "programmes"]
NEW_CONSTRAINTS = [
    ("programmes", "programmes_pkey"),
    ("programmes", "programmes_key_key"),
    ("programmes", "programmes_coordinator_project_fkey"),
    ("programmes", "programmes_last_change_valid"),
    ("programme_gates", "programme_gates_pkey"),
    ("programme_gates", "programme_gates_programme_id_key_key"),
    ("programme_gates", "programme_gates_programme_id_fkey"),
    ("programme_gates", "programme_gates_owner_project_fkey"),
    ("programme_gates", "programme_gates_depends_on_valid"),
    ("programme_gates", "programme_gates_progress_valid"),
    ("programmes", "programmes_updated_by_valid"),
    ("programme_gates", "programme_gates_updated_by_valid"),
]


def _sha256(name: str) -> str:
    return hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()


def _manifest(version: int) -> dict:
    return json.loads((RECOVERY / f"brain-v42-v{version}.json").read_text())


def test_v22_assets_remain_byte_identical() -> None:
    for name, expected in V22_ASSETS.items():
        assert _sha256(name) == expected, name


def test_v23_assets_are_pinned() -> None:
    for name, expected in V23_ASSETS.items():
        assert _sha256(name) == expected, name


def test_v23_identity_and_counts_move_in_both_variants() -> None:
    for name in VARIANTS:
        text = (RECOVERY / name).read_text()
        assert " 'contract_id', 'brain-v42/postgresql-recovery/v23'," in text, name
        assert " 'schema_version', 23" in text, name
        assert "         'foreign_keys', 65," in text, name
        assert "         'indexes', 199," in text, name
        assert "postgresql-recovery/v22" not in text, name


def test_v23_names_the_065_footprint_in_both_variants() -> None:
    for name in VARIANTS:
        text = (RECOVERY / name).read_text()
        for table in NEW_TABLES:
            assert f"     ('{table}')" in text, (name, table)
        for table, constraint in NEW_CONSTRAINTS:
            assert f"'{table}',\n         '{constraint}'," in text, (name, constraint)


#: The ONLY v22 lines absent from v23, in both variants: the last row of
#: `expected_tables` (it gains a trailing comma when the two tables are appended),
#: the two moved catalog counts and the identity. Every other v22 line is kept.
V22_LINES_DROPPED = sorted(
    [
        "     ('brain_schema_compat')",
        "         'foreign_keys', 62,",
        "         'indexes', 195,",
        " 'contract_id', 'brain-v42/postgresql-recovery/v22',",
        " 'schema_version', 22",
    ]
)


def test_v23_keeps_every_v22_line_but_the_declared_ones() -> None:
    for variant in VARIANTS:
        before = (RECOVERY / variant.replace("v23", "v22")).read_text().splitlines()
        after = (RECOVERY / variant).read_text().splitlines()
        removed = [line[2:] for line in difflib.ndiff(before, after) if line.startswith("- ")]
        assert sorted(removed) == V22_LINES_DROPPED, variant


def test_v23_adds_no_trigger_function_or_runtime_trigger() -> None:
    for variant in VARIANTS:
        before = (RECOVERY / variant.replace("v23", "v22")).read_text().splitlines()
        after = (RECOVERY / variant).read_text().splitlines()
        added = [line[2:] for line in difflib.ndiff(before, after) if line.startswith("+ ")]
        assert not any("programme" in line and "_notify" in line for line in added), variant
        assert all("runtime_acl" not in line for line in added), variant


def test_v23_manifest_is_v22_with_identity_counts_and_tables_moved() -> None:
    v22, v23 = _manifest(22), _manifest(23)
    counts = next(check for check in v23["checks"] if check["id"] == "catalog_counts")
    assert (counts["foreign_keys"], counts["indexes"]) == (65, 199)
    tables = next(check for check in v23["checks"] if check["id"] == "table_set")
    assert tables["tables"] == sorted(
        [*next(c for c in v22["checks"] if c["id"] == "table_set")["tables"], *NEW_TABLES]
    )
    old_counts = next(check for check in v22["checks"] if check["id"] == "catalog_counts")
    old_counts.update(foreign_keys=65, indexes=199)
    next(c for c in v22["checks"] if c["id"] == "table_set")["tables"] = tables["tables"]
    v22["contract_id"], v22["schema_version"] = "brain-v42/postgresql-recovery/v23", 23
    assert v23 == v22
    assert (RECOVERY / "brain-v42-v23.json").read_text() == (
        json.dumps(v23, sort_keys=True, separators=(",", ":")) + "\n"
    )


def test_v23_ships_no_acl_asset_because_065_grants_nothing() -> None:
    for suffix in ("-acl.sql", "-acl.json", "-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v23{suffix}").exists()
    assert (RECOVERY / "brain-v42-v22-acl.sql").is_file()


def test_current_binding_names_v23_and_pins_each_asset() -> None:
    current = json.loads((RECOVERY / "current.json").read_text())
    assert current["contract_id"] == "brain-v42/postgresql-recovery/v23"
    assert current["contract_version"] == 23
    assert current["schema_head"] == "065"
    for key, name in (
        ("attestation_sql", "brain-v42-v23.sql"),
        ("restored_attestation_sql", "brain-v42-v23-pgrestore.sql"),
        ("manifest", "brain-v42-v23.json"),
    ):
        assert current[key]["path"] == f"ops/recovery/{name}"
        assert current[key]["sha256"] == _sha256(name)
