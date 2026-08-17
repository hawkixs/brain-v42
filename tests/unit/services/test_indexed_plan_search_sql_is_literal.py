"""Garde d'invariant du `# nosec B608` de indexed_plan_search_service.

Les deux requêtes de ce service sont des f-strings : bandit les signale (B608) et
l'exception posée sur les lignes de fermeture repose sur UN invariant unique —
le seul fragment interpolé, ``{where}``, est le join de fragments qui sont TOUS
des littéraux de `_build_where`. Toute valeur d'appel (texte de recherche,
embedding, project_key(s), tags) n'atteint le SQL que par un bind.

Ces tests échouent si quelqu'un rend un fragment de clause dynamique (f-string,
`%`, `+`, `.format`, ou un paramètre passé tel quel), c'est-à-dire exactement au
moment où le nosec cesserait d'être justifié.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

import pytest

from brain_v42.services import indexed_plan_search_service as mod
from brain_v42.services.indexed_plan_search_service import IndexedPlanSearchService

# Injections classiques, plus une valeur qui ressemble à un fragment SQL valide.
HOSTILE = "x' OR 1=1 --"


def _service() -> IndexedPlanSearchService:
    return IndexedPlanSearchService(session_factory=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Invariant structurel : les clauses sont des littéraux, pas du texte calculé
# ---------------------------------------------------------------------------


def _build_where_ast() -> ast.FunctionDef:
    src = inspect.getsource(mod)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_where":
            return node
    raise AssertionError("_build_where introuvable — le nosec B608 n'a plus de garde")


def test_build_where_only_ever_appends_string_literals() -> None:
    """Chaque fragment de clause doit être un littéral écrit dans le source.

    Échoue si `_build_where` devient dynamique : `clauses.append(f"...")`,
    `clauses.append("x = " + col)`, `.format(...)`, ou l'append d'un paramètre.
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

    # Le littéral initial de la liste doit l'être aussi.
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
            for elt in node.value.elts:
                assert isinstance(elt, ast.Constant) and isinstance(elt.value, str)


# ---------------------------------------------------------------------------
# 2. Invariant comportemental : rien d'hostile ne ressort dans le WHERE
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
# 3. Le texte de recherche utilisateur et l'embedding passent par un BIND
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
