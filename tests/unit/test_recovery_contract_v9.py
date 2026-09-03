"""Recovery contract v9 — the head 052 generation, minted from v8 by ADDITION.

Same shape as `test_recovery_contract_v8.py`, and the same reason for it: v9 is
v8 PLUS insertions, so byte equality against a rebuilt asset is not available as
a property. What replaces it is stronger where it matters — the delta must be
purely ADDITIVE, and every removed line must be one that CARRIES a
version-dependent number.

This generation removes THREE lines, not the four v8 removed, and the missing one
is informative rather than an oversight: `foreign_keys` stays at 27 because
`access_log_daily` carries NO foreign key. Its `entity_id` deliberately points at
no table — the same doctrine as the knowledge tables, where dropping a context
must not take the trail down with it. A fourth removal here would mean the count
moved, which would mean the table gained a reference nobody declared.

Every fingerprint below was MEASURED, never derived by hand: on a disposable
database built by the alembic chain to 052 for the base asset, and on a REAL
`pg_dump`/`pg_restore` of that database for the twin, in both cases by cutting
the `observed_*` CTEs out of the asset itself so the md5 is computed by the very
expression the contract checks with. The two came back IDENTICAL — a result, and
not the assumption it would have been to copy the live values into the twin.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

RECOVERY = Path(__file__).resolve().parents[2] / "ops" / "recovery"

V8_JSON = RECOVERY / "brain-v42-v8.json"
V8_SQL = RECOVERY / "brain-v42-v8.sql"
V8_PGRESTORE = RECOVERY / "brain-v42-v8-pgrestore.sql"
V8_ACL_SQL = RECOVERY / "brain-v42-v8-acl.sql"
V8_ACL_JSON = RECOVERY / "brain-v42-v8-acl.json"
V8_ACL_PGRESTORE = RECOVERY / "brain-v42-v8-acl-pgrestore.sql"
V8_ACL_PGRESTORE_JSON = RECOVERY / "brain-v42-v8-acl-pgrestore.json"

V9_JSON = RECOVERY / "brain-v42-v9.json"
V9_SQL = RECOVERY / "brain-v42-v9.sql"
V9_PGRESTORE = RECOVERY / "brain-v42-v9-pgrestore.sql"
V9_ACL_SQL = RECOVERY / "brain-v42-v9-acl.sql"
V9_ACL_JSON = RECOVERY / "brain-v42-v9-acl.json"
V9_ACL_PGRESTORE = RECOVERY / "brain-v42-v9-acl-pgrestore.sql"
V9_ACL_PGRESTORE_JSON = RECOVERY / "brain-v42-v9-acl-pgrestore.json"

#: The v8 assets are FROZEN to the byte, exactly as v8 froze v7 and v7 froze v6:
#: an attestation lineage is never rewritten, each contract describes the state it
#: described.
V8_SHA256 = {
    "brain-v42-v8.json": "0f51dc211393a23f4214f4bc025fa2364a42a3a0d7a6e07504509463674639a4",
    "brain-v42-v8.sql": "c4f28662a0445518ae330a1f3ee9e1a58521cafeb8570124e8e0f614a0fc1a6e",
    "brain-v42-v8-pgrestore.sql": (
        "cd6e2e117efdf2d5e9786e8b547e80a00c68e7eae83162ed13174f15a6c1f460"
    ),
    "brain-v42-v8-acl.sql": "ce1dc9823c2e7a34679d2e881fe372b04122b63f1a2fe6b25b4d3b8003fe74fc",
    "brain-v42-v8-acl.json": "19f932ca0a9ee8503063adebd5e344258e70eaf46a2c6e5458a28fb569695ca5",
    "brain-v42-v8-acl-pgrestore.sql": (
        "28bd150b1fcf70b998f0b6beef47f294a06d030fe636fbe900ff8ea82dbb94a1"
    ),
    "brain-v42-v8-acl-pgrestore.json": (
        "be30d0ff598c6bb8562ab7866199f578ed49509acef99dd0b1b22483320f9a5b"
    ),
}

#: The only lines this mint may REMOVE. THREE, not four: `foreign_keys` is absent
#: on purpose — see the module docstring.
REMOVABLE = {
    "         'indexes', 134,",
    " 'contract_id', 'brain-v42/postgresql-recovery/v8',",
    " 'schema_version', 8",
}

NEW_TABLE = "access_log_daily"


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


def _added_blocks(before: Path, after: Path) -> list[list[str]]:
    """Runs of consecutive added lines, i.e. whole inserted tuples.

    A tuple spans several lines; judging them one by one would either reject the
    bare `'access_log_daily',` or accept a stray md5 from anywhere.
    """
    diff = list(
        difflib.ndiff(
            before.read_text(encoding="utf-8").splitlines(),
            after.read_text(encoding="utf-8").splitlines(),
        )
    )
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in diff:
        if line.startswith("+ "):
            current.append(line[2:])
        elif line.startswith("? "):
            continue
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def test_v8_recovery_assets_remain_byte_identical() -> None:
    """A generation is immutable. Re-minting v9 must not touch what v8 attested."""
    for name, expected in V8_SHA256.items():
        digest = hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()
        assert digest == expected, f"{name} changed: v8 is frozen"


def test_the_v9_sql_assets_only_add() -> None:
    for before, after in ((V8_SQL, V9_SQL), (V8_PGRESTORE, V9_PGRESTORE)):
        added, removed = _added_and_removed(before, after)
        assert set(removed) <= REMOVABLE, (
            f"{after.name} removes lines a mint may not touch: {sorted(set(removed) - REMOVABLE)}"
        )
        assert len(removed) == len(REMOVABLE), f"{after.name}: {sorted(removed)}"
        assert added, f"{after.name} adds nothing"

        for block in _added_blocks(before, after):
            joined = " ".join(block)
            assert re.search(
                rf"{NEW_TABLE}|pk_access_log_daily|ix_access_log_daily_entity_day"
                r"|'indexes', 136,|postgresql-recovery/v9|'schema_version', 9",
                joined,
            ), f"{after.name} adds something that is not 052's: {joined!r}"


def test_the_foreign_key_count_is_untouched() -> None:
    """The absence of a fourth removal is itself the assertion.

    `access_log_daily` references nothing. If a future edit gave it a foreign key
    without saying so, `foreign_keys` would move and this test would name it.
    """
    for asset in (V9_SQL, V9_PGRESTORE):
        text = asset.read_text(encoding="utf-8")
        assert "'foreign_keys', 27," in text
        assert "'foreign_keys', 28," not in text


def test_v9_json_is_the_v8_manifest_plus_the_table_and_the_index_count() -> None:
    document = json.loads(V8_JSON.read_text(encoding="utf-8"))
    document["contract_id"] = "brain-v42/postgresql-recovery/v9"
    document["schema_version"] = 9
    for check in document["checks"]:
        if check["id"] == "catalog_counts":
            check["indexes"] = 136
        if check["id"] == "table_set":
            check["tables"] = sorted([*check["tables"], NEW_TABLE])
    document["checks"].sort(key=lambda check: check["id"])

    expected = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    assert V9_JSON.read_text(encoding="utf-8") == expected


def test_the_v9_identities_are_exact() -> None:
    assert "'brain-v42/postgresql-recovery/v9'" in V9_SQL.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v9'" in V9_PGRESTORE.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v9-acl'" in V9_ACL_SQL.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v9-acl-pgrestore'" in V9_ACL_PGRESTORE.read_text(
        encoding="utf-8"
    )
    for asset in (V9_SQL, V9_PGRESTORE, V9_ACL_SQL, V9_ACL_PGRESTORE):
        assert "postgresql-recovery/v8" not in asset.read_text(encoding="utf-8")


def test_v9_carries_the_052_mechanism_in_both_variants() -> None:
    """The table, its primary key and BOTH indexes — live and restored alike."""
    for asset in (V9_SQL, V9_PGRESTORE):
        text = asset.read_text(encoding="utf-8")
        assert f"('{NEW_TABLE}')," in text, f"{asset.name}: table missing from the set"
        assert text.count(f"'{NEW_TABLE}',") == 4, (
            f"{asset.name}: expected 4 tuples (1 column fingerprint, 1 constraint, 2 indexes)"
        )
        assert "'pk_access_log_daily'" in text
        assert "'ix_access_log_daily_entity_day'" in text


def test_the_twin_agrees_with_the_live_asset_on_052() -> None:
    """MEASURED on a real restore, not assumed.

    v8's mint found exactly one constraint whose definition `pg_restore`
    re-serialises (050's `varchar[]` cast). 052 has no such shape: a plain primary
    key and a plain b-tree canonicalise to themselves. Pinning that equality here
    is what would notice the day it stops holding.
    """
    live = V9_SQL.read_text(encoding="utf-8")
    twin = V9_PGRESTORE.read_text(encoding="utf-8")
    for fingerprint in (
        "a97bbbde971f8ae4bf1d84cfd8309a89",  # column fingerprint
        "09937d1779a1edad27c9bf6b459231da",  # pk constraint
        "8208ffcc94dca6305b1deb3054f170c6",  # pk index
        "0e68e5a29e34d9430e0652b12fd087f4",  # ix_..._entity_day
    ):
        assert fingerprint in live, f"missing from the live asset: {fingerprint}"
        assert fingerprint in twin, f"missing from the twin: {fingerprint}"


def test_the_acl_assets_differ_from_v8_only_by_their_identity() -> None:
    """052 adds no GRANT, so the ACL surface is unchanged — and that is checked.

    Copying the ACL assets forward is only legitimate if nothing in the migration
    touches ownership or privileges. That premise is asserted, not trusted.
    """
    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "052_access_log_daily.py"
    ).read_text(encoding="utf-8")
    assert "GRANT" not in migration.upper().replace("GRANTED", "")

    for old, new in (
        (V8_ACL_SQL, V9_ACL_SQL),
        (V8_ACL_PGRESTORE, V9_ACL_PGRESTORE),
        (V8_ACL_JSON, V9_ACL_JSON),
        (V8_ACL_PGRESTORE_JSON, V9_ACL_PGRESTORE_JSON),
    ):
        rebuilt = old.read_text(encoding="utf-8").replace("/v8", "/v9")
        rebuilt = rebuilt.replace('"schema_version": 8', '"schema_version": 9')
        rebuilt = rebuilt.replace('"schema_version":8', '"schema_version":9')
        assert new.read_text(encoding="utf-8") == rebuilt, (
            f"{new.name} differs from {old.name} by more than its identity"
        )
