"""Exercise owner migrations and runtime grants on a disposable database only.

Requires BRAIN_V42_TEST_DISPOSABLE_CLUSTER=1: LOGIN, passwords and role settings
are cluster-wide, even when the database itself is disposable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from brain_v42.config import Settings
from brain_v42.db.engine import pg_server_settings
from brain_v42.scripts.migrate_cli import main
from tests.integration.disposable_db import (
    asyncpg_dsn,
    fresh_head_database,
    repository_head,
    run_sql,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("BRAIN_V42_TEST_DISPOSABLE_CLUSTER") != "1",
        reason="cluster role mutations require BRAIN_V42_TEST_DISPOSABLE_CLUSTER=1",
    ),
]
ROOT = Path(__file__).parents[3]


def _query(url: str, sql: str) -> list[dict[str, Any]]:
    async def query() -> list[dict[str, Any]]:
        connection = await asyncpg.connect(asyncpg_dsn(url))
        try:
            return [dict(row) for row in await connection.fetch(sql)]
        finally:
            await connection.close()

    return asyncio.run(query())


def _write_schema_probe(directory: Path) -> None:
    """A small real SQL proof isolates CLI mechanics from corpus recovery requirements."""
    directory.mkdir()
    contract_id = "test/migrate-cli/schema"
    manifest = {"contract_id": contract_id, "schema_version": 1, "checks": [{"id": "head"}]}
    sql = (
        "SELECT jsonb_build_object('contract_id', 'test/migrate-cli/schema', "
        "'schema_version', 1, 'checks', jsonb_build_array(jsonb_build_object("
        "'id', 'head', 'status', CASE WHEN (SELECT array_agg(version_num::text) "
        f"FROM public.alembic_version) = ARRAY['{repository_head()}']::text[] "
        "THEN 'pass' ELSE 'fail' END)))::text"
    )
    binding: dict[str, Any] = {
        "contract_id": contract_id,
        "contract_version": 1,
        "schema_head": repository_head(),
        "release_sha": "a" * 40,
    }
    for key, name, content in (
        ("manifest", "manifest.json", json.dumps(manifest)),
        ("attestation_sql", "live.sql", sql),
        ("restored_attestation_sql", "restored.sql", sql),
    ):
        path = directory / name
        path.write_text(content)
        binding[key] = {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (directory / "recovery-binding.json").write_text(json.dumps(binding))


@pytest.fixture
def runtime(
    migration_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, list[str], Path]]:
    # Migration 064 has already created the shared cluster role. Only configure
    # an inactive NOLOGIN test role; never rotate an operator's LOGIN credential.
    if _query(
        migration_database_url,
        "SELECT 1 FROM pg_roles WHERE rolname = 'brain_app' AND rolcanlogin",
    ):
        pytest.skip("brain_app is an active LOGIN role; refusing to mutate it")
    if not _query(
        migration_database_url,
        "SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper",
    ):
        pytest.skip("runtime role integration requires a disposable superuser test cluster")
    original = _query(
        migration_database_url,
        "SELECT a.rolpassword, r.rolconnlimit, r.rolconfig FROM pg_catalog.pg_authid a "
        "JOIN pg_catalog.pg_roles r ON r.oid = a.oid WHERE r.rolname = 'brain_app'",
    )[0]
    password_file = tmp_path / "password"
    password_file.write_text("integration-only-password-'\\")
    recovery = tmp_path / "recovery"
    _write_schema_probe(recovery)
    monkeypatch.setenv("BRAIN_APP_PASSWORD_FILE", str(password_file))
    monkeypatch.delenv("BRAIN_APP_GRANT_PG_CONTROL_SYSTEM", raising=False)
    try:
        with fresh_head_database(migration_database_url, prefix="brain_migrate_cli") as url:
            monkeypatch.setenv("POSTGRES_URL", url)
            yield url, ["--recovery-dir", str(recovery)], password_file
    finally:
        # The session fixture's database still has ACL dependencies on brain_app.
        # Restore attributes instead of dropping the role or leaving it LOGIN.
        verifier = original["rolpassword"]
        quoted = "NULL" if verifier is None else "'" + verifier.replace("'", "''") + "'"
        statements = [
            f"ALTER ROLE brain_app NOLOGIN CONNECTION LIMIT {original['rolconnlimit']} "
            f"PASSWORD {quoted}",
            "ALTER ROLE brain_app RESET ALL",
        ]
        for setting in original["rolconfig"] or []:
            name, value = setting.split("=", 1)
            name = name.replace('"', '""')
            value = value.replace("'", "''")
            statements.append(f"ALTER ROLE brain_app SET \"{name}\" = '{value}'")
        run_sql(asyncpg_dsn(migration_database_url), statements)


def test_equal_head_role_grants_future_defaults_and_password_rotation(runtime, capsys) -> None:
    url, arguments, password_file = runtime
    assert main(arguments) == 0
    role = _query(
        url,
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
        "rolbypassrls, rolconnlimit, rolconfig FROM pg_roles WHERE rolname = 'brain_app'",
    )[0]
    assert role["rolcanlogin"] is True
    for flag in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"):
        assert role[flag] is False
    expected = pg_server_settings(Settings(postgres_url=url), "interactive")
    for name, value in expected.items():
        if name != "application_name":
            assert f"{name}={value}" in role["rolconfig"]
    assert _query(url, "SELECT * FROM public.brain_schema_compat") == []
    run_sql(
        asyncpg_dsn(url),
        [
            "CREATE TABLE public.migrate_future (id bigserial PRIMARY KEY, value text)",
        ],
    )
    for schema in ("public",):
        privileges = _query(
            url,
            f"SELECT has_schema_privilege('brain_app', '{schema}', 'USAGE') AS schema_usage, "
            f"has_table_privilege('brain_app', '{schema}.migrate_future', "
            "'SELECT,INSERT,UPDATE,DELETE') AS table_dml, "
            f"has_sequence_privilege('brain_app', '{schema}.migrate_future_id_seq', "
            "'USAGE,SELECT') AS sequence_usage",
        )[0]
        assert all(privileges.values())
    old_verifier = _query(url, "SELECT rolpassword FROM pg_authid WHERE rolname = 'brain_app'")[0]
    password_file.write_text("integration-only-rotated-password")
    assert main(arguments) == 0
    new_verifier = _query(url, "SELECT rolpassword FROM pg_authid WHERE rolname = 'brain_app'")[0]
    rotated = old_verifier != new_verifier
    assert rotated
    output = capsys.readouterr()
    assert "integration-only-password" not in output.out + output.err
    assert "integration-only-rotated-password" not in output.out + output.err


def test_ancestor_upgrade_records_head_pinned_compatibility(runtime) -> None:
    url, arguments, _ = runtime
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "downgrade", "062"],
        env={**os.environ, "POSTGRES_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "disposable database downgrade failed"
    assert main(arguments) == 0
    compat = _query(
        url, "SELECT schema_head, oldest_compatible_code_head FROM public.brain_schema_compat"
    )
    assert compat == [
        {"schema_head": repository_head(), "oldest_compatible_code_head": repository_head()}
    ]


def test_unknown_database_head_refuses_without_altering_role(runtime, capsys) -> None:
    url, arguments, _ = runtime
    attributes = (
        "SELECT rolcanlogin, rolconfig, rolconnlimit FROM pg_roles WHERE rolname = 'brain_app'"
    )
    before = _query(url, attributes)
    run_sql(asyncpg_dsn(url), ["UPDATE public.alembic_version SET version_num = 'unknown-head'"])
    assert main(arguments) == 1
    assert _query(url, attributes) == before
    assert _query(url, "SELECT version_num FROM public.alembic_version") == [
        {"version_num": "unknown-head"}
    ]
    output = capsys.readouterr()
    assert "unknown-head" in output.out + output.err
    assert repository_head() in output.out + output.err


def test_cli_leaves_migration_derived_function_acls_and_defaults_unchanged(runtime) -> None:
    url, arguments, _ = runtime
    inventory = (
        "SELECT 'function' AS kind, oid::text AS id, proacl::text AS acl "
        "FROM pg_proc WHERE pronamespace = 'public'::regnamespace UNION ALL "
        "SELECT 'default', oid::text, defaclacl::text FROM pg_default_acl ORDER BY kind, id"
    )
    before = _query(url, inventory)
    assert main(arguments) == 0
    assert _query(url, inventory) == before
