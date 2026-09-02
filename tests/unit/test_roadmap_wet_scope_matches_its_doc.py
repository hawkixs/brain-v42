"""What the nightly wet applies must be said the same way in code and in the plan.

Ticket `e9b2faf4`, defect 1 — and it was false in the REASSURING direction, which
is why it is worth a test rather than a one-line edit.

`WET_APPLYABLE_OPS = VALID_OPS` since the evening of 2026-07-04: the aggressive
regime applies all four ops, merge and rename included. That is a dated operator
decision, recorded in the constant's own comment and pinned by
`test_roadmap_curate.py::test_wet_applyable_ops_includes_all_ops`. The plan
document, written before it, still told a reader the opposite — that flipping
`BRAIN_DREAM_ROADMAP_DRY_RUN=false` was safe because only `archive`/`status`
would apply. An operator who believed it would have armed merge and rename.

THE GUARD IS SCOPED, AND ON PURPOSE. This module governs the plan's two LIVE
normative statements — the Global Constraints bullet and the Rollout step that
tells an operator when to flip the flag. It deliberately does NOT scan the whole
file: the plan also carries LISTINGS of the source and tests it originally
prescribed, including `WET_APPLYABLE_OPS = ("archive", "status")`. Those describe
what the plan asked for in July and are historical; rewriting them would erase
the fact that the delivered code diverged by an explicit later decision. Same
doctrine as `test_runbook_normative_values_have_one_source.py`: the regions are
named here, and adding one costs a reviewed edit of this file.

The expected claim is DERIVED from the constant, never retyped: the day someone
narrows `WET_APPLYABLE_OPS` back to `{archive, status}`, this test turns red and
requires the document to follow — in that direction too.
"""

from __future__ import annotations

from pathlib import Path

from scripts.roadmap_curate import VALID_OPS, WET_APPLYABLE_OPS

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN = REPO_ROOT / "docs" / "superpowers" / "plans" / "2026-07-04-roadmap-curation.md"

#: The two live normative statements, each anchored by text a rewrite cannot keep
#: by accident. They are the only regions this module governs.
_GLOBAL_CONSTRAINT_ANCHOR = "- The nightly run"
_ROLLOUT_ANCHOR = "**Step 6: Rollout**"

#: Phrasings that assert the narrow scope. Any of them is false while
#: WET_APPLYABLE_OPS equals VALID_OPS.
_NARROW_CLAIMS = (
    "restricted to `archive`/`status`",
    "applies ONLY archive/status",
    "NEVER applies merge/rename",
)


def _plan_lines() -> list[str]:
    return PLAN.read_text(encoding="utf-8").splitlines()


def _live_lines() -> list[str]:
    """The lines of the two governed statements, and nothing else."""
    lines = _plan_lines()
    return [
        line
        for line in lines
        if line.lstrip().startswith(_GLOBAL_CONSTRAINT_ANCHOR) or _ROLLOUT_ANCHOR in line
    ]


def test_the_two_governed_statements_exist() -> None:
    """Non-vacuity: a renamed section must redden here, not silently empty the guard.

    Without this, deleting the Global Constraints bullet would make every
    assertion below true on nothing at all.
    """
    live = _live_lines()
    assert any(_GLOBAL_CONSTRAINT_ANCHOR in line for line in live), (
        "the Global Constraints bullet about the nightly run is gone — this guard "
        "now governs nothing, and the claim it protects can drift freely"
    )
    assert any(_ROLLOUT_ANCHOR in line for line in live), (
        "the Rollout step 6 is gone — it is the sentence that tells an operator "
        "when flipping BRAIN_DREAM_ROADMAP_DRY_RUN is safe"
    )


def test_the_wet_scope_is_the_aggressive_regime() -> None:
    """The premise, read from the code, so the rest of this module cannot be stale."""
    assert set(WET_APPLYABLE_OPS) == set(VALID_OPS)
    assert {"merge", "rename"} <= set(WET_APPLYABLE_OPS)


def test_no_governed_statement_claims_the_narrow_scope() -> None:
    """The plan must not promise a bound the code stopped holding on 2026-07-04."""
    offenders = [
        (claim, line.strip()) for line in _live_lines() for claim in _NARROW_CLAIMS if claim in line
    ]
    assert not offenders, (
        f"the plan still claims the narrow wet scope while WET_APPLYABLE_OPS is "
        f"{sorted(set(WET_APPLYABLE_OPS))}: {offenders}. False in the reassuring "
        f"direction — an operator reading it would arm merge and rename believing "
        f"they were bounded."
    )


def test_the_rollout_step_names_what_a_flip_would_actually_arm() -> None:
    """Absence is not enough: the operator needs the true statement, at the flip.

    Removing the false clause and saying nothing would leave step 6 mute on the
    one fact that decides whether the flip is safe.
    """
    step = next(line for line in _live_lines() if _ROLLOUT_ANCHOR in line)
    for op in sorted(set(WET_APPLYABLE_OPS)):
        assert op in step, (
            f"rollout step 6 does not name {op!r}, which the nightly wet would "
            f"apply the moment BRAIN_DREAM_ROADMAP_DRY_RUN is set to false"
        )
