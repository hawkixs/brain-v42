"""Runtime ACLs are reproducible from migrations, on disposable databases only.

Requires BRAIN_V42_TEST_DISPOSABLE_CLUSTER=1: creating or dropping brain_app
affects the entire cluster, including databases outside these fixtures.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.integration.disposable_db import asyncpg_dsn, fresh_head_database, run_sql

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("BRAIN_V42_TEST_DISPOSABLE_CLUSTER") != "1",
        reason="cluster role mutations require BRAIN_V42_TEST_DISPOSABLE_CLUSTER=1",
    ),
]
ROOT = Path(__file__).parents[3]


def _alembic(url: str, action: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), action, revision],
        env={**os.environ, "POSTGRES_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "disposable database migration failed"


def _query(url: str, sql: str, *, runtime: bool = False) -> list[dict[str, Any]]:
    async def query() -> list[dict[str, Any]]:
        connection = await asyncpg.connect(asyncpg_dsn(url))
        try:
            if runtime:
                await connection.execute("SET ROLE brain_app")
            return [dict(row) for row in await connection.fetch(sql)]
        finally:
            await connection.close()

    return asyncio.run(query())


@pytest.fixture
def role_database(migration_database_url: str) -> Iterator[str]:
    # SET ROLE avoids changing a cluster-wide runtime password or LOGIN attribute.
    if not _query(
        migration_database_url,
        "SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper",
    ):
        pytest.skip("runtime ACL integration requires a superuser test cluster")
    with fresh_head_database(migration_database_url, prefix="brain_runtime_acl") as url:
        yield url


def test_runtime_can_insert_bigserial_and_outbox_and_execute_direct_helpers(
    role_database: str,
) -> None:
    url = role_database
    # register_referenced_project(text) is called by LegacyGraphStore. Outbox,
    # lease and notification code otherwise uses DML, builtins and row triggers.
    _query(url, "SELECT public.register_referenced_project('runtime-acl-test')", runtime=True)
    assert _query(
        url,
        "SELECT public.vector_dims('[3,4]'::public.vector) AS dims, "
        "public.vector_norm('[3,4]'::public.vector) AS norm",
        runtime=True,
    ) == [{"dims": 2, "norm": 5.0}]
    inserted = _query(
        url,
        "INSERT INTO public.graph_outbox (entity_id, aggregate_revision, operation) "
        "SELECT id, revision + 1, 'upsert_entity' FROM public.brain_entities "
        "WHERE entity_type = 'project' AND entity_key = 'runtime-acl-test' RETURNING id",
        runtime=True,
    )
    assert len(inserted) == 1 and inserted[0]["id"] > 0
    audit = _query(
        url,
        "INSERT INTO public.brain_credential_audit (event, payload) "
        "VALUES ('credentials.issued', '{}') RETURNING id",
        runtime=True,
    )
    assert len(audit) == 1


def test_owner_future_tables_and_sequences_have_runtime_dml_defaults(role_database: str) -> None:
    url = role_database
    run_sql(
        asyncpg_dsn(url),
        ["CREATE TABLE public.runtime_future (id bigserial PRIMARY KEY, value text)"],
    )
    inserted = _query(
        url,
        "INSERT INTO public.runtime_future (value) VALUES ('before') RETURNING id",
        runtime=True,
    )
    assert inserted == [{"id": 1}]
    assert _query(
        url, "UPDATE public.runtime_future SET value = 'after' RETURNING value", runtime=True
    ) == [{"value": "after"}]
    assert _query(url, "SELECT value FROM public.runtime_future", runtime=True) == [
        {"value": "after"}
    ]
    assert _query(url, "DELETE FROM public.runtime_future RETURNING id", runtime=True) == [
        {"id": 1}
    ]
    forbidden = _query(
        url,
        "SELECT has_table_privilege('brain_app', 'public.runtime_future', 'TRUNCATE') AS t, "
        "has_table_privilege('brain_app', 'public.runtime_future', 'REFERENCES') AS r, "
        "has_table_privilege('brain_app', 'public.runtime_future', 'TRIGGER') AS g, "
        "has_sequence_privilege('brain_app', 'public.runtime_future_id_seq', 'UPDATE') AS u",
    )[0]
    assert not any(forbidden.values())


@pytest.mark.parametrize(
    "sql", ["CREATE TABLE public.runtime_forbidden (id int)", "SELECT * FROM pg_catalog.pg_authid"]
)
def test_runtime_cannot_create_tables_or_read_auth_catalog(role_database: str, sql: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        _query(role_database, sql, runtime=True)


def test_downgrade_revokes_acl_and_defaults_without_dropping_shared_role(
    role_database: str,
) -> None:
    url = role_database
    run_sql(
        asyncpg_dsn(url),
        ["CREATE TABLE public.runtime_future (id bigserial PRIMARY KEY, value text)"],
    )
    _alembic(url, "downgrade", "063")
    # The session fixture's other database still depends on the same cluster role.
    assert _query(url, "SELECT 1 AS present FROM pg_roles WHERE rolname = 'brain_app'") == [
        {"present": 1}
    ]
    assert (
        _query(
            url,
            "SELECT 1 FROM pg_shdepend WHERE dbid = (SELECT oid FROM pg_database "
            "WHERE datname = current_database()) AND refclassid = 'pg_authid'::regclass "
            "AND refobjid = (SELECT oid FROM pg_roles WHERE rolname = 'brain_app')",
        )
        == []
    )
    run_sql(asyncpg_dsn(url), ["CREATE TABLE public.runtime_after_down (id bigserial)"])
    assert _query(
        url,
        "SELECT has_table_privilege('brain_app', 'public.runtime_after_down', 'INSERT') AS ok",
    ) == [{"ok": False}]
    _alembic(url, "upgrade", "064")


def test_upgrade_reuses_existing_cluster_role_without_changing_attributes(
    role_database: str,
) -> None:
    url = role_database
    attributes = "SELECT rolcanlogin, rolsuper, rolconnlimit, rolconfig FROM pg_roles "
    attributes += "WHERE rolname = 'brain_app'"
    before = _query(url, attributes)
    _alembic(url, "downgrade", "063")
    assert _query(url, attributes) == before
    _alembic(url, "upgrade", "064")
    assert _query(url, attributes) == before
    _query(url, "SELECT public.register_referenced_project('existing-role-test')", runtime=True)
