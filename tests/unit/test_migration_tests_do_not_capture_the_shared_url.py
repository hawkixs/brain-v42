"""No test under `tests/integration/db/` may capture its database URL at import.

Ticket `f7af0977`. The migration tests downgrade and re-upgrade their database,
and every downgrade burns `attnum` slots that PostgreSQL never gives back. While
they ran against the SHARED `brain_test`, that database had a finite, measurable
life — and it ended: measured 2026-09-03, one run of `tests/integration/db` burns
**77 dropped columns per decay table**, so 1600 / 77 is roughly **20 runs**.

The fix moves the whole directory onto a database created for the session and
destroyed after. That retargeting works by rebinding `BRAIN_V42_TEST_DB_URL`
before the engine is built — which a module that read the variable at IMPORT time
would silently bypass, because imports happen during collection, before any
fixture runs.

So this is the guard on the fix, and it is hermetic: it reads source, needs no
database, and fails on the exact shape that would re-attach one module to the
shared database without anybody noticing.
"""

from __future__ import annotations

import ast
from pathlib import Path

MIGRATION_TESTS = Path(__file__).parents[1] / "integration" / "db"

#: Names that resolve to the shared database when read at import time.
_SHARED_URL_NAMES = frozenset({"BRAIN_V42_TEST_DB_URL", "INTEGRATION_DB_URL"})


def _module_level_captures(source: str) -> list[str]:
    """Assignments at module scope whose value mentions a shared-URL name."""
    tree = ast.parse(source)
    captures: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        value = node.value
        if value is None:
            continue
        mentioned = {
            child.value
            for child in ast.walk(value)
            if isinstance(child, ast.Constant) and child.value in _SHARED_URL_NAMES
        } | {
            child.id
            for child in ast.walk(value)
            if isinstance(child, ast.Name) and child.id in _SHARED_URL_NAMES
        }
        if mentioned:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            captures.append(f"{', '.join(names) or '<expr>'} = … {sorted(mentioned)}")
    return captures


def _imports_the_shared_constant(source: str) -> bool:
    """`from tests.integration.conftest import INTEGRATION_DB_URL`, anywhere.

    Function-local or not: the constant is bound at the CONFTEST's import time,
    so importing it at all captures the pre-rebind value.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name in _SHARED_URL_NAMES for alias in node.names
        ):
            return True
    return False


def test_no_migration_test_reads_the_shared_url_at_import_time() -> None:
    offenders: list[str] = []
    for path in sorted(MIGRATION_TESTS.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(MIGRATION_TESTS.parents[1])
        offenders += [f"{relative}: {capture}" for capture in _module_level_captures(source)]
        if _imports_the_shared_constant(source):
            offenders.append(f"{relative}: imports a shared-URL constant")

    assert not offenders, (
        "these modules bind the database URL before any fixture can rebind it:\n"
        + "\n".join(f"    {offender}" for offender in offenders)
        + "\n\nRead the environment inside the function that uses it. A module-level capture "
        "happens\nduring collection, so the session fixture that points this directory at its "
        "own\ndisposable database cannot reach it — and the module silently keeps downgrading "
        "the\nSHARED database, which is what ticket f7af0977 measured the cost of."
    )


def test_the_detector_still_recognises_the_shape_it_was_written_for() -> None:
    """A guard whose AST walk silently stopped matching is worse than none."""
    assert _module_level_captures('_DB_URL = os.environ.get("BRAIN_V42_TEST_DB_URL", "")')
    assert _module_level_captures('_DB_URL: str = os.environ["BRAIN_V42_TEST_DB_URL"]')
    assert _imports_the_shared_constant("from tests.integration.conftest import INTEGRATION_DB_URL")

    assert not _module_level_captures(
        'def target() -> str:\n    return os.environ["BRAIN_V42_TEST_DB_URL"]'
    )
    assert not _imports_the_shared_constant("from tests.integration.conftest import something_else")
