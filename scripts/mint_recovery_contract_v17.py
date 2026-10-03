"""Mint the v17 DR contract as a surgical delta of v16, for head 059.

Derived from `mint_recovery_contract_v15.py` (the last MEASURED mint; v16 was a
text-only delta). 059 adds to `tickets` one column (`target_release`), one CHECK
(`tickets_target_release_valid`) and one partial index
(`idx_tickets_to_project_target_release`). The contract therefore moves exactly:

* the `tickets` row of `expected_table_columns` (one md5 over the table's column
  definitions) is REWRITTEN;
* one row is ADDED to `expected_table_constraints` and one to
  `expected_table_indexes`;
* `catalog_counts.indexes` goes from 172 to 173 (one index more; no foreign key);
* the identity moves to v17 / schema version 17.

Additions are DECLARED, unlike v15's open append: an observed row outside
`ALLOWED_ADDITIONS` stops the mint, because a schema object nobody declared is
exactly what this contract exists to catch.

Usage:
    python scripts/mint_recovery_contract_v17.py <dsn> [src.sql] [dst.sql]
    python scripts/mint_recovery_contract_v17.py --manifest [src.json] [dst.json]

Measure the base asset against a database the alembic chain just built to head
059, and the `-pgrestore` twin against a real custom-format `pg_dump`/`pg_restore`
of a disposable 058 source migrated to 059 after the restore. After writing, the
mint re-measures its own output in BOTH directions and refuses to report success
while any row still disagrees.

Never regenerates. The unit test diffs v16 and v17 LINE BY LINE.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import asyncpg

#: Pairs to reconcile: expected block name -> observed block name.
PAIRS = [
    ("expected_table_columns", "observed_table_columns"),
    ("expected_table_constraints", "observed_table_constraints"),
    ("expected_table_indexes", "observed_table_indexes"),
    ("expected_trigger_functions", "observed_trigger_functions"),
]

#: Business-key width per paired block: the columns that name the object,
#: before the fingerprint.
KEY_WIDTH = {
    "expected_table_columns": 1,
    "expected_table_constraints": 2,
    "expected_table_indexes": 2,
    "expected_trigger_functions": 1,
}

#: The row 059 is allowed to REWRITE: `tickets` gains a column, so its
#: table-wide column fingerprint changes. Anything else that already exists and
#: measures differently is a modification nobody declared, and the mint stops.
REWRITABLE_ROWS = {
    ("expected_table_columns", ("tickets",)),
}

#: The rows 059 is allowed to ADD, and nothing else.
ALLOWED_ADDITIONS = {
    ("expected_table_constraints", ("tickets", "tickets_target_release_valid")),
    ("expected_table_indexes", ("tickets", "idx_tickets_to_project_target_release")),
}

#: Divergences the `-pgrestore` twin carries BY DESIGN, kept from v14: when the
#: twin is measured against a chain-built database instead of a real restore,
#: pg_restore's re-serialised form of this index shows up as a modification.
#: Measured against a real restore, it does not show up at all.
TWIN_DIVERGENCE_BY_DESIGN = {
    ("expected_table_indexes", ("dream_promotions", "idx_dream_promotions_source_materialized")),
}


def find_block(lines: list[str], name: str) -> tuple[int, int, str]:
    """Return (header_index, closing_index, column_spec) for a CTE block.

    The closing line is the first line equal to `),` in column 0 after the
    header -- the asset indents every nested construct, so column 0 is
    unambiguous.
    """
    header = None
    for index, line in enumerate(lines):
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


def _difference_query(lines: list[str], expected_name: str, observed_name: str) -> str:
    """Both directions of the comparison, tagged, in the expected block's columns."""
    e_head, e_close, spec = find_block(lines, expected_name)
    o_head, o_close, o_spec = find_block(lines, observed_name)
    observed_header = f"observed({o_spec})" if o_spec else "observed"
    return (
        f"WITH {observed_header} AS (\n{body(lines, o_head, o_close)}\n),\n"
        f"expected({spec}) AS (\n{body(lines, e_head, e_close)}\n)\n"
        f"(SELECT 'missing' AS side, {spec} FROM observed "
        f"EXCEPT SELECT 'missing', {spec} FROM expected)\n"
        f"UNION ALL\n"
        f"(SELECT 'stale' AS side, {spec} FROM expected "
        f"EXCEPT SELECT 'stale', {spec} FROM observed)\n"
        f"ORDER BY 1, 2, 3"
    )


async def measure(dsn: str, lines: list[str]) -> dict[str, dict[str, list[tuple]]]:
    """For each pair: the observed rows the block lacks, and the rows it
    carries that nothing observes any more."""
    result: dict[str, dict[str, list[tuple]]] = {}
    connection = await asyncpg.connect(dsn)
    try:
        for expected_name, observed_name in PAIRS:
            rows = await connection.fetch(_difference_query(lines, expected_name, observed_name))
            result[expected_name] = {
                "missing": [tuple(r)[1:] for r in rows if r[0] == "missing"],
                "stale": [tuple(r)[1:] for r in rows if r[0] == "stale"],
            }
    finally:
        await connection.close()
    return result


def render(expected_name: str, row: tuple) -> list[str]:
    """Format one row in the block's own style."""
    if expected_name == "expected_trigger_functions":
        name, digest, octets = row
        return [f"     ('{name}', '{digest}', {octets}),"]
    parts = ",\n".join(f"         '{value}'" for value in row)
    return ["     ("] + parts.split("\n") + ["     ),"]


def append_rows(lines: list[str], name: str, rows: list[tuple]) -> list[str]:
    """Append new rows at the END of a VALUES block.

    The contract compares SETS, so position carries no meaning, and an append
    keeps the diff to pure insertions.
    """
    if not rows:
        return lines
    _, close, _ = find_block(lines, name)
    last = close - 1
    if lines[last].rstrip().endswith(")") and not lines[last].rstrip().endswith("),"):
        lines[last] = lines[last] + ","
    block: list[str] = []
    for row in rows:
        block.extend(render(name, row))
    block[-1] = block[-1].rstrip(",")
    return lines[:close] + block + lines[close:]


def rewrite_row(lines: list[str], name: str, row: tuple) -> list[str]:
    """Replace the fingerprint of an existing row, in place.

    Handles every `expected_table_*` shape: the `KEY_WIDTH[name]` key lines, then
    the fingerprint line. Exactly one contiguous key run must exist in the block
    -- zero means the row is not a rewrite, two mean the block is already corrupt.
    """
    head, close, _ = find_block(lines, name)
    width = KEY_WIDTH[name]
    key_values = list(row[:width])
    fingerprint = row[width]
    key_lines = [f"         '{value}'," for value in key_values]
    matches = [i for i in range(head + 1, close) if lines[i : i + len(key_lines)] == key_lines]
    if len(matches) != 1:
        raise SystemExit(
            f"{name}: {len(matches)} row(s) keyed {tuple(key_values)!r}, expected exactly one"
        )
    target = matches[0] + len(key_lines)
    old = lines[target]
    if not re.fullmatch(r"         '[0-9a-f]{32}'", old):
        raise SystemExit(f"{name}: unexpected fingerprint line for {tuple(key_values)!r}: {old!r}")
    lines[target] = f"         '{fingerprint}'"
    return lines


def split_delta(
    name: str, missing: list[tuple], stale: list[tuple]
) -> tuple[list[tuple], list[tuple]]:
    """Separate additions from declared rewrites; refuse everything else.

    `observed EXCEPT expected` cannot tell an ADDED object from a MODIFIED one:
    a changed fingerprint yields a row the block does not carry. Pairing it
    with the `stale` side is what tells them apart -- a key on both sides is a
    modification, and only `REWRITABLE_ROWS` may modify.
    """
    width = KEY_WIDTH[name]
    stale_keys = {tuple(r[:width]) for r in stale}
    additions: list[tuple] = []
    rewrites: list[tuple] = []
    refused: list[tuple] = []
    for row in missing:
        key = tuple(row[:width])
        if (name, key) in TWIN_DIVERGENCE_BY_DESIGN:
            print(f"  {name}: divergence by design kept {key}", flush=True)
            continue
        if key in stale_keys:
            (rewrites if (name, key) in REWRITABLE_ROWS else refused).append(row)
        elif (name, key) in ALLOWED_ADDITIONS:
            additions.append(row)
        else:
            refused.append(row)
    rewritten = {tuple(r[:width]) for r in rewrites}
    orphans = [
        r
        for r in stale
        if tuple(r[:width]) not in rewritten
        and (name, tuple(r[:width])) not in TWIN_DIVERGENCE_BY_DESIGN
    ]
    if refused or orphans:
        raise SystemExit(
            f"{name}: undeclared modification(s) {refused[:5]}; "
            f"row(s) nothing observes any more {orphans[:5]}"
        )
    return additions, rewrites


def patch_scalars(text: str) -> str:
    """The values that CHANGE rather than get added, anchored one by one.

    059 adds one index and no foreign key: the index count and the identity
    move. A global replacement of `16` or of a revision number is how `v4.sql`
    nearly lost five invariants that merely NAMED the migration that installed
    them.
    """
    replacements = [
        (
            " 'contract_id', 'brain-v42/postgresql-recovery/v16',",
            " 'contract_id', 'brain-v42/postgresql-recovery/v17',",
        ),
        (" 'schema_version', 16", " 'schema_version', 17"),
        ("         'indexes', 172,", "         'indexes', 173,"),
    ]
    for before, after in replacements:
        if text.count(before) != 1:
            raise SystemExit(f"anchor not unique ({text.count(before)}x): {before!r}")
        text = text.replace(before, after)
    return text


def mint_manifest(src: Path, dst: Path) -> None:
    """The JSON contract moves the identity and the index count, nothing else.

    Serialised the way every manifest of this directory is: sorted keys,
    compact separators, one trailing newline. 059 adds no table, so
    `table_set` stays v16's; it adds one index, so `catalog_counts` moves.
    """
    document = json.loads(src.read_text(encoding="utf-8"))
    if document["contract_id"] != "brain-v42/postgresql-recovery/v16":
        raise SystemExit(f"not the v16 manifest: {document['contract_id']}")
    document["contract_id"] = "brain-v42/postgresql-recovery/v17"
    document["schema_version"] = 17
    counts = [check for check in document["checks"] if check["id"] == "catalog_counts"]
    if len(counts) != 1 or counts[0]["indexes"] != 172:
        raise SystemExit(f"unexpected catalog_counts check: {counts}")
    counts[0]["indexes"] = 173
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote : {dst}", flush=True)


async def main(dsn: str, src: Path, dst: Path) -> int:
    lines = src.read_text(encoding="utf-8").split("\n")
    print(f"measuring {src.name} against the 059 database ...", flush=True)
    deltas = await measure(dsn, lines)

    for expected_name, _ in PAIRS:
        additions, rewrites = split_delta(
            expected_name, deltas[expected_name]["missing"], deltas[expected_name]["stale"]
        )
        print(
            f"  {expected_name}: {len(additions)} addition(s), {len(rewrites)} rewrite(s)",
            flush=True,
        )
        for row in rewrites:
            lines = rewrite_row(lines, expected_name, row)
        lines = append_rows(lines, expected_name, additions)

    text = patch_scalars("\n".join(lines))

    # Self-check: the written contract must agree with the database it was
    # measured on, in BOTH directions, bar the declared twin divergence.
    remaining = await measure(dsn, text.split("\n"))
    leftovers = {
        name: {
            side: [
                r
                for r in rows
                if (name, tuple(r[: KEY_WIDTH[name]])) not in TWIN_DIVERGENCE_BY_DESIGN
            ]
            for side, rows in sides.items()
        }
        for name, sides in remaining.items()
    }
    if any(rows for sides in leftovers.values() for rows in sides.values()):
        raise SystemExit(f"the written contract still does not describe the database : {leftovers}")

    dst.write_text(text, encoding="utf-8")
    print(f"\nwrote : {dst}", flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "--manifest":
        mint_manifest(
            Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v16.json"),
            Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v17.json"),
        )
        sys.exit(0)
    sys.exit(
        asyncio.run(
            main(
                sys.argv[1],
                Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v16.sql"),
                Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v17.sql"),
            )
        )
    )
