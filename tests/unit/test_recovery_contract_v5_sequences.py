"""The v5 contract attests the NINE sequences — their shape, and above all their advance.

`f36846a1`, second child of the `8eaefe36` gate. The hole was TOTAL and could be
measured with one command: `grep -ci sequence ops/recovery/brain-v42-v4.sql`
returned **0**. No sequence was attested, neither by its shape, nor by its binding
to its column, nor by its advance.

**The check that matters is not the shape, it is `last_value >= max(id)`.** That
is the SILENT restore failure: the sequences restart at 1, the database looks
restored, every `SELECT` passes — and the first `INSERT` hits a primary-key
collision. A restoration that declares itself successful and refuses the first
write is exactly what this contract exists to catch, and it is the batch's only
check that can bite ONLY on a real restore: on live production it is true by
construction.

**Volume quantified BEFORE writing, measured on 2026-08-22 against head `046`:**
9 sequences, 9 bound to a column, 0 orphaned.

**Maximal parity, measured.** These CTEs read neither `pg_get_indexdef` nor
`column_default`: there is nothing `pg_restore` normalises, so the four added CTEs
are the same bytes on both sides. The CLOSED list of CTE differences stays at its
TWO entries.

**READ-ONLY, and that was not free.** `last_value` is read from the `pg_sequences`
view, never through `currval()` nor `nextval()` — an attestation contract that
advanced a sequence to observe it would change the object it measures.
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

CHECK_ID = "sequence_shape"

#: MEASURED on 2026-08-22 against production at head `046`, then replayed against
#: a database built AFRESH by `alembic upgrade head`: the nine sequences are
#: identical there. `(sequence, table, column, type, step, min, max, start)` —
#: `cycle` is FALSE everywhere and does not enter this table, it is asserted
#: separately so that a `TRUE` appearing does not hide inside a column.
SEQUENCES: tuple[tuple[str, str, str, str, int, int, int, int], ...] = (
    ("access_log_id_seq", "access_log", "id", "bigint", 1, 1, 9223372036854775807, 1),
    (
        "consolidation_log_id_seq",
        "consolidation_log",
        "id",
        "bigint",
        1,
        1,
        9223372036854775807,
        1,
    ),
    ("dream_promotions_id_seq", "dream_promotions", "id", "bigint", 1, 1, 9223372036854775807, 1),
    # The ONLY `integer` of the nine, and it is precisely the kind of difference a
    # hand-written list would flatten without seeing it.
    ("dream_runs_id_seq", "dream_runs", "id", "integer", 1, 1, 2147483647, 1),
    ("graph_outbox_id_seq", "graph_outbox", "id", "bigint", 1, 1, 9223372036854775807, 1),
    (
        "roadmap_curation_proposals_id_seq",
        "roadmap_curation_proposals",
        "id",
        "bigint",
        1,
        1,
        9223372036854775807,
        1,
    ),
    ("search_log_id_seq", "search_log", "id", "bigint", 1, 1, 9223372036854775807, 1),
    (
        "ticket_extraction_attempts_id_seq",
        "ticket_extraction_attempts",
        "id",
        "bigint",
        1,
        1,
        9223372036854775807,
        1,
    ),
    (
        "ticket_extraction_proposals_id_seq",
        "ticket_extraction_proposals",
        "id",
        "bigint",
        1,
        1,
        9223372036854775807,
        1,
    ),
)

NEW_CTES = (
    "expected_sequences",
    "observed_sequences",
    "sequence_property_mismatches",
    "sequence_high_water",
    "sequence_backfill_mismatches",
)


def _cte_body(path: Path, name: str) -> str:
    match = re.search(
        rf"^{name}(?:\([^)]*\))? AS \((.*?)^\),$",
        path.read_text(encoding="utf-8"),
        re.M | re.S,
    )
    assert match is not None, f"{path.name}: {name}"
    return match.group(1)


def test_the_measured_volume_is_pinned_and_internally_consistent() -> None:
    """The volume, quantified before writing — and reddening if it moves without a re-measure."""
    assert len(SEQUENCES) == 9
    assert len({entry[0] for entry in SEQUENCES}) == 9
    # One sequence per table, and never two sequences on the same column:
    # PostgreSQL's `<table>_<column>_seq` shape suggests it, it does not guarantee
    # it — an `ALTER SEQUENCE ... OWNED BY` can move it.
    assert len({(entry[1], entry[2]) for entry in SEQUENCES}) == 9
    for name, table, column, *_ in SEQUENCES:
        assert name == f"{table}_{column}_seq", name


def test_both_assets_declare_every_sequence_with_its_owning_column() -> None:
    for asset in (V5_SQL, V5_PGRESTORE):
        expected = _cte_body(asset, "expected_sequences")
        for name, table, column, data_type, *_ in SEQUENCES:
            assert f"'{name}', '{table}', '{column}', '{data_type}'" in expected, (
                f"{asset.name}: {name}"
            )
        # `cycle` FALSE on all nine: a cyclic sequence would re-emit identifiers
        # already allocated.
        assert expected.count("FALSE)") == 9, asset.name
        assert "TRUE)" not in expected, asset.name


def test_the_high_water_mark_reads_every_owning_table() -> None:
    """`last_value >= max(id)` cannot be generic in static SQL.

    The nine tables must be named, one by one. It is verbose and that is the price:
    a loop would require dynamic SQL, hence a function, hence a write — in a
    contract that must stay READ-ONLY.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "sequence_high_water")
        for name, table, column, *_ in SEQUENCES:
            assert f"('{name}', (SELECT max({column}) FROM {table}))" in body, (
                f"{asset.name}: {name}"
            )
        assert body.count("SELECT max(") == 9, asset.name


def test_the_backfill_check_treats_a_never_called_sequence_as_zero() -> None:
    """`last_value` is NULL when the sequence has never been used — not "up to date".

    That is exactly the state of a freshly restored sequence that has not received
    its `setval`. Without the `COALESCE`, the comparison would return NULL, the
    `WHERE` would be false, and the failure we are looking for would pass in
    silence. The `COALESCE` on `max(id)` is the other half: an EMPTY table and a
    never-called sequence are consistent with each other.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "sequence_backfill_mismatches")
        assert "COALESCE(sequence_state.last_value, 0)" in body, asset.name
        assert "COALESCE(high_water.highest_assigned, 0)" in body, asset.name
        assert "sequence_state.sequencename IS NULL" in body, asset.name


def test_the_contract_never_advances_a_sequence_to_observe_it() -> None:
    """An attestation that mutates its object attests nothing.

    `currval()` would raise outside a session; `nextval()` would advance the
    sequence — hence pass a check that should have failed, while making it fail one
    more time at the next restore. The `pg_sequences` view reads without writing.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        assert "pg_catalog.pg_sequences" in sql, asset.name
        assert "nextval(" not in sql, asset.name
        assert "currval(" not in sql, asset.name
        assert "setval(" not in sql, asset.name


def test_the_property_check_is_bidirectional() -> None:
    """Without the second direction, a hand-CREATED sequence would pass unnoticed.

    And it would be caught by nothing else: `table_set` reads `pg_tables`, which
    ignores sequences, and `catalog_counts` only counts indexes and foreign keys.
    This second term is the ONLY net.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "sequence_property_mismatches")
        assert body.count("SELECT count(*)") == 2, asset.name
        assert "LEFT JOIN observed_sequences" in body, asset.name
        assert "LEFT JOIN expected_sequences" in body, asset.name
        assert "WHERE expected_sequence.sequence_name IS NULL" in body, asset.name


def test_the_backfill_check_has_one_direction_and_says_why() -> None:
    """A check that CANNOT fail is worse than no check.

    The advance has only one direction, and that is a FACT about sequences, not a
    choice: a sequence AHEAD of `max(id)` is the normal regime — every rolled-back
    transaction leaves one. A "last_value too large" term would redden on nominal
    operation. This test pins the absence so that nobody "completes" the symmetry
    later with a false term.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "sequence_backfill_mismatches")
        assert body.count("SELECT count(*)") == 1, asset.name
        assert ">" not in body.replace(">=", ""), asset.name


def test_the_new_ctes_are_byte_identical_across_the_two_variants() -> None:
    """MAXIMAL parity, and measured rather than decreed.

    These CTEs read neither `pg_get_indexdef` nor `column_default`: there is
    literally nothing `pg_restore` normalises. If a future migration added a cast
    default on one of these columns, this test would redden — and that would be the
    right moment to ask the question.
    """
    for name in NEW_CTES:
        assert _cte_body(V5_SQL, name) == _cte_body(V5_PGRESTORE, name), name

    for asset in (V5_SQL, V5_PGRESTORE):
        for name in NEW_CTES:
            body = _cte_body(asset, name)
            assert "'::character varying::text'" not in body, f"{asset.name}: {name}"
            assert "']::text[]'" not in body, f"{asset.name}: {name}"


def test_the_check_row_names_its_two_counters_in_both_assets() -> None:
    """A failure must say WHICH of the two checks moved.

    Template from `brain_runtime_032_036_037` and `historical_relation_shape`: a
    bare boolean would force re-reading all the SQL to find out whether it is the
    shape or the advance that drifted — and the two have entirely different causes
    and fixes.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        row = sql.split(f"'{CHECK_ID}',", 1)[1].split("UNION ALL", 1)[0]
        assert "'sequence_backfill_mismatches', 0" in row, asset.name
        assert "'sequence_property_mismatches', 0" in row, asset.name
        assert "sequence_backfill_mismatches.value = 0" in row, asset.name
        assert "AND sequence_property_mismatches.value = 0" in row, asset.name


def test_the_json_manifest_declares_the_sequence_check() -> None:
    checks = json.loads(V5_JSON.read_text(encoding="utf-8"))["checks"]
    entry = next(check for check in checks if check["id"] == CHECK_ID)
    assert entry == {
        "id": CHECK_ID,
        "kind": "brain_schema_invariant",
        "name": CHECK_ID,
    }
