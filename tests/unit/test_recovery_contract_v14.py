"""Recovery contract v14 — a candidate for schema head 057.

The generation is immutable lineage, not a production receipt. Its base and
``pg_restore`` variants are v13 plus 057's footprint: one nullable column on
``search_log``, no index, no CHECK, no trigger. Each variant was measured with
its OWN recipe — the base against a disposable database the alembic chain
built at head 057, the twin against a real custom-format ``pg_dump``/
``pg_restore`` of that same database — never retyped, because guessing an md5
expression produces hashes that never match.

Narrower than v13: ``expected_table_columns`` carries ONE md5 per table over
its whole column list, so 057's single new column REWRITES the ``search_log``
row instead of appending one — the same shape v13 forced onto
``project_contexts``, but touching no other block. Measured before writing:
the base and the twin land on the SAME fingerprint for ``search_log``, unlike
055's `knowledge_claim_current` view, because a plain nullable `TEXT` column
carries no cast `pg_restore` re-serialises.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V13_ASSETS = {
    "brain-v42-v13.json": "6602aab4fb037091841b1e0c3d00456b6d37a5e4c99e1ce885415ad6df0b1f86",
    "brain-v42-v13.sql": "8579f6089389db2d34653a98e19d118b336bca548ac11dfbb26a9ea5b2e0aeb2",
    "brain-v42-v13-pgrestore.sql": (
        "e1adf402b2c39b83c99c5bbb7fe8eda677a1dc5e89b526147432c0549ef7ad03"
    ),
}

V14_ASSETS = {
    "brain-v42-v14.json": "c6f963bf17450c7db340716b8c396b3358dec4e745369d5f245aaa36b4a48e60",
    "brain-v42-v14.sql": "0264e4b5b26106c440ef30cfc6b38a3b2e547a8b929ea6204432ac3e6308fd36",
    "brain-v42-v14-pgrestore.sql": (
        "1333d01eb31b0e7cfadb4029c67bd80d743cf8a9f758c2ae389492e11a1e665a"
    ),
}

V13_SQL = RECOVERY / "brain-v42-v13.sql"
V13_PGRESTORE = RECOVERY / "brain-v42-v13-pgrestore.sql"
V13_JSON = RECOVERY / "brain-v42-v13.json"
V14_SQL = RECOVERY / "brain-v42-v14.sql"
V14_PGRESTORE = RECOVERY / "brain-v42-v14-pgrestore.sql"
V14_JSON = RECOVERY / "brain-v42-v14.json"

#: The `search_log` column fingerprint v13 carries — identical in both
#: variants — and the one line the rewrite takes away.
V13_SEARCH_LOG_COLUMNS_MD5 = "2460b7be646c241e769f01253305c8b3"

#: 057's rewritten fingerprint, measured on a chain-built AND a restored
#: database: both land on the same value, so base and twin share it.
V14_SEARCH_LOG_COLUMNS_MD5 = "0f3bafdd11070755f919ad21ebec7bed"

#: Lines v14 REMOVES from v13, in each variant, and every one accounted for:
#: the two identity lines and the rewritten column fingerprint. Nothing else
#: leaves: 057 adds no table and no index, so no VALUES row loses its terminal
#: position.
REMOVABLE_SQL_LINES = {
    " 'contract_id', 'brain-v42/postgresql-recovery/v13',",
    " 'schema_version', 13",
    f"         '{V13_SEARCH_LOG_COLUMNS_MD5}'",
}

_PAIR_ROW = re.compile(r"\(\s*'([^']+)',\s*'([0-9a-f]{32})'\s*\)")


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


def test_v13_recovery_assets_remain_byte_identical() -> None:
    """The previous generation is lineage: it never changes again."""
    for name, expected in V13_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == expected


def test_v14_candidate_assets_freeze_the_measured_fingerprints() -> None:
    for name, expected in V14_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == expected


def test_v14_sql_assets_are_the_057_candidate() -> None:
    for before, after in ((V13_SQL, V14_SQL), (V13_PGRESTORE, V14_PGRESTORE)):
        added, removed = _added_and_removed(before, after)
        assert set(removed) == REMOVABLE_SQL_LINES, after.name
        assert f"         '{V14_SEARCH_LOG_COLUMNS_MD5}'" in added
        text = after.read_text(encoding="utf-8")
        assert "postgresql-recovery/v13" not in text
        assert "'brain-v42/postgresql-recovery/v14'" in text
        assert " 'schema_version', 14" in text


def test_the_search_log_column_row_is_rewritten_not_duplicated() -> None:
    """One row per table, before and after: a rewrite that APPENDED would leave
    the stale fingerprint beside the new one, and `table_column_mismatches`
    would count it on every replay."""
    for before, after in ((V13_SQL, V14_SQL), (V13_PGRESTORE, V14_PGRESTORE)):
        old, new = _column_rows(before), _column_rows(after)
        assert set(new) == set(old)
        assert old["search_log"] == V13_SEARCH_LOG_COLUMNS_MD5
        assert new["search_log"] == V14_SEARCH_LOG_COLUMNS_MD5
        assert {table for table in new if new[table] != old[table]} == {"search_log"}
    base_row = _column_rows(V14_SQL)["search_log"]
    assert base_row == _column_rows(V14_PGRESTORE)["search_log"]


def test_057_adds_no_constraint_no_index_and_no_trigger() -> None:
    """Unlike 056, 057 is a bare `add_column`: every other block is untouched.

    The base and the twin variants therefore carry IDENTICAL constraint,
    index and trigger-function blocks to v13's — measured on the 057
    database before writing, not assumed from the migration source alone.
    """
    for block in (
        "expected_table_constraints",
        "expected_table_indexes",
        "expected_trigger_functions",
    ):
        for before, after in ((V13_SQL, V14_SQL), (V13_PGRESTORE, V14_PGRESTORE)):
            assert _block(before, block) == _block(after, block), block


def test_v14_manifest_is_v13_with_only_the_identity_moved() -> None:
    """The manifest moves two values and nothing else — 057 adds no table and
    no index, so `catalog_counts` and `table_set` stay v13's."""
    v13 = json.loads(V13_JSON.read_text(encoding="utf-8"))
    v14 = json.loads(V14_JSON.read_text(encoding="utf-8"))
    assert v14["contract_id"] == "brain-v42/postgresql-recovery/v14"
    assert v14["schema_version"] == 14

    v13["contract_id"] = v14["contract_id"]
    v13["schema_version"] = v14["schema_version"]
    assert v14 == v13
    assert V14_JSON.read_text(encoding="utf-8") == (
        json.dumps(v14, sort_keys=True, separators=(",", ":")) + "\n"
    )


def test_057_ships_no_acl_asset_because_it_grants_nothing() -> None:
    """057 contains no GRANT and no REVOKE, so the ACL authority stays at v11."""
    for suffix in ("-acl.sql", "-acl.json", "-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v14{suffix}").exists()
        assert (RECOVERY / f"brain-v42-v11{suffix}").is_file()
