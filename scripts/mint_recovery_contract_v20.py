"""Mint the v20 DR contract as a declared delta of v19, for head 062.

062 adds no table. It adds two partial indexes on `delivery_confirmations`
(`idx_delivery_confirmations_binding_errors` and `..._context_errors`) and creates
the `pg_stat_statements` extension in schema `monitoring`, so nothing lands in
`public`. It adds no trigger, no function and no grant in `public`: ACL v11 stays
the access contract. The contract moves exactly that footprint: two index rows, the
index count (184 -> 186) and the database-wide extension inventory, which gains
`pg_stat_statements` at the version READ FROM THE DATABASE, never typed in here. An
observed row outside that footprint stops the mint.

Derived from `mint_recovery_contract_v19.py` (pairs, declared additions,
bidirectional self-check), which was itself derived from the v18, v17 and v12 mints.

Usage:
    python scripts/mint_recovery_contract_v20.py <dsn> [src.sql] [dst.sql]
    python scripts/mint_recovery_contract_v20.py --manifest [src.json] [dst.json]

Measure the base asset against a database the alembic chain just built to head
062, and the `-pgrestore` twin against a real custom-format `pg_dump`/`pg_restore`
of a disposable 061 source migrated to 062 after the restore. The mint refuses a
database whose name is `brain` or `brain_test`. Never regenerates.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import asyncpg

ALLOWED_INDEX_ROWS = frozenset(
    {
        ("delivery_confirmations", "idx_delivery_confirmations_binding_errors"),
        ("delivery_confirmations", "idx_delivery_confirmations_context_errors"),
    }
)
CATALOG = {"indexes": (184, 186)}
EXTENSION_NAMES = ["pg_stat_statements", "plpgsql", "vector"]
OLD_INVENTORY = "plpgsql 1.0, vector 0.8.2"
OLD_NAMES = '["plpgsql", "vector"]'
NEW_NAMES = '["pg_stat_statements", "plpgsql", "vector"]'
PROTECTED_DATABASES = ("brain", "brain_test")

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
    """The rows 062 may ADD, and nothing else: its two partial-index fingerprints."""
    return name == "expected_table_indexes" and tuple(key) in ALLOWED_INDEX_ROWS


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


async def confirmation_triggers(dsn: str) -> set[tuple[str, str]]:
    """Triggers on the one table 062 touches: `delivery_confirmations` carries none."""
    rows = await scalar_rows(
        dsn,
        "SELECT c.relname, t.tgname FROM pg_catalog.pg_trigger t "
        "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT t.tgisinternal "
        "AND c.relname = 'delivery_confirmations'",
    )
    return {(str(table), str(trigger)) for table, trigger in rows}


async def observed_extensions(dsn: str) -> list[tuple[str, str, str]]:
    """(name, version, schema) of every installed extension, ordered by name."""
    rows = await scalar_rows(
        dsn,
        "SELECT e.extname, e.extversion, n.nspname FROM pg_catalog.pg_extension e "
        "JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace ORDER BY e.extname",
    )
    return [(str(a), str(b), str(c)) for a, b, c in rows]


async def observed_catalog_counts(dsn: str, lines: list[str]) -> dict[str, int]:
    head, close, _ = find_block(lines, "catalog_counts")
    rows = await scalar_rows(
        dsn,
        f"WITH catalog_counts AS (\n{body(lines, head, close)}\n)\n"
        "SELECT (observed->>'indexes')::int FROM catalog_counts",
    )
    return {"indexes": int(rows[0][0])}


def patch_extensions(text: str, version: str) -> str:
    """Move the extension inventory; the anchor counts are part of the contract.

    The base asset pins the inventory string twice (the expected JSON and the
    comparison); the `-pgrestore` twin pins the names list twice and the origin
    inventory once, and never judges versions.
    """
    inventory = f"pg_stat_statements {version}, plpgsql 1.0, vector 0.8.2"
    twin = "'origin_inventory'" in text
    replacements = (
        [
            (f"'{OLD_NAMES}'::jsonb", f"'{NEW_NAMES}'::jsonb", 2),
            (
                f"'origin_inventory', '{OLD_INVENTORY}'",
                f"'origin_inventory', '{inventory}'",
                1,
            ),
        ]
        if twin
        else [(f"'{OLD_INVENTORY}'", f"'{inventory}'", 2)]
    )
    for before, after, count in replacements:
        if text.count(before) != count:
            raise SystemExit(f"anchor expected {count}x, found {text.count(before)}x: {before!r}")
        text = text.replace(before, after)
    return text


def patch_scalars(text: str) -> str:
    replacements = [
        (
            " 'contract_id', 'brain-v42/postgresql-recovery/v19',",
            " 'contract_id', 'brain-v42/postgresql-recovery/v20',",
        ),
        (" 'schema_version', 19", " 'schema_version', 20"),
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
    """The inventory comes from the minted base asset, which read it from the database."""
    base = dst.with_suffix(".sql")
    found = re.search(
        r"'pg_stat_statements (\d+\.\d+), plpgsql 1\.0, vector 0\.8\.2'", base.read_text()
    )
    if found is None:
        raise SystemExit(f"{base} carries no pg_stat_statements inventory: mint it first")
    document = json.loads(src.read_text(encoding="utf-8"))
    if document["contract_id"] != "brain-v42/postgresql-recovery/v19":
        raise SystemExit(f"not the v19 manifest: {document['contract_id']}")
    document["contract_id"] = "brain-v42/postgresql-recovery/v20"
    document["schema_version"] = 20
    counts = [check for check in document["checks"] if check["id"] == "catalog_counts"]
    extensions = [check for check in document["checks"] if check["id"] == "extension_versions"]
    if len(counts) != 1 or len(extensions) != 1:
        raise SystemExit("catalog_counts and extension_versions must each appear once")
    for key, (old, new) in CATALOG.items():
        if counts[0][key] != old:
            raise SystemExit(f"catalog_counts.{key} is {counts[0][key]}, expected {old}")
        counts[0][key] = new
    if extensions[0]["inventory"] != OLD_INVENTORY:
        raise SystemExit(f"extension inventory is {extensions[0]['inventory']!r}")
    extensions[0]["inventory"] = f"pg_stat_statements {found.group(1)}, plpgsql 1.0, vector 0.8.2"
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote : {dst}", flush=True)


async def main(dsn: str, src: Path, dst: Path) -> int:
    database = dsn.rsplit("/", 1)[-1].split("?")[0]
    if database in PROTECTED_DATABASES:
        raise SystemExit(f"refusing to mint against the shared database {database!r}")
    lines = src.read_text(encoding="utf-8").split("\n")
    print(f"measuring {src.name} against the 062 database ...", flush=True)
    deltas = await measure(dsn, lines)

    for expected_name, _ in PAIRS:
        additions, rewrites = split_delta(
            expected_name, deltas[expected_name]["missing"], deltas[expected_name]["stale"]
        )
        print(f"  {expected_name}: {len(additions)} addition(s), {len(rewrites)} rewrite(s)")
        if rewrites:
            raise SystemExit(f"{expected_name}: 062 rewrites nothing, observed {rewrites[:5]}")
        lines = append_rows(lines, expected_name, additions)

    # 062 creates no trigger and no function: a trigger on the table it indexes would
    # be a row this mint has no block to declare, so it stops rather than guesses.
    triggers = await confirmation_triggers(dsn)
    if triggers:
        raise SystemExit(f"062 declares no trigger, measured {sorted(triggers)}")

    extensions = await observed_extensions(dsn)
    if [name for name, _, _ in extensions] != EXTENSION_NAMES:
        raise SystemExit(f"extension inventory measured {extensions}, declared {EXTENSION_NAMES}")
    by_name = {name: (version, schema) for name, version, schema in extensions}
    if by_name["pg_stat_statements"][1] != "monitoring" or by_name["plpgsql"][0] != "1.0":
        raise SystemExit(f"extension placement or version unexpected: {extensions}")

    measured = await observed_catalog_counts(dsn, lines)
    declared = {key: new for key, (_, new) in CATALOG.items()}
    if measured != declared:
        raise SystemExit(f"catalog counts measured {measured}, declared {declared}")

    text = patch_extensions(patch_scalars("\n".join(lines)), by_name["pg_stat_statements"][0])

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
            Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v19.json"),
            Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v20.json"),
        )
        sys.exit(0)
    sys.exit(
        asyncio.run(
            main(
                sys.argv[1],
                Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v19.sql"),
                Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v20.sql"),
            )
        )
    )
