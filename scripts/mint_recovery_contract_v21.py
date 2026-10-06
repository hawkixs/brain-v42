"""Mint the v21 DR contract as a declared delta of v20, for head 063.

063 (client credentials and admin elevations) adds four tables
(`brain_client_credentials`, `brain_admin_elevations`, `brain_schema_compat` and
`brain_credential_audit`, whose `bigserial` brings one sequence), four trigger
functions and the five triggers that use them, a nullable column and a format CHECK on
each of `brain_sessions` (`opener_client_id`) and `brain_session_connections`
(`client_id`), and one foreign key. The contract moves exactly what the migration
declares; an observed row outside it stops the mint. The extension inventory is v20's,
unchanged: 063 installs no extension.

Derived from `mint_recovery_contract_v20.py` (pairs, bidirectional self-check, DSN
guard) and `mint_recovery_contract_v18.py` (new tables, trigger functions, user
triggers, the `brain_sessions` fingerprints), the last mint that did the same kind of
work.

Usage:
    python scripts/mint_recovery_contract_v21.py <dsn> [src.sql] [dst.sql]
    python scripts/mint_recovery_contract_v21.py --manifest [src.json] [dst.json]

Measure the base asset against a database the alembic chain just built to head
063, and the `-pgrestore` twin against a real custom-format `pg_dump`/`pg_restore`
of a disposable 062 source migrated to 063 after the restore. The mint refuses any DSN
that is not a disposable `brain_<tag>` database, then re-checks `current_database()`. Never regenerates.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg

NEW_TABLES = (
    "brain_admin_elevations",
    "brain_client_credentials",
    "brain_credential_audit",
    "brain_schema_compat",
)
NEW_TRIGGER_FUNCTIONS = (
    "brain_client_credentials_notify",
    "brain_credential_audit_notify",
    "brain_session_connections_owner_check",
    "brain_sessions_opener_immutable",
)
#: (table, constraint) rows 063 adds to a table that already existed.
NEW_ROWS_ON_OLD_TABLES = {
    ("brain_session_connections", "brain_session_connections_client_id_format"),
    ("brain_sessions", "brain_sessions_opener_client_id_format"),
}
OPENER_FORMAT = "brain_sessions_opener_client_id_format"
#: Tables 063 adds a column to: their column fingerprint is rewritten, never added.
ALTERED_TABLES = ("brain_session_connections", "brain_sessions")
NEW_USER_TRIGGERS = {
    ("brain_client_credentials", "brain_client_credentials_notify"),
    ("brain_client_credentials", "brain_client_credentials_notify_update"),
    ("brain_credential_audit", "brain_credential_audit_notify"),
    ("brain_session_connections", "brain_session_connections_owner_check"),
    ("brain_sessions", "brain_sessions_opener_immutable"),
}
#: Tables whose user triggers the contract judges, beyond those v20 already names.
NEW_TRIGGER_TABLES = (*NEW_TABLES, "brain_session_connections")
NEW_SEQUENCE = (
    "brain_credential_audit_id_seq",
    "brain_credential_audit",
    "id",
    "bigint",
    1,
    1,
    9223372036854775807,
    1,
    False,
)
CATALOG = {"foreign_keys": (61, 62), "indexes": (186, 195)}
# Only a database this mint (or the tests) created: generated names are `brain_<tag>_...`
# and the two shared databases never match the second half of the rule.
DISPOSABLE_DATABASE = re.compile(r"brain_[a-z0-9_]+")
PROTECTED_DATABASES = ("brain", "brain_test")


def assert_disposable_dsn(dsn: str) -> str:
    """Refuse any DSN that is not exactly a disposable database, parsed like asyncpg does.

    A naive `rsplit("/")` misreads `.../brain?application_name=x/y` (it inspects `y`
    while the driver connects to `brain`) and cannot see `/%62rain`. The path is
    parsed and percent-decoded, and any query parameter is refused outright: a
    `dbname=` or `host=` override would redirect the connection after the check.
    """
    parts = urlsplit(dsn)
    if parts.query:
        raise SystemExit("refusing a DSN with query parameters: it could override the database")
    name = unquote(parts.path)
    if not name.startswith("/") or "/" in name[1:]:
        raise SystemExit(f"refusing a DSN whose path is not a single database name: {name!r}")
    database = name[1:]
    if database in PROTECTED_DATABASES or not DISPOSABLE_DATABASE.fullmatch(database):
        raise SystemExit(f"refusing to mint against {database!r}: not a disposable database")
    return database


async def connect(dsn: str) -> asyncpg.Connection:
    """Connect, then verify what the server says we are connected to, before any use."""
    assert_disposable_dsn(dsn)
    connection = await asyncpg.connect(dsn)
    try:
        current = await connection.fetchval("SELECT current_database()")
        if current in PROTECTED_DATABASES or not DISPOSABLE_DATABASE.fullmatch(str(current)):
            raise SystemExit(f"connected to {current!r}, which is not a disposable database")
    except BaseException:
        await connection.close()
        raise
    return connection


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
REWRITABLE_ROWS = {("expected_table_columns", (table,)) for table in ALTERED_TABLES}
TWIN_DIVERGENCE_BY_DESIGN = {
    ("expected_table_indexes", ("dream_promotions", "idx_dream_promotions_source_materialized")),
}


def allowed_addition(name: str, key: tuple) -> bool:
    """The rows 063 may ADD, and nothing else."""
    if name == "expected_trigger_functions":
        return key[0] in NEW_TRIGGER_FUNCTIONS
    if key[0] in NEW_TABLES:
        return True
    return name == "expected_table_constraints" and tuple(key) in NEW_ROWS_ON_OLD_TABLES


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
    connection = await connect(dsn)
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


async def scalar_rows(dsn: str, query: str) -> list[tuple]:
    connection = await connect(dsn)
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


def declared_user_triggers(lines: list[str]) -> set[tuple[str, str]]:
    head, close, _ = find_block(lines, "expected_runtime_user_triggers")
    return set(re.findall(r"\('(\w+)', '(\w+)'\)", body(lines, head, close)))


async def new_user_triggers(dsn: str, lines: list[str]) -> set[tuple[str, str]]:
    """Every user trigger on a table 063 touches that the source contract does not name."""
    touched = ", ".join(f"'{table}'" for table in (*NEW_TABLES, *ALTERED_TABLES))
    rows = await scalar_rows(
        dsn,
        "SELECT c.relname, t.tgname FROM pg_catalog.pg_trigger t "
        "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = 'public' AND NOT t.tgisinternal AND c.relname IN ({touched})",
    )
    return {(str(table), str(trigger)) for table, trigger in rows} - declared_user_triggers(lines)


async def observed_sequence(dsn: str, name: str) -> tuple:
    """The sequence row as the contract's `observed_sequences` block measures it."""
    rows = await scalar_rows(
        dsn,
        "SELECT sequence_record.relname, owning_table.relname, owning_column.attname, "
        "sequence_definition.seqtypid::pg_catalog.regtype::text, "
        "sequence_definition.seqincrement, sequence_definition.seqmin, "
        "sequence_definition.seqmax, sequence_definition.seqstart, "
        "sequence_definition.seqcycle "
        "FROM pg_catalog.pg_class AS sequence_record "
        "JOIN pg_catalog.pg_namespace AS namespace_record "
        "  ON namespace_record.oid = sequence_record.relnamespace "
        " AND namespace_record.nspname = 'public' "
        "JOIN pg_catalog.pg_sequence AS sequence_definition "
        "  ON sequence_definition.seqrelid = sequence_record.oid "
        "JOIN pg_catalog.pg_depend AS ownership_link "
        "  ON ownership_link.objid = sequence_record.oid "
        " AND ownership_link.classid = 'pg_catalog.pg_class'::pg_catalog.regclass "
        " AND ownership_link.refclassid = 'pg_catalog.pg_class'::pg_catalog.regclass "
        " AND ownership_link.deptype IN ('a', 'i') "
        "JOIN pg_catalog.pg_class AS owning_table ON owning_table.oid = ownership_link.refobjid "
        "JOIN pg_catalog.pg_attribute AS owning_column "
        "  ON owning_column.attrelid = ownership_link.refobjid "
        " AND owning_column.attnum = ownership_link.refobjsubid "
        f"WHERE sequence_record.relkind = 'S' AND sequence_record.relname = '{name}'",
    )
    if len(rows) != 1:
        raise SystemExit(f"sequence {name}: measured {rows}")
    return rows[0]


def render_sequence(row: tuple) -> str:
    name, table, column, data_type, increment, minimum, maximum, start, cycles = row
    return (
        f"     ('{name}', '{table}', '{column}', '{data_type}', {increment}, {minimum}, "
        f"{maximum}, {start}, {'TRUE' if cycles else 'FALSE'}),"
    )


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
            " 'contract_id', 'brain-v42/postgresql-recovery/v20',",
            " 'contract_id', 'brain-v42/postgresql-recovery/v21',",
        ),
        (" 'schema_version', 20", " 'schema_version', 21"),
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
    """The manifest moves identity, the two catalog counts and the table set."""
    document = json.loads(src.read_text(encoding="utf-8"))
    if document["contract_id"] != "brain-v42/postgresql-recovery/v20":
        raise SystemExit(f"not the v20 manifest: {document['contract_id']}")
    document["contract_id"] = "brain-v42/postgresql-recovery/v21"
    document["schema_version"] = 21
    counts = [check for check in document["checks"] if check["id"] == "catalog_counts"]
    tables = [check for check in document["checks"] if check["id"] == "table_set"]
    if len(counts) != 1 or len(tables) != 1:
        raise SystemExit("catalog_counts and table_set must each appear once")
    for key, (old, new) in CATALOG.items():
        if counts[0][key] != old:
            raise SystemExit(f"catalog_counts.{key} is {counts[0][key]}, expected {old}")
        counts[0][key] = new
    if set(NEW_TABLES) & set(tables[0]["tables"]):
        raise SystemExit("table_set already names a 063 table")
    tables[0]["tables"] = sorted([*tables[0]["tables"], *NEW_TABLES])
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote : {dst}", flush=True)


async def main(dsn: str, src: Path, dst: Path) -> int:
    assert_disposable_dsn(dsn)
    lines = src.read_text(encoding="utf-8").split("\n")
    print(f"measuring {src.name} against the 063 database ...", flush=True)
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

    rewritten = {row[0] for row in split["expected_table_columns"][1]}
    if rewritten != set(ALTERED_TABLES):
        raise SystemExit(f"column fingerprints rewritten for {sorted(rewritten)}")
    added_old = {
        (row[0], row[1])
        for name in ("expected_table_constraints",)
        for row in split[name][0]
        if row[0] not in NEW_TABLES
    }
    if added_old != NEW_ROWS_ON_OLD_TABLES:
        raise SystemExit(f"constraints added on existing tables: {sorted(added_old)}")
    opener = [row for row in split["expected_table_constraints"][0] if row[1] == OPENER_FORMAT]
    if len(opener) != 1:
        raise SystemExit(f"{OPENER_FORMAT}: {opener}")
    lines = insert_plain(
        lines,
        "expected_session_constraints",
        [f"     ('{OPENER_FORMAT}', 'c', NULL::text, '{opener[0][2]}'),"],
    )
    lines = rewrite_single_line(
        lines,
        "expected_column_fingerprints",
        r"     \('brain_sessions', '[0-9a-f]{32}'\),?",
        await session_column_fingerprint(dsn, lines),
    )

    triggers = await new_user_triggers(dsn, lines)
    if triggers != NEW_USER_TRIGGERS:
        raise SystemExit(f"063 user triggers measured {sorted(triggers)}")
    lines = insert_plain(
        lines,
        "expected_runtime_user_triggers",
        [f"     ('{table}', '{trigger}')," for table, trigger in sorted(triggers)],
    )
    lines = insert_plain(
        lines,
        "expected_runtime_trigger_tables",
        [f"     ('{table}')," for table in NEW_TRIGGER_TABLES],
    )

    measured_sequence = await observed_sequence(dsn, NEW_SEQUENCE[0])
    if measured_sequence != NEW_SEQUENCE:
        raise SystemExit(f"sequence measured {measured_sequence}, declared {NEW_SEQUENCE}")
    lines = insert_plain(lines, "expected_sequences", [render_sequence(measured_sequence)])
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
            Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v20.json"),
            Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v21.json"),
        )
        sys.exit(0)
    sys.exit(
        asyncio.run(
            main(
                sys.argv[1],
                Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ops/recovery/brain-v42-v20.sql"),
                Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ops/recovery/brain-v42-v21.sql"),
            )
        )
    )
