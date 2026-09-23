"""Recovery contract v13 — a candidate for schema head 056.

The generation is immutable lineage, not a production receipt. Its base and
``pg_restore`` variants are v12 plus 056's footprint on ``project_contexts``:
two nullable columns, one CHECK binding only that pair, one partial index. Each
variant was measured with its OWN recipes — the base against a disposable
database the alembic chain built at head 056, the twin against a real
custom-format ``pg_dump``/``pg_restore`` of that same database — never retyped,
because guessing an md5 expression produces hashes that never match.

v13 is the first mint since v7 that is not purely additive, and the reason is
structural rather than a choice: ``expected_table_columns`` carries ONE md5 per
table over its whole column list, so two new columns REWRITE the
``project_contexts`` row instead of appending one. The mint refuses every other
rewrite (its key-collision guard); this file enumerates the one it allows, the
same way it enumerates the counts and the identity lines.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V12_ASSETS = {
    "brain-v42-v12.json": "38a682cf71bb20b925086013c0ece058badb2c6c972baf334113d54b0b65322e",
    "brain-v42-v12.sql": "c77d1a8189238782d0b396517d7706c51ad5ffef3ed799abd16c852b8c31372c",
    "brain-v42-v12-pgrestore.sql": (
        "0dd58dafc85bf4075b71f49169654573d80cfa24e7e36e20b851ddb6ce3f507c"
    ),
}

V13_ASSETS = {
    "brain-v42-v13.json": "6602aab4fb037091841b1e0c3d00456b6d37a5e4c99e1ce885415ad6df0b1f86",
    "brain-v42-v13.sql": "8579f6089389db2d34653a98e19d118b336bca548ac11dfbb26a9ea5b2e0aeb2",
    "brain-v42-v13-pgrestore.sql": (
        "e1adf402b2c39b83c99c5bbb7fe8eda677a1dc5e89b526147432c0549ef7ad03"
    ),
}

V12_SQL = RECOVERY / "brain-v42-v12.sql"
V12_PGRESTORE = RECOVERY / "brain-v42-v12-pgrestore.sql"
V12_JSON = RECOVERY / "brain-v42-v12.json"
V13_SQL = RECOVERY / "brain-v42-v13.sql"
V13_PGRESTORE = RECOVERY / "brain-v42-v13-pgrestore.sql"
V13_JSON = RECOVERY / "brain-v42-v13.json"

#: The objects 056 adds, by name. Each must be attested by both variants.
ARCHIVAL_CHECK = "ck_project_contexts_archived_reason_needs_a_date"
ARCHIVAL_INDEX = "idx_project_contexts_archived"

#: 056 moves one count and no other: a partial index, and no foreign key.
EXPECTED_CATALOG = {"foreign_keys": 53, "indexes": 172}

#: The `project_contexts` column fingerprint v12 carries — identical in both
#: variants — and the one line the rewrite takes away.
V12_PROJECT_CONTEXTS_COLUMNS_MD5 = "edbdc6262165d235ade64e4f67da0aa5"

#: Lines v13 REMOVES from v12, in each variant, and every one accounted for:
#: the index count, the two identity lines, and the rewritten column
#: fingerprint. Nothing else leaves: 056 adds no table, so no single-line
#: VALUES row loses its terminal position.
REMOVABLE_SQL_LINES = {
    "         'indexes', 171,",
    " 'contract_id', 'brain-v42/postgresql-recovery/v12',",
    " 'schema_version', 12",
    f"         '{V12_PROJECT_CONTEXTS_COLUMNS_MD5}'",
}

#: `knowledge_claim_current` (055) as `pg_get_viewdef` renders it on the chain-built
#: database and in production, and as it renders after a real `pg_dump -Fc` /
#: `pg_restore`. The view filters `verdict IN ('holds', 'falsified')` on a varchar
#: column, and PostgreSQL does not deparse that idempotently: the restored view says
#: `ARRAY['holds'::character varying::text, ...]` where the source said
#: `ARRAY['holds'::character varying, ...]::text[]`. Same meaning, other md5.
#: Measured on 2026-09-23 on a real restore of the production pre056 dump: the
#: twin inherited the chain-built value from v12 without re-measuring views, and
#: failed `view_definition_mismatches` on the first real restore (ticket 1ec33903).
KNOWLEDGE_CLAIM_CURRENT_CHAIN_MD5 = "30fdf69fd0413f03c00ae3ca2ca4e756"
KNOWLEDGE_CLAIM_CURRENT_RESTORED_MD5 = "6565fd7e2fe5e52a9c5175651964f34e"
V12_TWIN_VIEW_LINE = (
    f"     ('knowledge_claim_current', FALSE, '{KNOWLEDGE_CLAIM_CURRENT_CHAIN_MD5}')"
)

_TRIPLE_ROW = re.compile(r"\(\s*'([^']+)',\s*'([^']+)',\s*'([0-9a-f]{32})'\s*\)")
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


def _triple_rows(asset: Path, name: str) -> dict[tuple[str, str], str]:
    return {(m[0], m[1]): m[2] for m in _TRIPLE_ROW.findall(_block(asset, name))}


def _column_rows(asset: Path) -> dict[str, str]:
    return {m[0]: m[1] for m in _PAIR_ROW.findall(_block(asset, "expected_table_columns"))}


def test_v12_recovery_assets_remain_byte_identical() -> None:
    """The previous generation is lineage: it never changes again."""
    for name, expected in V12_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == expected


def test_v13_candidate_assets_freeze_the_measured_fingerprints() -> None:
    for name, expected in V13_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == expected


def test_v13_sql_assets_are_the_056_candidate() -> None:
    for before, after, removable in (
        (V12_SQL, V13_SQL, REMOVABLE_SQL_LINES),
        # The twin also corrects the one view row v12 carried in its chain form.
        (V12_PGRESTORE, V13_PGRESTORE, REMOVABLE_SQL_LINES | {V12_TWIN_VIEW_LINE}),
    ):
        added, removed = _added_and_removed(before, after)
        assert set(removed) == removable, after.name
        assert "         'indexes', 172," in added
        text = after.read_text(encoding="utf-8")
        assert "postgresql-recovery/v12" not in text
        assert "'brain-v42/postgresql-recovery/v13'" in text
        assert " 'schema_version', 13" in text
        assert f"'{ARCHIVAL_CHECK}'" in text, f"056's CHECK is attested by nothing in {after.name}"
        assert f"'{ARCHIVAL_INDEX}'" in text, f"056's index is attested by nothing in {after.name}"


def test_the_project_contexts_column_row_is_rewritten_not_duplicated() -> None:
    """One row per table, before and after: a rewrite that APPENDED would leave
    the stale fingerprint beside the new one, and `table_column_mismatches`
    would count it on every replay."""
    for before, after in ((V12_SQL, V13_SQL), (V12_PGRESTORE, V13_PGRESTORE)):
        old, new = _column_rows(before), _column_rows(after)
        assert set(new) == set(old)
        assert old["project_contexts"] == V12_PROJECT_CONTEXTS_COLUMNS_MD5
        assert new["project_contexts"] != V12_PROJECT_CONTEXTS_COLUMNS_MD5
        assert {table for table in new if new[table] != old[table]} == {"project_contexts"}
    base_row = _column_rows(V13_SQL)["project_contexts"]
    assert base_row == _column_rows(V13_PGRESTORE)["project_contexts"]


def test_the_two_variants_attest_the_same_056_objects() -> None:
    """Same objects on both sides, and for 056 the same fingerprints too.

    Measured, not assumed: the twin's rows come from a real restore, and none
    of 056's three objects carries a cast pg_restore re-serialises — unlike
    055's `varchar IN (…)` checks, which is why v12 had to compare objects
    rather than lines.
    """
    for block, key in (
        ("expected_table_constraints", ("project_contexts", ARCHIVAL_CHECK)),
        ("expected_table_indexes", ("project_contexts", ARCHIVAL_INDEX)),
    ):
        base = _triple_rows(V13_SQL, block)
        twin = _triple_rows(V13_PGRESTORE, block)
        assert set(base) == set(twin)
        assert set(base) - set(_triple_rows(V12_SQL, block)) == {key}
        assert base[key] == twin[key]


def _view_rows(asset: Path) -> dict[str, str]:
    block = _block(asset, "expected_contract_views")
    return {
        m[0]: m[2]
        for m in re.findall(r"\(\s*'([^']+)',\s*(TRUE|FALSE),\s*'([0-9a-f]{32})'\s*\)", block)
    }


def test_the_twin_attests_knowledge_claim_current_in_its_restored_form() -> None:
    """The base asset replays against production, the twin against a restore.

    Each must carry the form its target really renders; one shared md5 makes one
    of the two replays fail by construction.
    """
    base, twin = _view_rows(V13_SQL), _view_rows(V13_PGRESTORE)
    assert base["knowledge_claim_current"] == KNOWLEDGE_CLAIM_CURRENT_CHAIN_MD5
    assert twin["knowledge_claim_current"] == KNOWLEDGE_CLAIM_CURRENT_RESTORED_MD5
    assert {name for name in base if base[name] != twin[name]} == {"knowledge_claim_current"}


def test_v13_manifest_is_v12_with_the_056_catalog() -> None:
    """The manifest moves three values and nothing else — 056 adds no table."""
    v12 = json.loads(V12_JSON.read_text(encoding="utf-8"))
    v13 = json.loads(V13_JSON.read_text(encoding="utf-8"))
    assert v13["contract_id"] == "brain-v42/postgresql-recovery/v13"
    assert v13["schema_version"] == 13
    checks = {check["id"]: check for check in v13["checks"]}
    assert {key: checks["catalog_counts"][key] for key in EXPECTED_CATALOG} == EXPECTED_CATALOG

    for check in v12["checks"]:
        if check["id"] == "catalog_counts":
            check["indexes"] = EXPECTED_CATALOG["indexes"]
    v12["contract_id"] = v13["contract_id"]
    v12["schema_version"] = v13["schema_version"]
    assert v13 == v12
    assert V13_JSON.read_text(encoding="utf-8") == (
        json.dumps(v13, sort_keys=True, separators=(",", ":")) + "\n"
    )


def test_056_ships_no_acl_asset_because_it_grants_nothing() -> None:
    """Measured 2026-09-22, not inferred: both v11 ACL assets replay with ZERO
    failures against a chain-built 056 database and against a real restore of
    it. 056 contains no GRANT and no REVOKE, so the ACL authority stays at v11.
    """
    for suffix in ("-acl.sql", "-acl.json", "-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v13{suffix}").exists()
        assert (RECOVERY / f"brain-v42-v11{suffix}").is_file()
