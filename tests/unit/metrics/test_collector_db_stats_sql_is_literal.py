"""Invariant guard for the two `# nosec B608` in ``MetricsCollector.collect_db_stats``.

``collect_db_stats`` builds two queries by f-string: bandit flags them (B608,
lines 603 and 641). The exceptions placed on those lines rest on ONE single
invariant — the only interpolated fragment, ``table_name``, can come only from
the local literal dict ``embedding_tables``, written 36 lines above in the same
function, which accepts no parameter. The second query's only variable value,
``settings.embedding_dimension``, goes through the ``:dim`` bind.

These tests fail at the precise instant the nosec would stop being justified: if
``embedding_tables`` becomes dynamic, if ``table_name`` gets another source, if
the function gains a parameter, or if the dimension is interpolated.
"""

from __future__ import annotations

import ast
import inspect
import re

from brain_v42.metrics import collector as mod

# An unquoted PostgreSQL table name, as hardcoded in the source.
_BARE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _collect_db_stats_ast() -> ast.AsyncFunctionDef:
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "collect_db_stats":
            return node
    raise AssertionError("collect_db_stats introuvable — les nosec B608 n'ont plus de garde")


def test_collect_db_stats_takes_no_argument_that_could_reach_the_sql() -> None:
    """No caller can pass a table name: the function takes only ``self``."""
    fn = _collect_db_stats_ast()
    args = fn.args
    assert [a.arg for a in args.args] == ["self"], (
        "collect_db_stats a gagné un paramètre : le nosec B608 affirmait qu'aucune "
        "valeur d'appel ne pouvait atteindre le SQL interpolé"
    )
    assert not args.posonlyargs and not args.kwonlyargs
    assert args.vararg is None and args.kwarg is None


def test_embedding_tables_is_a_dict_of_string_literals() -> None:
    """``embedding_tables`` must stay a literal dict of hardcoded names.

    Fails on a `dict(...)`, a comprehension, a settings read, a `+ suffix` or a
    non-constant key/value — each would make the interpolated table depend on
    something other than the source.
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
    """``table_name`` can only receive the literal dict's values.

    ``tables_with_embeddings`` must stay ``list(embedding_tables.values())``, and
    ``table_name`` must be bound only by ``for`` loops over that list. Fails on a
    `+ [...]`, an `extend`, or a `table_name = <something else>`.
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
    """Any other f-string hole would invalidate both exceptions.

    The embedding dimension in particular must stay a ``:dim`` bind: it is the
    function's only value that comes from configuration.
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
