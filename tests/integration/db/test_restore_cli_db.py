"""Restore a real head archive, including migration 064's runtime-role grants.

Roles are cluster-wide: even a disposable database shares them with production
on the developer cluster. This entire module skips before fixture setup unless
BRAIN_V42_TEST_DISPOSABLE_CLUSTER=1 explicitly authorizes role mutations. Set
that flag only for a throwaway cluster, as the CI integration job does.

The CLI intentionally targets only the production database name. This test
redirects its connection and database comment to a generated disposable name;
pg_dump, pg_restore, role bootstrap and ACL replay all run without substitution.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

from brain_v42.scripts import restore_cli
from tests.integration.disposable_db import create_database, drop_database, repository_head

pytestmark = pytest.mark.skipif(
    os.environ.get("BRAIN_V42_TEST_DISPOSABLE_CLUSTER") != "1",
    reason="Cluster role mutations require BRAIN_V42_TEST_DISPOSABLE_CLUSTER=1",
)

_GRANTS_SQL = """
WITH object_acls AS (
    SELECT 'schema' AS kind, nspname AS name, nspacl AS acl
    FROM pg_catalog.pg_namespace WHERE nspname = 'public'
    UNION ALL
    SELECT 'relation', c.relkind::text || ':' || c.relname, c.relacl
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'function', p.oid::regprocedure::text, p.proacl
    FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
    UNION ALL
    SELECT 'default', r.rolname || ':' || COALESCE(n.nspname, '') || ':' || d.defaclobjtype::text,
           d.defaclacl
    FROM pg_catalog.pg_default_acl d
    JOIN pg_catalog.pg_roles r ON r.oid = d.defaclrole
    LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
)
SELECT COALESCE(json_agg(json_build_array(kind, name, a.privilege_type, a.is_grantable)
                       ORDER BY kind, name, a.privilege_type, a.is_grantable), '[]'::json)
FROM object_acls
CROSS JOIN LATERAL pg_catalog.aclexplode(acl) a
JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
WHERE r.rolname = 'brain_app';
"""


def test_head_archive_restores_runtime_grants_into_empty_database(
    migration_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for executable in ("pg_dump", "pg_restore", "psql"):
        assert shutil.which(executable), f"PostgreSQL 16 client required: {executable}"

    url = make_url(migration_database_url)
    # Reuse the real credential handling without permitting the CLI to connect
    # to 'brain'. Every executed connection below names a disposable database.
    monkeypatch.setattr(
        restore_cli,
        "Settings",
        lambda: SimpleNamespace(postgres_url=url.set(database="brain")),
    )
    connection, env = restore_cli._connection()
    database_index = connection.index("--dbname") + 1
    assert url.database and url.database.startswith("brain_migration_")
    connection[database_index] = url.database
    head = repository_head()
    assert int(head) >= 64, "The archive must include migration 064's brain_app grants"
    assert (
        restore_cli._sql("SELECT version_num FROM public.alembic_version;", connection, env) == head
    )
    expected_grants = json.loads(restore_cli._sql(_GRANTS_SQL, connection, env))
    assert ["relation", "r:decisions", "SELECT", False] in expected_grants

    dump = tmp_path / "head.dump"
    subprocess.run(
        ["pg_dump", "--format=custom", *connection, "--file", str(dump)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    Path(str(dump) + ".sha256").write_text(digest + "\n", encoding="utf-8")

    database = f"brain_restore_{uuid4().hex[:12]}"
    create_database(migration_database_url, database)
    try:
        target = connection.copy()
        target[database_index] = database
        assert json.loads(restore_cli._sql(restore_cli._STATE_SQL, target, env))["relations"] == []
        real_sql = restore_cli._sql

        def disposable_sql(statement: str, args: list[str], environ: dict[str, str]) -> str:
            statement = statement.replace(
                "COMMENT ON DATABASE brain IS", f'COMMENT ON DATABASE "{database}" IS'
            )
            return real_sql(statement, args, environ)

        monkeypatch.setattr(restore_cli, "_connection", lambda: (target, env))
        monkeypatch.setattr(restore_cli, "_sql", disposable_sql)
        assert restore_cli.main(["--dump", str(dump)]) == 0
        assert real_sql("SELECT version_num FROM public.alembic_version;", target, env) == head
        assert json.loads(real_sql(_GRANTS_SQL, target, env)) == expected_grants
        marker = json.loads(real_sql(restore_cli._STATE_SQL, target, env))["marker"]
        assert marker.startswith(f"brain-v42-restore sha256={digest} at=")
        assert restore_cli.main(["--dump", str(dump)]) == 0
    finally:
        drop_database(migration_database_url, database)
