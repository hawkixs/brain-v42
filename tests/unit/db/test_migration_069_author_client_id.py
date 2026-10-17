"""Author columns have credential grammar and a fail-closed downgrade."""

import importlib.util
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from brain_v42.db.tables import METADATA

TABLES = ("decisions", "learnings", "snippets", "runbooks", "adrs", "focus_slots")
ROOT = Path(__file__).resolve().parents[3]


def migration():
    path = next((ROOT / "alembic/versions").glob("069_*.py"))
    spec = importlib.util.spec_from_file_location("author_client_id", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_only_six_nullable_columns_and_format_constraints():
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    module = migration()
    assert (module.revision, module.down_revision) == ("069", "068")
    with Operations.context(context):
        module.upgrade()
    sql = output.getvalue()
    assert sql.count("ADD COLUMN author_client_id TEXT") == 6
    assert "CREATE INDEX" not in sql and "UPDATE " not in sql
    for table in TABLES:
        column = METADATA.tables[table].c.author_client_id
        assert column.nullable and isinstance(column.type, sa.Text)
        assert column.server_default is None
        constraint = next(
            c
            for c in METADATA.tables[table].constraints
            if c.name == f"{table}_author_client_id_format"
        )
        assert str(constraint.sqltext) in sql


@pytest.mark.parametrize("count", [0, 1])
@pytest.mark.parametrize("opt_in", [None, "no", "yes"])
def test_downgrade_checks_all_authors_before_any_ddl(monkeypatch, count, opt_in):
    module = migration()
    operations = MagicMock()
    operations.get_bind.return_value.execute.return_value.scalar_one.return_value = count
    monkeypatch.setattr(module, "op", operations)
    monkeypatch.setattr(
        module.context, "get_x_argument", lambda **kw: {"allow_author_client_id_downgrade": opt_in}
    )
    if count and opt_in != "yes":
        with pytest.raises(RuntimeError, match="allow_author_client_id_downgrade"):
            module.downgrade()
        operations.drop_column.assert_not_called()
        operations.drop_constraint.assert_not_called()
    else:
        module.downgrade()
        assert operations.drop_column.call_count == 6
    statements = " ".join(
        str(c.args[0]) for c in operations.get_bind.return_value.execute.call_args_list
    )
    for table in TABLES:
        assert table in statements
    assert "author_client_id IS NOT NULL" in statements
