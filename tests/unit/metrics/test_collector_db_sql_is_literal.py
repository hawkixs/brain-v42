"""Garde d'invariant des deux `# nosec B608` de ``metrics/collector_db.py``.

Deux requêtes y sont construites par f-string, et bandit les signale (B608) :

* ``collect_process_metrics`` (ligne 142) interpole ``PROCESS_METRICS_IS_LIVE_SQL``
  et ``PROCESS_METRICS_FRESH_SQL`` — deux constantes importées de
  ``metrics/retention.py``, elles-mêmes figées sur des entiers littéraux ;
* ``collect_graph_inventory`` (ligne 328) interpole ``table``, clé du dict
  littéral de module ``_PG_LABEL_MAP``.

Aucun des deux fragments ne peut venir d'un appelant. Ces tests le prouvent, et
échouent si l'une des constantes devient dynamique — c'est-à-dire à l'instant où
les exceptions cesseraient d'être justifiées.
"""

from __future__ import annotations

import ast
import inspect
import re

from brain_v42.metrics import collector_db as mod
from brain_v42.metrics import retention as retention_mod
from brain_v42.metrics.collector_db import _PG_LABEL_MAP

# Un nom de table PostgreSQL non cité, tel qu'écrit en dur dans le source.
_BARE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

# Aucun caractère hors de ce jeu ne peut apparaître dans un prédicat figé sur un int.
_FROZEN_PREDICATE = re.compile(r"^\(?updated_at [<>] NOW\(\) - INTERVAL '\d+ seconds'\)?$")


def _function_ast(name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} introuvable — le nosec B608 n'a plus de garde")


def _holes(fn: ast.AST) -> list[ast.FormattedValue]:
    return [
        part
        for node in ast.walk(fn)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
        if isinstance(part, ast.FormattedValue)
    ]


def test_pg_label_map_is_a_module_dict_of_string_literals() -> None:
    """Les noms de table interpolés en ligne 328 sont écrits en dur dans le source.

    Échoue sur un `dict(...)`, une compréhension, une clé construite, ou une clé
    qui ne serait plus un identifiant nu — chacun rendant `{table}` injectable.
    """
    tree = ast.parse(inspect.getsource(mod))
    assigns = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign | ast.Assign) and _targets(node) == ["_PG_LABEL_MAP"]
    ]
    assert len(assigns) == 1, "`_PG_LABEL_MAP` doit avoir une définition unique au module"
    value = assigns[0].value
    assert isinstance(value, ast.Dict), (
        "`_PG_LABEL_MAP` n'est plus un dict littéral : le nosec B608 est injustifié"
    )
    for key in value.keys:
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            "clé non littérale dans `_PG_LABEL_MAP` : nosec B608 injustifié"
        )
        assert _BARE_IDENTIFIER.match(key.value), (
            f"nom de table interpolable non conforme : {key.value!r}"
        )

    # Le même invariant, vu à l'exécution : rien ne mute la constante à l'import.
    assert list(_PG_LABEL_MAP) == ["decisions", "learnings", "snippets", "runbooks", "adrs"]


def _targets(node: ast.AnnAssign | ast.Assign) -> list[str]:
    targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
    return [t.id for t in targets if isinstance(t, ast.Name)]


def test_graph_inventory_interpolates_only_the_literal_table_key() -> None:
    """`table` ne peut être lié que par la boucle sur `_PG_LABEL_MAP.items()`."""
    fn = _function_ast("collect_graph_inventory")

    loops = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Tuple):
            names = [e.id for e in node.target.elts if isinstance(e, ast.Name)]
            if "table" not in names:
                continue
            iterated = node.iter
            assert isinstance(iterated, ast.Call)
            assert isinstance(iterated.func, ast.Attribute) and iterated.func.attr == "items"
            assert (
                isinstance(iterated.func.value, ast.Name)
                and iterated.func.value.id == "_PG_LABEL_MAP"
            ), f"ligne {node.lineno} : `table` itère autre chose que le dict littéral"
            loops += 1
        if isinstance(node, ast.Assign):
            assert not any(isinstance(t, ast.Name) and t.id == "table" for t in node.targets), (
                f"ligne {node.lineno} : `table` est affecté hors boucle — nosec B608 injustifié"
            )
    assert loops == 1, f"attendu 1 boucle sur `_PG_LABEL_MAP`, trouvé {loops}"

    holes = _holes(fn)
    assert holes, "plus aucune f-string dans collect_graph_inventory — retirer le nosec B608"
    for hole in holes:
        assert isinstance(hole.value, ast.Name) and hole.value.id == "table", (
            f"ligne {hole.lineno} : fragment interpolé autre que `table` — non couvert"
        )

    # `graph_svc`, le seul paramètre, est un service : il ne doit jamais toucher au SQL.
    assert [a.arg for a in fn.args.args] == ["self", "graph_svc"]


def test_process_metrics_interpolates_only_the_two_retention_constants() -> None:
    """Les deux trous de la requête ligne 142 sont des constantes de module importées."""
    fn = _function_ast("collect_process_metrics")

    assert [a.arg for a in fn.args.args] == ["self"], (
        "collect_process_metrics a gagné un paramètre : le nosec B608 affirmait "
        "qu'aucune valeur d'appel ne pouvait atteindre le SQL interpolé"
    )

    holes = _holes(fn)
    interpolated = []
    for hole in holes:
        assert isinstance(hole.value, ast.Name), (
            f"ligne {hole.lineno} : expression interpolée au lieu d'un nom de constante"
        )
        interpolated.append(hole.value.id)
    assert interpolated == [
        "PROCESS_METRICS_IS_LIVE_SQL",
        "PROCESS_METRICS_FRESH_SQL",
    ], f"fragments interpolés inattendus : {interpolated}"

    # Ces deux noms sont bien importés de retention, pas des locales reconstruites.
    tree = ast.parse(inspect.getsource(mod))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "brain_v42.metrics.retention"
        for alias in node.names
    }
    assert set(interpolated) <= imported, (
        "un fragment n'est plus la constante importée de metrics/retention.py"
    )


def test_retention_predicates_are_frozen_on_integer_literals() -> None:
    """Les constantes interpolées ne portent qu'un entier, jamais une chaîne d'entrée.

    Échoue si quelqu'un fabrique l'intervalle depuis l'environnement ou depuis un
    paramètre : la valeur rendue cesserait d'être un prédicat figé, et le nosec
    B608 de ``collect_process_metrics`` deviendrait un vrai défaut.
    """
    assert _FROZEN_PREDICATE.match(retention_mod.PROCESS_METRICS_FRESH_SQL), (
        f"prédicat de lecture inattendu : {retention_mod.PROCESS_METRICS_FRESH_SQL!r}"
    )
    assert _FROZEN_PREDICATE.match(retention_mod.PROCESS_METRICS_IS_LIVE_SQL), (
        f"prédicat de vivacité inattendu : {retention_mod.PROCESS_METRICS_IS_LIVE_SQL!r}"
    )

    tree = ast.parse(inspect.getsource(retention_mod))
    int_literals = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert int_literals == {
        "PROCESS_METRICS_RETENTION_SECONDS": 3600,
        "PROCESS_METRICS_LIVE_SECONDS": 60,
    }

    for name in ("PROCESS_METRICS_FRESH_SQL", "PROCESS_METRICS_IS_LIVE_SQL"):
        assign = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        )
        holes = _holes(assign)
        assert holes, f"{name} n'interpole plus rien — la forme gardée a changé"
        for hole in holes:
            assert isinstance(hole.value, ast.Name) and hole.value.id in int_literals, (
                f"{name} interpole {ast.dump(hole.value)} au lieu d'un entier littéral : "
                "le nosec B608 de collect_process_metrics n'est plus justifié"
            )
