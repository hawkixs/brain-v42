"""Invariant guard for PgIndexedPlanRepo.list_plans' `# nosec B608`.

`list_plans` builds its WHERE clause by f-string: bandit flags it (B608) and the
exception placed on the closing line rests on ONE invariant — the only
interpolated fragment, ``where``, is the join of fragments that are ALL literals,
written just above in the same function. project_key, plan_type, status, limit
and offset reach the SQL only through binds.

These tests fail if anyone makes a clause fragment dynamic, the exact moment the
nosec would stop being justified.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.repositories import pg_indexed_plan_repo as mod
from brain_v42.repositories.pg_indexed_plan_repo import PgIndexedPlanRepo

HOSTILE = "x' OR 1=1 --"


def _list_plans_ast() -> ast.AsyncFunctionDef:
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "list_plans":
            return node
    raise AssertionError("list_plans introuvable — le nosec B608 n'a plus de garde")


def test_list_plans_where_fragments_are_string_literals() -> None:
    """Every fragment of `clauses` must be a literal written in the source.

    Fails on `clauses.append(f"{col} = ...")`, a concatenation, a `.format(...)`
    or appending one of the function's parameters.
    """
    fn = _list_plans_ast()

    literals: list[str] = []

    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "clauses" in targets:
                for elt in node.value.elts:
                    assert isinstance(elt, ast.Constant) and isinstance(elt.value, str), (
                        "élément initial de `clauses` non littéral : nosec B608 injustifié"
                    )
                    literals.append(elt.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "clauses"
        ):
            arg = node.args[0]
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
                f"fragment de clause non littéral en ligne {arg.lineno} de list_plans : "
                "le nosec B608 de la requête n'est plus justifié"
            )
            literals.append(arg.value)

    assert literals, "aucun fragment de clause trouvé — la forme gardée a changé"
    assert set(literals) == {
        "1=1",
        "project_key = :project_key",
        "plan_type = :plan_type",
        "status = :status",
    }


@pytest.mark.asyncio
async def test_list_plans_binds_filters_instead_of_interpolating() -> None:
    session = MagicMock()
    result = MagicMock()
    result.mappings.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)

    repo = PgIndexedPlanRepo(session)
    await repo.list_plans(
        project_key=HOSTILE,
        plan_type=HOSTILE,
        status=HOSTILE,
        limit=5,
        offset=0,
    )

    sql_arg, params = session.execute.await_args.args
    rendered = str(sql_arg)
    assert HOSTILE not in rendered, "un filtre d'appel a fui dans le SQL interpolé"

    bound: dict[str, Any] = dict(params)
    assert bound["project_key"] == HOSTILE
    assert bound["plan_type"] == HOSTILE
    assert bound["status"] == HOSTILE
    for name in (":project_key", ":plan_type", ":status", ":limit", ":offset"):
        assert name in rendered
