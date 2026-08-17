"""Invariant guarding the `# nosec B608` suppressions in `metrics/memory_stats.py`.

`collect_memory_stats` inlines table names into raw SQL instead of binding them
(PostgreSQL cannot bind an identifier). The suppressions are only honest while
*every* interpolated fragment provably comes from a module-level tuple of string
literals and never from a caller-supplied value.

These tests fail the moment someone makes those constants dynamic, widens them to
a non-identifier string, adds a table-name parameter to the function, or
interpolates a new expression into the SQL. Whoever does that must re-justify the
suppression rather than inherit it silently.
"""

from __future__ import annotations

import ast
import inspect
import re
from typing import Any

import pytest

from brain_v42.metrics import memory_stats

_TABLE_CONSTANTS = ("_EPISODIC_TABLES", "_SEMANTIC_TABLES")

# Unquoted PostgreSQL identifier: no quote, no whitespace, no comment, no semicolon.
_SQL_IDENTIFIER_RE = re.compile(r"\A[a-z_][a-z0-9_]{0,62}\Z")

# The only names allowed inside an f-string placeholder in this module: the loop
# variable bound to a table constant, and the two pre-joined IN-lists.
_ALLOWED_PLACEHOLDER_NAMES = frozenset({"t", "ep_inlist", "se_inlist"})

_SOURCE = inspect.getsource(memory_stats)
_TREE = ast.parse(_SOURCE)


def _module_level_assignments(name: str) -> list[ast.Assign]:
    return [
        node
        for node in _TREE.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]


def _all_assignments_to(name: str) -> list[ast.AST]:
    hits: list[ast.AST] = []
    for node in ast.walk(_TREE):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.For):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            hits.append(node)
    return hits


def _collect_memory_stats_node() -> ast.AsyncFunctionDef:
    for node in _TREE.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "collect_memory_stats":
            return node
    raise AssertionError("collect_memory_stats not found in memory_stats.py")


@pytest.mark.parametrize("name", _TABLE_CONSTANTS)
def test_table_constant_holds_only_literal_sql_identifiers(name: str) -> None:
    value: Any = getattr(memory_stats, name)
    assert isinstance(value, tuple), f"{name} must stay a tuple, got {type(value)!r}"
    assert value, f"{name} must not be empty"
    for table in value:
        assert isinstance(table, str), f"{name} holds a non-str entry: {table!r}"
        assert _SQL_IDENTIFIER_RE.fullmatch(table), (
            f"{name} holds {table!r}, which is not a bare SQL identifier — "
            "the # nosec B608 suppressions in memory_stats.py no longer hold"
        )


@pytest.mark.parametrize("name", _TABLE_CONSTANTS)
def test_table_constant_is_a_static_tuple_of_string_literals(name: str) -> None:
    """Runtime values could be patched; this pins the *source* as literal.

    A call, a comprehension, an env lookup or a name reference on the right-hand
    side would let an outside value reach the SQL — this is the check that fails.
    """
    assignments = _module_level_assignments(name)
    assert len(assignments) == 1, f"{name} must have exactly one module-level assignment"
    rhs = assignments[0].value
    assert isinstance(rhs, ast.Tuple), (
        f"{name} must be assigned a literal tuple, got {type(rhs).__name__} — "
        "a dynamic table list invalidates the # nosec B608 suppressions"
    )
    for element in rhs.elts:
        assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
            f"{name} must contain only string literals, found {ast.dump(element)}"
        )


@pytest.mark.parametrize("name", _TABLE_CONSTANTS)
def test_table_constant_is_never_reassigned(name: str) -> None:
    assert len(_all_assignments_to(name)) == 1, (
        f"{name} is assigned more than once in memory_stats.py — a rebind could "
        "route an input value into the interpolated SQL"
    )


def test_collect_memory_stats_accepts_no_table_name_argument() -> None:
    """No caller-supplied value exists that could reach the SQL string."""
    params = list(inspect.signature(memory_stats.collect_memory_stats).parameters)
    assert params == ["session_factory"], (
        f"collect_memory_stats now takes {params} — any new argument must be proven "
        "unable to reach the interpolated SQL before the # nosec B608 stays"
    )


def test_sql_placeholders_only_reference_the_table_constants() -> None:
    """Every `{...}` interpolated into the SQL resolves to the literal constants."""
    func = _collect_memory_stats_node()

    # Only the SQL-bearing expressions: the `text(...)` calls and the two
    # IN-list builders. (The `hnsw (m=...)` display f-string is not SQL.)
    sql_roots: list[ast.AST] = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "text"
    ]
    sql_roots += [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id.endswith("_inlist") for t in node.targets)
    ]
    assert len(sql_roots) == 6, f"expected 4 text() calls + 2 IN-lists, got {len(sql_roots)}"

    placeholders = [
        node.value
        for root in sql_roots
        for node in ast.walk(root)
        if isinstance(node, ast.FormattedValue)
    ]
    assert placeholders, "expected interpolated SQL fragments in collect_memory_stats"
    for expr in placeholders:
        assert isinstance(expr, ast.Name), (
            f"SQL placeholder {ast.dump(expr)} is not a plain name — re-audit B608"
        )
        assert expr.id in _ALLOWED_PLACEHOLDER_NAMES, (
            f"SQL placeholder {{{expr.id}}} is not one of {sorted(_ALLOWED_PLACEHOLDER_NAMES)}"
        )

    # `t` is only ever bound by comprehensions iterating the table constants,
    # so `{t}` cannot carry anything but a literal identifier.
    for comp in ast.walk(func):
        if not isinstance(comp, ast.GeneratorExp | ast.ListComp | ast.SetComp):
            continue
        for generator in comp.generators:
            assert isinstance(generator.target, ast.Name) and generator.target.id == "t"
            assert isinstance(generator.iter, ast.Name), (
                f"comprehension iterates {ast.dump(generator.iter)}, not a table constant"
            )
            assert generator.iter.id in _TABLE_CONSTANTS, (
                f"comprehension iterates {generator.iter.id}, not one of {_TABLE_CONSTANTS}"
            )


def test_inlist_helpers_are_built_from_the_table_constants() -> None:
    """`ep_inlist` / `se_inlist` quote the constants — nothing else feeds them."""
    func = _collect_memory_stats_node()
    seen: dict[str, str] = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id.endswith("_inlist")):
                continue
            sources = [
                gen.iter.id
                for value in ast.walk(node.value)
                if isinstance(value, ast.GeneratorExp)
                for gen in value.generators
                if isinstance(gen.iter, ast.Name)
            ]
            assert len(sources) == 1, f"{target.id} is not built from a single iterable"
            seen[target.id] = sources[0]

    assert seen == {"ep_inlist": "_EPISODIC_TABLES", "se_inlist": "_SEMANTIC_TABLES"}


def test_no_blanket_nosec_in_memory_stats() -> None:
    """Operator rule: suppressions are per-line and name the test id."""
    comments = re.findall(r"#\s*nosec[^\n]*", _SOURCE)
    assert comments, "expected the audited # nosec B608 suppressions to be present"
    for comment in comments:
        assert re.match(r"#\s*nosec\s+B608\b", comment), (
            f"blanket or untargeted suppression: {comment!r}"
        )
