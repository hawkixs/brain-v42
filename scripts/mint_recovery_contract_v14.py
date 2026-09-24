"""Mint the v14 DR contract as a surgical delta of v13, for head 057.

Derived from `mint_recovery_contract_v13.py`. 057 adds no table, no index, no
CHECK and no trigger: it is a single nullable column, `search_log.embedding_model`
(TEXT, no default, no backfill). `expected_table_columns` carries ONE md5 per
table over its whole column list, so that one new column REWRITES the
`search_log` row rather than appending one -- the same shape 056 forced onto
`project_contexts` in v13, narrower still: 057 touches no other object at all.

Usage:
    python scripts/mint_recovery_contract_v14.py <dsn> [src.sql] [dst.sql]
    python scripts/mint_recovery_contract_v14.py --manifest [src.json] [dst.json]

Measure the base asset against a database the alembic chain just built to head
057, and the `-pgrestore` twin against a real custom-format `pg_dump`/`pg_restore`
of that same database: the twin is the contract for a RESTORED target, so that is
where its rows are true. After writing, the mint re-measures its own output in
BOTH directions and refuses to report success while any row still disagrees --
`observed EXCEPT expected` alone cannot see a stale row left behind.

Never regenerates. The additivity test diffs the two assets LINE BY LINE and
allows only an explicit list of removed lines, so a reformat would fail even
if every value were right.

Unlike v13, this mint corrects no view row: 057 adds no view and touches no
existing one, so `search_log`'s new column is the only source of divergence
between the chain-built base and a real restore of it -- confirmed by measuring
both variants against the same 057 database before writing anything.
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

#: The rows 057 is allowed to REWRITE rather than append. One table-wide column
#: fingerprint, because one column was added to that table. Anything else
#: that already exists and measures differently is a modification nobody
#: declared, and the mint stops on it.
REWRITABLE_ROWS = {
    ("expected_table_columns", ("search_log",)),
}

#: Divergences the `-pgrestore` twin carries BY DESIGN, kept from v13: when the
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
    """Replace the fingerprint of an existing multi-line row, in place.

    Only for `expected_table_columns`-shaped rows: the key line, then the
    fingerprint line. Exactly one key line must exist in the block -- zero means
    the row is not a rewrite, two mean the block is already corrupt.
    """
    head, close, _ = find_block(lines, name)
    key, fingerprint = row
    matches = [i for i in range(head + 1, close) if lines[i] == f"         '{key}',"]
    if len(matches) != 1:
        raise SystemExit(f"{name}: {len(matches)} row(s) keyed {key!r}, expected exactly one")
    target = matches[0] + 1
    old = lines[target]
    if not re.fullmatch(r"         '[0-9a-f]{32}'", old):
        raise SystemExit(f"{name}: unexpected fingerprint line for {key!r}: {old!r}")
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
            print(f"  {name}: divergence par conception conservée {key}", flush=True)
            continue
        if key in stale_keys:
            (rewrites if (name, key) in REWRITABLE_ROWS else refused).append(row)
        else:
            additions.append(row)
    rewritten = {tuple(r[:width]) for r in rewrites}
    orphans = [
        r
        for r in stale
        if tuple(r[:width]) not in rewritten
        and (name, tuple(r[:width])) not in TWIN_DIVERGENCE_BY_DESIGN
    ]
    if refused or orphans:
        raise SystemExit(
            f"{name}: modification(s) non déclarée(s) {refused[:5]} ; "
            f"ligne(s) que plus rien n'observe {orphans[:5]}"
        )
    return additions, rewrites


def patch_scalars(text: str) -> str:
    """The values that CHANGE rather than get added, anchored one by one.

    057 adds no index, no constraint and no table: only the identity lines move.
    """
    replacements = [
        (
            " 'contract_id', 'brain-v42/postgresql-recovery/v13',",
            " 'contract_id', 'brain-v42/postgresql-recovery/v14',",
        ),
        (" 'schema_version', 13", " 'schema_version', 14"),
    ]
    for before, after in replacements:
        if text.count(before) != 1:
            raise SystemExit(f"anchor not unique ({text.count(before)}x): {before!r}")
        text = text.replace(before, after)
    return text


def mint_manifest(src: Path, dst: Path) -> None:
    """The JSON contract moves the two identity values, nothing else.

    Serialised the way every manifest of this directory is: sorted keys,
    compact separators, one trailing newline. 057 adds no table and no index,
    so `catalog_counts` and `table_set` stay v13's.
    """
    document = json.loads(src.read_text(encoding="utf-8"))
    if document["contract_id"] != "brain-v42/postgresql-recovery/v13":
        raise SystemExit(f"not the v13 manifest: {document['contract_id']}")
    document["contract_id"] = "brain-v42/postgresql-recovery/v14"
    document["schema_version"] = 14
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"écrit : {dst}", flush=True)


async def main(dsn: str, src: Path, dst: Path) -> int:
    lines = src.read_text(encoding="utf-8").split("\n")
    print(f"mesure de {src.name} contre la base 057 …", flush=True)
    deltas = await measure(dsn, lines)

    for expected_name, _ in PAIRS:
        additions, rewrites = split_delta(
            expected_name, deltas[expected_name]["missing"], deltas[expected_name]["stale"]
        )
        print(
            f"  {expected_name}: {len(additions)} ajout(s), {len(rewrites)} réécriture(s)",
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
        raise SystemExit(f"le contrat écrit ne décrit toujours pas la base : {leftovers}")

    dst.write_text(text, encoding="utf-8")
    print(f"\nécrit : {dst}", flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "--manifest":
        mint_manifest(
            Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v13.json"),
            Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v14.json"),
        )
        sys.exit(0)
    sys.exit(
        asyncio.run(
            main(
                sys.argv[1],
                Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v13.sql"),
                Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v14.sql"),
            )
        )
    )
