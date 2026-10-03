"""Mint the v18 DR contract as a declared delta of v17, for head 060.

060 (ADR #34) adds three tables (`focus_slots`, `focus_slot_anchors`,
`focus_slot_history`), three trigger functions and their three triggers, two
columns, two CHECKs, two foreign keys and two partial unique indexes on
`brain_sessions`, and rewrites `brain_sessions_terminal_state_valid` (16314b31).
The contract moves exactly what the plan declares (Task 2, "The declared
delta"); an observed row outside it stops the mint. `project_focus_history` and
`require_project_focus_history` stay byte-identical (Q3).

Derived from `mint_recovery_contract_v17.py` (pairs, declared additions,
bidirectional self-check) and `mint_recovery_contract_v12.py` (table rows and
inline trigger blocks, the last mint that added tables).

Usage:
    python scripts/mint_recovery_contract_v18.py <dsn> [src.sql] [dst.sql]
    python scripts/mint_recovery_contract_v18.py --manifest [src.json] [dst.json]

Measure the base asset against a database the alembic chain just built to head
060, and the `-pgrestore` twin against a real custom-format `pg_dump`/`pg_restore`
of a disposable 059 source migrated to 060 after the restore. Never regenerates.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import asyncpg

NEW_TABLES = ("focus_slot_anchors", "focus_slot_history", "focus_slots")
NEW_TRIGGER_FUNCTIONS = (
    "focus_slot_anchors_append_only",
    "focus_slot_history_append_only",
    "require_focus_slot_history",
)
#: name -> (contype, delete action as the block renders it)
NEW_SESSION_CONSTRAINTS = {
    "brain_sessions_relay_requires_slot": ("c", "NULL::text"),
    "brain_sessions_relayed_from_session_id_fkey": ("f", "'r'"),
    "brain_sessions_slot_id_fkey": ("f", "'r'"),
    "brain_sessions_slot_operator_only": ("c", "NULL::text"),
}
NEW_SESSION_INDEXES = ("uq_brain_sessions_open_slot", "uq_brain_sessions_relayed_from")
NEW_USER_TRIGGERS = {
    ("focus_slot_anchors", "focus_slot_anchors_append_only_trigger"),
    ("focus_slot_history", "focus_slot_history_append_only_trigger"),
    ("focus_slots", "focus_slots_history_required"),
}
TERMINAL = "brain_sessions_terminal_state_valid"
TERMINAL_FRAGMENT = "nature is not null"
CATALOG = {"foreign_keys": (53, 60), "indexes": (173, 183)}

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
REWRITABLE_ROWS = {
    ("expected_table_columns", ("brain_sessions",)),
    ("expected_table_constraints", ("brain_sessions", TERMINAL)),
}
TWIN_DIVERGENCE_BY_DESIGN = {
    ("expected_table_indexes", ("dream_promotions", "idx_dream_promotions_source_materialized")),
}


def allowed_addition(name: str, key: tuple) -> bool:
    """The rows 060 may ADD, and nothing else."""
    if name == "expected_trigger_functions":
        return key[0] in NEW_TRIGGER_FUNCTIONS
    if key[0] in NEW_TABLES:
        return True
    if name == "expected_table_constraints":
        return key == ("brain_sessions", key[1]) and key[1] in NEW_SESSION_CONSTRAINTS
    if name == "expected_table_indexes":
        return key == ("brain_sessions", key[1]) and key[1] in NEW_SESSION_INDEXES
    return False


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


def rewrite_row(lines: list[str], name: str, row: tuple) -> list[str]:
    head, close, _ = find_block(lines, name)
    width = KEY_WIDTH[name]
    key_lines = [f"         '{value}'," for value in row[:width]]
    matches = [i for i in range(head + 1, close) if lines[i : i + len(key_lines)] == key_lines]
    if len(matches) != 1:
        raise SystemExit(f"{name}: {len(matches)} row(s) keyed {row[:width]!r}, expected one")
    target = matches[0] + len(key_lines)
    if not re.fullmatch(r"         '[0-9a-f]{32}'", lines[target]):
        raise SystemExit(f"{name}: unexpected fingerprint line {lines[target]!r}")
    lines[target] = f"         '{row[width]}'"
    return lines


def rewrite_single_line(lines: list[str], name: str, pattern: str, new_md5: str) -> list[str]:
    """Replace the md5 of the one single-line row of `name` matching `pattern`."""
    head, close, _ = find_block(lines, name)
    hits = [i for i in range(head + 1, close) if re.fullmatch(pattern, lines[i])]
    if len(hits) != 1:
        raise SystemExit(f"{name}: {len(hits)} line(s) match {pattern!r}, expected one")
    lines[hits[0]] = re.sub(r"'[0-9a-f]{32}'", f"'{new_md5}'", lines[hits[0]])
    return lines


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


async def session_column_fingerprint(dsn: str, lines: list[str]) -> str:
    e_head, e_close, _ = find_block(lines, "expected_column_fingerprints")
    o_head, o_close, _ = find_block(lines, "observed_column_fingerprints")
    rows = await scalar_rows(
        dsn,
        "WITH expected_column_fingerprints(object_name, definition_md5) AS (\n"
        f"{body(lines, e_head, e_close)}\n),\n"
        "observed_column_fingerprints(object_name, definition_md5) AS (\n"
        f"{body(lines, o_head, o_close)}\n)\n"
        "SELECT definition_md5 FROM observed_column_fingerprints "
        "WHERE object_name = 'brain_sessions'",
    )
    if len(rows) != 1:
        raise SystemExit(f"brain_sessions column fingerprint: {rows}")
    return str(rows[0][0])


async def new_user_triggers(dsn: str) -> set[tuple[str, str]]:
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
            " 'contract_id', 'brain-v42/postgresql-recovery/v17',",
            " 'contract_id', 'brain-v42/postgresql-recovery/v18',",
        ),
        (" 'schema_version', 17", " 'schema_version', 18"),
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
    if document["contract_id"] != "brain-v42/postgresql-recovery/v17":
        raise SystemExit(f"not the v17 manifest: {document['contract_id']}")
    document["contract_id"] = "brain-v42/postgresql-recovery/v18"
    document["schema_version"] = 18
    counts = [check for check in document["checks"] if check["id"] == "catalog_counts"]
    tables = [check for check in document["checks"] if check["id"] == "table_set"]
    if len(counts) != 1 or len(tables) != 1:
        raise SystemExit("catalog_counts and table_set must each appear once")
    for key, (old, new) in CATALOG.items():
        if counts[0][key] != old:
            raise SystemExit(f"catalog_counts.{key} is {counts[0][key]}, expected {old}")
        counts[0][key] = new
    if set(NEW_TABLES) & set(tables[0]["tables"]):
        raise SystemExit("table_set already names a 060 table")
    tables[0]["tables"] = sorted([*tables[0]["tables"], *NEW_TABLES])
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote : {dst}", flush=True)


async def main(dsn: str, src: Path, dst: Path) -> int:
    lines = src.read_text(encoding="utf-8").split("\n")
    print(f"measuring {src.name} against the 060 database ...", flush=True)
    deltas = await measure(dsn, lines)

    split: dict[str, tuple[list, list]] = {}
    for expected_name, _ in PAIRS:
        split[expected_name] = split_delta(
            expected_name, deltas[expected_name]["missing"], deltas[expected_name]["stale"]
        )
        additions, rewrites = split[expected_name]
        print(f"  {expected_name}: {len(additions)} addition(s), {len(rewrites)} rewrite(s)")
        for row in rewrites:
            lines = rewrite_row(lines, expected_name, row)
        lines = append_rows(lines, expected_name, additions)

    constraint_adds, constraint_rewrites = split["expected_table_constraints"]
    session_constraints = {row[1]: row[2] for row in constraint_adds if row[0] == "brain_sessions"}
    if set(session_constraints) != set(NEW_SESSION_CONSTRAINTS):
        raise SystemExit(f"brain_sessions constraint additions: {sorted(session_constraints)}")
    lines = insert_plain(
        lines,
        "expected_session_constraints",
        [
            f"     ('{name}', '{NEW_SESSION_CONSTRAINTS[name][0]}', "
            f"{NEW_SESSION_CONSTRAINTS[name][1]}, '{md5}'),"
            for name, md5 in sorted(session_constraints.items())
        ],
    )
    terminal = [row for row in constraint_rewrites if row[:2] == ("brain_sessions", TERMINAL)]
    if len(terminal) != 1:
        raise SystemExit(f"{TERMINAL} rewrite: {terminal}")
    lines = rewrite_single_line(
        lines,
        "expected_session_constraints",
        rf"     \('{TERMINAL}', 'c', NULL::text, '[0-9a-f]{{32}}'\),?",
        terminal[0][2],
    )
    lines = insert_plain(
        lines,
        "expected_session_constraint_fragments",
        [f"     ('{TERMINAL}', '{TERMINAL_FRAGMENT}'),"],
    )

    index_adds, _ = split["expected_table_indexes"]
    session_indexes = {row[1]: row[2] for row in index_adds if row[0] == "brain_sessions"}
    if set(session_indexes) != set(NEW_SESSION_INDEXES):
        raise SystemExit(f"brain_sessions index additions: {sorted(session_indexes)}")
    lines = insert_plain(
        lines,
        "expected_session_indexes",
        [f"     ('{name}', '{md5}')," for name, md5 in sorted(session_indexes.items())],
    )

    lines = rewrite_single_line(
        lines,
        "expected_column_fingerprints",
        r"     \('brain_sessions', '[0-9a-f]{32}'\),?",
        await session_column_fingerprint(dsn, lines),
    )

    triggers = await new_user_triggers(dsn)
    if triggers != NEW_USER_TRIGGERS:
        raise SystemExit(f"060 user triggers measured {sorted(triggers)}")
    lines = insert_plain(
        lines,
        "expected_runtime_user_triggers",
        [f"     ('{table}', '{trigger}')," for table, trigger in sorted(triggers)],
    )
    lines = insert_plain(
        lines, "expected_runtime_trigger_tables", [f"     ('{t}')," for t in NEW_TABLES]
    )
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
            Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v17.json"),
            Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v18.json"),
        )
        sys.exit(0)
    sys.exit(
        asyncio.run(
            main(
                sys.argv[1],
                Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v17.sql"),
                Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v18.sql"),
            )
        )
    )
