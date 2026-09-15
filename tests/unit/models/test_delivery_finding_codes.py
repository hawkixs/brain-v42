"""The delivery evaluator emits a closed, published vocabulary of finding codes.

Downstream consumers freeze a copy of this list and compare it, as data,
without importing ``brain_v42``.  Three guards keep the copies equal: every
``_finding(`` emitter belongs to the constant, the constant has the expected
size, and the JSON contract file mirrors the constant.

The emitter scan is an AST walk over ``delivery_evaluator.py``.  It resolves
the first argument of every ``_finding(`` call to the set of codes it can
evaluate to, and fails loudly on any shape it does not understand — a textual
count of ``_finding(`` occurrences guards the walk itself against emitters it
would not even visit (a method, an ``async def``, an alias).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import get_args

REPO_ROOT = Path(__file__).resolve().parents[3]
EVALUATOR = REPO_ROOT / "src" / "brain_v42" / "models" / "delivery_evaluator.py"
CONTRACT_FILE = REPO_ROOT / "docs" / "contracts" / "delivery_finding_codes.json"


def _check_conclusions() -> tuple[str, ...]:
    from brain_v42.models.delivery import CheckAttempt

    return get_args(CheckAttempt.model_fields["conclusion"].annotation)


def _parents(function: ast.FunctionDef) -> dict[ast.AST, ast.AST]:
    return {
        child: parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)
    }


def _targets_name(target: ast.expr, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, ast.Tuple | ast.List):
        return any(_targets_name(item, name) for item in target.elts)
    return False


def _assignments(function: ast.FunctionDef, name: str) -> list[ast.expr]:
    """Every plain ``name = value`` in the function; any other binding fails loudly."""
    values: list[ast.expr] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            targets = node.targets
            if any(_targets_name(target, name) for target in targets):
                assert len(targets) == 1 and isinstance(targets[0], ast.Name), (
                    f"{function.name}: `{name}` is bound by unpacking at line {node.lineno}"
                )
                values.append(node.value)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            assert not _targets_name(node.target, name), (
                f"{function.name}: `{name}` is rebound at line {node.lineno}"
            )
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            assert not _targets_name(node.target, name), (
                f"{function.name}: `{name}` is a loop target at line {node.target.lineno}"
            )
        elif isinstance(node, ast.NamedExpr):
            assert not _targets_name(node.target, name), (
                f"{function.name}: `{name}` is bound by a walrus at line {node.lineno}"
            )
        elif isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                assert item.optional_vars is None or not _targets_name(item.optional_vars, name), (
                    f"{function.name}: `{name}` is bound by `with` at line {node.lineno}"
                )
    assert values, f"{function.name}: `{name}` is never assigned"
    return values


def _compared_constant(test: ast.expr, operator: type[ast.cmpop]) -> str | None:
    """The literal in a single ``x <op> "literal"`` comparison, if that is its shape."""
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], operator)
        and isinstance(test.comparators[0], ast.Constant)
        and isinstance(test.comparators[0].value, str)
    ):
        return test.comparators[0].value
    return None


def _enclosing_exclusions(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> frozenset[str]:
    """Constants the enclosing ``if``/``elif`` chain has already ruled out at ``node``.

    Inside the body of ``if x != "c"`` the value ``"c"`` is unreachable; so is
    it in the ``else`` of ``if x == "c"``.  This mirrors, for statements, what
    the ``IfExp`` branch below does for expressions.
    """
    excluded: set[str] = set()
    child, parent = node, parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.If):
            if child in parent.body:
                constant = _compared_constant(parent.test, ast.NotEq)
            elif child in parent.orelse:
                constant = _compared_constant(parent.test, ast.Eq)
            else:
                constant = None
            if constant is not None:
                excluded.add(constant)
        child, parent = parent, parents.get(parent)
    return frozenset(excluded)


def _emitted_codes(
    expression: ast.expr,
    function: ast.FunctionDef,
    parents: dict[ast.AST, ast.AST],
    excluded: frozenset[str],
) -> set[str]:
    """Every code a ``_finding`` first argument can evaluate to, or fail loudly."""
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return {expression.value}
    if isinstance(expression, ast.IfExp):
        body_excluded = excluded | ({_compared_constant(expression.test, ast.NotEq)} - {None})
        else_excluded = excluded | ({_compared_constant(expression.test, ast.Eq)} - {None})
        return _emitted_codes(expression.body, function, parents, body_excluded) | _emitted_codes(
            expression.orelse, function, parents, else_excluded
        )
    if isinstance(expression, ast.Name):
        codes: set[str] = set()
        for value in _assignments(function, expression.id):
            codes |= _emitted_codes(
                value, function, parents, excluded | _enclosing_exclusions(value, parents)
            )
        return codes
    if isinstance(expression, ast.JoinedStr):
        # The only dynamic family: ``check_<conclusion>`` is closed by the
        # CheckAttempt.conclusion Literal, so the interpolated value must be
        # exactly that attribute — any other f-string is an unknown shape.
        parts = expression.values
        assert (
            len(parts) == 2
            and isinstance(parts[0], ast.Constant)
            and isinstance(parts[1], ast.FormattedValue)
            and isinstance(parts[1].value, ast.Attribute)
            and parts[1].value.attr == "conclusion"
        ), f"{function.name}: unsupported f-string shape at line {expression.lineno}"
        prefix = parts[0].value
        return {f"{prefix}{value}" for value in _check_conclusions() if value not in excluded}
    raise AssertionError(
        f"{function.name}: unsupported `_finding` code expression at line {expression.lineno}"
    )


def _finding_call_sites() -> list[tuple[ast.FunctionDef, ast.Call]]:
    module = ast.parse(EVALUATOR.read_text(encoding="utf-8"))
    # `_finding` may only ever be CALLED: an alias (`_emit = _finding`) or a
    # reference passed around would hide an emitter from both the walk below
    # and the textual count in the test.
    module_parents = {
        child: parent for parent in ast.walk(module) for child in ast.iter_child_nodes(parent)
    }
    for node in ast.walk(module):
        if isinstance(node, ast.Name) and node.id == "_finding":
            parent = module_parents.get(node)
            assert isinstance(parent, ast.Call) and parent.func is node, (
                f"`_finding` is referenced without being called at line {node.lineno}"
            )
    sites: list[tuple[ast.FunctionDef, ast.Call]] = []
    for function in module.body:
        if not isinstance(function, ast.FunctionDef) or function.name == "_finding":
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_finding"
            ):
                sites.append((function, node))
    return sites


def test_every_finding_emitter_uses_a_published_code() -> None:
    from brain_v42.models.delivery_evaluator import DELIVERY_FINDING_CODES

    sites = _finding_call_sites()
    # Every textual `_finding(` but the definition must be a site the walk
    # visited: an emitter behind an alias, in a method or in an `async def`
    # would otherwise be skipped without a word.
    textual = EVALUATOR.read_text(encoding="utf-8").count("_finding(") - 1
    assert len(sites) == textual, f"the walk visited {len(sites)} of {textual} `_finding(` sites"
    emitted: set[str] = set()
    for function, call in sites:
        assert call.args and not call.keywords, (
            f"{function.name}: `_finding` must receive its code positionally"
        )
        parents = _parents(function)
        emitted |= _emitted_codes(
            call.args[0], function, parents, _enclosing_exclusions(call, parents)
        )
    assert emitted <= DELIVERY_FINDING_CODES, sorted(emitted - DELIVERY_FINDING_CODES)
    assert emitted == DELIVERY_FINDING_CODES, {
        "published_but_never_emitted": sorted(DELIVERY_FINDING_CODES - emitted)
    }


def test_technical_check_codes_are_a_subset_of_the_published_vocabulary() -> None:
    from brain_v42.models.delivery_evaluator import (
        _TECHNICAL_CHECK_CODES,
        DELIVERY_FINDING_CODES,
    )

    assert _TECHNICAL_CHECK_CODES <= DELIVERY_FINDING_CODES


def test_published_vocabulary_is_frozen_at_its_published_size() -> None:
    """Adding or removing a code is a contract change: consumers froze this list."""
    from brain_v42.models.delivery_evaluator import DELIVERY_FINDING_CODES

    assert isinstance(DELIVERY_FINDING_CODES, frozenset)
    assert len(DELIVERY_FINDING_CODES) == 43


def test_json_contract_file_mirrors_the_published_constant() -> None:
    from brain_v42.models.delivery_evaluator import DELIVERY_FINDING_CODES

    document = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))
    assert document["contract"] == "brain-v42.delivery.finding_codes"
    assert document["source"] == "brain_v42.models.delivery_evaluator.DELIVERY_FINDING_CODES"
    codes = document["codes"]
    assert codes == sorted(codes), "codes must be sorted for a stable diff"
    assert len(codes) == len(set(codes)), "codes must be unique"
    assert frozenset(codes) == DELIVERY_FINDING_CODES
