"""Cluster role benches must opt in before any database fixture can run."""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MODULES = ("test_migrate_cli_db", "test_migration_064_runtime_role")


def _load(name: str):
    path = ROOT / "tests/integration/db" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", MODULES)
@pytest.mark.parametrize("opt_in", [None, "0", "true", "1"])
def test_role_benches_require_exact_disposable_cluster_opt_in(monkeypatch, name, opt_in):
    if opt_in is None:
        monkeypatch.delenv("BRAIN_V42_TEST_DISPOSABLE_CLUSTER", raising=False)
    else:
        monkeypatch.setenv("BRAIN_V42_TEST_DISPOSABLE_CLUSTER", opt_in)
    module = _load(name)
    marks = module.pytestmark
    if not isinstance(marks, list):
        marks = [marks]
    guards = [mark for mark in marks if mark.name == "skipif"]
    assert len(guards) == 1, "role mutations need a module guard before fixtures run"
    assert guards[0].args == (opt_in != "1",)
    assert "BRAIN_V42_TEST_DISPOSABLE_CLUSTER=1" in guards[0].kwargs["reason"]
    assert "BRAIN_V42_TEST_DISPOSABLE_CLUSTER=1" in module.__doc__


def test_only_throwaway_integration_ci_opts_into_cluster_role_mutations():
    workflow = yaml.safe_load((ROOT / ".github/workflows/continuous-integration.yml").read_text())
    assert workflow["jobs"]["test-integration"]["env"]["BRAIN_V42_TEST_DISPOSABLE_CLUSTER"] == "1"
    assert "BRAIN_V42_TEST_DISPOSABLE_CLUSTER" not in workflow["env"]


def test_cli_bench_reads_role_settings_from_the_roles_catalog(monkeypatch, tmp_path):
    module = _load("test_migrate_cli_db")
    settings_read = []

    def query(url, sql):
        if "rolpassword" in sql:
            assert "rolconfig FROM pg_authid" not in sql, "pg_authid has no rolconfig column"
            assert "pg_roles" in sql, "role settings belong to pg_roles"
            settings_read.append(sql)
            return [{"rolpassword": None, "rolconnlimit": -1, "rolconfig": None}]
        if "rolsuper" in sql:
            return [{"present": 1}]
        return []

    @contextmanager
    def database(url, **kwargs):
        yield url

    monkeypatch.setattr(module, "_query", query)
    monkeypatch.setattr(module, "fresh_head_database", database)
    monkeypatch.setattr(module, "run_sql", lambda *args: None)
    fixture = module.runtime.__wrapped__("postgresql://test", tmp_path, monkeypatch)
    try:
        next(fixture)
        assert len(settings_read) == 1
    finally:
        fixture.close()


def test_064_downgrade_guards_every_role_operation_when_role_is_missing(monkeypatch):
    path = ROOT / "alembic/versions/064_brain_app_runtime_role.py"
    spec = importlib.util.spec_from_file_location("runtime_role_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    statements = []
    monkeypatch.setattr(migration.op, "execute", statements.append)
    migration.downgrade()
    for statement in statements:
        guard = "IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'brain_app')"
        assert guard in statement, "downgrade must tolerate a missing cluster role"
        assert statement.index(guard) < statement.index("REVOKE")
