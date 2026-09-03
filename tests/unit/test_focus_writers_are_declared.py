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
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
SOURCES = (ROOT / "src",)
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
    # ── writers OUTSIDE the MCP layer, and outside the bound ──────────────
    "src/brain_v42/scripts/scrub_xml_tool_call_leak.py": (
        "writer",
        False,
        "maintenance scrub, --live rewrites the prose to strip a leaked tool call "
        "and sets neither focus_updated_at nor the bound — measured 2026-09-03",
    ),
    "src/brain_v42/scripts/migrate_neo4j_to_pg.py": (
        "writer",
        False,
        "one-shot datalake_v2 import, referenced nowhere else in the tree; writes "
        "the column through a generic row dict, unstamped and unbounded",
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

_STAMPS = re.compile(r"focus_stamp|focus_updated_at")


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
        if role == "writer"
        and stamps
        and not _STAMPS.search((ROOT / module).read_text(encoding="utf-8"))
    )

    assert not liars, (
        "declared as stamping, but neither `focus_stamp` nor `focus_updated_at` appears "
        "in the source — the focus age stops being honest after these write:\n    "
        + "\n    ".join(liars)
    )


#: The writers that do NOT stamp, measured on 2026-09-03 and frozen here.
#: Frozen rather than merely tolerated: a NEW unstamped writer reddens the test
#: below, and STAMPING one of these two also reddens it — which is the point.
#: Neither is fixed in this batch. Both write the production database when run,
#: so the decision to route them through the shared path is an operator's, taken
#: with the ticket in hand, not a side effect of a census.
UNSTAMPED_WRITERS = frozenset(
    {
        "src/brain_v42/scripts/scrub_xml_tool_call_leak.py",
        "src/brain_v42/scripts/migrate_neo4j_to_pg.py",
    }
)


def test_the_writers_outside_the_shared_path_are_exactly_the_two_named_ones() -> None:
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
        assert not _STAMPS.search((ROOT / module).read_text(encoding="utf-8")), (
            f"{module} now stamps — move it out of UNSTAMPED_WRITERS and say so"
        )
