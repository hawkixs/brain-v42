"""`end`'s replacement gate: the JUDGEMENT, and nothing but the judgement.

With the XOR removed, `end` no longer measures the client's diligence. What
remains must be exactly what the server **cannot manufacture on the user's
behalf** — otherwise we would have replaced one receipt with another.

The survey below proves it instead of arguing it: `summary` has only ONE writer
in all of `src/`, and it is the explicit closure. The sweep leaves the column at
`NULL`, and the `closed_inactive` branch of the 046 CHECK FORBIDS it from doing
otherwise even if it tried. No server path can therefore produce a `summary`:
that is what makes judgement the only object of `end` out of the server's reach.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.unit.repositories.test_pg_brain_session import (
    _make_session,
    _session_row,
    _terminal_router,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "src"
MIGRATION_046 = REPO_ROOT / "alembic" / "versions" / "046_session_identity_and_nature.py"

#: The only sites in `src/` that carry a session `summary` — and their list IS
#: the result: two links of one human chain, no third party.
DECLARED_SUMMARY_WRITERS = frozenset(
    {
        # The explicit tool: it RELAYS the user's text, it does not manufacture
        # any. This is the only entry door.
        (
            "src/brain_v42/mcp/tools/session_lifecycle_tools.py"
            "::register_session_lifecycle_tools.brain_session_end"
        ),
        # And the only site that persists it, at the end of that same command.
        "src/brain_v42/repositories/pg_brain_session.py::PgBrainSessionRepo._mark_ended",
    }
)


@pytest.mark.asyncio
async def test_end_with_an_empty_ledger_and_no_reason_now_passes() -> None:
    """The gate is NO LONGER diligence: having produced nothing is a valid outcome.

    Before, this closure required a written justification. Derivation makes that
    requirement absurd — the user no longer controls what the ledger contains —
    and above all it punished the honest case.
    """
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    opened = _session_row()
    ended = _session_row(
        session_id=opened["id"],
        status="ended",
        summary="reviewed design",
        next_focus="implement tools",
    )
    _, _statements, _, factory = _make_session(
        _terminal_router(
            opened,
            updated_row=ended,
            focus_row={"current_focus": "implement tools", "focus_revision": 8},
            current_focus_row={"current_focus": "old focus", "focus_revision": 7},
        )
    )

    result = await PgBrainSessionRepo(factory).end(
        opened["id"], "client-a", "reviewed design", "implement tools", 7, None
    )

    assert result.session.captured_knowledge_ids == []
    assert result.session.nothing_to_capture_reason is None


@pytest.mark.parametrize("field", ["summary", "next_focus"])
def test_the_model_still_refuses_a_blank_judgement(field: str) -> None:
    """The Pydantic rail guards what the server cannot produce."""
    from brain_v42.models.brain_session import BrainSession

    payload = dict(_session_row(status="ended", summary="s", next_focus="n"))
    payload["nothing_to_capture_reason"] = "nothing durable"
    payload[field] = "   "

    with pytest.raises(ValueError, match=f"ended session requires {field}"):
        BrainSession.model_validate(payload)


def test_a_blank_reason_is_still_refused() -> None:
    """Giving a reason is still an act: a blank reason is not one."""
    from brain_v42.models.brain_session import BrainSession

    payload = dict(_session_row(status="ended", summary="s", next_focus="n"))
    payload["nothing_to_capture_reason"] = "   "

    with pytest.raises(ValueError, match="must not be blank"):
        BrainSession.model_validate(payload)


class _SummaryKeywordVisitor(ast.NodeVisitor):
    """Collect every call passing a ``summary`` keyword, with its enclosing def."""

    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.found: set[str] = set()
        self._scope: list[str] = []

    def _enter(self, node: ast.AST, name: str) -> None:
        self._scope.append(name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        if any(keyword.arg == "summary" for keyword in node.keywords):
            self.found.add(f"{self.relative}::{'.'.join(self._scope) or '<module>'}")
        self.generic_visit(node)


def _summary_writers() -> set[str]:
    """Every site in `src/` that carries a session ``summary``.

    Surveyed by the COLUMN and not by the tool name, and restricted to files that
    name ``brain_sessions``: elsewhere, `summary` summarizes something else and
    has nothing to do with closing a session.
    """
    writers: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "brain_sessions" not in text:
            continue
        visitor = _SummaryKeywordVisitor(path.relative_to(REPO_ROOT).as_posix())
        visitor.visit(ast.parse(text, filename=str(path)))
        writers.update(visitor.found)
    return writers


def test_no_server_path_can_write_a_session_summary() -> None:
    """THE proof, not the argument: one writer, and it is the explicit closure.

    If a server path gained the right to write `summary`, `end` would stop
    measuring a human judgement and become a receipt again — exactly the defect
    just removed, reintroduced elsewhere.
    """
    assert _summary_writers() == set(DECLARED_SUMMARY_WRITERS)


def test_the_closed_inactive_branch_forbids_a_summary_outright() -> None:
    """And the database refuses it too: the guarantee is not only in the app.

    The sweep leaves `summary` at `NULL`; the `closed_inactive` branch of the 046
    CHECK REQUIRES it to. Even a sweep that tried would be rejected by the
    database.
    """
    branch = MIGRATION_046.read_text(encoding="utf-8")
    closed_inactive = branch.split("status = 'closed_inactive'")[1].split(")")[0]
    assert "summary IS NULL" in closed_inactive
    assert "next_focus IS NULL" in closed_inactive


def test_the_census_is_not_blind() -> None:
    """Non-vacuity witness: a surveyor that finds nothing would pass forever."""
    assert _summary_writers(), "le recenseur ne désigne plus AUCUN site — il est aveugle"
