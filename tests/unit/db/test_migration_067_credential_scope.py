"""Credential scope must not add a function needing its own runtime ACL grant."""

from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from brain_v42.db.tables import brain_client_credentials

ROOT = Path(__file__).resolve().parents[3]


def _migration() -> ModuleType:
    path = next((ROOT / "alembic/versions").glob("*_credential_scope.py"))
    spec = importlib.util.spec_from_file_location("credential_scope", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scope_upgrade_uses_an_inline_check_without_creating_a_function() -> None:
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        _migration().upgrade()
    sql = output.getvalue()
    assert "CREATE FUNCTION" not in sql
    assert "array_position(allowed_projects, NULL) IS NULL" in sql
    assert "cardinality(allowed_projects) >= 1" in sql
    assert "cardinality(string_to_array(array_to_string(allowed_projects, ','), ','))" in sql


def test_scope_downgrade_does_not_drop_a_function(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration()
    operations = MagicMock()
    operations.get_bind.return_value.execute.return_value.scalar_one.return_value = 0
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration.context, "get_x_argument", lambda **kw: {})
    migration.downgrade()
    assert operations.execute.call_count == 0


def test_scope_metadata_and_migration_declare_the_same_inline_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration()
    operations = MagicMock()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    check = next(
        constraint
        for constraint in brain_client_credentials.constraints
        if isinstance(constraint, sa.CheckConstraint)
        and constraint.name == "brain_client_credentials_allowed_projects_valid"
    )
    expression = operations.create_check_constraint.call_args_list[0].args[2]
    assert str(check.sqltext) == expression
    assert "brain_credential_projects_valid" not in expression
