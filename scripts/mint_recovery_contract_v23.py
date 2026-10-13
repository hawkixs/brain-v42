"""Mint the v23 DR contract as a declared delta of v22, for head 065.

065 (programme progress, contract `docs/contracts/programmes_v1.md`) adds two tables,
`programmes` and `programme_gates`, with their CHECK constraints, two primary keys
and two UNIQUE constraints (four indexes) and three foreign keys (gates to
programmes, and each table's project to `project_contexts`). It adds no trigger, no
trigger function, no sequence, no view and no extension, and changes no existing
table: the contract moves exactly that footprint, and an observed row outside it
stops the mint. The extension inventory and v22's runtime ACL check are unchanged;
064's default privileges grant `brain_app` the new tables, so no ACL asset moves
(as for v21, whose new tables shipped no ACL asset either).

Derived from `mint_recovery_contract_v21.py` (pairs, bidirectional self-check, DSN
guard, new tables) and `mint_recovery_contract_v22.py` (binding written last, from
the final assets' digests).

Usage (paths default to the repository containing this script):
    python scripts/mint_recovery_contract_v23.py <dsn> [src.sql] [dst.sql]
    python scripts/mint_recovery_contract_v23.py --manifest [src.json] [dst.json]
    python scripts/mint_recovery_contract_v23.py --bind

Measure the base asset against a database the alembic chain just built to head 065,
and the `-pgrestore` twin against a real custom-format `pg_dump`/`pg_restore
--no-owner --no-acl` of a disposable 064 source migrated to 065 after the restore.
The mint refuses any DSN that is not a disposable `brain_<tag>` database, then
re-checks `current_database()`. Never regenerates: `--bind` runs last.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
RECOVERY = ROOT / "ops/recovery"
NEW_TABLES = ("programme_gates", "programmes")
CATALOG = {"foreign_keys": (62, 65), "indexes": (195, 199)}
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
#: 065 alters no existing table: no fingerprint is rewritten, only rows are added.
REWRITABLE_ROWS: set[tuple[str, tuple]] = set()
#: v21 exempted one historical index row from the delta. The v23 measurements never
#: meet it (each variant is measured on the database form it describes), so the
#: exemption is EMPTY: any divergence on any row outside the 065 footprint stops the
#: mint instead of being skipped (review round 1 of #376).
TWIN_DIVERGENCE_BY_DESIGN: set[tuple[str, tuple]] = set()
#: What the written asset may still report when replayed in full on the database it
#: was measured on: the data checks (a disposable database is empty), the trigger
#: 050 ships disabled (production armed it; a chain-built database has not), and, on
#: the live asset only, the extension build of the host (vector 0.8.2 is pinned,
#: the disposable server runs another build).
DATA_CHECK_KINDS = frozenset({"row_count_sum_min"})
PINNED_RUNTIME_DRIFT = {
    "view_column_mismatches": 0,
    "view_option_mismatches": 0,
    "artifact_index_mismatches": 0,
    "ended_snapshot_mismatches": 0,
    "focus_revision_violations": 0,
    "session_column_mismatches": 0,
    "artifact_column_mismatches": 0,
    "runtime_trigger_mismatches": 1,
    "view_definition_mismatches": 0,
    "artifact_project_mismatches": 0,
    "session_constraint_mismatches": 0,
    "artifact_constraint_mismatches": 0,
}


def allowed_addition(name: str, key: tuple) -> bool:
    """The rows 065 may ADD, and nothing else: rows of its two new tables."""
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


async def scalar_rows(dsn: str, query: str) -> list[tuple]:
    connection = await connect(dsn)
    try:
        return [tuple(r) for r in await connection.fetch(query)]
    finally:
        await connection.close()


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
            " 'contract_id', 'brain-v42/postgresql-recovery/v22',",
            " 'contract_id', 'brain-v42/postgresql-recovery/v23',",
        ),
        (" 'schema_version', 22", " 'schema_version', 23"),
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
    if document["contract_id"] != "brain-v42/postgresql-recovery/v22":
        raise SystemExit(f"not the v22 manifest: {document['contract_id']}")
    document["contract_id"] = "brain-v42/postgresql-recovery/v23"
    document["schema_version"] = 23
    counts = [check for check in document["checks"] if check["id"] == "catalog_counts"]
    tables = [check for check in document["checks"] if check["id"] == "table_set"]
    if len(counts) != 1 or len(tables) != 1:
        raise SystemExit("catalog_counts and table_set must each appear once")
    for key, (old, new) in CATALOG.items():
        if counts[0][key] != old:
            raise SystemExit(f"catalog_counts.{key} is {counts[0][key]}, expected {old}")
        counts[0][key] = new
    if set(NEW_TABLES) & set(tables[0]["tables"]):
        raise SystemExit("table_set already names a 065 table")
    tables[0]["tables"] = sorted([*tables[0]["tables"], *NEW_TABLES])
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote : {dst}", flush=True)


def bind() -> None:
    """Bind digests only after minting the final assets; never edit an old contract."""
    binding: dict[str, object] = {
        "contract_id": "brain-v42/postgresql-recovery/v23",
        "contract_version": 23,
        "schema_head": "065",
    }
    for key, suffix in (
        ("attestation_sql", ".sql"),
        ("restored_attestation_sql", "-pgrestore.sql"),
        ("manifest", ".json"),
    ):
        asset = RECOVERY / f"brain-v42-v23{suffix}"
        binding[key] = {
            "path": str(asset.relative_to(ROOT)),
            "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
        }
    (RECOVERY / "current.json").write_text(json.dumps(binding, indent=2, sort_keys=True) + "\n")


async def main(dsn: str, src: Path, dst: Path) -> int:
    assert_disposable_dsn(dsn)
    lines = src.read_text(encoding="utf-8").split("\n")
    connection = await connect(dsn)
    try:
        head = await connection.fetchval("SELECT version_num FROM public.alembic_version")
    finally:
        await connection.close()
    if head != "065":
        raise SystemExit(f"v23 measurement requires head 065, found {head!r}")
    print(f"measuring {src.name} against the 065 database ...", flush=True)
    deltas = await measure(dsn, lines)

    for expected_name, _ in PAIRS:
        additions, rewrites = split_delta(
            expected_name, deltas[expected_name]["missing"], deltas[expected_name]["stale"]
        )
        if rewrites:
            raise SystemExit(f"{expected_name}: 065 rewrites nothing, measured {rewrites}")
        print(f"  {expected_name}: {len(additions)} addition(s)")
        if expected_name == "expected_trigger_functions" and additions:
            raise SystemExit(f"065 adds no trigger function, measured {additions}")
        lines = append_rows(lines, expected_name, additions)

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
    await replay_written(dsn, dst, restored="-pgrestore" in dst.name)
    print(f"replayed: {dst.name} passes every structural check on the measured database")
    return 0


async def replay_written(dsn: str, asset: Path, *, restored: bool) -> None:
    """Re-read the asset FROM DISK and replay it in full, read-only (review of #376).

    The pair measurement above checks four fingerprint families; the full replay also
    covers views, sequences, extensions, catalog counts and every invariant, on the
    bytes that were actually written.
    """
    manifest = json.loads((RECOVERY / "brain-v42-v22.json").read_text(encoding="utf-8"))
    kinds = {check["id"]: check["kind"] for check in manifest["checks"]}
    connection = await connect(dsn)
    try:
        async with connection.transaction(readonly=True):
            receipt = json.loads(await connection.fetchval(asset.read_text(encoding="utf-8")))
    finally:
        await connection.close()
    failures = {check["id"]: check for check in receipt["checks"] if check["status"] != "pass"}
    unexplained = {}
    for check_id, failure in failures.items():
        if kinds.get(check_id) in DATA_CHECK_KINDS:
            continue
        if check_id == "brain_runtime_032_036_037" and failure["observed"] == PINNED_RUNTIME_DRIFT:
            continue
        if check_id == "extension_versions" and not restored:
            continue
        unexplained[check_id] = failure
    if unexplained:
        asset.unlink()
        raise SystemExit(f"the written asset fails on its own database, removed: {unexplained}")


if __name__ == "__main__":
    if sys.argv[1] == "--manifest":
        mint_manifest(
            Path(sys.argv[2]) if len(sys.argv) > 2 else RECOVERY / "brain-v42-v22.json",
            Path(sys.argv[3]) if len(sys.argv) > 3 else RECOVERY / "brain-v42-v23.json",
        )
        sys.exit(0)
    if sys.argv[1] == "--bind":
        bind()
        sys.exit(0)
    sys.exit(
        asyncio.run(
            main(
                sys.argv[1],
                Path(sys.argv[2]) if len(sys.argv) > 2 else RECOVERY / "brain-v42-v22.sql",
                Path(sys.argv[3]) if len(sys.argv) > 3 else RECOVERY / "brain-v42-v23.sql",
            )
        )
    )
