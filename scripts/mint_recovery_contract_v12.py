"""Mint the v12 DR contract as a surgical delta of v11.

Committed because no minting tool existed at all: v11 and its ten ancestors
were assembled by a gesture nobody recorded, which is why the handoff could
demand "mint from the migrated clone, never by hand" while offering no way to
obey. This is that way, for one generation.

A v13 needs three edits: the four scalar anchors in `patch_scalars`, the new
tables in `main`, and a fresh look at `TWIN_DIVERGENCE_BY_DESIGN`. The block
plumbing is generic. Making the contract MEASURE rather than carry constants
is the real fix and has its own ticket (learning f6eb3ae3, 2026-08-28).

Usage:
    python scripts/mint_recovery_contract_v12.py <dsn> [src.sql] [dst.sql]

Never regenerates. `test_vNN_sql_assets_are_an_additive_candidate` diffs the
two assets LINE BY LINE and allows only an explicit allowlist of removed
lines, so a reformat would fail even if every value were right.

The delta is computed by SQL, not by text: for each `expected_X` block the
script runs the asset's OWN `observed_X` query against a database the alembic
chain just built, and subtracts the asset's own expected VALUES. Measuring
with the contract's own instrument is what makes the result trustworthy --
guessing the md5 expressions produces hashes that never match (learning
2b3fadae, which also forbids any global sed on a revision number).
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import asyncpg

#: Source and destination come from argv so the same mint runs on the asset
#: and on its `-pgrestore` twin: the twin is the SAME contract measured on a
#: restored target, so it must receive the SAME delta or the two would drift.
SRC = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v11.sql")
DST = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v12.sql")

#: Pairs to reconcile: expected block name -> observed block name.
PAIRS = [
    ("expected_table_columns", "observed_table_columns"),
    ("expected_table_constraints", "observed_table_constraints"),
    ("expected_table_indexes", "observed_table_indexes"),
    ("expected_trigger_functions", "observed_trigger_functions"),
]


def find_block(lines: list[str], name: str) -> tuple[int, int, str]:
    """Return (header_index, closing_index, column_spec) for a CTE block.

    The closing line is the first line equal to `),` in column 0 after the
    header -- the asset indents every nested construct, so column 0 is
    unambiguous.
    """
    header = None
    for index, line in enumerate(lines):
        # The first CTE of the file carries the `WITH ` keyword on its own
        # header line, so an anchored match on the bare name misses it.
        if line.startswith((f"{name}(", f"{name} AS (", f"WITH {name}(")):
            header = index
            break
    if header is None:
        raise SystemExit(f"block {name} not found")
    spec_match = re.search(rf"{re.escape(name)}\(([^)]*)\)", lines[header])
    spec = spec_match.group(1) if spec_match else ""
    for index in range(header + 1, len(lines)):
        if lines[index] == "),":
            return header, index, spec
    raise SystemExit(f"block {name} has no closing line")


def body(lines: list[str], header: int, close: int) -> str:
    """The CTE body without its header line and without the closing `),`."""
    return "\n".join(lines[header + 1 : close])


async def measure(dsn: str, lines: list[str]) -> dict[str, list[tuple]]:
    """For each pair, the observed rows the expected block does NOT carry."""
    deltas: dict[str, list[tuple]] = {}
    connection = await asyncpg.connect(dsn)
    try:
        for expected_name, observed_name in PAIRS:
            e_head, e_close, spec = find_block(lines, expected_name)
            o_head, o_close, o_spec = find_block(lines, observed_name)
            # Keep the observed block's OWN column spec when re-wrapping it:
            # `observed_table_columns(table_name, definition_md5)` names its
            # columns in the header, not in its SELECT, so dropping the spec
            # leaves them with raw catalog names and the projection fails.
            observed_header = f"observed({o_spec})" if o_spec else "observed"
            query = (
                f"WITH {observed_header} AS (\n{body(lines, o_head, o_close)}\n),\n"
                f"expected({spec}) AS (\n{body(lines, e_head, e_close)}\n)\n"
                # Project BOTH sides onto the expected block's own column
                # names: `observed_trigger_functions` carries a fourth column
                # (`function_oid`) that the contract does not compare, and a
                # bare `SELECT *` makes the EXCEPT a syntax error.
                f"SELECT {spec} FROM observed "
                f"EXCEPT SELECT {spec} FROM expected ORDER BY 1, 2"
            )
            rows = await connection.fetch(query)
            deltas[expected_name] = [tuple(r) for r in rows]
            print(f"  {expected_name}: {len(rows)} ligne(s) à ajouter", flush=True)
    finally:
        await connection.close()
    return deltas


def render(expected_name: str, row: tuple) -> list[str]:
    """Format one row in the block's own style, read from the block itself."""
    if expected_name == "expected_trigger_functions":
        name, digest, octets = row
        return [f"     ('{name}', '{digest}', {octets}),"]
    parts = ",\n".join(f"         '{value}'" for value in row)
    return ["     ("] + parts.split("\n") + ["     ),"]


def insert_sorted(lines: list[str], name: str, rows: list[tuple]) -> list[str]:
    """Splice new rows into a VALUES block, keeping the block's sort order.

    Rows are appended at the END of the block rather than interleaved: the
    contract compares SETS (`EXCEPT`), so position carries no meaning, and an
    append keeps the diff to pure insertions -- which is exactly what the
    additivity test measures.
    """
    if not rows:
        return lines
    _, close, _ = find_block(lines, name)
    # the last VALUES row ends with `)` not `),`; give it its comma back
    last = close - 1
    if lines[last].rstrip().endswith(")") and not lines[last].rstrip().endswith("),"):
        lines[last] = lines[last] + ","
    block: list[str] = []
    for row in rows:
        block.extend(render(name, row))
    # the final row of the block must not carry a trailing comma
    block[-1] = block[-1].rstrip(",")
    return lines[:close] + block + lines[close:]


def patch_scalars(text: str) -> str:
    """The four values that CHANGE rather than get added.

    Written one by one, anchored on their exact surrounding line. A global
    replacement of `11` or of a revision number is how `v4.sql` nearly lost
    five invariants that merely NAMED the migration that installed them.
    """
    replacements = [
        # `table_set` compared two sorted lists under DIFFERENT collations:
        # `expected_tables` is `text` (database collation) while
        # `pg_tables.tablename` is `name` (C). With 44 tables no pair differed;
        # `knowledge_claims` vs `knowledge_claim_verdicts` differ exactly on the
        # `_`/`s` boundary and the check went red with identical SETS. Pinning C
        # on both sides also makes the contract portable: a restore target with
        # another lc_collate would otherwise produce a false red.
        (
            "     (SELECT jsonb_agg(table_name ORDER BY table_name)"
            " FROM expected_tables) AS expected,",
            '     (SELECT jsonb_agg(table_name ORDER BY table_name COLLATE "C")'
            " FROM expected_tables) AS expected,",
        ),
        (
            "         SELECT COALESCE(jsonb_agg(tablename ORDER BY tablename), '[]'::jsonb)",
            '         SELECT COALESCE(jsonb_agg(tablename ORDER BY tablename COLLATE "C"),'
            " '[]'::jsonb)",
        ),
        ("         'foreign_keys', 48,", "         'foreign_keys', 53,"),
        ("         'indexes', 160,", "         'indexes', 171,"),
        (
            " 'contract_id', 'brain-v42/postgresql-recovery/v11',",
            " 'contract_id', 'brain-v42/postgresql-recovery/v12',",
        ),
        (" 'schema_version', 11", " 'schema_version', 12"),
    ]
    for before, after in replacements:
        if text.count(before) != 1:
            raise SystemExit(f"anchor not unique ({text.count(before)}x): {before!r}")
        text = text.replace(before, after)
    return text


SEQ_SPEC = (
    "sequence_name, owning_table, owning_column, data_type, "
    "increment_by, min_value, max_value, start_value, cycles"
)


def find_multiline_block(lines: list[str], name: str) -> tuple[int, int]:
    """`expected_sequences` spreads its column spec over ten lines, so the
    single-line regex of `find_block` cannot see it."""
    header = next(i for i, line in enumerate(lines) if line.startswith(f"{name}("))
    close = next(i for i in range(header + 1, len(lines)) if lines[i] == "),")
    return header, close


def sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    return f"'{value}'"


async def measure_sequences(dsn: str, lines: list[str]) -> list[tuple]:
    header, close = find_multiline_block(lines, "expected_sequences")
    values_start = next(i for i in range(header, close) if lines[i].strip() == "VALUES")
    o_head, o_close, _ = find_block(lines, "observed_sequences")
    query = (
        f"WITH observed AS (\n{body(lines, o_head, o_close)}\n),\n"
        f"expected({SEQ_SPEC}) AS (\n" + "\n".join(lines[values_start:close]) + "\n)\n"
        f"SELECT {SEQ_SPEC} FROM observed "
        f"EXCEPT SELECT {SEQ_SPEC} FROM expected ORDER BY 1"
    )
    connection = await asyncpg.connect(dsn)
    try:
        rows = [tuple(r) for r in await connection.fetch(query)]
    finally:
        await connection.close()
    print(f"  expected_sequences: {len(rows)} ligne(s) à ajouter", flush=True)
    return rows


def insert_plain(lines: list[str], name: str, rendered: list[str]) -> list[str]:
    """Append pre-rendered rows to a VALUES block, fixing the trailing comma."""
    if not rendered:
        return lines
    try:
        _, close, _ = find_block(lines, name)
    except SystemExit:
        _, close = find_multiline_block(lines, name)
    last = close - 1
    stripped = lines[last].rstrip()
    if stripped.endswith(")") and not stripped.endswith("),"):
        lines[last] = lines[last] + ","
    rendered = list(rendered)
    rendered[-1] = rendered[-1].rstrip(",")
    return lines[:close] + rendered + lines[close:]


#: Blocks the file compares INLINE rather than through an `observed_X` CTE,
#: so their measuring expression is copied verbatim from the comparison block
#: that judges them -- the closest available equivalent to reusing the asset's
#: own instrument. Codex review 2026-09-21: without these, 055's view and its
#: five trigger BINDINGS are attested by nothing, and dropping all five
#: triggers would leave the contract green while the append-only guarantee the
#: migration exists for is gone.
INLINE = {
    "expected_contract_views": (
        "SELECT c.relname, "
        "COALESCE('security_barrier=true' = ANY(c.reloptions), FALSE), "
        "md5(pg_catalog.pg_get_viewdef(c.oid, TRUE)) "
        "FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'v'",
        "view_name, security_barrier, definition_md5",
        lambda r: f"     ('{r[0]}', {'TRUE' if r[1] else 'FALSE'}, '{r[2]}'),",
    ),
    "expected_runtime_user_triggers": (
        "SELECT c.relname, t.tgname "
        "FROM pg_catalog.pg_trigger t "
        "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT t.tgisinternal"
        " AND c.relname IN ('knowledge_claims', 'knowledge_claim_verdicts', 'knowledge_fact_definitions')",
        "table_name, trigger_name",
        lambda r: f"     ('{r[0]}', '{r[1]}'),",
    ),
    "expected_runtime_trigger_tables": (
        "SELECT DISTINCT c.relname "
        "FROM pg_catalog.pg_trigger t "
        "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT t.tgisinternal"
        " AND c.relname IN ('knowledge_claims', 'knowledge_claim_verdicts', 'knowledge_fact_definitions')",
        "table_name",
        lambda r: f"     ('{r[0]}'),",
    ),
}


async def measure_inline(dsn: str, lines: list[str]) -> dict[str, list[str]]:
    """Rows an inline-compared block is missing, already rendered."""
    rendered: dict[str, list[str]] = {}
    connection = await asyncpg.connect(dsn)
    try:
        for name, (query, spec, render_row) in INLINE.items():
            e_head, e_close, _ = find_block(lines, name)
            full = (
                f"WITH observed({spec}) AS (\n{query}\n),\n"
                f"expected({spec}) AS (\n{body(lines, e_head, e_close)}\n)\n"
                f"SELECT {spec} FROM observed EXCEPT SELECT {spec} FROM expected ORDER BY 1"
            )
            rows = [tuple(r) for r in await connection.fetch(full)]
            rendered[name] = [render_row(r) for r in rows]
            print(f"  {name}: {len(rows)} ligne(s) à ajouter", flush=True)
    finally:
        await connection.close()
    return rendered


def assert_no_key_collision(lines: list[str], name: str, rows: list[tuple], keys: int) -> None:
    """Refuse a delta whose business key ALREADY exists in the expected block.

    `observed EXCEPT expected` cannot tell an ADDED object from a MODIFIED one:
    a changed fingerprint yields a row the block does not carry, and appending
    it would leave the stale row beside it. For these blocks a survivor turns
    the bidirectional check red rather than hiding anything, so the risk is
    absent -- this assertion makes it IMPOSSIBLE instead, which is the
    difference Codex asked for on 2026-09-21.
    """
    head, close, _ = find_block(lines, name)
    # Collapse ALL whitespace: these blocks are multi-line, so a naive strip of
    # one indent width silently never matches and the guard passes on
    # everything -- which is exactly how the first version of this assertion
    # failed to catch the pgrestore twin's re-serialized index.
    existing = re.sub(r"\s+", "", "\n".join(lines[head:close]))
    collisions = []
    for row in rows:
        key = "(" + ",".join(f"'{value}'" for value in row[:keys]) + ","
        if key in existing:
            collisions.append(key)
    if collisions:
        raise SystemExit(
            f"{name}: {len(collisions)} clé(s) déjà présente(s) — MODIFICATION, pas ajout: "
            + ", ".join(collisions[:5])
        )


#: Divergences the `-pgrestore` twin carries BY DESIGN and must keep.
#:
#: `test_the_pgrestore_twin_diverges_from_a_fresh_head_by_exactly_one_index`
#: asserts that the restored-target asset differs from a chain-built database
#: by this index and nothing else: pg_restore re-serializes its definition, so
#: the twin records the restored form. Folding the fresh-head form in would
#: erase the divergence and fail that test from the other side -- zero
#: divergence where exactly one is required.
#:
#: Found by the key-collision guard, which reported it as a MODIFICATION
#: rather than an addition. That is the guard working, not an obstacle.
TWIN_DIVERGENCE_BY_DESIGN = {
    ("expected_table_indexes", ("dream_promotions", "idx_dream_promotions_source_materialized")),
}


def drop_by_design(name: str, rows: list[tuple]) -> list[tuple]:
    kept = [r for r in rows if (name, tuple(r[:2])) not in TWIN_DIVERGENCE_BY_DESIGN]
    if len(kept) != len(rows):
        print(
            f"  {name}: {len(rows) - len(kept)} divergence(s) par conception conservée(s)",
            flush=True,
        )
    return kept


async def main() -> int:
    dsn = sys.argv[1]
    lines = SRC.read_text(encoding="utf-8").split("\n")
    print("mesure du delta contre la base 055 …", flush=True)
    deltas = await measure(dsn, lines)

    for expected_name, _ in PAIRS:
        keys = 1 if expected_name in {"expected_table_columns", "expected_trigger_functions"} else 2
        deltas[expected_name] = drop_by_design(expected_name, deltas[expected_name])
        assert_no_key_collision(lines, expected_name, deltas[expected_name], keys)
        lines = insert_sorted(lines, expected_name, deltas[expected_name])

    # `expected_tables` has no observed twin -- `table_sets` compares it
    # inline against pg_tables -- so the three names come from the measured
    # table_set delta rather than from a paired query.
    new_tables = ["knowledge_claim_verdicts", "knowledge_claims", "knowledge_fact_definitions"]
    lines = insert_plain(lines, "expected_tables", [f"     ('{t}')," for t in new_tables])
    print(f"  expected_tables: {len(new_tables)} ligne(s) à ajouter", flush=True)

    inline = await measure_inline(dsn, lines)
    for name, rendered in inline.items():
        lines = insert_plain(lines, name, rendered)

    sequences = await measure_sequences(dsn, lines)
    lines = insert_plain(
        lines,
        "expected_sequences",
        ["     (" + ", ".join(sql_literal(v) for v in row) + ")," for row in sequences],
    )

    # `sequence_high_water` carries a SUBQUERY per row, not a measured value:
    # it is what lets the contract catch a restore whose sequence sits behind
    # max(id) -- green today, unique-violation on the next insert (Codex P1,
    # 2026-09-21). Emitted from the owning table/column of the sequences just
    # added, so it cannot drift from them.
    lines = insert_plain(
        lines,
        "sequence_high_water",
        [f"     ('{row[0]}', (SELECT max({row[2]}) FROM {row[1]}))," for row in sequences],
    )
    print(f"  sequence_high_water: {len(sequences)} ligne(s) à ajouter", flush=True)

    text = patch_scalars("\n".join(lines))
    DST.write_text(text, encoding="utf-8")
    print(f"\nécrit : {DST}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
