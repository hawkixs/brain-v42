"""Every site that writes `project_contexts.current_focus` is DECLARED here.

Ticket `5281f0ef`, opened 2026-08-23 by the worker who had just shipped the
bound and named this as the most probable hole in its own batch. The 10 000
character bound lives at the MCP layer ALONE: the model, the service and the
column (`text`) are unbounded, and the two paths converge nowhere before the
column itself. A writer outside MCP — a `scripts/` job, the Codex gateway, a
migration, psql by hand — writes an unbounded focus and no test sees it.

THE CENSUS IS ITSELF THE TRAP, and the ticket says so. The same batch found its
third MCP writer only late, because its focus argument is OPTIONAL (learning
`325320d4`): the ticket, its mandate and the test module all said "the OTHER
writer", singular. Censusing by the SHAPE of an argument misses paths
structurally. So this module censuses BY THE COLUMN, and coarsely: any module
under `src/` that so much as NAMES `current_focus` must appear below.

Coarse on purpose. A precise AST scan for `.values(current_focus=…)` would have
missed the scrub, whose write goes through `values = dict(updates)` with the
column name coming from a module-level tuple — invisible to any scan looking for
the keyword. Being noisy and absorbing the noise in a declared list has no false
negatives; being clever has.

WHAT THIS GUARD STILL CANNOT SEE, said rather than discovered. It scans `src/`
AND the repository's own `scripts/`, but it keys on the COLUMN. A shim that names
the MODULE and never the column is invisible to it — `scripts/migrate_neo4j_to_pg.py`
is exactly that: two lines, `sys.modules[__name__] = _impl`, no mention of
`current_focus`. Finding that one is the census's job, not this guard's, and on
2026-09-03 a truncated `grep` missed it (a `head -5` over a list whose `grep -v`
filter silently matched nothing). The lesson belongs here because this file is
where someone will look next: count first, abbreviate after.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
SOURCES = (ROOT / "src", ROOT / "scripts")
COLUMN = "current_focus"

#: What each module does with the column. `stamps` answers ONE question — does
#: this site set `focus_updated_at` (directly or through `focus_stamp`) — and it
#: is `None` for a module that only reads.
FOCUS_WRITERS: dict[str, tuple[str, bool | None, str]] = {
    # ── writers, through the shared path ──────────────────────────────────
    "src/brain_v42/repositories/pg_project_context.py": (
        "writer",
        True,
        "create + get_or_create (both branches) + update + update_focus",
    ),
    "src/brain_v42/repositories/pg_brain_session.py": (
        "writer",
        True,
        "_apply_focus_if_current — the applied CAS of brain_session_end",
    ),
    "src/brain_v42/services/roadmap_service.py": (
        "writer",
        True,
        "update_project_focus — the CAS of brain_update_project_focus",
    ),
    # ── writers OUTSIDE the MCP layer: stamped since 2026-09-03, still unbounded ──
    "src/brain_v42/scripts/scrub_xml_tool_call_leak.py": (
        "writer",
        True,
        "maintenance scrub; --live rewrites the prose, so `focus_stamp` applies by "
        "040's own rule. Still outside the 10 000-character bound, which lives at "
        "the MCP layer alone",
    ),
    "src/brain_v42/scripts/migrate_neo4j_to_pg.py": (
        "writer",
        True,
        "datalake_v2 import; sets `focus_updated_at` EXPLICITLY to NULL — the row "
        "predates brain_v42 and `now()` would date the prose at import time. Still "
        "outside the bound",
    ),
    # ── the rest: readers, models, plumbing ───────────────────────────────
    "src/brain_v42/db/focus_stamp.py": ("rule", None, "the stamping rule itself"),
    "src/brain_v42/db/focus_history.py": ("rule", None, "the audit write path (050)"),
    "src/brain_v42/db/tables.py": ("schema", None, "the column definition"),
    "src/brain_v42/models/project_context.py": ("model", None, "carries the field"),
    "src/brain_v42/models/brain_session.py": ("model", None, "carries the field"),
    "src/brain_v42/services/project_context_service.py": ("reader", None, "delegates"),
    "src/brain_v42/mcp/tools/project_context_tools.py": ("reader", None, "tool surface"),
    "src/brain_v42/mcp/tools/session_tools.py": ("reader", None, "briefing"),
    "src/brain_v42/mcp/tools/formatters.py": ("reader", None, "rendering"),
    "src/brain_v42/maintenance/plan_index_repair_store.py": (
        "reader",
        None,
        "snapshots the row; its two UPDATEs never name the column",
    ),
    "src/brain_v42/maintenance/xml_scrub.py": ("reader", None, "the scrubbing regex"),
}


#: Does this module DECIDE what happens to `focus_updated_at`? Answered from the
#: AST, and that took two tries — both failures of the same family, both found by
#: removing the stamp and watching the guard stay green:
#:
#:   1. matching raw source let a COMMENT satisfy the rule, so the prose written
#:      to explain the stamp stood in for the stamp;
#:   2. matching code let the IMPORT satisfy it — `from … import focus_stamp` on
#:      a module that no longer calls it.
#:
#: A mention is not a decision. What counts is a CALL to `focus_stamp`, or
#: `focus_updated_at` in a position that assigns it: a dict key, a keyword
#: argument, or a subscript target.
def _decides_the_focus_date(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "focus_stamp":
                return True
        if isinstance(node, ast.keyword) and node.arg == "focus_updated_at":
            return True
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == "focus_updated_at":
                    return True
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if node.slice.value == "focus_updated_at":
                return True
    return False


def _modules_naming_the_column() -> set[str]:
    found: set[str] = set()
    for root in SOURCES:
        for path in root.rglob("*.py"):
            if COLUMN in path.read_text(encoding="utf-8"):
                found.add(path.relative_to(ROOT).as_posix())
    return found


def test_no_module_touches_the_column_without_being_declared() -> None:
    """A new writer appearing anywhere under `src/` reddens HERE, not in production."""
    undeclared = sorted(_modules_naming_the_column() - set(FOCUS_WRITERS))

    assert not undeclared, (
        "these modules name `current_focus` and are declared nowhere — say what each "
        "one does with it, and whether it stamps:\n    " + "\n    ".join(undeclared)
    )


def test_the_declaration_does_not_outlive_the_code_it_describes() -> None:
    """The mirror: a declared module that no longer names the column is a stale entry."""
    stale = sorted(set(FOCUS_WRITERS) - _modules_naming_the_column())

    assert not stale, "declared but no longer naming the column:\n    " + "\n    ".join(stale)


def test_every_writer_that_claims_to_stamp_really_does() -> None:
    """`stamps=True` is checked against the source, never taken on trust."""
    liars = sorted(
        module
        for module, (role, stamps, _note) in FOCUS_WRITERS.items()
        if role == "writer" and stamps and not _decides_the_focus_date(ROOT / module)
    )

    assert not liars, (
        "declared as stamping, but neither `focus_stamp` nor `focus_updated_at` appears "
        "in the source — the focus age stops being honest after these write:\n    "
        + "\n    ".join(liars)
    )


#: EMPTY since 2026-09-03: every writer decides what happens to
#: `focus_updated_at`. It stays as a named set rather than being deleted, because
#: an empty set is an assertion — "no writer escapes the rule" — and the test
#: below turns a new escapee into a red line instead of a silent one.
UNSTAMPED_WRITERS: frozenset[str] = frozenset()


def test_no_writer_escapes_the_stamping_rule() -> None:
    """Ticket `5281f0ef`: the hole its own author predicted, measured and pinned.

    The bound of 2026-08-23 lives at the MCP layer alone. These two writers reach
    the column without passing through it, and without `focus_stamp` either — so
    after either of them runs, `focus_updated_at` answers "when was this prose
    written?" with a date older than the prose.

    The list is exact in BOTH directions. A third unstamped writer reddens it; so
    does stamping one of these two, which forces the fix and its record to travel
    together.
    """
    measured = {
        module
        for module, (role, stamps, _note) in FOCUS_WRITERS.items()
        if role == "writer" and stamps is False
    }

    assert measured == UNSTAMPED_WRITERS
    for module in UNSTAMPED_WRITERS:
        assert not _decides_the_focus_date(ROOT / module), (
            f"{module} now stamps — move it out of UNSTAMPED_WRITERS and say so"
        )
