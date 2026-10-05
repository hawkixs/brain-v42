"""Mint the v19 DR contract as a declared delta of v18, for head 061.

061 adds one table, `brain_session_connections` (the connections an operator
session's lifecycle calls were seen on): a composite primary key, a foreign key
to `brain_sessions` with ON DELETE CASCADE and one non-blank CHECK. It touches no
other table, adds no trigger and no function. The contract moves exactly that
footprint; an observed row outside it stops the mint.

Derived from `mint_recovery_contract_v18.py` (pairs, declared additions,
bidirectional self-check), which was itself derived from the v17 and v12 mints.

Usage:
    python scripts/mint_recovery_contract_v19.py <dsn> [src.sql] [dst.sql]
    python scripts/mint_recovery_contract_v19.py --manifest [src.json] [dst.json]

Measure the base asset against a database the alembic chain just built to head
061, and the `-pgrestore` twin against a real custom-format `pg_dump`/`pg_restore`
of a disposable 060 source migrated to 061 after the restore. Never regenerates.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import asyncpg

NEW_TABLES = ("brain_session_connections",)
CATALOG = {"foreign_keys": (60, 61), "indexes": (183, 184)}

PAIRS = [
    ("expected_table_columns", "observed_table_columns"),
    ("expected_table_constraints", "observed_table_constraints"),
    ("expected_table_indexes", "observed_table_indexes"),
    ("expected_trigger_functions", "observed_trigger_functions"),
]
KEY_WIDTH = {
    "expected_table_columns": 1,
    "expected_table_constraints": 2,
    "expected_table_indexes": 2,
    "expected_trigger_functions": 1,
}
REWRITABLE_ROWS: set[tuple[str, tuple]] = set()
TWIN_DIVERGENCE_BY_DESIGN = {
    ("expected_table_indexes", ("dream_promotions", "idx_dream_promotions_source_materialized")),
}


def allowed_addition(name: str, key: tuple) -> bool:
    """The rows 061 may ADD, and nothing else: every row of its one new table."""
    return name != "expected_trigger_functions" and key[0] in NEW_TABLES


def find_block(lines: list[str], name: str) -> tuple[int, int, str]:
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
    return "\n".join(lines[header + 1 : close])


def _difference_query(lines: list[str], expected_name: str, observed_name: str) -> str:
    e_head, e_close, spec = find_block(lines, expected_name)
    o_head, o_close, o_spec = find_block(lines, observed_name)
    observed_header = f"observed({o_spec})" if o_spec else "observed"
    return (
        f"WITH {expected_name}({spec}) AS (\n{body(lines, e_head, e_close)}\n),\n"
        f"{observed_header} AS (\n{body(lines, o_head, o_close)}\n),\n"
        f"expected AS (SELECT * FROM {expected_name})\n"
        f"(SELECT 'missing' AS side, {spec} FROM observed "
        f"EXCEPT SELECT 'missing', {spec} FROM expected)\n"
        f"UNION ALL\n"
        f"(SELECT 'stale' AS side, {spec} FROM expected "
        f"EXCEPT SELECT 'stale', {spec} FROM observed)\n"
        f"ORDER BY 1, 2, 3"
    )


async def measure(dsn: str, lines: list[str]) -> dict[str, dict[str, list[tuple]]]:
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
    if expected_name == "expected_trigger_functions":
        name, digest, octets = row
        return [f"     ('{name}', '{digest}', {octets}),"]
    parts = ",\n".join(f"         '{value}'" for value in row)
    return ["     ("] + parts.split("\n") + ["     ),"]


def insert_plain(lines: list[str], name: str, rendered: list[str]) -> list[str]:
    """Append pre-rendered rows at the end of a VALUES block, fixing the comma."""
    if not rendered:
        return lines
    _, close, _ = find_block(lines, name)
    last = close - 1
    stripped = lines[last].rstrip()
    if stripped.endswith(")") and not stripped.endswith("),"):
        lines[last] = lines[last] + ","
    rendered = list(rendered)
    rendered[-1] = rendered[-1].rstrip(",")
    return lines[:close] + rendered + lines[close:]


def append_rows(lines: list[str], name: str, rows: list[tuple]) -> list[str]:
    block: list[str] = []
    for row in rows:
        block.extend(render(name, row))
    return insert_plain(lines, name, block)


def split_delta(name: str, missing: list[tuple], stale: list[tuple]) -> tuple[list, list]:
    width = KEY_WIDTH[name]
    stale_keys = {tuple(r[:width]) for r in stale}
    additions: list[tuple] = []
    rewrites: list[tuple] = []
    refused: list[tuple] = []
    for row in missing:
        key = tuple(row[:width])
        if (name, key) in TWIN_DIVERGENCE_BY_DESIGN:
            continue
        if key in stale_keys:
            (rewrites if (name, key) in REWRITABLE_ROWS else refused).append(row)
        elif allowed_addition(name, key):
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


async def scalar_rows(dsn: str, query: str) -> list[tuple]:
    connection = await asyncpg.connect(dsn)
    try:
        return [tuple(r) for r in await connection.fetch(query)]
    finally:
        await connection.close()


async def new_table_triggers(dsn: str) -> set[tuple[str, str]]:
    names = ", ".join(f"'{t}'" for t in NEW_TABLES)
    rows = await scalar_rows(
        dsn,
        "SELECT c.relname, t.tgname FROM pg_catalog.pg_trigger t "
        "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = 'public' AND NOT t.tgisinternal AND c.relname IN ({names})",
    )
    return {(str(table), str(trigger)) for table, trigger in rows}


async def observed_catalog_counts(dsn: str, lines: list[str]) -> dict[str, int]:
    head, close, _ = find_block(lines, "catalog_counts")
    rows = await scalar_rows(
        dsn,
        f"WITH catalog_counts AS (\n{body(lines, head, close)}\n)\n"
        "SELECT (observed->>'foreign_keys')::int, (observed->>'indexes')::int "
        "FROM catalog_counts",
    )
    return {"foreign_keys": int(rows[0][0]), "indexes": int(rows[0][1])}


def patch_scalars(text: str) -> str:
    replacements = [
        (
            " 'contract_id', 'brain-v42/postgresql-recovery/v18',",
            " 'contract_id', 'brain-v42/postgresql-recovery/v19',",
        ),
        (" 'schema_version', 18", " 'schema_version', 19"),
        *(
            (f"         '{key}', {old},", f"         '{key}', {new},")
            for key, (old, new) in CATALOG.items()
        ),
    ]
    for before, after in replacements:
        if text.count(before) != 1:
            raise SystemExit(f"anchor not unique ({text.count(before)}x): {before!r}")
        text = text.replace(before, after)
    return text


def mint_manifest(src: Path, dst: Path) -> None:
    document = json.loads(src.read_text(encoding="utf-8"))
    if document["contract_id"] != "brain-v42/postgresql-recovery/v18":
        raise SystemExit(f"not the v18 manifest: {document['contract_id']}")
    document["contract_id"] = "brain-v42/postgresql-recovery/v19"
    document["schema_version"] = 19
    counts = [check for check in document["checks"] if check["id"] == "catalog_counts"]
    tables = [check for check in document["checks"] if check["id"] == "table_set"]
    if len(counts) != 1 or len(tables) != 1:
        raise SystemExit("catalog_counts and table_set must each appear once")
    for key, (old, new) in CATALOG.items():
        if counts[0][key] != old:
            raise SystemExit(f"catalog_counts.{key} is {counts[0][key]}, expected {old}")
        counts[0][key] = new
    if set(NEW_TABLES) & set(tables[0]["tables"]):
        raise SystemExit("table_set already names a 061 table")
    tables[0]["tables"] = sorted([*tables[0]["tables"], *NEW_TABLES])
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote : {dst}", flush=True)


async def main(dsn: str, src: Path, dst: Path) -> int:
    lines = src.read_text(encoding="utf-8").split("\n")
    print(f"measuring {src.name} against the 061 database ...", flush=True)
    deltas = await measure(dsn, lines)

    for expected_name, _ in PAIRS:
        additions, rewrites = split_delta(
            expected_name, deltas[expected_name]["missing"], deltas[expected_name]["stale"]
        )
        print(f"  {expected_name}: {len(additions)} addition(s), {len(rewrites)} rewrite(s)")
        if rewrites:
            raise SystemExit(f"{expected_name}: 061 rewrites nothing, observed {rewrites[:5]}")
        lines = append_rows(lines, expected_name, additions)

    # 061 creates no trigger and no function: a trigger on the new table would be
    # a row this mint has no block to declare, so it stops rather than guesses.
    triggers = await new_table_triggers(dsn)
    if triggers:
        raise SystemExit(f"061 declares no trigger, measured {sorted(triggers)}")
    lines = insert_plain(lines, "expected_tables", [f"     ('{t}')," for t in NEW_TABLES])

    measured = await observed_catalog_counts(dsn, lines)
    declared = {key: new for key, (_, new) in CATALOG.items()}
    if measured != declared:
        raise SystemExit(f"catalog counts measured {measured}, declared {declared}")

    text = patch_scalars("\n".join(lines))

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
        raise SystemExit(f"the written contract still does not describe the database: {leftovers}")

    dst.write_text(text, encoding="utf-8")
    print(f"\nwrote : {dst}", flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "--manifest":
        mint_manifest(
            Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v18.json"),
            Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v19.json"),
        )
        sys.exit(0)
    sys.exit(
        asyncio.run(
            main(
                sys.argv[1],
                Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v18.sql"),
                Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v19.sql"),
            )
        )
    )
