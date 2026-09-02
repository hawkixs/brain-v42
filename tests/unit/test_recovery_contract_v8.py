"""Recovery contract v8 -- the 050/051 mint: a focus history, session checkpoints.

Why a v8: migrations 050 and 051 landed in production on 2026-09-02, after v7 was
minted against head 049 the same morning. Replayed live at head 051 the v7 receipt
renders 24/30, and every one of its six reds is a consequence of those two
migrations rather than a degradation:

* `table_set` -- two new tables, `project_focus_history` (050) and
  `brain_session_checkpoints` (051).
* `table_shape` -- 2 column fingerprints, 10 constraints and 3 indexes, all of
  them belonging to those two tables, plus the constraint trigger 050 puts on
  `project_contexts`.
* `catalog_counts` -- 131 indexes and 26 foreign keys become 134 and 27.
* `sequence_shape` -- `brain_session_checkpoints_id_seq`, the only new sequence
  (`project_focus_history` has a composite key and no sequence).
* `trigger_function_fingerprints` -- three new functions.
* `brain_runtime_032_036_037` -- the armed `project_contexts_focus_history_required`
  trigger, unexpected on a table this check watches.

The S1 lineage (decision `9d22bc6a`) answers that with a mint, never a rewrite:
v7 stays frozen byte for byte and keeps describing head 049.

Where v8 differs in KIND from every mint before it: v7 was a substitution of seven
fingerprints, so it could be rebuilt from v6 by `str.replace`. 050 and 051 add
objects, so v8 is v7 plus insertions. That makes the auditable property different
too -- not "the delta is a closed set of values" but "the delta is purely
ADDITIVE": no expected value of v7 is re-signed. `test_the_v8_sql_assets_only_add`
proves exactly that, by diffing the frozen v7 bytes against v8 and demanding that
the only removed lines are the two counters and the identity.

How the `-pgrestore` twin's ten new constraint fingerprints were obtained, no
restored bench being available: the twin canonicalizes what it observes
(`::character varying::text` -> `::character varying`, `]::text[]` -> `]`), which
is what absorbs the pg_dump/pg_restore round trip, so its expected value is
`md5(canonicalize(source_definition))` -- computable read-only against production.
Measured rather than assumed: nine of the ten new constraints canonicalize to
their own live value, and exactly one moves --
`project_focus_history_source_valid`, whose IN-list is a `varchar[]` cast, which
is the very shape the canonicalization exists to absorb.

What is therefore NOT proven here, written down instead of glossed over: the v8
twin has never been replayed against a real restore, and neither was v7's. The
three new indexes are plain b-tree primary-key and unique indexes, which is why
their live and restored fingerprints are taken to be equal -- that is a reasoning
about `pg_get_indexdef`, not a bench measurement.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RECOVERY = ROOT / "ops" / "recovery"
VERSIONS = ROOT / "alembic" / "versions"

V7_JSON = RECOVERY / "brain-v42-v7.json"
V7_SQL = RECOVERY / "brain-v42-v7.sql"
V7_PGRESTORE = RECOVERY / "brain-v42-v7-pgrestore.sql"
V7_ACL_SQL = RECOVERY / "brain-v42-v7-acl.sql"
V7_ACL_PGRESTORE = RECOVERY / "brain-v42-v7-acl-pgrestore.sql"

V8_JSON = RECOVERY / "brain-v42-v8.json"
V8_SQL = RECOVERY / "brain-v42-v8.sql"
V8_PGRESTORE = RECOVERY / "brain-v42-v8-pgrestore.sql"
V8_ACL_SQL = RECOVERY / "brain-v42-v8-acl.sql"
V8_ACL_JSON = RECOVERY / "brain-v42-v8-acl.json"
V8_ACL_PGRESTORE = RECOVERY / "brain-v42-v8-acl-pgrestore.sql"
V8_ACL_PGRESTORE_JSON = RECOVERY / "brain-v42-v8-acl-pgrestore.json"

#: The v7 assets are FROZEN to the byte, exactly as v7 froze v6: an attestation
#: lineage is never rewritten, each contract describes the state it described.
V7_SHA256 = {
    "brain-v42-v7.json": "5c878e9d07260330a18d508b4b8fb76925287186b13ece7c5e57e29e00fa3f7e",
    "brain-v42-v7.sql": "d56e713563a0958ad2ec1f971db91d53cf1195a9ed8e7c98c9c6a102816207db",
    "brain-v42-v7-pgrestore.sql": (
        "42e54fdefdf95cf5b98985b7de44545e0b8158c85b33b8ba5a04d72f15a9f4ab"
    ),
    "brain-v42-v7-acl.sql": "4923dce191eb63ba7c1a63aaeed20710b471421a2ca493c52a43aa1658e088ee",
    "brain-v42-v7-acl.json": "d3fd621954a68d3ebdec36c5093cb3d87ee60f42486b9e1735365866674a8da7",
    "brain-v42-v7-acl-pgrestore.sql": (
        "b2fa076df963591396cce1f2946c5788058295c9b9ddf46a8e332188d7cfed11"
    ),
    "brain-v42-v7-acl-pgrestore.json": (
        "fdd26f7ed5e612a0f73c13387eced8bd6b94d75ff5cc95de699e121ca60a49ec"
    ),
}

#: The only lines a mint may REMOVE: the two catalog counters and the identity.
#: Anything else in this set means an expected value was re-signed.
REMOVABLE = {
    "         'foreign_keys', 26,",
    "         'indexes', 131,",
    " 'contract_id', 'brain-v42/postgresql-recovery/v7',",
    " 'schema_version', 7",
}

NEW_TABLES = ("brain_session_checkpoints", "project_focus_history")

#: The four CHECK constraints 051 names, plus the uniqueness and the RESTRICT FK.
CHECKPOINT_CONSTRAINTS = (
    "brain_session_checkpoints_seq_positive",
    "brain_session_checkpoints_progress_nonempty",
    "brain_session_checkpoints_next_step_nonempty",
    "brain_session_checkpoints_blocker_nonempty",
    "uq_brain_session_checkpoints_session_seq",
    "brain_session_checkpoints_session_id_fkey",
)

NEW_TRIGGER_FUNCTIONS = (
    "brain_session_checkpoints_append_only",
    "project_focus_history_append_only",
    "require_project_focus_history",
)

#: `project_focus_history_source_valid` is the one constraint whose fingerprint
#: differs between the live contract and its restored twin.
SOURCE_VALID_LIVE = "15d5b7704cebb39d5768f4f02ca5c064"
SOURCE_VALID_RESTORED = "5cfbf6855067385e03c2b2c9b3e7a675"


def _added_and_removed(before: Path, after: Path) -> tuple[list[str], list[str]]:
    # A list, not the generator: two comprehensions over one generator would
    # leave the second empty, and an empty "removed" reads as "nothing removed".
    diff = list(
        difflib.ndiff(
            before.read_text(encoding="utf-8").splitlines(),
            after.read_text(encoding="utf-8").splitlines(),
        )
    )
    added = [line[2:] for line in diff if line.startswith("+ ")]
    removed = [line[2:] for line in diff if line.startswith("- ")]
    return added, removed


def test_v7_recovery_assets_remain_byte_identical() -> None:
    for name, expected in V7_SHA256.items():
        digest = hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest()
        assert digest == expected, f"{name} was rewritten -- the lineage forbids that"


def test_the_v8_sql_assets_only_add() -> None:
    """The property that makes this mint auditable: nothing was re-signed.

    A substitution mint could be rebuilt and compared byte for byte. An additive
    one cannot, so the guard moves to the shape of the delta: every removed line
    must be one of the four that CARRY a version-dependent number. A fingerprint
    quietly replaced because the schema drifted would show up here as a fifth.
    """
    for before, after in ((V7_SQL, V8_SQL), (V7_PGRESTORE, V8_PGRESTORE)):
        added, removed = _added_and_removed(before, after)
        assert set(removed) <= REMOVABLE, (
            f"{after.name} removes lines a mint may not touch: {sorted(set(removed) - REMOVABLE)}"
        )
        assert len(removed) == len(REMOVABLE), f"{after.name}: {sorted(removed)}"
        assert added, f"{after.name} adds nothing"

        # Group consecutive additions back into the tuples they came from: a
        # tuple spans several lines, and judging them one by one would either
        # reject `'project_contexts',` or accept a bare md5 from anywhere.
        for block in _added_blocks(before, after):
            joined = " ".join(block)
            assert re.search(
                r"brain_session_checkpoints|project_focus_history"
                r"|project_contexts_focus_history_required|require_project_focus_history"
                r"|'foreign_keys', 27,|'indexes', 134,"
                r"|postgresql-recovery/v8|'schema_version', 8",
                joined,
            ), f"{after.name} adds something belonging to neither 050 nor 051: {joined!r}"


def _added_blocks(before: Path, after: Path) -> list[list[str]]:
    """Runs of consecutive added lines, i.e. whole inserted tuples."""
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
        else:
            if current:
                blocks.append(current)
                current = []
    if current:
        blocks.append(current)
    return blocks


def test_v8_json_is_the_v7_receipt_plus_the_two_tables_and_the_counts() -> None:
    document = json.loads(V7_JSON.read_text(encoding="utf-8"))
    document["contract_id"] = "brain-v42/postgresql-recovery/v8"
    document["schema_version"] = 8
    for check in document["checks"]:
        if check["id"] == "catalog_counts":
            check["indexes"], check["foreign_keys"] = 134, 27
        if check["id"] == "table_set":
            check["tables"] = sorted([*check["tables"], *NEW_TABLES])
    document["checks"].sort(key=lambda check: check["id"])

    expected = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    assert V8_JSON.read_text(encoding="utf-8") == expected


def test_the_v8_identities_are_exact() -> None:
    assert "'brain-v42/postgresql-recovery/v8'" in V8_SQL.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v8'" in V8_PGRESTORE.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v8-acl'" in V8_ACL_SQL.read_text(encoding="utf-8")
    assert "'brain-v42/postgresql-recovery/v8-acl-pgrestore'" in V8_ACL_PGRESTORE.read_text(
        encoding="utf-8"
    )
    for asset in (V8_SQL, V8_PGRESTORE, V8_ACL_SQL, V8_ACL_PGRESTORE):
        assert "'schema_version', 8" in asset.read_text(encoding="utf-8"), asset.name


def test_v8_carries_the_050_and_051_mechanisms() -> None:
    """The mint encodes WHAT the migrations added, at the right multiplicity."""
    live = V8_SQL.read_text(encoding="utf-8")
    restored = V8_PGRESTORE.read_text(encoding="utf-8")

    for asset, name in ((live, "live"), (restored, "pgrestore")):
        for table in NEW_TABLES:
            assert f"     ('{table}')," in asset, f"{name}: {table} absent from the table set"
        for constraint in CHECKPOINT_CONSTRAINTS:
            assert f"'{constraint}'" in asset, f"{name}: {constraint}"
        for function in NEW_TRIGGER_FUNCTIONS:
            assert f"('{function}'," in asset, f"{name}: {function}"
        assert "'brain_session_checkpoints_id_seq'" in asset, name
        assert "'project_focus_history_source_valid'" in asset, name
        assert "'indexes', 134," in asset, name
        assert "'foreign_keys', 27," in asset, name

    # 050 has no sequence of its own: a composite key, hence exactly one new one.
    assert "'project_focus_history_id_seq'" not in live


def test_the_restored_twin_moves_exactly_one_new_constraint() -> None:
    """Nine of the ten new constraints canonicalize to their own live value.

    Measured against production, not assumed. If a second one ever moves, the
    twin's derivation stopped matching its own canonicalization and the mint has
    to be re-measured rather than patched.
    """
    live = V8_SQL.read_text(encoding="utf-8")
    restored = V8_PGRESTORE.read_text(encoding="utf-8")

    assert SOURCE_VALID_LIVE in live
    assert SOURCE_VALID_LIVE not in restored
    assert SOURCE_VALID_RESTORED in restored
    assert SOURCE_VALID_RESTORED not in live

    for constraint_md5 in (
        "3df4abf41faaf07333aaa0f8a5826a5b",  # blocker_nonempty
        "9d3b4c771dad0ddd11c2eaf1459d9e57",  # next_step_nonempty
        "64df634dca7747898cb916f36b7d2774",  # progress_nonempty
        "de6a9b9ad4f3fac265d9a7c672935202",  # seq_positive
        "33577b1d6597cae9e8a8641af65ff234",  # session_id_fkey, ON DELETE RESTRICT
        "961fa4ce931b4430eac185433e6ad5bd",  # uq_..._session_seq
        "40b04baf4eb12bc74afe359f88bfeaaa",  # project_contexts_focus_history_required
    ):
        assert constraint_md5 in live and constraint_md5 in restored, constraint_md5


def test_the_focus_history_trigger_is_pinned_as_ARMED() -> None:
    """050 ships the trigger; arming it was a separate operator gesture.

    The contract does not merely require the trigger to EXIST -- the runtime check
    joins on `tgenabled = 'O'`, so a trigger disabled to work around something
    reddens the receipt instead of passing quietly.
    """
    for asset in (V8_SQL, V8_PGRESTORE):
        text = asset.read_text(encoding="utf-8")
        assert "('project_contexts', 'project_contexts_focus_history_required')," in text
        assert "observed_user_trigger.tgenabled = 'O'" in text, asset.name


def test_v8_watches_the_append_only_triggers_of_both_new_tables() -> None:
    """A strengthening this mint chose, and which the report names as such.

    Both new tables are append-only by trigger. Adding them to the watched set
    means the disappearance of either trigger reddens the receipt -- the same
    guarantee `brain_session_artifacts` has had since v5.
    """
    for asset in (V8_SQL, V8_PGRESTORE):
        text = asset.read_text(encoding="utf-8")
        for table in NEW_TABLES:
            assert f"     ('{table}')," in text
        assert "('brain_session_checkpoints', 'brain_session_checkpoints_append_only')," in text, (
            asset.name
        )
        assert "('project_focus_history', 'project_focus_history_append_only_trigger')," in text, (
            asset.name
        )


def test_v8_keeps_the_047_048_049_mechanisms() -> None:
    """A mint inherits its ancestors' proofs; it does not quietly drop them."""
    for asset in (V8_SQL, V8_PGRESTORE):
        text = asset.read_text(encoding="utf-8")
        assert (
            "'cardinality(captured_knowledge_ids) = 0 and nothing_to_capture_reason is not null'"
            not in text
        ), f"{asset.name} still pins the XOR that 047 destroyed"
        assert "nothing_to_capture_reason is null or btrim(nothing_to_capture_reason)" in text
        assert "brain_session_artifacts_attribution_mode_valid" in text, asset.name
        assert "idx_brain_session_artifacts_derived_window" in text, asset.name
        assert "738f6bf6328d407c972ac0d65f49ca05" in text, f"{asset.name} lost the 049 dream_runs"


def test_the_two_v8_variants_keep_their_exact_cte_parity() -> None:
    """The allowed divergence stays CLOSED: exactly two CTEs, not "about two"."""

    def cte_names(path: Path) -> set[str]:
        return set(
            re.findall(
                r"^([a-z_][a-z0-9_]*)\s*(?:\([^)]*\))?\s*AS \(",
                path.read_text(encoding="utf-8"),
                re.M,
            )
        )

    live = cte_names(V8_SQL)
    pgrestore = cte_names(V8_PGRESTORE)

    assert pgrestore - live == {"observed_artifact_constraints", "observed_session_constraints"}
    assert not (live - pgrestore)


def test_the_v8_assets_are_regular_non_executable_files() -> None:
    for asset in (
        V8_JSON,
        V8_SQL,
        V8_PGRESTORE,
        V8_ACL_SQL,
        V8_ACL_JSON,
        V8_ACL_PGRESTORE,
        V8_ACL_PGRESTORE_JSON,
    ):
        assert asset.is_file(), asset.name
        assert not asset.stat().st_mode & 0o111, f"{asset.name} is executable"


def test_the_v8_contracts_read_and_never_write() -> None:
    """An attestation that writes is no longer an attestation."""
    forbidden = re.compile(
        r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|DROP|CREATE|ALTER|GRANT|REVOKE|COPY|CALL|DO)\b",
        re.IGNORECASE | re.M,
    )
    allowed = re.compile(r"^\s*(WITH|SELECT)\b", re.IGNORECASE | re.M)

    for asset, floor in (
        (V8_SQL, 100),
        (V8_PGRESTORE, 100),
        (V8_ACL_SQL, 5),
        (V8_ACL_PGRESTORE, 5),
    ):
        text = asset.read_text(encoding="utf-8")
        assert not forbidden.findall(text), asset.name
        assert len(allowed.findall(text)) > floor, asset.name


def test_the_v8_acl_assets_are_v7_verbatim_but_for_their_identity() -> None:
    """050 and 051 grant nothing and touch no view, so the ACL pair only renames."""
    for v7_asset, v8_asset, suffix in (
        (V7_ACL_SQL, V8_ACL_SQL, "-acl"),
        (V7_ACL_PGRESTORE, V8_ACL_PGRESTORE, "-acl-pgrestore"),
    ):
        normalized = (
            v8_asset.read_text(encoding="utf-8")
            .replace(
                f"'brain-v42/postgresql-recovery/v8{suffix}'",
                f"'brain-v42/postgresql-recovery/v7{suffix}'",
            )
            .replace("'schema_version', 8", "'schema_version', 7")
        )
        assert normalized == v7_asset.read_text(encoding="utf-8"), v8_asset.name

    for v7_name, v8_path, identity in (
        ("brain-v42-v7-acl.json", V8_ACL_JSON, "v8-acl"),
        ("brain-v42-v7-acl-pgrestore.json", V8_ACL_PGRESTORE_JSON, "v8-acl-pgrestore"),
    ):
        document = json.loads((RECOVERY / v7_name).read_text(encoding="utf-8"))
        document["contract_id"] = f"brain-v42/postgresql-recovery/{identity}"
        document["schema_version"] = 8
        expected = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        assert v8_path.read_text(encoding="utf-8") == expected, v8_path.name


def test_the_acl_grant_list_is_derived_from_every_migration() -> None:
    """Ticket 60708007: derive the premise from ALL migrations, not named ones.

    050 and 051 grant nothing -- a measurement here rather than a claim, and one
    that stays true for 052 and beyond: a codex view added without a re-mint
    reddens CI instead of leaving the ACL twin quietly stale.
    """
    granted: set[str] = set()
    for migration in sorted(VERSIONS.glob("*.py")):
        source = migration.read_text(encoding="utf-8")
        granted |= set(re.findall(r"GRANT SELECT ON (\w+) TO codex_ro", source))

    assert granted, "no codex_ro grant found -- the pattern stopped matching"

    for asset in (V8_ACL_SQL, V8_ACL_PGRESTORE):
        pinned = set(
            re.findall(r"\('(\w+)', 'codex_ro', 'SELECT'\)", asset.read_text(encoding="utf-8"))
        )
        assert pinned == granted, (
            f"{asset.name}: the GRANT list diverges from the migrations -- "
            f"missing={sorted(granted - pinned)} extra={sorted(pinned - granted)}"
        )
