"""What the nightly wet applies must be said the same way in code and in the plan.

Ticket `e9b2faf4`, defect 1 — and it was false in the REASSURING direction, which
is why it is worth a test rather than a one-line edit.

The scope has now moved TWICE, which is the whole reason this guard is derived
rather than typed. `WET_APPLYABLE_OPS = VALID_OPS` from the evening of 2026-07-04
(aggressive regime); narrowed back to `("archive", "status")` on 2026-09-02 on an
operator decision taken against measurement — of the 181 proposals the wet ever
applied, 150 were `merge` or `rename`, and human review rejected 592 against those
181. Between those two dates the plan promised a bound the code had stopped
holding, and an operator who believed it would have armed merge and rename.

THE GUARD IS BIDIRECTIONAL, and it has to be. Its first version only asked
whether the plan understated the scope, because that was the live defect. A guard
built that way goes quiet the moment the code moves the other way: after the
2026-09-02 narrowing the document could have kept announcing all four ops
forever — overstating this time — and nothing would have reddened. The claim
checked is therefore SELECTED by the constant: whichever of the two families
contradicts the code is the one that must be absent.

THE GUARD IS ALSO SCOPED, on purpose. This module governs the plan's two LIVE
normative statements — the Global Constraints bullet and the Rollout step that
tells an operator when to flip the flag. It deliberately does NOT scan the whole
file: the plan also carries LISTINGS of the source and tests it originally
prescribed, including `WET_APPLYABLE_OPS = ("archive", "status")`. Those describe
what the plan asked for in July and are historical; rewriting them would erase the
fact that the delivered code diverged and came back by two explicit decisions.
Same doctrine as `test_runbook_normative_values_have_one_source.py`: the regions
are named here, and adding one costs a reviewed edit of this file.
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

#: Phrasings that assert the NARROW scope. False while the wet applies all four.
_NARROW_CLAIMS = (
    "restricted to `archive`/`status`",
    "applies ONLY archive/status",
    "NEVER applies merge/rename",
)

#: Phrasings that assert the WIDE scope. False while the wet is bounded to
#: archive/status — the direction the 2026-09-02 narrowing opened, and which the
#: first version of this module could not see.
_WIDE_CLAIMS = (
    "is all four ops",
    "applies ALL FOUR ops",
    "merge and rename included",
    "apply `merge`, `rename`, `archive` and `status` without review",
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


def _wet_is_wide() -> bool:
    """Read the live scope from the code. Nothing below may retype it."""
    return {"merge", "rename"} <= set(WET_APPLYABLE_OPS)


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


def test_the_wet_scope_is_bounded_to_archive_and_status() -> None:
    """The premise, read from the code, so the rest of this module cannot be stale.

    Narrowed on 2026-09-02; `merge`/`rename` stay PROPOSABLE, hence the strict
    subset rather than an equality against a second hand-written pair.
    """
    assert set(WET_APPLYABLE_OPS) == {"archive", "status"}
    assert set(WET_APPLYABLE_OPS) < set(VALID_OPS)
    assert not _wet_is_wide()


def test_no_governed_statement_contradicts_the_code() -> None:
    """The plan must not promise a bound the code does not hold — either way round.

    The offending family is chosen BY the constant: understating is the fault
    while the wet is wide, overstating is the fault while it is narrow. Typing
    one of them here would freeze the guard against the scope of the day it was
    written, which is the failure this module exists to prevent.
    """
    forbidden = _NARROW_CLAIMS if _wet_is_wide() else _WIDE_CLAIMS
    direction = "understates" if _wet_is_wide() else "overstates"
    offenders = [
        (claim, line.strip()) for line in _live_lines() for claim in forbidden if claim in line
    ]
    assert not offenders, (
        f"the plan {direction} the wet scope while WET_APPLYABLE_OPS is "
        f"{sorted(set(WET_APPLYABLE_OPS))}: {offenders}. An operator reading it "
        f"would misjudge what flipping BRAIN_DREAM_ROADMAP_DRY_RUN arms."
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


def test_the_rollout_step_says_review_still_applies_the_excluded_ops() -> None:
    """Bounding the unattended path must not read as retiring the op.

    `merge` and `rename` are still proposed every night and still applicable by
    review. A step 6 that named only the two wet ops would let a reader conclude
    the other two had been dropped, which would silently strand the 140 pending
    proposals they cover.
    """
    excluded = sorted(set(VALID_OPS) - set(WET_APPLYABLE_OPS))
    if not excluded:
        return
    step = next(line for line in _live_lines() if _ROLLOUT_ANCHOR in line)
    for op in excluded:
        assert op in step, (
            f"rollout step 6 never mentions {op!r}: bounded out of the nightly wet, "
            f"it is still proposed and still applicable under review, and a reader "
            f"must not take its absence for a retirement"
        )
    assert "review" in step.lower()
