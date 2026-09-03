"""`PgAccessLogRepo` aggregates ONE way — ticket `fd05f4a3`.

`aggregate_and_flush` opened its own session and committed before the caller had
updated the entities, leaving a window where a crash lost access counts for good.
`aggregate_in_session` replaced it and takes the caller's session, so the
aggregate, the DELETE and the entity updates commit together.

The old one then survived as a deprecated duplicate with no caller in `src/` or
`scripts/` — and a bench aimed at it wrote into the void, which is how a dead
path costs something: not by running, but by looking runnable.

Two aggregation methods on one repository is the defect. This asserts there is
one, by name, from the AST — so a second cannot come back under a new spelling
without reddening here.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).parents[3] / "src" / "brain_v42" / "repositories" / "pg_access_log.py"
#: The survivor. Named, not inferred: a rule that says "exactly one method whose
#: name starts with aggregate" would be satisfied by the WRONG one surviving.
THE_AGGREGATION = "aggregate_in_session"


def _methods() -> list[str]:
    tree = ast.parse(REPO.read_text(encoding="utf-8"))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert classes, "pg_access_log.py declares no class — the scan reads nothing"
    return [
        node.name
        for klass in classes
        for node in klass.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    ]


def test_the_repository_offers_exactly_one_aggregation_path() -> None:
    aggregators = sorted(name for name in _methods() if "aggregate" in name)

    assert aggregators == [THE_AGGREGATION], (
        "a second aggregation path is back on PgAccessLogRepo. The one that must "
        f"survive is {THE_AGGREGATION}, which takes the CALLER's session so the "
        f"aggregate, the DELETE and the entity updates commit together. Found: {aggregators}"
    )


def test_the_surviving_aggregation_takes_the_callers_session() -> None:
    """Naming it is not enough: what makes it the right one is its signature.

    Without this, deleting the atomic method and keeping the self-committing one
    under its name would satisfy the assertion above.
    """
    tree = ast.parse(REPO.read_text(encoding="utf-8"))
    survivor = next(
        node
        for klass in tree.body
        if isinstance(klass, ast.ClassDef)
        for node in klass.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == THE_AGGREGATION
    )
    parameters = [argument.arg for argument in survivor.args.args]

    assert "session" in parameters, (
        f"{THE_AGGREGATION} no longer receives a session — it opens its own again, "
        "which is the window this consolidation closed"
    )
