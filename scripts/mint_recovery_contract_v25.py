"""Mint recovery contract v25 as a measured delta of v24 for head 067.

067 adds one nullable credential scope column and an inline CHECK, and widens the
credential audit event CHECK. It adds no function, table, index, foreign key,
sequence, trigger or ACL grant. Every fingerprint outside those three rows must
stay unchanged. Catalog counts are recounted even though their values do not move.

Measure the base on a disposable database migrated to 067. Measure the restored
twin on a custom-format dump of a disposable 066 source restored with
pg_restore --no-owner --no-acl, then migrated to 067. Integration tests also replay
a source dumped at 067. Neither variant may be measured on a shared database.

Usage after preparing those disposable databases:
    python scripts/mint_recovery_contract_v25.py "$BRAIN_V25_BASE_DSN"
    python scripts/mint_recovery_contract_v25.py "$BRAIN_V25_RESTORED_DSN" ops/recovery/brain-v42-v24-pgrestore.sql ops/recovery/brain-v42-v25-pgrestore.sql
    python scripts/mint_recovery_contract_v25.py --manifest
    python scripts/mint_recovery_contract_v25.py --acl
    python scripts/mint_recovery_contract_v25.py --bind

Both DSNs must name disposable brain_<tag> databases, never brain or brain_test.
--acl advances v24's separate live ACL asset without changing its grants.
--bind runs last and hashes final assets. Historical contracts remain unchanged.
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
import structlog

ROOT = Path(__file__).resolve().parents[1]
RECOVERY = ROOT / "ops/recovery"
logger = structlog.get_logger()
CATALOG = {"foreign_keys": 74, "indexes": 215, "invalid_indexes": 0, "unvalidated_constraints": 0}
NEW_CONSTRAINT = ("brain_client_credentials", "brain_client_credentials_allowed_projects_valid")
REWRITABLE_ROWS = {
    ("expected_table_columns", ("brain_client_credentials",)),
    (
        "expected_table_constraints",
        ("brain_credential_audit", "brain_credential_audit_event_valid"),
    ),
}
TWIN_DIVERGENCE_BY_DESIGN: set[tuple[str, tuple]] = set()
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
    ("expected_sequences", "observed_sequences"),
]
KEY_WIDTH = {
    "expected_table_columns": 1,
    "expected_table_constraints": 2,
    "expected_table_indexes": 2,
    "expected_trigger_functions": 1,
    "expected_sequences": 1,
}
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
#: The live asset's extension drift is exempt only as exactly this inventory with a
#: vector build PROVED harmless (review round 2 of #376): any other extension, or an
#: unproven vector build, is a divergence and stops the mint.
EXTENSIONS_EXPECTED = "pg_stat_statements 1.10, plpgsql 1.0, vector 0.8.2"


def proven_extension_inventories() -> frozenset[str]:
    proven = json.loads((RECOVERY / "proven_vector_versions.json").read_text(encoding="utf-8"))
    return frozenset(
        f"pg_stat_statements 1.10, plpgsql 1.0, vector {version}" for version in proven["versions"]
    )


def allowed_addition(name: str, key: tuple) -> bool:
    """Only the scope CHECK may be added; all other schema objects are inherited."""
    return name == "expected_table_constraints" and key == NEW_CONSTRAINT


def find_block(lines: list[str], name: str) -> tuple[int, int, str]:
    header = None
    for index, line in enumerate(lines):
        if line.startswith((f"{name}(", f"{name} AS (", f"WITH {name}(")):
            header = index
            break
    if header is None:
        raise SystemExit(f"block {name} not found")
    for index in range(header + 1, len(lines)):
        if lines[index] == "),":
            declaration = " ".join(lines[header:index])
            spec_match = re.search(rf"{re.escape(name)}\(([^)]*)\)", declaration)
            spec = spec_match.group(1).strip() if spec_match else ""
            opening = next(i for i in range(header, index) if lines[i].rstrip().endswith(" AS ("))
            return opening, index, spec
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
    if expected_name == "expected_sequences":
        values = [
            f"'{value}'"
            if isinstance(value, str)
            else ("TRUE" if value else "FALSE")
            if isinstance(value, bool)
            else str(value)
            for value in row
        ]
        return ["     (" + ", ".join(values) + "),"]
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


def rewrite_row(lines: list[str], name: str, row: tuple) -> list[str]:
    """Rewrite only the fingerprint of a declared existing row, preserving its key."""
    width = KEY_WIDTH[name]
    if (name, tuple(row[:width])) not in REWRITABLE_ROWS:
        raise SystemExit(f"undeclared fingerprint rewrite: {name}, {row}")
    head, close, _ = find_block(lines, name)
    key_lines = [f"         '{value}'," for value in row[:width]]
    matches = [i for i in range(head + 1, close) if lines[i : i + width] == key_lines]
    if len(matches) != 1:
        raise SystemExit(f"{name}: {len(matches)} row(s) keyed {row[:width]!r}, expected one")
    target = matches[0] + width
    if not re.fullmatch(r"         '[0-9a-f]{32}'", lines[target]):
        raise SystemExit(f"{name}: unexpected fingerprint line {lines[target]!r}")
    lines[target] = f"         '{row[width]}'"
    return lines


async def observed_catalog_counts(dsn: str, lines: list[str]) -> dict[str, int]:
    head, close, _ = find_block(lines, "catalog_counts")
    rows = await scalar_rows(
        dsn,
        f"WITH catalog_counts AS (\n{body(lines, head, close)}\n)\n"
        "SELECT observed::text FROM catalog_counts",
    )
    return dict(json.loads(rows[0][0]))


def _replace_once(text: str, before: str, after: str) -> str:
    if text.count(before) != 1:
        raise SystemExit(f"anchor not unique ({text.count(before)}x): {before[:80]!r}")
    return text.replace(before, after)


def patch_scalars(text: str) -> str:
    """Move only identity: 067 changes no catalog count in the strict manifest."""
    text = _replace_once(
        text,
        " 'contract_id', 'brain-v42/postgresql-recovery/v24',",
        " 'contract_id', 'brain-v42/postgresql-recovery/v25',",
    )
    return _replace_once(text, " 'schema_version', 24", " 'schema_version', 25")


def mint_manifest(src: Path, dst: Path) -> None:
    """Preserve v24's exact check shapes and ACL grants for red-backup 0.1.1."""
    document = json.loads(src.read_text(encoding="utf-8"))
    if document["contract_id"] != "brain-v42/postgresql-recovery/v24":
        raise SystemExit(f"not the v24 manifest: {document['contract_id']}")
    counts = [check for check in document["checks"] if check["id"] == "catalog_counts"]
    if len(counts) != 1 or any(counts[0][key] != value for key, value in CATALOG.items()):
        raise SystemExit("expected v24 catalog counts")
    document.update(contract_id="brain-v42/postgresql-recovery/v25", schema_version=25)
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    logger.info("recovery.manifest_written", path=str(dst))


def mint_acl_assets() -> None:
    """Keep 066's restricted grants and advance only the separate asset's identity."""
    source = (RECOVERY / "brain-v42-v24-acl.sql").read_text(encoding="utf-8")
    text = _replace_once(
        source,
        "'brain-v42/postgresql-recovery/v24-acl'",
        "'brain-v42/postgresql-recovery/v25-acl'",
    )
    text = _replace_once(text, " 'schema_version', 24", " 'schema_version', 25")
    document = json.loads((RECOVERY / "brain-v42-v24-acl.json").read_text(encoding="utf-8"))
    if document["contract_id"] != "brain-v42/postgresql-recovery/v24-acl":
        raise SystemExit("expected the frozen v24 ACL manifest")
    document.update(contract_id="brain-v42/postgresql-recovery/v25-acl", schema_version=25)
    (RECOVERY / "brain-v42-v25-acl.sql").write_text(text, encoding="utf-8")
    (RECOVERY / "brain-v42-v25-acl.json").write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def bind() -> None:
    """Hash final assets only after both measured SQL variants and ACL assets exist."""
    binding: dict[str, object] = {
        "contract_id": "brain-v42/postgresql-recovery/v25",
        "contract_version": 25,
        "schema_head": "067",
    }
    for key, suffix in (
        ("attestation_sql", ".sql"),
        ("restored_attestation_sql", "-pgrestore.sql"),
        ("manifest", ".json"),
    ):
        asset = RECOVERY / f"brain-v42-v25{suffix}"
        binding[key] = {
            "path": str(asset.relative_to(ROOT)),
            "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
        }
    for suffix in ("-acl.sql", "-acl.json"):
        (RECOVERY / f"brain-v42-v25{suffix}").read_bytes()
    (RECOVERY / "current.json").write_text(json.dumps(binding, indent=2, sort_keys=True) + "\n")


async def main(dsn: str, src: Path, dst: Path) -> int:
    assert_disposable_dsn(dsn)
    lines = src.read_text(encoding="utf-8").split("\n")
    connection = await connect(dsn)
    try:
        heads = await connection.fetch("SELECT version_num FROM public.alembic_version")
    finally:
        await connection.close()
    if [row[0] for row in heads] != ["067"]:
        raise SystemExit(f"v25 measurement requires single head 067, found {heads!r}")
    logger.info("recovery.measuring", source=src.name, head="067")
    deltas = await measure(dsn, lines)

    for expected_name, _ in PAIRS:
        additions, rewrites = split_delta(
            expected_name, deltas[expected_name]["missing"], deltas[expected_name]["stale"]
        )
        declared_additions = (
            {NEW_CONSTRAINT} if expected_name == "expected_table_constraints" else set()
        )
        declared_rewrites = {key for name, key in REWRITABLE_ROWS if name == expected_name}
        width = KEY_WIDTH[expected_name]
        if {tuple(row[:width]) for row in additions} != declared_additions:
            raise SystemExit(f"{expected_name}: incomplete 067 additions: {additions}")
        if {tuple(row[:width]) for row in rewrites} != declared_rewrites:
            raise SystemExit(f"{expected_name}: incomplete 067 rewrites: {rewrites}")
        for row in rewrites:
            lines = rewrite_row(lines, expected_name, row)
        lines = append_rows(lines, expected_name, additions)
        logger.info(
            "recovery.delta", block=expected_name, additions=len(additions), rewrites=len(rewrites)
        )

    measured = await observed_catalog_counts(dsn, lines)
    if measured != CATALOG:
        raise SystemExit(f"catalog counts measured {measured}, declared {CATALOG}")
    text = patch_scalars("\n".join(lines))
    remaining = await measure(dsn, text.split("\n"))
    if any(rows for sides in remaining.values() for rows in sides.values()):
        raise SystemExit(f"the written contract still does not describe the database: {remaining}")

    dst.write_text(text, encoding="utf-8")
    logger.info("recovery.asset_written", path=str(dst))
    await replay_written(dsn, dst, restored="-pgrestore" in dst.name)
    logger.info("recovery.asset_replayed", path=str(dst))
    return 0


async def replay_written(dsn: str, asset: Path, *, restored: bool) -> None:
    """Re-read the asset FROM DISK and replay it in full, read-only (review of #376).

    The pair measurement above checks four fingerprint families; the full replay also
    covers views, sequences, extensions, catalog counts and every invariant, on the
    bytes that were actually written.
    """
    manifest = json.loads((RECOVERY / "brain-v42-v24.json").read_text(encoding="utf-8"))
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
        if (
            check_id == "extension_versions"
            and not restored
            and failure.get("expected") == EXTENSIONS_EXPECTED
            and failure.get("observed") in proven_extension_inventories()
        ):
            continue
        unexplained[check_id] = failure
    if unexplained:
        asset.unlink()
        raise SystemExit(f"the written asset fails on its own database, removed: {unexplained}")


if __name__ == "__main__":
    if sys.argv[1] == "--manifest":
        mint_manifest(
            Path(sys.argv[2]) if len(sys.argv) > 2 else RECOVERY / "brain-v42-v24.json",
            Path(sys.argv[3]) if len(sys.argv) > 3 else RECOVERY / "brain-v42-v25.json",
        )
        sys.exit(0)
    if sys.argv[1] == "--acl":
        mint_acl_assets()
        sys.exit(0)
    if sys.argv[1] == "--bind":
        bind()
        sys.exit(0)
    sys.exit(
        asyncio.run(
            main(
                sys.argv[1],
                Path(sys.argv[2]) if len(sys.argv) > 2 else RECOVERY / "brain-v42-v24.sql",
                Path(sys.argv[3]) if len(sys.argv) > 3 else RECOVERY / "brain-v42-v25.sql",
            )
        )
    )
