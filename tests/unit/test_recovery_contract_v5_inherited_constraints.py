"""The v5 contract attests INHERITED constraints by definition, no longer by name.

A hole verified by ticket `81c4f366`, one of the five children of the `8eaefe36`
gate: the tables of the v4/v5 scope are attested by **definition**
(`md5(pg_get_constraintdef(...))`, cf. `session_constraint_mismatches` and
`artifact_constraint_mismatches`), but the constraints **inherited** from
migrations 033/034/035 and from `projects` were attested by **NAME** only:

- `graph_foundation_033_observation`: `conname = 'projects_key_format_valid'` and
  `convalidated`, nothing more;
- `graph_projection_034_035_observation`: four `conname` of
  `graph_projection_leases`, and `convalidated`, nothing more.

A constraint whose DEFINITION drifts — a literal removed from an `IN`, a bound
relaxed — keeps its name and therefore passed green. The check is added as a
DEDICATED check row (`inherited_constraint_definitions`) and not merged into
`brain_runtime_032_036_037`: merged, the hardening would be invisible in the
receipt, which would keep returning 25/25 while verifying more. The denominator
moves to 26, and that is the point.

**The two md5 sets are not interchangeable.** `pgrestore` additionally normalises
`::character varying::text` and `]::text[]`; six of the twenty-nine constraints
depend on it. Confusing them would produce a red contract without saying why.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RECOVERY = ROOT / "ops" / "recovery"
V5_JSON = RECOVERY / "brain-v42-v5.json"
V5_SQL = RECOVERY / "brain-v42-v5.sql"
V5_PGRESTORE = RECOVERY / "brain-v42-v5-pgrestore.sql"

#: The five tables whose constraints were attested by name only.
INHERITED_TABLES = (
    "brain_entities",
    "entity_relations",
    "graph_outbox",
    "graph_projection_leases",
    "projects",
)

#: MEASURED on 2026-08-22 against production at head `046`, with the existing
#: contract's literal expression, then REPLAYED independently in Python on the raw
#: `pg_get_constraintdef` (29/29, zero difference) — the value therefore does not
#: come from Postgres's `md5()` alone.
#:
#: Order: (table, constraint, contype, confdeltype, live md5, pgrestore md5).
INHERITED_CONSTRAINTS: tuple[tuple[str, str, str, str | None, str, str], ...] = (
    # fmt: off
    (
        "brain_entities",
        "brain_entities_lifecycle_valid",
        "c",
        None,
        "994ec2f2e5fb99bf449a8f52645a8632",
        "cd2da96da432b61cd47e7266f197cd3b",
    ),
    (
        "brain_entities",
        "brain_entities_pkey",
        "p",
        None,
        "cc3552dbb61b18accca876af5296eb1f",
        "cc3552dbb61b18accca876af5296eb1f",
    ),
    (
        "brain_entities",
        "brain_entities_project_key_fkey",
        "f",
        "r",
        "f263e7d4d142bbc04ada44537963b892",
        "f263e7d4d142bbc04ada44537963b892",
    ),
    (
        "brain_entities",
        "brain_entities_scope_valid",
        "c",
        None,
        "7a2522cefd4d98d52bc658343f54daa9",
        "7a2522cefd4d98d52bc658343f54daa9",
    ),
    (
        "brain_entities",
        "uq_brain_entities_type_key",
        "u",
        None,
        "e8fd9124dae08d47c87177bf033b8e00",
        "e8fd9124dae08d47c87177bf033b8e00",
    ),
    (
        "entity_relations",
        "entity_relations_confidence_valid",
        "c",
        None,
        "8418df632947fd26246036ee546af632",
        "8418df632947fd26246036ee546af632",
    ),
    (
        "entity_relations",
        "entity_relations_lifecycle_valid",
        "c",
        None,
        "994ec2f2e5fb99bf449a8f52645a8632",
        "cd2da96da432b61cd47e7266f197cd3b",
    ),
    (
        "entity_relations",
        "entity_relations_no_self_loop",
        "c",
        None,
        "5a41e6500e5c57c7f2fd2b366d996831",
        "5a41e6500e5c57c7f2fd2b366d996831",
    ),
    (
        "entity_relations",
        "entity_relations_pkey",
        "p",
        None,
        "cc3552dbb61b18accca876af5296eb1f",
        "cc3552dbb61b18accca876af5296eb1f",
    ),
    (
        "entity_relations",
        "entity_relations_source_entity_id_fkey",
        "f",
        "r",
        "10d758915d3544cd60fbd31505ee24d6",
        "10d758915d3544cd60fbd31505ee24d6",
    ),
    (
        "entity_relations",
        "entity_relations_target_entity_id_fkey",
        "f",
        "r",
        "8188bcc6f479fce005c8af00803e91e8",
        "8188bcc6f479fce005c8af00803e91e8",
    ),
    (
        "entity_relations",
        "entity_relations_type_valid",
        "c",
        None,
        "5445bcaba9b1962b9223adf4312e7928",
        "de56c40c7349fe61da69b553ee8ad88a",
    ),
    (
        "entity_relations",
        "uq_entity_relations_endpoints_type",
        "u",
        None,
        "aafe3b4835484bc8352bb3e383f3b3de",
        "aafe3b4835484bc8352bb3e383f3b3de",
    ),
    (
        "graph_outbox",
        "graph_outbox_entity_id_fkey",
        "f",
        "c",
        "41669d749feab45b3b21507cbe1e72f8",
        "41669d749feab45b3b21507cbe1e72f8",
    ),
    (
        "graph_outbox",
        "graph_outbox_event_id_key",
        "u",
        None,
        "759bdd8d95917e86a4535f61383231f2",
        "759bdd8d95917e86a4535f61383231f2",
    ),
    (
        "graph_outbox",
        "graph_outbox_exactly_one_aggregate",
        "c",
        None,
        "43e61c6f8f1d8edd4c7ad839435f3b94",
        "43e61c6f8f1d8edd4c7ad839435f3b94",
    ),
    (
        "graph_outbox",
        "graph_outbox_operation_valid",
        "c",
        None,
        "c06d4e91f1efdc7a54e37e57f12e237b",
        "02563be4b2f7d105be2c25775fd09852",
    ),
    (
        "graph_outbox",
        "graph_outbox_pkey",
        "p",
        None,
        "cc3552dbb61b18accca876af5296eb1f",
        "cc3552dbb61b18accca876af5296eb1f",
    ),
    (
        "graph_outbox",
        "graph_outbox_relation_id_fkey",
        "f",
        "c",
        "22cd3849e65c946557e6c4a9ea483648",
        "22cd3849e65c946557e6c4a9ea483648",
    ),
    (
        "graph_outbox",
        "uq_graph_outbox_entity_revision",
        "u",
        None,
        "7b1e742994175d24227a5f0a6cff40a6",
        "7b1e742994175d24227a5f0a6cff40a6",
    ),
    (
        "graph_outbox",
        "uq_graph_outbox_relation_revision",
        "u",
        None,
        "d5fb45f4a7893c5d45460da33fc32d3b",
        "d5fb45f4a7893c5d45460da33fc32d3b",
    ),
    (
        "graph_projection_leases",
        "graph_projection_leases_armed_generation_valid",
        "c",
        None,
        "e8cee37772e9bc681ba229a778eace5d",
        "e8cee37772e9bc681ba229a778eace5d",
    ),
    (
        "graph_projection_leases",
        "graph_projection_leases_pkey",
        "p",
        None,
        "3608ac6e0b09678c35217c69cc4de206",
        "3608ac6e0b09678c35217c69cc4de206",
    ),
    (
        "graph_projection_leases",
        "graph_projection_leases_protocol_valid",
        "c",
        None,
        "3c5970bbe99c7f44f1a0127458293dea",
        "3c5970bbe99c7f44f1a0127458293dea",
    ),
    (
        "graph_projection_leases",
        "graph_projection_leases_recovery_state_valid",
        "c",
        None,
        "da1a1dfd81cb4d6f562aa15f101ec34d",
        "da1a1dfd81cb4d6f562aa15f101ec34d",
    ),
    (
        "projects",
        "projects_key_format_valid",
        "c",
        None,
        "d2f0e69b15612f6476efceb2a228c6fb",
        "d2f0e69b15612f6476efceb2a228c6fb",
    ),
    (
        "projects",
        "projects_pkey",
        "p",
        None,
        "b449ae3aa5c5dbcebd0e93fd552a7787",
        "b449ae3aa5c5dbcebd0e93fd552a7787",
    ),
    (
        "projects",
        "projects_registry_status_valid",
        "c",
        None,
        "49f2a9f97e70e1f147b1fa22e52d86cb",
        "7da8b1fc307de0337b6647b895313e2e",
    ),
    (
        "projects",
        "projects_source_valid",
        "c",
        None,
        "e0aa1763fa7473f08755ff620c23a600",
        "dea4cf93bb2488104f419ca18ed1bcd2",
    ),
    # fmt: on
)

#: The five constraints the two historical sites checked by NAME alone. That is the
#: minimum coverage this check must take over — the rest is gain, these are the debt
#: named by `81c4f366`.
NAME_ONLY_BEFORE = {
    "projects_key_format_valid",
    "graph_projection_leases_armed_generation_valid",
    "graph_projection_leases_pkey",
    "graph_projection_leases_protocol_valid",
    "graph_projection_leases_recovery_state_valid",
}

CHECK_ID = "inherited_constraint_definitions"


def _sql_row(entry: tuple[str, str, str, str | None, str, str], variant: str) -> str:
    table, name, contype, delete_action, live_md5, pgrestore_md5 = entry
    action = "NULL::text" if delete_action is None else f"'{delete_action}'"
    md5 = live_md5 if variant == "live" else pgrestore_md5
    return f"('{table}', '{name}', '{contype}', {action}, '{md5}')"


def test_the_measured_table_is_neither_a_copy_nor_a_confusion() -> None:
    """INTERNAL negative witness: the two md5 columns differ, and not everywhere.

    Two opposite failure modes, both silent if not pinned here: copying the `live`
    column into `pgrestore` (all 29 md5 equal), or passing one set for the other
    (all 29 different). The measured truth is between the two — six differences,
    exactly those whose definition carries a cast `pg_restore` normalises.
    """
    assert len(INHERITED_CONSTRAINTS) == 29
    assert len({(entry[0], entry[1]) for entry in INHERITED_CONSTRAINTS}) == 29
    assert {entry[0] for entry in INHERITED_CONSTRAINTS} == set(INHERITED_TABLES)

    divergent = [entry for entry in INHERITED_CONSTRAINTS if entry[4] != entry[5]]
    assert len(divergent) == 6, "le nombre d'écarts live/pgrestore mesuré a bougé"
    assert 0 < len(divergent) < len(INHERITED_CONSTRAINTS)

    # A normalisation difference can only arise from a CHECK: neither a key, nor a
    # unique constraint, nor an FK carries a cast expression.
    assert {entry[2] for entry in divergent} == {"c"}

    # A delete action is only declared for FKs — otherwise the contract would
    # compare a field Postgres leaves at ' ' outside FKs.
    for _, _, contype, delete_action, _, _ in INHERITED_CONSTRAINTS:
        assert (delete_action is None) == (contype != "f")

    assert NAME_ONLY_BEFORE <= {entry[1] for entry in INHERITED_CONSTRAINTS}


def test_both_v5_assets_declare_every_inherited_constraint_by_definition() -> None:
    for asset, variant in ((V5_SQL, "live"), (V5_PGRESTORE, "pgrestore")):
        sql = asset.read_text(encoding="utf-8")
        for entry in INHERITED_CONSTRAINTS:
            assert _sql_row(entry, variant) in sql, f"{asset.name}: {entry[0]}.{entry[1]}"


def test_neither_asset_carries_the_other_variant_md5() -> None:
    """The most likely mutation: pasting the wrong set into the right file."""
    live_sql = V5_SQL.read_text(encoding="utf-8")
    pgrestore_sql = V5_PGRESTORE.read_text(encoding="utf-8")

    for entry in INHERITED_CONSTRAINTS:
        if entry[4] == entry[5]:
            continue
        assert _sql_row(entry, "pgrestore") not in live_sql, entry[1]
        assert _sql_row(entry, "live") not in pgrestore_sql, entry[1]


def test_the_inherited_check_is_wired_end_to_end_in_both_assets() -> None:
    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        assert "expected_inherited_constraints(" in sql, asset.name
        assert "observed_inherited_constraints AS (" in sql, asset.name
        assert "inherited_constraint_mismatches AS (" in sql, asset.name
        assert f"'{CHECK_ID}'," in sql, asset.name
        assert "FROM inherited_constraint_mismatches" in sql, asset.name

        # The scope's five tables are bounded in the observation, otherwise the
        # "present off-list" check would count the whole catalogue.
        for table in INHERITED_TABLES:
            assert f"'{table}'" in sql, f"{asset.name}: {table}"


def test_the_inherited_check_is_bidirectional() -> None:
    """Missing-or-divergent AND present-off-list: without the second, a constraint
    added by hand on a table in scope would pass unnoticed."""
    for asset in (V5_SQL, V5_PGRESTORE):
        body = re.search(
            r"^inherited_constraint_mismatches AS \((.*?)^\),$",
            asset.read_text(encoding="utf-8"),
            re.M | re.S,
        )
        assert body is not None, asset.name
        text = body.group(1)
        assert text.count("SELECT count(*)") == 2, asset.name
        assert "LEFT JOIN observed_inherited_constraints" in text, asset.name
        assert "LEFT JOIN expected_inherited_constraints" in text, asset.name
        assert "constraint_record.validated" in text, asset.name


def test_the_pgrestore_normalisation_lives_only_in_the_pgrestore_variant() -> None:
    """Negative witness for the parity: the same CTE, two distinct normalisations.

    The CTE name is identical on both sides — the CLOSED difference list of
    `test_the_two_v5_variants_keep_their_exact_cte_parity` therefore stays at two
    entries, untouched. What differs is the body, exactly as for
    `session_constraint_mismatches`.
    """

    def observed_body(path: Path) -> str:
        match = re.search(
            r"^observed_inherited_constraints AS \((.*?)^\),$",
            path.read_text(encoding="utf-8"),
            re.M | re.S,
        )
        assert match is not None, path.name
        return match.group(1)

    live = observed_body(V5_SQL)
    pgrestore = observed_body(V5_PGRESTORE)

    assert "::character varying::text" not in live
    assert "]::text[]" not in live
    assert "::character varying::text" in pgrestore
    assert "]::text[]" in pgrestore
    assert live != pgrestore


def test_the_json_manifest_declares_the_inherited_check() -> None:
    document = json.loads(V5_JSON.read_text(encoding="utf-8"))
    ids = [check["id"] for check in document["checks"]]

    assert CHECK_ID in ids
    assert ids == sorted(ids)
    # The exact COUNT lives in `test_v5_json_is_the_exact_v4_delta`, and nowhere
    # else: three DR splits still pass through this contract, and a figure
    # duplicated here would force them to edit another split's test for a truth
    # that is not its own.
    assert next(check for check in document["checks"] if check["id"] == CHECK_ID) == {
        "id": CHECK_ID,
        "kind": "brain_schema_invariant",
        "name": CHECK_ID,
    }


def test_the_md5_census_keys_on_the_row_never_on_the_digest_alone() -> None:
    """A TWO-pattern census — and the naive pattern is wrong here, that is measured.

    First draft: "none of my md5 must appear elsewhere". RED, and the test was
    wrong. `cc3552dbb61b18accca876af5296eb1f` is `md5('primary key (id)')`: **24
    constraints of the database share it** (measured on 2026-08-22), and it already
    lives in v3, v4 and v5. An md5 fingerprints a DEFINITION, not a constraint —
    two constraints alike in shape collide by construction, that is not a leak.

    The census therefore bears on the ROW — `(table, name, type, action, md5)` — and
    the digest-only pattern now serves only one thing for which it is correct: the
    JSON manifest must carry NO md5, ever.
    """
    hex32 = re.compile(r"\b[0-9a-f]{32}\b")

    # Pattern 1 — the whole row. The v4 assets are frozen: none of mine.
    for name in ("brain-v42-v4.sql", "brain-v42-v4-pgrestore.sql"):
        text = (RECOVERY / name).read_text(encoding="utf-8")
        for entry in INHERITED_CONSTRAINTS:
            assert _sql_row(entry, "live") not in text, f"{name}: {entry[1]}"
            assert _sql_row(entry, "pgrestore") not in text, f"{name}: {entry[1]}"

    # Pattern 2 — the bare digest, on the only file where it is an invariant.
    assert not hex32.findall(V5_JSON.read_text(encoding="utf-8"))

    # The legitimate collision, pinned: removing it from the corpus would make
    # pattern 1 useless and would suggest pattern 2 applies everywhere.
    shared = "cc3552dbb61b18accca876af5296eb1f"
    assert sum(1 for entry in INHERITED_CONSTRAINTS if entry[4] == shared) == 3
    assert shared in hex32.findall((RECOVERY / "brain-v42-v4.sql").read_text(encoding="utf-8"))

    live_hex = set(hex32.findall(V5_SQL.read_text(encoding="utf-8")))
    pgrestore_hex = set(hex32.findall(V5_PGRESTORE.read_text(encoding="utf-8")))
    assert {entry[4] for entry in INHERITED_CONSTRAINTS} <= live_hex
    assert {entry[5] for entry in INHERITED_CONSTRAINTS} <= pgrestore_hex
