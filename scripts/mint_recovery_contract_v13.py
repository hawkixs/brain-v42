"""Mint the v13 DR contract as a surgical delta of v12, for head 056.

Derived from `mint_recovery_contract_v12.py`, whose docstring named the three
edits a v13 would need: the scalar anchors, the new tables, and a fresh look at
`TWIN_DIVERGENCE_BY_DESIGN`. 056 needed a fourth, and it is the reason this is
a new file rather than a flag on the old one: the v12 mint can only APPEND.

056 adds no table. It adds two nullable columns, one CHECK and one partial
index to `project_contexts`. The CHECK and the index are new rows. The columns
are not: `expected_table_columns` carries ONE md5 per table over its whole
column list, so they REWRITE the `project_contexts` row. The v12 key-collision
guard rightly refuses that as a modification. `REWRITABLE_ROWS` names the one
rewrite 056 is allowed; every other collision still refuses.

Usage:
    python scripts/mint_recovery_contract_v13.py <dsn> [src.sql] [dst.sql]
    python scripts/mint_recovery_contract_v13.py --manifest [src.json] [dst.json]

Measure the base asset against a database the alembic chain just built, and
the `-pgrestore` twin against a real custom-format `pg_dump`/`pg_restore` of
that database: the twin is the contract for a RESTORED target, so that is
where its rows are true. After writing, the mint re-measures its own output in
BOTH directions and refuses to report success while any row still disagrees --
`observed EXCEPT expected` alone cannot see a stale row left behind.

Never regenerates. The additivity test diffs the two assets LINE BY LINE and
allows only an explicit list of removed lines, so a reformat would fail even
if every value were right.

The v12 mint's inline blocks (views, trigger bindings) and its sequence blocks
are not re-measured here: 056 creates no view, trigger, function or sequence.
The fresh-head yardstick replays the WHOLE asset, but against the CHAIN, so it
cannot see a twin row that is right for the chain and wrong for a restore. That
is exactly what shipped: the twin inherited `knowledge_claim_current` (055) in
its chain form from v12, and the first real restore of production failed
`view_definition_mismatches` (2026-09-23, ticket 1ec33903). The view's
`varchar IN (...)` filter does not deparse idempotently through
`pg_dump`/`pg_restore`. `TWIN_VIEW_ROWS` carries the form measured on that real
restore, and the mint writes it into the twin, so a re-mint cannot regress it.
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

#: The rows 056 is allowed to REWRITE rather than append. One table-wide column
#: fingerprint, because two columns were added to that table. Anything else
#: that already exists and measures differently is a modification nobody
#: declared, and the mint stops on it.
REWRITABLE_ROWS = {
    ("expected_table_columns", ("project_contexts",)),
}

#: Divergences the `-pgrestore` twin carries BY DESIGN, kept from v12: when the
#: twin is measured against a chain-built database instead of a real restore,
#: pg_restore's re-serialised form of this index shows up as a modification.
#: Measured against a real restore, it does not show up at all.
TWIN_DIVERGENCE_BY_DESIGN = {
    ("expected_table_indexes", ("dream_promotions", "idx_dream_promotions_source_materialized")),
}


#: View rows the `-pgrestore` twin carries in the form a REAL restore renders,
#: measured on 2026-09-23 on a restore of the production pre056 dump (ticket
#: 1ec33903). The base asset keeps the chain form, which production renders.
TWIN_VIEW_ROWS = {
    "knowledge_claim_current": (
        "30fdf69fd0413f03c00ae3ca2ca4e756",
        "6565fd7e2fe5e52a9c5175651964f34e",
    ),
}


def correct_twin_views(text: str) -> str:
    """Swap each declared view row from its chain form to its restored form."""
    for view, (chain_md5, restored_md5) in TWIN_VIEW_ROWS.items():
        chain_row = f"('{view}', FALSE, '{chain_md5}')"
        if text.count(chain_row) != 1:
            raise SystemExit(f"la ligne de vue {view} n'est pas sous sa forme de chaîne attendue")
        text = text.replace(chain_row, f"('{view}', FALSE, '{restored_md5}')")
    return text


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

    056 adds an index and no foreign key. A global replacement of `12` or of a
    revision number is how `v4.sql` nearly lost five invariants that merely
    NAMED the migration that installed them.
    """
    replacements = [
        ("         'indexes', 171,", "         'indexes', 172,"),
        (
            " 'contract_id', 'brain-v42/postgresql-recovery/v12',",
            " 'contract_id', 'brain-v42/postgresql-recovery/v13',",
        ),
        (" 'schema_version', 12", " 'schema_version', 13"),
    ]
    for before, after in replacements:
        if text.count(before) != 1:
            raise SystemExit(f"anchor not unique ({text.count(before)}x): {before!r}")
        text = text.replace(before, after)
    return text


def mint_manifest(src: Path, dst: Path) -> None:
    """The JSON contract moves the same three values as the SQL, nothing else.

    Serialised the way every manifest of this directory is: sorted keys,
    compact separators, one trailing newline.
    """
    document = json.loads(src.read_text(encoding="utf-8"))
    counts = [check for check in document["checks"] if check["id"] == "catalog_counts"]
    if len(counts) != 1 or counts[0]["indexes"] != 171:
        raise SystemExit(f"catalog_counts is not v12's: {counts}")
    counts[0]["indexes"] = 172
    if document["contract_id"] != "brain-v42/postgresql-recovery/v12":
        raise SystemExit(f"not the v12 manifest: {document['contract_id']}")
    document["contract_id"] = "brain-v42/postgresql-recovery/v13"
    document["schema_version"] = 13
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"écrit : {dst}", flush=True)


async def main(dsn: str, src: Path, dst: Path) -> int:
    lines = src.read_text(encoding="utf-8").split("\n")
    print(f"mesure de {src.name} contre la base 056 …", flush=True)
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
    if dst.name.endswith("-pgrestore.sql"):
        text = correct_twin_views(text)

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
            Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v12.json"),
            Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v13.json"),
        )
        sys.exit(0)
    sys.exit(
        asyncio.run(
            main(
                sys.argv[1],
                Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v12.sql"),
                Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v13.sql"),
            )
        )
    )
