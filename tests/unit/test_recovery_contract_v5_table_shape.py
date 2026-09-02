"""The v5 contract attests the SHAPE of the 32 tables, not that of seven of them.

`8eaefe36`, last child of the gate. Before this batch, the column, constraint and
index fingerprints covered SEVEN tables — `brain_sessions` and
`brain_session_artifacts` through `brain_runtime_032_036_037`, the five historical
relations through `historical_relation_shape`. The other twenty-five could change
column, constraint or index without a byte of the receipt moving.

**The scope is SETTLED: the 32 tables, never a named list.** A named list would be
existential, hence blind to the 33rd table. The OBSERVED side is therefore derived
from the whole catalogue, and the difference is counted in BOTH directions: an
expected table that is missing OR diverges, and an observed table the contract does
not know. It is the second term that bites on a 33rd table.

**The rank is DENSE, and that is not a shape detail.** `ordinal_position` equals
`pg_attribute.attnum`, which carries the HOLES of dropped columns. Measured on
2026-08-23 across three databases at the same head `046`: 24 dropped columns over 8
tables in production, 11 over 6 tables on a database built afresh by `alembic
upgrade head`, **ZERO** on a database out of `pg_restore`. The ordinal fingerprint
therefore measures the INSTANCE's HISTORY, not the schema: extended to the 32
tables as is, it made **8 tables out of 32** diverge between production and its own
restoration, and 6 more between that restoration and a fresh Alembic database. With
the dense rank, the three databases return the **32** same fingerprints.

**The trap within the trap**: the dense rank is computed AFTER the `NOT
attisdropped` filter. Computed before, it equals `attnum` on every row — and the
bug SURVIVES green tests, because on a database with no dropped column the two
expressions coincide. The dropped-column proof therefore lives in
`tests/integration/db/test_recovery_contract_dense_column_rank.py`, with its control
mutation in both directions; this module keeps only the shape.

**A DEDICATED check row, and the denominator moves to 30.** First draft: the three
counters grafted onto `catalog_counts`, to hold the receipt at `29/29`. That held
the number and nothing else. A hardening merged into an existing check row verifies
more while always returning the same score — that is `_expected_v5()`'s wording
about `81c4f366`, and it is the reason v5 has four check rows. Second reason, this
one measured: `red-backup` models `catalog_counts_equals` in Pydantic
`extra="forbid"` over exactly FOUR fields
(`ReD_v1/projects/red-backup/src/backup/recovery_contract.py`), so a fifth signal
merged in there does not cost zero — it breaks another repository. The `dr-current`
declaration of `docs/PLAN_INDEX_REPAIR_RUNBOOK.md` follows.

**The index expression is NOT switched, and this module pins that.**
`pg_get_indexdef(oid, 0, true)` would cancel the restoration residue measured at ONE
index (`idx_dream_promotions_source_materialized`), but only combined with the four
normalisations, and it would move the **130** index fingerprints. That is an
operator decision; until it is taken, the contract keeps a bare
`pg_get_indexdef(oid)` and the residue stays named.
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

ASSETS = (V5_SQL, V5_PGRESTORE)

CHECK_ID = "table_shape"

#: The check row's three sub-counters, all expected at zero.
COUNTERS = (
    "table_column_mismatches",
    "table_constraint_mismatches",
    "table_index_mismatches",
)

#: MEASURED on 2026-08-23 against production at head `046`, then replayed
#: identically against a `pg_restore` database and an `alembic upgrade head`
#: database: 117 constraints and 130 indexes over the 32 base tables of the
#: `public` schema.
EXPECTED_CONSTRAINT_ROWS = 117
EXPECTED_INDEX_ROWS = 130


def _tables_of_the_contract() -> list[str]:
    """The contract's tables, READ from `table_set` — never retyped here.

    Retyping the list would create a second source of truth that would only
    diverge at read time: the day a 33rd table enters `table_set`, this module must
    require its coverage, not keep counting 32.
    """
    checks = json.loads(V5_JSON.read_text(encoding="utf-8"))["checks"]
    table_set = next(check for check in checks if check["id"] == "table_set")
    return list(table_set["tables"])


def _cte_block(sql: str, cte_name: str) -> str:
    """A CTE's body, from the name at column 0 to its closing parenthesis."""
    start = sql.index(f"\n{cte_name}")
    return sql[start : sql.index("\n),\n", start)]


def _quoted_names(block: str) -> list[str]:
    return re.findall(r"'([a-z0-9_]+)'", block)


def test_the_column_fingerprint_covers_every_table_of_the_table_set() -> None:
    """The two numbers SIDE BY SIDE: covered tables, existing tables."""
    tables = _tables_of_the_contract()
    assert len(tables) == 32

    for asset in ASSETS:
        block = _cte_block(asset.read_text(encoding="utf-8"), "expected_table_columns")
        covered = [name for name in _quoted_names(block) if not re.fullmatch(r"[0-9a-f]{32}", name)]
        assert covered == sorted(tables), asset.name
        assert len(covered) == len(tables) == 32, asset.name


def test_the_constraint_and_index_fingerprints_carry_every_object() -> None:
    for asset in ASSETS:
        sql = asset.read_text(encoding="utf-8")
        for cte, expected_rows in (
            ("expected_table_constraints", EXPECTED_CONSTRAINT_ROWS),
            ("expected_table_indexes", EXPECTED_INDEX_ROWS),
        ):
            block = _cte_block(sql, cte)
            digests = re.findall(r"'[0-9a-f]{32}'", block)
            assert len(digests) == expected_rows, f"{asset.name}: {cte}"


def test_the_observed_side_is_universal_and_never_filtered_by_the_expected_list() -> None:
    """Blindness to the 33rd table is coded in, it is not forgotten.

    A `WHERE table_name IN (SELECT … FROM expected_…)` on the observed side makes
    the check existential: it proves that what we know is conforming, and says
    nothing about what we do not know.
    """
    for asset in ASSETS:
        sql = asset.read_text(encoding="utf-8")
        for observed, expected in (
            ("observed_table_columns", "expected_table_columns"),
            ("observed_table_constraints", "expected_table_constraints"),
            ("observed_table_indexes", "expected_table_indexes"),
        ):
            block = _cte_block(sql, observed)
            assert expected not in block, f"{asset.name}: {observed} filtre sur {expected}"
            assert "relkind IN ('r', 'p')" in block, f"{asset.name}: {observed}"


def test_every_mismatch_counter_counts_in_both_directions() -> None:
    for asset in ASSETS:
        sql = asset.read_text(encoding="utf-8")
        for counter, expected, observed in (
            ("table_column_mismatches", "expected_table_columns", "observed_table_columns"),
            (
                "table_constraint_mismatches",
                "expected_table_constraints",
                "observed_table_constraints",
            ),
            ("table_index_mismatches", "expected_table_indexes", "observed_table_indexes"),
        ):
            block = _cte_block(sql, counter)
            # Direction 1: expected with no conforming observed. Direction 2: unknown observed.
            assert f"FROM {expected} AS" in block, f"{asset.name}: {counter}"
            assert f"FROM {observed} AS" in block, f"{asset.name}: {counter}"
            assert block.count("LEFT JOIN") == 2, f"{asset.name}: {counter}"


def test_the_column_rank_is_dense_and_computed_after_the_dropped_column_filter() -> None:
    for asset in ASSETS:
        block = _cte_block(asset.read_text(encoding="utf-8"), "observed_table_columns")
        assert "NOT attribute_record.attisdropped" in block, asset.name
        assert "row_number() OVER (" in block, asset.name
        assert "PARTITION BY observed_column.table_name" in block, asset.name
        # The dense rank REPLACES `ordinal_position` in the fingerprint: leaving it
        # in the payload would re-engrave the instance's history.
        payload = block.split("jsonb_build_array(", 1)[1]
        assert "ordinal_position" not in payload, asset.name
        assert "dense_position" in payload, asset.name


def test_the_index_expression_is_not_switched_to_the_pretty_form() -> None:
    """The switch is QUANTIFIED and NOT applied — pinned so that it stays visible."""
    for asset in ASSETS:
        sql = asset.read_text(encoding="utf-8")
        block = _cte_block(sql, "observed_table_indexes")
        assert "pg_catalog.pg_get_indexdef(index_record.indexrelid)" in block, asset.name
        assert ", 0, true)" not in block, asset.name
        assert "pg_get_indexdef(index_record.indexrelid, 0" not in sql, asset.name


def test_the_shape_check_is_a_row_of_its_own_and_leaves_catalog_counts_alone() -> None:
    """The hardening is COUNTED in the receipt, and `catalog_counts` keeps its 4 fields."""
    checks = json.loads(V5_JSON.read_text(encoding="utf-8"))["checks"]
    assert len(checks) == 30

    entry = next(check for check in checks if check["id"] == CHECK_ID)
    assert entry == {"id": CHECK_ID, "kind": "brain_schema_invariant", "name": CHECK_ID}

    # `red-backup` refuses one more field here: `CatalogCountsEquals` is
    # `extra="forbid"` over exactly these four integers.
    catalog_counts = next(check for check in checks if check["id"] == "catalog_counts")
    assert set(catalog_counts) == {
        "foreign_keys",
        "id",
        "indexes",
        "invalid_indexes",
        "kind",
        "schema",
        "unvalidated_constraints",
    }


def test_the_receipt_names_the_three_counters_rather_than_a_bare_boolean() -> None:
    """Template from `sequence_shape`: a bare boolean would force re-reading all the SQL.

    Columns, constraints and indexes have entirely different causes and fixes — a
    column drift is a missing migration, an index drift a `REINDEX`, a constraint
    drift a restoration round-trip.
    """
    for asset in ASSETS:
        sql = asset.read_text(encoding="utf-8")
        row = sql.split(f"'{CHECK_ID}',", 1)[1].split("UNION ALL", 1)[0]
        for counter in COUNTERS:
            assert f"'{counter}', 0" in row, f"{asset.name}: {counter} attendu"
            assert f"'{counter}', {counter}.value" in row, f"{asset.name}: {counter} observé"
            assert f"{counter}.value = 0" in row, f"{asset.name}: {counter} verdict"
