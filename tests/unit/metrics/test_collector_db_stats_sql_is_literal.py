"""Garde d'invariant des deux `# nosec B608` de ``MetricsCollector.collect_db_stats``.

``collect_db_stats`` construit deux requêtes par f-string : bandit les signale
(B608, lignes 603 et 641). Les exceptions posées sur ces lignes reposent sur UN
invariant unique — le seul fragment interpolé, ``table_name``, ne peut venir que
du dict littéral local ``embedding_tables``, écrit 36 lignes plus haut dans la
même fonction, laquelle n'accepte aucun paramètre. La seule valeur variable de
la seconde requête, ``settings.embedding_dimension``, passe par le bind ``:dim``.

Ces tests échouent à l'instant précis où le nosec cesserait d'être justifié :
si ``embedding_tables`` devient dynamique, si ``table_name`` reçoit une autre
source, si la fonction gagne un paramètre, ou si la dimension est interpolée.
"""

from __future__ import annotations

import ast
import inspect
import re

from brain_v42.metrics import collector as mod

# Un nom de table PostgreSQL non cité, tel qu'écrit en dur dans le source.
_BARE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _collect_db_stats_ast() -> ast.AsyncFunctionDef:
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "collect_db_stats":
            return node
    raise AssertionError("collect_db_stats introuvable — les nosec B608 n'ont plus de garde")


def test_collect_db_stats_takes_no_argument_that_could_reach_the_sql() -> None:
    """Aucun appelant ne peut passer de nom de table : la fonction n'a que ``self``."""
    fn = _collect_db_stats_ast()
    args = fn.args
    assert [a.arg for a in args.args] == ["self"], (
        "collect_db_stats a gagné un paramètre : le nosec B608 affirmait qu'aucune "
        "valeur d'appel ne pouvait atteindre le SQL interpolé"
    )
    assert not args.posonlyargs and not args.kwonlyargs
    assert args.vararg is None and args.kwarg is None


def test_embedding_tables_is_a_dict_of_string_literals() -> None:
    """``embedding_tables`` doit rester un dict littéral de noms écrits en dur.

    Échoue sur un `dict(...)`, une compréhension, une lecture de settings, un
    `+ suffix` ou une clé/valeur non constante — chacun rendrait la table
    interpolée dépendante d'autre chose que du source.
    """
    fn = _collect_db_stats_ast()

    assigns = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "embedding_tables" for t in node.targets)
    ]
    assert len(assigns) == 1, (
        f"`embedding_tables` est affecté {len(assigns)} fois — la forme gardée par le "
        "nosec B608 a changé"
    )
    value = assigns[0].value
    assert isinstance(value, ast.Dict), (
        "`embedding_tables` n'est plus un dict littéral : le nosec B608 est injustifié"
    )

    tables: list[str] = []
    for key, val in zip(value.keys, value.values, strict=True):
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            "clé non littérale dans `embedding_tables` : nosec B608 injustifié"
        )
        assert isinstance(val, ast.Constant) and isinstance(val.value, str), (
            "nom de table non littéral dans `embedding_tables` : nosec B608 injustifié"
        )
        assert _BARE_IDENTIFIER.match(val.value), (
            f"nom de table interpolable non conforme : {val.value!r}"
        )
        tables.append(val.value)

    assert tables == ["decisions", "learnings", "snippets", "runbooks", "adrs"]


def test_table_name_is_only_ever_bound_by_a_loop_over_the_literal_tables() -> None:
    """``table_name`` ne peut recevoir que les valeurs du dict littéral.

    ``tables_with_embeddings`` doit rester ``list(embedding_tables.values())``, et
    ``table_name`` ne doit être lié que par des ``for`` sur cette liste. Échoue sur
    un `+ [...]`, un `extend`, ou un `table_name = <autre chose>`.
    """
    fn = _collect_db_stats_ast()

    derivations = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "tables_with_embeddings" for t in node.targets)
    ]
    assert len(derivations) == 1, "`tables_with_embeddings` doit avoir une source unique"
    derived = derivations[0].value
    assert isinstance(derived, ast.Call)
    assert isinstance(derived.func, ast.Name) and derived.func.id == "list"
    assert len(derived.args) == 1
    inner = derived.args[0]
    assert isinstance(inner, ast.Call)
    assert isinstance(inner.func, ast.Attribute) and inner.func.attr == "values"
    assert isinstance(inner.func.value, ast.Name) and inner.func.value.id == "embedding_tables", (
        "`tables_with_embeddings` ne dérive plus du dict littéral : nosec B608 injustifié"
    )

    loops = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            if node.target.id != "table_name":
                continue
            assert isinstance(node.iter, ast.Name) and node.iter.id == "tables_with_embeddings", (
                f"ligne {node.lineno} : `table_name` itère autre chose que la liste littérale"
            )
            loops += 1
        if isinstance(node, ast.Assign):
            assert not any(
                isinstance(t, ast.Name) and t.id == "table_name" for t in node.targets
            ), (
                f"ligne {node.lineno} : `table_name` est affecté hors boucle — le nosec B608 "
                "n'affirme plus la bonne chose"
            )
    assert loops == 2, f"attendu 2 boucles sur les tables littérales, trouvé {loops}"


def test_the_only_interpolated_fragment_is_table_name() -> None:
    """Tout autre trou d'f-string invaliderait les deux exceptions posées.

    La dimension d'embedding en particulier doit rester un bind ``:dim`` : c'est
    la seule valeur de la fonction qui vienne de la configuration.
    """
    fn = _collect_db_stats_ast()

    holes = [
        part
        for node in ast.walk(fn)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
        if isinstance(part, ast.FormattedValue)
    ]
    assert holes, "plus aucune f-string dans collect_db_stats — retirer les nosec B608"
    for hole in holes:
        assert isinstance(hole.value, ast.Name) and hole.value.id == "table_name", (
            f"ligne {hole.lineno} : fragment interpolé autre que `table_name` "
            f"({ast.dump(hole.value)}) — le nosec B608 ne le couvre pas"
        )

    source = inspect.getsource(mod)
    assert '{"dim": dim}' in source, (
        "la dimension d'embedding n'est plus passée par un bind : si elle est "
        "interpolée, le nosec B608 devient un vrai défaut"
    )
