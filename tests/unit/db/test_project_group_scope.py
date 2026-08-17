"""SQL contract for project-group mutation scoping."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from brain_v42.db.project_group_scope import project_key_in_group, ticket_in_group
from brain_v42.db.tables import features, tickets


def _sql(expression) -> str:
    return str(expression.compile(dialect=postgresql.dialect()))


def test_project_scope_uses_registry_group_and_colon_base_semantics() -> None:
    statement = features.update().where(project_key_in_group(features.c.project_key, "red"))
    sql = _sql(statement)

    assert "project_contexts" in sql
    assert "project_group" in sql
    assert " LIKE " in sql
    assert " NOT LIKE " in sql
    assert "features.project_key" in sql


def test_ticket_scope_accepts_either_participant() -> None:
    statement = tickets.update().where(
        ticket_in_group(tickets.c.from_project, tickets.c.to_project, "red")
    )
    sql = _sql(statement)

    assert "tickets.from_project" in sql
    assert "tickets.to_project" in sql
    assert " OR " in sql


def test_project_scope_is_composable_without_cte_name_collisions() -> None:
    statement = sa.select(features.c.id).where(
        project_key_in_group(features.c.project_key, "red"),
        project_key_in_group(features.c.project_key, "other"),
    )

    sql = _sql(statement)

    assert sql.count("EXISTS") == 2
    assert "WITH project_group_base" not in sql


@pytest.mark.parametrize("project_group", [None, "", "   "])
def test_project_scope_refuses_an_empty_group(project_group) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        project_key_in_group(features.c.project_key, project_group)
