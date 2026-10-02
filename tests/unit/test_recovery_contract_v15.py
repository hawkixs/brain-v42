"""Recovery contract v15 — a candidate for schema head 058.

The generation is immutable lineage, not a production receipt. Its base and
``pg_restore`` variants are v14 plus 058's footprint: one named CHECK,
``knowledge_claims_provenance_valid``, widened from ``('measured', 'declared')``
to ``('measured', 'declared', 'extracted')``. Each variant was measured with its
OWN recipe — the base against a disposable database the alembic chain built at
head 058, the twin against a real custom-format ``pg_dump``/``pg_restore`` of a
disposable 057 source migrated to 058 — never retyped, because guessing an md5
expression produces hashes that never match.

058 drops and recreates one named CHECK, so the constraint fingerprint for
``knowledge_claims`` / ``knowledge_claims_provenance_valid`` is REWRITTEN in
``expected_table_constraints`` rather than appended. 058 adds no table, no
column, no index and no trigger, so every other block stays v14's — the same
shape 057 forced onto ``search_log`` in v14, narrower still: the rewrite lands on
a single constraint row instead of a whole table's column row.

The SHA-256 values below are placeholders (``REPLACE_AFTER_MINT``): they are
filled by the operator after ``scripts/mint_recovery_contract_v15.py`` has
measured the three assets, and this test stays red until then.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V14_ASSETS = {
    "brain-v42-v14.json": "c6f963bf17450c7db340716b8c396b3358dec4e745369d5f245aaa36b4a48e60",
    "brain-v42-v14.sql": "0264e4b5b26106c440ef30cfc6b38a3b2e547a8b929ea6204432ac3e6308fd36",
    "brain-v42-v14-pgrestore.sql": (
        "1333d01eb31b0e7cfadb4029c67bd80d743cf8a9f758c2ae389492e11a1e665a"
    ),
}

#: The SHA-256 of the three v15 assets, filled after minting. Until then each
#: value is the literal placeholder below and the freeze test is red — first
#: because the assets do not exist, then because the measured digest does not
#: equal the placeholder, until the operator pastes the real one in.
V15_ASSETS = {
    "brain-v42-v15.json": "db8a31d2bf594051a6dee9f4889155d6901a6a423e03d425577e4d7f6f3b1d2e",
    "brain-v42-v15.sql": "512efe488386192bc90d9321d0116748c85a1d14d8bcbe153e60496e2003e1aa",
    "brain-v42-v15-pgrestore.sql": "6e028f17db48c43937b0615d2b424721906007700b18074e0760a8b89b047d54",
}

V14_SQL = RECOVERY / "brain-v42-v14.sql"
V14_PGRESTORE = RECOVERY / "brain-v42-v14-pgrestore.sql"
V14_JSON = RECOVERY / "brain-v42-v14.json"
V15_SQL = RECOVERY / "brain-v42-v15.sql"
V15_PGRESTORE = RECOVERY / "brain-v42-v15-pgrestore.sql"
V15_JSON = RECOVERY / "brain-v42-v15.json"

#: The `knowledge_claims_provenance_valid` CHECK fingerprints v14 carries, in
#: each variant: the base measured against a chain-built database, the twin
#: against a real restore — different because pg_restore re-serialises the
#: `IN (...)` cast syntax. Both are the two-value CHECK 058 replaces.
V14_PROVENANCE_CHECK_MD5_BASE = "41d43cbdb380d7d6c1f27f5feecac838"
V14_PROVENANCE_CHECK_MD5_TWIN = "bb7f9e9df77db4214367222b680991b9"

PROVENANCE_KEY = ("knowledge_claims", "knowledge_claims_provenance_valid")

#: Lines v15 REMOVES from v14, in each variant, and every one accounted for:
#: the two identity lines and the rewritten provenance CHECK fingerprint.
#: Nothing else leaves: 058 adds no table, no column, no index and no trigger,
#: so no other VALUES row moves.
REMOVABLE_SQL_LINES = {
    " 'contract_id', 'brain-v42/postgresql-recovery/v14',",
    " 'schema_version', 14",
    f"         '{V14_PROVENANCE_CHECK_MD5_BASE}'",
}

TWIN_REMOVABLE_SQL_LINES = {
    " 'contract_id', 'brain-v42/postgresql-recovery/v14',",
    " 'schema_version', 14",
    f"         '{V14_PROVENANCE_CHECK_MD5_TWIN}'",
}

_PAIR_ROW = re.compile(r"\(\s*'([^']+)',\s*'([0-9a-f]{32})'\s*\)")
_TRIPLE_ROW = re.compile(r"\(\s*'([^']+)',\s*'([^']+)',\s*'([0-9a-f]{32})'\s*\)")
_FINGERPRINT_LINE = re.compile(r"         '[0-9a-f]{32}'")


def _sha256(name: str) -> str:
    return hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()


def _added_and_removed(before: Path, after: Path) -> tuple[list[str], list[str]]:
    diff = list(
        difflib.ndiff(
            before.read_text(encoding="utf-8").splitlines(),
            after.read_text(encoding="utf-8").splitlines(),
        )
    )
    return (
        [line[2:] for line in diff if line.startswith("+ ")],
        [line[2:] for line in diff if line.startswith("- ")],
    )


def _block(asset: Path, name: str) -> str:
    text = asset.read_text(encoding="utf-8")
    start = text.index(f"{name}(")
    return text[start : text.index("\n),", start)]


def _column_rows(asset: Path) -> dict[str, str]:
    return {m[0]: m[1] for m in _PAIR_ROW.findall(_block(asset, "expected_table_columns"))}


def _constraint_rows(asset: Path) -> dict[tuple[str, str], str]:
    return {
        (m[0], m[1]): m[2] for m in _TRIPLE_ROW.findall(_block(asset, "expected_table_constraints"))
    }


def test_v14_recovery_assets_remain_byte_identical() -> None:
    """The previous generation is lineage: it never changes again."""
    for name, expected in V14_ASSETS.items():
        assert _sha256(name) == expected


def test_v15_assets_freeze_only_the_058_delta() -> None:
    """v14 hashes are frozen; v15 moves the identity and the CHECK fingerprint.

    The three v15 SHA-256 values sit in `V15_ASSETS` as placeholders until the
    mint has measured them, and this test stays red until the operator fills
    them. The assets themselves must never carry a placeholder: every fingerprint
    inside them is a measured 32-hex md5, never a guess.
    """
    for name, expected in V14_ASSETS.items():
        assert _sha256(name) == expected

    for name, expected in V15_ASSETS.items():
        assert _sha256(name) == expected, (
            f"{name}: SHA-256 does not match the pinned value — mint the v15 asset "
            f"then paste its measured digest into V15_ASSETS"
        )

    for asset in (V15_SQL, V15_PGRESTORE):
        text = asset.read_text(encoding="utf-8")
        assert "REPLACE_AFTER_MINT" not in text
        assert "postgresql-recovery/v14" not in text
        assert "'brain-v42/postgresql-recovery/v15'" in text
        assert " 'schema_version', 15" in text


def test_v15_sql_assets_are_the_058_candidate() -> None:
    for before, after, removable in (
        (V14_SQL, V15_SQL, REMOVABLE_SQL_LINES),
        (V14_PGRESTORE, V15_PGRESTORE, TWIN_REMOVABLE_SQL_LINES),
    ):
        added, removed = _added_and_removed(before, after)
        assert set(removed) == removable, after.name
        fingerprints = [line for line in added if _FINGERPRINT_LINE.fullmatch(line)]
        assert len(fingerprints) == 1, after.name
        text = after.read_text(encoding="utf-8")
        assert "postgresql-recovery/v14" not in text
        assert "'brain-v42/postgresql-recovery/v15'" in text
        assert " 'schema_version', 15" in text


def test_the_provenance_constraint_row_is_rewritten_not_duplicated() -> None:
    """One row per constraint, before and after: a rewrite that APPENDED would
    leave the stale fingerprint beside the new one, and `table_constraint_mismatches`
    would count it on every replay."""
    for before, after in ((V14_SQL, V15_SQL), (V14_PGRESTORE, V15_PGRESTORE)):
        old, new = _constraint_rows(before), _constraint_rows(after)
        assert set(new) == set(old)
        assert old[PROVENANCE_KEY] != new[PROVENANCE_KEY]
        assert {key for key in new if new[key] != old[key]} == {PROVENANCE_KEY}


def test_058_changes_only_the_provenance_check() -> None:
    """Unlike 056, 058 is a bare `drop_constraint` + `create_check_constraint`:
    every block other than the constraint block is untouched.

    The base and the twin variants therefore carry IDENTICAL column, index and
    trigger-function blocks to v14's — measured on the 058 database before
    writing, not assumed from the migration source alone.
    """
    for block in (
        "expected_table_columns",
        "expected_table_indexes",
        "expected_trigger_functions",
    ):
        for before, after in ((V14_SQL, V15_SQL), (V14_PGRESTORE, V15_PGRESTORE)):
            assert _block(before, block) == _block(after, block), block


def test_v15_manifest_is_v14_with_only_the_identity_moved() -> None:
    """The manifest moves two values and nothing else — 058 adds no table and
    no index, so `catalog_counts` and `table_set` stay v14's."""
    v14 = json.loads(V14_JSON.read_text(encoding="utf-8"))
    v15 = json.loads(V15_JSON.read_text(encoding="utf-8"))
    assert v15["contract_id"] == "brain-v42/postgresql-recovery/v15"
    assert v15["schema_version"] == 15

    v14["contract_id"] = v15["contract_id"]
    v14["schema_version"] = v15["schema_version"]
    assert v15 == v14
    assert V15_JSON.read_text(encoding="utf-8") == (
        json.dumps(v15, sort_keys=True, separators=(",", ":")) + "\n"
    )


def test_058_ships_no_acl_asset_because_it_grants_nothing() -> None:
    """058 contains no GRANT and no REVOKE, so the ACL authority stays at v11."""
    for suffix in ("-acl.sql", "-acl.json", "-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v15{suffix}").exists()
        assert (RECOVERY / f"brain-v42-v11{suffix}").is_file()
