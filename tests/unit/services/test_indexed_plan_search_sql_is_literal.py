"""Invariant guard for indexed_plan_search_service's `# nosec B608`.

Both of this service's queries are f-strings: bandit flags them (B608) and the
exception placed on the closing lines rests on ONE single invariant — the only
interpolated fragment, ``{where}``, is the join of fragments that are ALL
literals from `_build_where`. Every call value (search text, embedding,
project_key(s), tags) reaches the SQL only through a bind.

These tests fail if anyone makes a clause fragment dynamic (f-string, `%`, `+`,
`.format`, or a parameter passed through), which is exactly the moment the nosec
would stop being justified.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

import pytest

from brain_v42.services import indexed_plan_search_service as mod
from brain_v42.services.indexed_plan_search_service import IndexedPlanSearchService

# Classic injections, plus a value that looks like a valid SQL fragment.
HOSTILE = "x' OR 1=1 --"


def _service() -> IndexedPlanSearchService:
    return IndexedPlanSearchService(session_factory=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Structural invariant: the clauses are literals, not computed text
# ---------------------------------------------------------------------------


def _build_where_ast() -> ast.FunctionDef:
    src = inspect.getsource(mod)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_where":
            return node
    raise AssertionError("_build_where introuvable — le nosec B608 n'a plus de garde")


def test_build_where_only_ever_appends_string_literals() -> None:
    """Every clause fragment must be a literal written in the source.

    Fails if `_build_where` becomes dynamic: `clauses.append(f"...")`,
    `clauses.append("x = " + col)`, `.format(...)`, or appending a parameter.
    """
    fn = _build_where_ast()

    appended: list[ast.expr] = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "clauses"
        ):
            appended.append(node.args[0])

    assert appended, "aucun clauses.append() trouvé — la forme gardée a changé"
    for arg in appended:
        assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
            f"fragment de clause non littéral en ligne {arg.lineno} de _build_where : "
            "le nosec B608 des requêtes FTS/vector n'est plus justifié"
        )

    # The list's initial literal must be one too.
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
            for elt in node.value.elts:
                assert isinstance(elt, ast.Constant) and isinstance(elt.value, str)


# ---------------------------------------------------------------------------
# 2. Behavioural invariant: nothing hostile comes back out in the WHERE
# ---------------------------------------------------------------------------


def test_build_where_never_embeds_caller_values() -> None:
    params: dict[str, Any] = {}
    clauses = _service()._build_where(
        params,
        project_key=HOSTILE,
        project_keys=[HOSTILE],
        tags=[HOSTILE],
        include_drafts=False,
        include_archived=False,
    )

    joined = " AND ".join(clauses)
    assert HOSTILE not in joined, "une valeur d'appel a fui dans le WHERE interpolé"
    assert params["project_key"] == HOSTILE, "la valeur doit voyager par le bind, pas le texte"
    assert params["tags"] == [HOSTILE]

    allowed = {
        "1=1",
        "c.status = 'active'",
        "p.freshness_status != 'archived'",
        "c.project_key = :project_key",
        "c.project_key = ANY(CAST(:pks AS VARCHAR[]))",
        "c.tags && CAST(:tags AS VARCHAR[])",
    }
    assert set(clauses) <= allowed, f"clause hors allowlist : {set(clauses) - allowed}"


def test_build_where_project_keys_branch_also_binds() -> None:
    params: dict[str, Any] = {}
    clauses = _service()._build_where(
        params,
        project_key=None,
        project_keys=[HOSTILE, "brain-v42"],
        tags=None,
        include_drafts=True,
        include_archived=True,
    )
    assert HOSTILE not in " AND ".join(clauses)
    assert params["pks"] == [HOSTILE, "brain-v42"]


# ---------------------------------------------------------------------------
# 3. The user's search text and the embedding go through a BIND
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fts_binds_the_user_search_text() -> None:
    svc = _service()
    captured: dict[str, Any] = {}

    async def fake_execute(sql: str, params: dict[str, Any]) -> list[Any]:
        captured["sql"] = sql
        captured["params"] = params
        return []

    svc._execute_query = fake_execute  # type: ignore[method-assign]

    await svc.search(query=HOSTILE, project_key=HOSTILE, tags=[HOSTILE], limit=5)

    assert HOSTILE not in captured["sql"], "le texte de recherche est interpolé dans le SQL"
    assert captured["params"]["q"] == HOSTILE
    assert "plainto_tsquery('english', :q)" in captured["sql"]


@pytest.mark.asyncio
async def test_vector_binds_the_embedding() -> None:
    svc = _service()
    captured: dict[str, Any] = {}

    async def fake_execute(sql: str, params: dict[str, Any]) -> list[Any]:
        captured["sql"] = sql
        captured["params"] = params
        return []

    svc._execute_query = fake_execute  # type: ignore[method-assign]

    await svc.semantic_search(embedding=[0.5, 0.25], project_keys=[HOSTILE], limit=3)

    assert "0.25" not in captured["sql"], "l'embedding est interpolé dans le SQL"
    assert HOSTILE not in captured["sql"]
    assert captured["params"]["emb"] == "[0.5, 0.25]"
    assert "CAST(:emb AS VECTOR)" in captured["sql"]
