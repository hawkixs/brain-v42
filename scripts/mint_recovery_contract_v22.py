"""Mint v22 as a declared delta of v21, for head 064, after the last schema edit.

064 changes only ACLs: every v21 schema fingerprint is preserved byte for byte.
The live variant adds a migration-derived runtime ACL proof; the sandbox variant
declares that check not applicable because red-backup restores with --no-owner
--no-acl, without an application role. No LOGIN or password is required by either
schema contract. The optional pg_catalog.pg_control_system grant belongs to R1.

The separate live ACL asset extends v11's codex/ownership proof with 064's
runtime grants and expected role. Historical assets remain immutable; no ACL
asset applies to a --no-owner --no-acl restore sandbox.

Like the v21 mint, replacements are anchored, the manifest is canonical JSON,
and database verification accepts only disposable DSNs and checks the actual
connection. The integration twin replays chain-built and real restored sources.

Usage (paths default to the repository containing this script):
    python scripts/mint_recovery_contract_v22.py
    python scripts/mint_recovery_contract_v22.py --manifest [src.json] [dst.json]
    python scripts/mint_recovery_contract_v22.py <dsn> [src.sql] [dst.sql]

The first form mints both variants, the manifest and current.json deterministically.
The DSN form also verifies the declared ACL delta read-only before writing an asset.
"""

from __future__ import annotations

import ast
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
DISPOSABLE_DATABASE = re.compile(r"brain_[a-z0-9_]+")
PROTECTED_DATABASES = ("brain", "brain_test")
CHECK_ID = "runtime_acl_064"
IDENTITY = (
    (
        " 'contract_id', 'brain-v42/postgresql-recovery/v21',",
        " 'contract_id', 'brain-v42/postgresql-recovery/v22',",
    ),
    (" 'schema_version', 21", " 'schema_version', 22"),
)
CHECK_ANCHOR = "check_rows(id, expected, observed, passed) AS (\n"
NOT_APPLICABLE = "not_applicable: pg_restore --no-owner --no-acl"


def assert_disposable_dsn(dsn: str) -> str:
    """Use v21's parsed, decoded database guard; refuse all query overrides."""
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
    """Check the server's database identity before any measurement."""
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


def runtime_grants(source: str | None = None) -> dict[str, list[str]]:
    """Read 064's upgrade declarations, never execute a migration to derive ACLs."""
    if source is None:
        source = (ROOT / "alembic/versions/064_brain_app_runtime_role.py").read_text()
    tree = ast.parse(source)
    upgrade = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    strings = [
        node.value
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    sql = "\n".join(strings)
    patterns = {
        "schemas": r"GRANT USAGE ON SCHEMA ([\w, ]+) TO brain_app",
        "tables": r"GRANT ([\w, ]+) ON ALL TABLES IN SCHEMA public TO brain_app",
        "sequences": r"GRANT ([\w, ]+) ON ALL SEQUENCES IN SCHEMA public TO brain_app",
    }
    result = {}
    for key, pattern in patterns.items():
        matches = re.findall(pattern, sql)
        if len(matches) != 1:
            raise SystemExit(f"064: expected one {key} grant declaration")
        result[key] = [item.strip() for item in matches[0].split(",")]
    for key, kind in (("tables", "TABLES"), ("sequences", "SEQUENCES")):
        declaration = f"GRANT {', '.join(result[key])} ON {kind} TO brain_app"
        if declaration not in sql:
            raise SystemExit(f"064: default privileges disagree with {key} grants")
    functions = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_DIRECT_FUNCTIONS"
            for target in node.targets
        )
    )
    result["functions"] = list(ast.literal_eval(functions.value))
    if "GRANT EXECUTE ON FUNCTION " not in sql or " TO brain_app" not in sql:
        raise SystemExit("064: direct function grants are missing")
    return result


def _values(items: list[str]) -> str:
    return ",\n".join(f"     ('{item}')" for item in items)


def live_delta() -> str:
    """Explicit ACLs are compared in both directions, including grants and defaults."""
    grants = runtime_grants()
    return f"""runtime_role AS (
 SELECT oid, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
 FROM pg_catalog.pg_roles WHERE rolname = 'brain_app'
),
runtime_expected_schemas(name) AS (
 VALUES
{_values(grants["schemas"])}
),
runtime_expected_table_privileges(privilege) AS (
 VALUES
{_values(grants["tables"])}
),
runtime_expected_sequence_privileges(privilege) AS (
 VALUES
{_values(grants["sequences"])}
),
runtime_expected_functions(signature) AS (
 VALUES
{_values(grants["functions"])}
),
runtime_relations AS (
 SELECT c.oid, c.relowner, c.relacl, c.relkind
 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
),
runtime_expected_relations(object_oid, privilege) AS (
 SELECT r.oid, p.privilege FROM runtime_relations r
 CROSS JOIN runtime_expected_table_privileges p WHERE r.relkind <> 'S'
 UNION ALL
 SELECT r.oid, p.privilege FROM runtime_relations r
 CROSS JOIN runtime_expected_sequence_privileges p WHERE r.relkind = 'S'
),
runtime_observed_relations AS (
 SELECT r.oid AS object_oid, a.privilege_type AS privilege,
        a.is_grantable OR a.grantor <> r.relowner AS invalid
 FROM runtime_relations r CROSS JOIN LATERAL pg_catalog.aclexplode(r.relacl) a
 WHERE a.grantee = (SELECT oid FROM runtime_role)
),
runtime_expected_defaults(kind, privilege) AS (
 SELECT 'r', privilege FROM runtime_expected_table_privileges
 UNION ALL
 SELECT 'S', privilege FROM runtime_expected_sequence_privileges
),
runtime_observed_defaults AS (
 SELECT d.defaclobjtype::text AS kind, a.privilege_type AS privilege,
        a.is_grantable OR a.grantor <> d.defaclrole
          OR d.defaclrole <> (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)
          AS invalid
 FROM pg_catalog.pg_default_acl d
 CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) a
 WHERE d.defaclnamespace = 'public'::pg_catalog.regnamespace
   AND a.grantee = (SELECT oid FROM runtime_role)
),
runtime_observed_functions AS (
 SELECT p.oid, a.is_grantable OR a.grantor <> p.proowner AS invalid
 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
 CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) a
 WHERE n.nspname = 'public' AND a.grantee = (SELECT oid FROM runtime_role)
   AND a.privilege_type = 'EXECUTE'
),
runtime_acl_counters AS (
 SELECT
   (SELECT CASE WHEN count(*) = 1 AND NOT bool_or(rolsuper OR rolcreatedb OR rolcreaterole
      OR rolreplication OR rolbypassrls) THEN 0 ELSE 1 END FROM runtime_role)
     + (SELECT count(*) FROM pg_catalog.pg_auth_members
        WHERE member = (SELECT oid FROM runtime_role)) AS role_mismatches,
   (SELECT count(*) FROM runtime_expected_schemas e
    WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n,
      LATERAL pg_catalog.aclexplode(n.nspacl) a
      WHERE n.nspname = e.name AND a.grantee = (SELECT oid FROM runtime_role)
        AND a.privilege_type = 'USAGE' AND NOT a.is_grantable AND a.grantor = n.nspowner))
     + (SELECT count(*) FROM pg_catalog.pg_namespace n,
        LATERAL pg_catalog.aclexplode(n.nspacl) a
        WHERE n.nspname IN (SELECT name FROM runtime_expected_schemas)
          AND a.grantee = (SELECT oid FROM runtime_role)
          AND (a.privilege_type <> 'USAGE' OR a.is_grantable OR a.grantor <> n.nspowner))
     AS schema_mismatches,
   (SELECT count(*) FROM runtime_expected_relations e
      LEFT JOIN runtime_observed_relations o USING (object_oid, privilege)
      WHERE o.object_oid IS NULL)
     + (SELECT count(*) FROM runtime_observed_relations o
        LEFT JOIN runtime_expected_relations e USING (object_oid, privilege)
        WHERE e.object_oid IS NULL OR o.invalid) AS relation_mismatches,
   (SELECT count(*) FROM runtime_expected_functions e
      WHERE NOT EXISTS (SELECT 1 FROM runtime_observed_functions o
        WHERE o.oid = pg_catalog.to_regprocedure(e.signature) AND NOT o.invalid))
     + (SELECT count(*) FROM runtime_observed_functions o
        WHERE o.invalid OR NOT EXISTS (SELECT 1 FROM runtime_expected_functions e
          WHERE pg_catalog.to_regprocedure(e.signature) = o.oid)) AS function_mismatches,
   (SELECT count(*) FROM runtime_expected_defaults e
      LEFT JOIN runtime_observed_defaults o USING (kind, privilege) WHERE o.kind IS NULL)
     + (SELECT count(*) FROM runtime_observed_defaults o
        LEFT JOIN runtime_expected_defaults e USING (kind, privilege)
        WHERE e.kind IS NULL OR o.invalid) AS default_mismatches,
   (SELECT count(*) FROM pg_catalog.pg_shdepend
    WHERE refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
      AND refobjid = (SELECT oid FROM runtime_role) AND deptype = 'o') AS ownership_mismatches
),
"""


def check_delta(*, restored: bool) -> str:
    if restored:
        return (
            f" SELECT '{CHECK_ID}', to_jsonb('{NOT_APPLICABLE}'::text),\n"
            f"     to_jsonb('{NOT_APPLICABLE}'::text), TRUE\n UNION ALL\n"
        )
    return f""" SELECT '{CHECK_ID}',
     jsonb_build_object('role_mismatches', 0, 'schema_mismatches', 0,
       'relation_mismatches', 0, 'function_mismatches', 0,
       'default_mismatches', 0, 'ownership_mismatches', 0),
     to_jsonb(runtime_acl_counters),
     role_mismatches = 0 AND schema_mismatches = 0 AND relation_mismatches = 0
       AND function_mismatches = 0 AND default_mismatches = 0 AND ownership_mismatches = 0
 FROM runtime_acl_counters
 UNION ALL
"""


def _replace_once(text: str, before: str, after: str) -> str:
    if text.count(before) != 1:
        raise SystemExit(f"anchor not unique ({text.count(before)}x): {before[:60]!r}")
    return text.replace(before, after)


def mint_sql(text: str, *, restored: bool = False) -> str:
    prefix = "" if restored else live_delta()
    text = _replace_once(text, CHECK_ANCHOR, prefix + CHECK_ANCHOR + check_delta(restored=restored))
    for before, after in IDENTITY:
        text = _replace_once(text, before, after)
    return text


def remove_delta(text: str, *, restored: bool) -> str:
    """Inverse for byte-for-byte verification of the inherited v21 contract."""
    prefix = "" if restored else live_delta()
    text = _replace_once(text, prefix + CHECK_ANCHOR + check_delta(restored=restored), CHECK_ANCHOR)
    for before, after in IDENTITY:
        text = _replace_once(text, after, before)
    return text


def mint_manifest(src: Path, dst: Path) -> None:
    document = json.loads(src.read_text(encoding="utf-8"))
    if document["contract_id"] != "brain-v42/postgresql-recovery/v21":
        raise SystemExit("not the v21 manifest")
    document.update(contract_id="brain-v42/postgresql-recovery/v22", schema_version=22)
    document["checks"].append(
        {
            "id": CHECK_ID,
            "kind": "migration_runtime_acl_invariant",
            "revision": "064",
            "grants": runtime_grants(),
            "proof_scope": "live-only",
            "restore_rule": "not-applicable-no-owner-no-acl",
        }
    )
    document["checks"].sort(key=lambda check: check["id"])
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")


def mint_acl_sql(text: str) -> str:
    """Retain v11's negative checks and add only migration-declared runtime ACLs."""
    text = _replace_once(
        text,
        "WITH expected_contract_grants(object_name, grantee, privilege_type) AS (\n",
        "WITH "
        + live_delta()
        + "expected_contract_grants(object_name, grantee, privilege_type) AS (\n",
    )
    text = _replace_once(
        text,
        "\n),\nexpected_roles(role_name) AS (",
        "\n UNION ALL\n"
        " SELECT relation_record.relname, 'brain_app', expected_grant.privilege\n"
        " FROM runtime_expected_relations AS expected_grant\n"
        " JOIN pg_catalog.pg_class AS relation_record\n"
        "   ON relation_record.oid = expected_grant.object_oid\n"
        "),\nexpected_roles(role_name) AS (",
    )
    text = _replace_once(
        text,
        " VALUES ('brain'), ('codex_ro')",
        " VALUES ('brain'), ('codex_ro'), ('brain_app')",
    )
    text = _replace_once(
        text, "NOT IN ('brain', 'codex_ro')", "NOT IN ('brain', 'codex_ro', 'brain_app')"
    )
    text = _replace_once(text, CHECK_ANCHOR, CHECK_ANCHOR + check_delta(restored=False))
    text = _replace_once(
        text,
        " 'contract_id', 'brain-v42/postgresql-recovery/v11-acl',",
        " 'contract_id', 'brain-v42/postgresql-recovery/v22-acl',",
    )
    return _replace_once(text, " 'schema_version', 11", " 'schema_version', 22")


def mint_acl_manifest(src: Path, dst: Path) -> None:
    """Publish the inherited codex proof and the live-only 064 grant declarations."""
    document = json.loads(src.read_text(encoding="utf-8"))
    if document["contract_id"] != "brain-v42/postgresql-recovery/v11-acl":
        raise SystemExit("not the v11 ACL manifest")
    document.update(contract_id="brain-v42/postgresql-recovery/v22-acl", schema_version=22)
    document["checks"][0]["roles"].append("brain_app")
    document["checks"].append(
        {
            "id": CHECK_ID,
            "kind": "migration_runtime_acl_invariant",
            "revision": "064",
            "grants": runtime_grants(),
            "proof_scope": "live-only",
            "restore_rule": "not-applicable-no-owner-no-acl",
        }
    )
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")


async def main(dsn: str, src: Path, dst: Path) -> int:
    """Measure the declared delta read-only before publishing a measured variant."""
    text = mint_sql(src.read_text(), restored="-pgrestore" in src.name)
    connection = await connect(dsn)
    try:
        async with connection.transaction(readonly=True):
            if await connection.fetchval("SELECT version_num FROM public.alembic_version") != "064":
                raise SystemExit("v22 measurement requires head 064")
            receipt = json.loads(await connection.fetchval(text))
            check = next(check for check in receipt["checks"] if check["id"] == CHECK_ID)
            if check["status"] != "pass":
                raise SystemExit(f"064 ACL delta does not match the database: {check}")
    finally:
        await connection.close()
    dst.write_text(text)
    return 0


def mint_all() -> None:
    """Bind digests only after minting the final assets; never edit an old contract."""
    binding = {
        "contract_id": "brain-v42/postgresql-recovery/v22",
        "contract_version": 22,
        "schema_head": "064",
    }
    for suffix, restored in ((".sql", False), ("-pgrestore.sql", True)):
        source = RECOVERY / f"brain-v42-v21{suffix}"
        (RECOVERY / f"brain-v42-v22{suffix}").write_text(
            mint_sql(source.read_text(), restored=restored)
        )
    mint_manifest(RECOVERY / "brain-v42-v21.json", RECOVERY / "brain-v42-v22.json")
    (RECOVERY / "brain-v42-v22-acl.sql").write_text(
        mint_acl_sql((RECOVERY / "brain-v42-v11-acl.sql").read_text())
    )
    mint_acl_manifest(RECOVERY / "brain-v42-v11-acl.json", RECOVERY / "brain-v42-v22-acl.json")
    for key, suffix in (
        ("attestation_sql", ".sql"),
        ("restored_attestation_sql", "-pgrestore.sql"),
        ("manifest", ".json"),
    ):
        asset = RECOVERY / f"brain-v42-v22{suffix}"
        binding[key] = {
            "path": str(asset.relative_to(ROOT)),
            "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
        }
    (RECOVERY / "current.json").write_text(json.dumps(binding, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        mint_all()
    elif sys.argv[1] == "--manifest":
        mint_manifest(
            Path(sys.argv[2]) if len(sys.argv) > 2 else RECOVERY / "brain-v42-v21.json",
            Path(sys.argv[3]) if len(sys.argv) > 3 else RECOVERY / "brain-v42-v22.json",
        )
    else:
        sys.exit(
            asyncio.run(
                main(
                    sys.argv[1],
                    Path(sys.argv[2]) if len(sys.argv) > 2 else RECOVERY / "brain-v42-v21.sql",
                    Path(sys.argv[3]) if len(sys.argv) > 3 else RECOVERY / "brain-v42-v22.sql",
                )
            )
        )
