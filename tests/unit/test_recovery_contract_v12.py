"""Recovery contract v12 — a candidate for schema head 055.

The generation is immutable lineage, not a production receipt. Its base and
``pg_restore`` variants are v11 plus insertions for 055's three claim ledgers,
measured with each asset's OWN recipes on a disposable chain-built database at
head 055 — never retyped, because guessing an md5 expression produces hashes
that never match. The executable yardstick separately proves those
fingerprints replay against a fresh head.

v12 covers more than the six blocks a table-shaped migration usually touches,
and that is the point of this generation: a Codex review on 2026-09-21 found
the first cut attested 055's five trigger FUNCTIONS while leaving their five
BINDINGS, the `knowledge_claim_current` view and two sequence high-water marks
outside the contract entirely. A check that does not know an object cannot
fail on it, so replaying clean proved self-consistency, not coverage.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V11_ASSETS = {
    "brain-v42-v11.json": "b7022b4a981d1c9805bf6ea4259b4727ed284784320a77e8ae78c5657ad509c4",
    "brain-v42-v11.sql": "0cabf7945dba8fdccc84d29b64a61304621537d17b75dc88d6792851df84a61c",
    "brain-v42-v11-pgrestore.sql": "f2c05ac82665aef5e103ed7224727867f449dfba9a063e301c979f16cc7dbb40",
    "brain-v42-v11-acl.sql": "767e89573bca09bb821468bfb6cfe616e96c7d6a81774fd2a5df3c82e871e2d2",
    "brain-v42-v11-acl.json": "bbff3fad6b7756a29f80097ae9451ebbe5e306c32169f1d43cae17b9323183e9",
    "brain-v42-v11-acl-pgrestore.sql": (
        "3e434e425080afa625dd6f163bc27f06d1a37602b0aa51b12df918df95e37de7"
    ),
    "brain-v42-v11-acl-pgrestore.json": (
        "43a580d16b5b40e9c4e3466241a4419b4b25df823e4bc58c89bfdf72d799dd02"
    ),
}
V12_ASSETS = {
    "brain-v42-v12.json": "38a682cf71bb20b925086013c0ece058badb2c6c972baf334113d54b0b65322e",
    "brain-v42-v12.sql": "c77d1a8189238782d0b396517d7706c51ad5ffef3ed799abd16c852b8c31372c",
    "brain-v42-v12-pgrestore.sql": (
        "0dd58dafc85bf4075b71f49169654573d80cfa24e7e36e20b851ddb6ce3f507c"
    ),
}

V11_SQL = RECOVERY / "brain-v42-v11.sql"
V11_PGRESTORE = RECOVERY / "brain-v42-v11-pgrestore.sql"
V12_SQL = RECOVERY / "brain-v42-v12.sql"
V12_PGRESTORE = RECOVERY / "brain-v42-v12-pgrestore.sql"
V12_JSON = RECOVERY / "brain-v42-v12.json"

CLAIM_TABLES = {
    "knowledge_claims",
    "knowledge_claim_verdicts",
    "knowledge_fact_definitions",
}

#: Objects 055 adds BEYOND its three tables. Enumerated because the first cut
#: of this mint silently omitted all of them.
CLAIM_OBJECTS = {
    "knowledge_claim_current",
    "knowledge_claims_insert_gate",
    "knowledge_claims_update_gate",
    "knowledge_claims_delete_gate",
    "knowledge_claim_verdicts_append_only",
    "knowledge_fact_definitions_append_only",
    "knowledge_claims_seq_seq",
    "knowledge_claim_verdicts_seq_seq",
}

EXPECTED_CATALOG = {"foreign_keys": 53, "indexes": 171}

#: Lines v12 REMOVES from v11 — the only non-additive part of the mint, and
#: every one of them accounted for. Two counts, two identity lines, two
#: ORDER BY clauses that gained `COLLATE "C"`, and seven last rows of a VALUES
#: block that lost their terminal position when the mint appended after them.
REMOVABLE_SQL_LINES = {
    "         'foreign_keys', 48,",
    "         'indexes', 160,",
    "         SELECT COALESCE(jsonb_agg(tablename ORDER BY tablename), '[]'::jsonb)",
    "     ('codex_ticket_v1', TRUE, '9b248acd2480ea9ac252e3cbef09b47a')",
    "     ('delivery_workflows')",
    (
        "     ('ticket_extraction_proposals_id_seq', 'ticket_extraction_proposals', "
        "'id', 'bigint', 1, 1, 9223372036854775807, 1, FALSE)"
    ),
    (
        "     ('ticket_extraction_proposals_id_seq', "
        "(SELECT max(id) FROM ticket_extraction_proposals))"
    ),
    "     ('tickets')",
    "     ('tickets', 'trg_ticket_participants_immutable')",
    (
        "     ('update_updated_at', "
        "'83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59', 96)"
    ),
    "     (SELECT jsonb_agg(table_name ORDER BY table_name) FROM expected_tables) AS expected,",
    " 'contract_id', 'brain-v42/postgresql-recovery/v11',",
    " 'schema_version', 11",
}

_CONSTRAINT_ROW = re.compile(r"\(\s*'([^']+)',\s*'([^']+)',\s*'([0-9a-f]{32})'\s*\)")


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


def _constraint_rows(asset: Path) -> dict[tuple[str, str], str]:
    text = asset.read_text(encoding="utf-8")
    start = text.index("expected_table_constraints(")
    body = text[start : text.index("\n),", start)]
    return {(m[0], m[1]): m[2] for m in _CONSTRAINT_ROW.findall(body)}


def test_v11_recovery_assets_remain_byte_identical() -> None:
    """The previous generation is lineage: it never changes again."""
    for name, expected in V11_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == expected


def test_v12_candidate_assets_freeze_the_measured_fingerprints() -> None:
    for name, expected in V12_ASSETS.items():
        assert hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() == expected


def test_v12_sql_assets_are_an_additive_055_candidate() -> None:
    for before, after in ((V11_SQL, V12_SQL), (V11_PGRESTORE, V12_PGRESTORE)):
        added, removed = _added_and_removed(before, after)
        assert set(removed) == REMOVABLE_SQL_LINES
        assert added
        text = after.read_text(encoding="utf-8")
        assert "postgresql-recovery/v11" not in text
        assert "'brain-v42/postgresql-recovery/v12'" in text
        for table in CLAIM_TABLES:
            assert f"'{table}'" in text
        for name in CLAIM_OBJECTS:
            assert f"'{name}'" in text, f"{name} is attested by nothing in {after.name}"


def test_the_two_variants_attest_the_same_objects() -> None:
    """The twin adds the same OBJECTS, with six fingerprints of its own.

    v11's equivalent test could assert the two variants gained identical lines,
    because 054's objects happened to serialise the same on both sides. 055's
    do not: the twin normalises `::character varying::text` and `]::text[]`
    because pg_restore re-serialises those casts, so every `varchar IN (…)`
    check differs — 49 across the whole asset, six of them 055's. Comparing
    LINES here would fail for a reason that is correct; comparing objects is
    the assertion that carries meaning.
    """
    base = _constraint_rows(V12_SQL)
    twin = _constraint_rows(V12_PGRESTORE)
    assert set(base) == set(twin)

    claim_constraints = {key for key in base if key[0] in CLAIM_TABLES}
    assert len(claim_constraints) == 30
    differing = {key for key in claim_constraints if base[key] != twin[key]}
    assert len(differing) == 6, sorted(differing)


def test_v12_manifest_is_the_current_055_catalog() -> None:
    document = json.loads(V12_JSON.read_text(encoding="utf-8"))
    assert document["contract_id"] == "brain-v42/postgresql-recovery/v12"
    assert document["schema_version"] == 12
    checks = {check["id"]: check for check in document["checks"]}
    assert {key: checks["catalog_counts"][key] for key in EXPECTED_CATALOG} == EXPECTED_CATALOG
    assert CLAIM_TABLES <= set(checks["table_set"]["tables"])
    assert len(checks["table_set"]["tables"]) == 47
    assert checks["table_set"]["tables"] == sorted(checks["table_set"]["tables"])


def test_the_table_set_comparison_is_collation_stable() -> None:
    """Both sides of `table_set` sort under C, and that is not cosmetic.

    `expected_tables` is `text` (database collation) while `pg_tables.tablename`
    is `name` (C). With 44 tables no pair differed; `knowledge_claims` and
    `knowledge_claim_verdicts` differ exactly on the `_`/`s` boundary, and the
    check went red with IDENTICAL sets. Pinning C also makes the contract
    portable: a restore target with another lc_collate would otherwise produce
    a false red on a healthy restoration.
    """
    for asset in (V12_SQL, V12_PGRESTORE):
        text = asset.read_text(encoding="utf-8")
        assert 'ORDER BY table_name COLLATE "C"' in text
        assert 'ORDER BY tablename COLLATE "C"' in text
        assert "ORDER BY table_name)" not in text
        assert "ORDER BY tablename)" not in text


def test_055_ships_no_acl_asset_because_it_grants_nothing_the_contract_sees() -> None:
    """Measured 2026-09-21, not inferred: both v11 ACL assets replay with ZERO
    failures against a chain-built 055 database, so the ACL authority does not
    move to a v12. 055 does mention GRANT, but the three new tables carry only
    their owner's default privileges — the grants are inert against a superuser
    application role (learning d857f21c), which is also why the append-only
    guarantee lives in triggers and not in privileges.
    """
    for suffix in ("-acl.sql", "-acl.json", "-acl-pgrestore.sql", "-acl-pgrestore.json"):
        assert not (RECOVERY / f"brain-v42-v12{suffix}").exists()
        assert (RECOVERY / f"brain-v42-v11{suffix}").is_file()
