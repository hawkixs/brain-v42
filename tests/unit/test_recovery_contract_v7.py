"""Recovery contract v7 -- the 049 mint: a dream_runs series, a widened freshness vocabulary.

Why a v7: migration 049 landed in production on 2026-08-30, after v6 was minted
against head 048 on 2026-08-29. Replayed live on 2026-09-02 the v6 receipt renders
29/30, and the single red is `table_shape` -- `table_column_mismatches: 1`,
`table_constraint_mismatches: 6`, indexes 0. Both numbers are consequences of the
migration, neither is a degradation:

* `dream_runs` gained `closed_inactive_count` and `thinking_tokens` (19 columns
  now), so its column fingerprint moved -- that is the 1.
* `freshness_source` accepted two more words, `plan_reindex` and `manual_update`,
  on the six tables the decay tracks, so those six CHECK constraints were dropped
  and recreated -- that is the 6.

The S1 lineage (decision `9d22bc6a`) answers that with a mint, never a rewrite:
v6 stays frozen byte for byte and keeps describing head 048, and v7 describes 049.

Three things this mint does NOT change, each proven rather than assumed:

1. **The JSON receipt is identity-only.** 049 adds no index, no table, no foreign
   key and no unvalidated constraint, so `catalog_counts` (131 indexes, 26 foreign
   keys), `table_set` and every other declared check survive untouched. v7.json is
   v6.json with two strings moved -- and this module derives it rather than
   trusting it.

2. **The two SQL variants are a 7-value delta of v6, and nothing else.** One
   column fingerprint plus six constraint fingerprints, in each variant. The test
   below rebuilds both v7 assets from the frozen v6 bytes and demands equality:
   an opportunistic edit smuggled into a mint fails here.

3. **The ACL pair does not move at all.** 049 grants nothing, creates no view and
   drops none, so v7-acl is v6-acl at its identity -- exactly as v6-acl was v5-acl.
   `test_the_acl_grant_list_is_derived_from_every_migration` proves the premise
   instead of asserting it: it recomputes the grantee list from *all* migrations,
   not just 036 and 045, so the next codex view added without a re-mint reddens CI.

How the `-pgrestore` twin's six new fingerprints were obtained, since no restored
bench was available on 2026-09-02: the twin canonicalizes what it observes
(`::character varying::text` -> `::character varying`, `]::text[]` -> `]`), and
that canonicalization is exactly what absorbs the pg_dump/pg_restore round trip.
So the twin's expected value is `md5(canonicalize(source_definition))`, computable
read-only against production. That is not an assumption here, it is measured: run
against live production, the canonicalized form reproduces v6's twin fingerprint
for 112 of the 118 constraints -- every single one that 049 did not touch. The six
that differ are the six this mint exists for.

What is therefore NOT proven by this module, and is written down instead of
glossed over: the v7 twin has never been replayed against a real restore. It is
derived, and its derivation is checked against a v6 twin that was benched.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RECOVERY = ROOT / "ops" / "recovery"
VERSIONS = ROOT / "alembic" / "versions"

V6_JSON = RECOVERY / "brain-v42-v6.json"
V6_SQL = RECOVERY / "brain-v42-v6.sql"
V6_PGRESTORE = RECOVERY / "brain-v42-v6-pgrestore.sql"
V6_ACL_SQL = RECOVERY / "brain-v42-v6-acl.sql"
V6_ACL_PGRESTORE = RECOVERY / "brain-v42-v6-acl-pgrestore.sql"

V7_JSON = RECOVERY / "brain-v42-v7.json"
V7_SQL = RECOVERY / "brain-v42-v7.sql"
V7_PGRESTORE = RECOVERY / "brain-v42-v7-pgrestore.sql"
V7_ACL_SQL = RECOVERY / "brain-v42-v7-acl.sql"
V7_ACL_JSON = RECOVERY / "brain-v42-v7-acl.json"
V7_ACL_PGRESTORE = RECOVERY / "brain-v42-v7-acl-pgrestore.sql"
V7_ACL_PGRESTORE_JSON = RECOVERY / "brain-v42-v7-acl-pgrestore.json"

#: The v6 assets are FROZEN to the byte, exactly as v6 froze v5 and v5 froze v4:
#: an attestation lineage is never rewritten, each contract describes the state it
#: described, and the next one is derived from the previous one.
V6_SHA256 = {
    "brain-v42-v6.json": "516b9f3d7a0cf7094b63b3343536460dad66d10abc383c50229a82a236def649",
    "brain-v42-v6.sql": "28fc2db9af76db1bb22c1b0a54107e4a750bbfe886825cb45bed01c13022aa38",
    "brain-v42-v6-pgrestore.sql": (
        "7ec5637ed095660198c4afe492257e7800ef6fbe012e33caa50b8f3e65d7caad"
    ),
    "brain-v42-v6-acl.sql": "cd79836469f19d38c0b540965e88289740062ee4eab4c7bf63877435092011b1",
    "brain-v42-v6-acl.json": "975d2af30b644bdc7e9b58baeb5d692ec9bc10c17f15365895766c423e1e0a72",
    "brain-v42-v6-acl-pgrestore.sql": (
        "fa9573c6b81f83a9c26aa2390784c54b7927dfba9e44abc3c75801579ec598af"
    ),
    "brain-v42-v6-acl-pgrestore.json": (
        "0e740f42e5406de82a58ab9b42200199ddedd126127b12710535d6e4661e835b"
    ),
}

#: The `dream_runs` column fingerprint, before and after 049 added
#: `closed_inactive_count` and `thinking_tokens`. Identical in both variants: a
#: nullable INTEGER column with no default survives a dump/restore round trip
#: unchanged, and v6 measured exactly that -- its 32 column fingerprints are the
#: same on both sides.
DREAM_RUNS_COLUMNS_048 = "bf67c985d3f29eca8d7e934b717a95f2"
DREAM_RUNS_COLUMNS_049 = "738f6bf6328d407c972ac0d65f49ca05"

#: The `ck_<table>_freshness_source` CHECK, before and after 049 admitted
#: `manual_update` and `plan_reindex`. The two variants disagree because a CHECK
#: comes back from `pg_restore` re-serialized; the twin absorbs that with its own
#: canonicalization, which is why the two columns below are different numbers for
#: the same constraint text.
FRESHNESS_SOURCE_CHECK_048 = "bc1d19c236b3b03de38cc13543da1b5d"
FRESHNESS_SOURCE_CHECK_049 = "308e6ee608483373e632a5ca0c2c1706"
FRESHNESS_SOURCE_CHECK_048_RESTORED = "829a8144b787e0f3b9ee7596037abbdd"
FRESHNESS_SOURCE_CHECK_049_RESTORED = "ca60d7872bdbc605de3055be32e3b182"

#: The six tables the decay tracks -- the same six 043 gave the CHECK to, and the
#: same six 049 widened.
DECAY_TABLES = ("adrs", "decisions", "indexed_plans", "learnings", "runbooks", "snippets")


def _mint(text: str, fingerprints: dict[str, str]) -> str:
    """Derive a v7 asset from a v6 one: identity, then the measured fingerprints."""
    minted = text.replace(
        "'brain-v42/postgresql-recovery/v6", "'brain-v42/postgresql-recovery/v7"
    ).replace("'schema_version', 6", "'schema_version', 7")
    for before, after in fingerprints.items():
        minted = minted.replace(before, after)
    return minted


def test_v6_recovery_assets_remain_byte_identical() -> None:
    for name, expected in V6_SHA256.items():
        digest = hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()
        assert digest == expected, f"{name} was rewritten -- the lineage forbids that"


def test_v7_json_is_the_exact_v6_delta() -> None:
    """049 adds no catalog object, so the receipt moves by its identity alone."""
    document = json.loads(V6_JSON.read_text(encoding="utf-8"))
    document["contract_id"] = "brain-v42/postgresql-recovery/v7"
    document["schema_version"] = 7
    document["checks"].sort(key=lambda check: check["id"])

    expected = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    assert V7_JSON.read_text(encoding="utf-8") == expected


def test_the_v7_identities_are_exact() -> None:
    assert "'brain-v42/postgresql-recovery/v7'" in V7_SQL.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v7'" in V7_PGRESTORE.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v7-acl'" in V7_ACL_SQL.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v7-acl-pgrestore'" in V7_ACL_PGRESTORE.read_text(
        encoding="utf-8"
    )
    for asset in (V7_SQL, V7_PGRESTORE, V7_ACL_SQL, V7_ACL_PGRESTORE):
        assert "'schema_version', 7" in asset.read_text(encoding="utf-8"), asset.name


def test_the_v7_sql_assets_are_the_derived_v6_delta_and_nothing_else() -> None:
    """Derived, never retyped -- and the derivation is a closed set of 7 values.

    This is the test that makes the whole mint auditable: rebuild both v7 assets
    from the frozen v6 bytes by substituting the measured fingerprints, and demand
    byte equality. A tidy-up smuggled in alongside the mint fails here, which is
    the point -- v5 froze v4 for the same reason.
    """
    live = _mint(
        V6_SQL.read_text(encoding="utf-8"),
        {
            DREAM_RUNS_COLUMNS_048: DREAM_RUNS_COLUMNS_049,
            FRESHNESS_SOURCE_CHECK_048: FRESHNESS_SOURCE_CHECK_049,
        },
    )
    assert V7_SQL.read_text(encoding="utf-8") == live

    restored = _mint(
        V6_PGRESTORE.read_text(encoding="utf-8"),
        {
            DREAM_RUNS_COLUMNS_048: DREAM_RUNS_COLUMNS_049,
            FRESHNESS_SOURCE_CHECK_048_RESTORED: FRESHNESS_SOURCE_CHECK_049_RESTORED,
        },
    )
    assert V7_PGRESTORE.read_text(encoding="utf-8") == restored


def test_v7_carries_the_049_mechanisms() -> None:
    """The mint encodes WHAT the migration changed, at the right multiplicity.

    Six tables, so six constraint fingerprints -- not five, not seven. Pinning the
    count is what catches a half-applied substitution, which byte equality alone
    would also catch but would not explain.
    """
    live = V7_SQL.read_text(encoding="utf-8")
    restored = V7_PGRESTORE.read_text(encoding="utf-8")

    for table in DECAY_TABLES:
        assert f"'ck_{table}_freshness_source'" in live, table
        assert f"'ck_{table}_freshness_source'" in restored, table

    assert live.count(FRESHNESS_SOURCE_CHECK_049) == len(DECAY_TABLES)
    assert restored.count(FRESHNESS_SOURCE_CHECK_049_RESTORED) == len(DECAY_TABLES)
    assert FRESHNESS_SOURCE_CHECK_048 not in live
    assert FRESHNESS_SOURCE_CHECK_048_RESTORED not in restored

    for asset in (live, restored):
        assert asset.count(DREAM_RUNS_COLUMNS_049) == 1
        assert DREAM_RUNS_COLUMNS_048 not in asset


def test_v7_keeps_the_047_and_048_mechanisms() -> None:
    """A mint inherits its ancestor's proofs; it does not quietly drop them."""
    for asset in (V7_SQL, V7_PGRESTORE):
        text = asset.read_text(encoding="utf-8")
        assert (
            "'cardinality(captured_knowledge_ids) = 0 and nothing_to_capture_reason is not null'"
            not in text
        ), f"{asset.name} still pins the XOR that 047 destroyed"
        assert "nothing_to_capture_reason is null or btrim(nothing_to_capture_reason)" in text
        assert "brain_session_artifacts_attribution_mode_valid" in text, asset.name
        assert "idx_brain_session_artifacts_derived_window" in text, asset.name
        assert "'indexes', 131," in text, asset.name


def test_the_two_v7_variants_keep_their_exact_cte_parity() -> None:
    """The allowed divergence stays CLOSED: exactly two CTEs, not "about two"."""

    def cte_names(path: Path) -> set[str]:
        return set(
            re.findall(
                r"^([a-z_][a-z0-9_]*)\s*(?:\([^)]*\))?\s*AS \(",
                path.read_text(encoding="utf-8"),
                re.M,
            )
        )

    live = cte_names(V7_SQL)
    pgrestore = cte_names(V7_PGRESTORE)

    assert pgrestore - live == {"observed_artifact_constraints", "observed_session_constraints"}
    assert not (live - pgrestore)


def test_the_v7_assets_are_regular_non_executable_files() -> None:
    for asset in (
        V7_JSON,
        V7_SQL,
        V7_PGRESTORE,
        V7_ACL_SQL,
        V7_ACL_JSON,
        V7_ACL_PGRESTORE,
        V7_ACL_PGRESTORE_JSON,
    ):
        assert asset.is_file(), asset.name
        assert not asset.stat().st_mode & 0o111, f"{asset.name} is executable"


def test_the_v7_contracts_read_and_never_write() -> None:
    """An attestation that writes is no longer an attestation."""
    forbidden = re.compile(
        r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|DROP|CREATE|ALTER|GRANT|REVOKE|COPY|CALL|DO)\b",
        re.IGNORECASE | re.M,
    )
    allowed = re.compile(r"^\s*(WITH|SELECT)\b", re.IGNORECASE | re.M)

    for asset, floor in (
        (V7_SQL, 100),
        (V7_PGRESTORE, 100),
        (V7_ACL_SQL, 5),
        (V7_ACL_PGRESTORE, 5),
    ):
        text = asset.read_text(encoding="utf-8")
        assert not forbidden.findall(text), asset.name
        assert len(allowed.findall(text)) > floor, asset.name


def test_the_v7_acl_assets_are_v6_verbatim_but_for_their_identity() -> None:
    """049 touches no privilege, so the ACL pair moves by its identity alone."""
    for v6_asset, v7_asset, suffix in (
        (V6_ACL_SQL, V7_ACL_SQL, "-acl"),
        (V6_ACL_PGRESTORE, V7_ACL_PGRESTORE, "-acl-pgrestore"),
    ):
        normalized = (
            v7_asset.read_text(encoding="utf-8")
            .replace(
                f"'brain-v42/postgresql-recovery/v7{suffix}'",
                f"'brain-v42/postgresql-recovery/v6{suffix}'",
            )
            .replace("'schema_version', 7", "'schema_version', 6")
        )
        assert normalized == v6_asset.read_text(encoding="utf-8"), v7_asset.name

    for v6_name, v7_path, identity in (
        ("brain-v42-v6-acl.json", V7_ACL_JSON, "v7-acl"),
        ("brain-v42-v6-acl-pgrestore.json", V7_ACL_PGRESTORE_JSON, "v7-acl-pgrestore"),
    ):
        document = json.loads((RECOVERY / v6_name).read_text(encoding="utf-8"))
        document["contract_id"] = f"brain-v42/postgresql-recovery/{identity}"
        document["schema_version"] = 7
        expected = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        assert v7_path.read_text(encoding="utf-8") == expected, v7_path.name


def test_the_acl_grant_list_is_derived_from_every_migration() -> None:
    """Ticket 60708007, widened: derive from ALL migrations, not two named ones.

    v6 recomputed the grantee list from 036 and 045 only, which proves the premise
    for the migrations that existed when it was written and is blind to every one
    added after. 049 is exactly such a migration -- it grants nothing, and that is
    a measurement here rather than a claim. Scanning the whole directory keeps the
    guard true for 050 and beyond: a codex view added without a re-mint reddens CI.
    """
    granted: set[str] = set()
    for migration in sorted(VERSIONS.glob("*.py")):
        source = migration.read_text(encoding="utf-8")
        granted |= set(re.findall(r"GRANT SELECT ON (\w+) TO codex_ro", source))

    assert granted, "no codex_ro grant found -- the pattern stopped matching"

    for asset in (V7_ACL_SQL, V7_ACL_PGRESTORE):
        pinned = set(
            re.findall(r"\('(\w+)', 'codex_ro', 'SELECT'\)", asset.read_text(encoding="utf-8"))
        )
        assert pinned == granted, (
            f"{asset.name}: the GRANT list diverges from the migrations -- "
            f"missing={sorted(granted - pinned)} extra={sorted(pinned - granted)}"
        )
