"""The briefing must not announce WET a phase `dream.sh` is running DRY.

Residual found by adversarial review of the fail-closed lot, 2026-09-04.
`scripts/dream.sh` now arms a phase's `--wet` only on the literal string
`false`; anything else -- empty, `False`, `0`, a typo -- stays DRY and says so.
`parse_killswitches` still coerced with `value.lower() == "true"`, so on the
same non-canonical value it reported `*_dry = False`, and the session briefing
and `/metrics` announced a WET phase while the night ran it DRY.

BEFORE that lot the two AGREED, both being fail-open. The lot created the
divergence, in precisely the scenario it exists for. It is safe in the
conservative direction -- the night does less than the display claims, never
more -- but a display that contradicts the rail is how an operator learns to
distrust the display.

WHAT WAS **NOT** BROKEN, and the measurement bounds the fix: the five
`*_ENABLED` flags already agreed on the canonical words. `dream.sh` reads them
`!= "true"` and SKIPS, so a non-canonical value disables the phase;
`parse_killswitches` read `== "true"` and reported it disabled. Same answer,
both fail-closed. Only the four `*_DRY_RUN` flags diverged on polarity.

ONE THING BEYOND THE FINDING, and it is stated rather than slipped in: the
comparison was CASE-INSENSITIVE on both families, so `True` read as enabled and
`False` as wet, while `dream.sh` compares the raw string and treats either as
"not the canonical word". Fixing the polarity while staying lenient about case
would have rebuilt the same divergence one line further along, so both families
now compare exactly. Measured on the live drop-in of 2026-09-04: every value is
already lower-case, so nothing that runs tonight changes.

A BOOLEAN CANNOT CARRY THE THIRD STATE. "dry because the operator said so" and
"dry because nobody could read what the operator wrote" are different facts, and
the second is the one worth a human's attention at 07:00 (learning 083d74e5).
The boolean is corrected so it never lies, and the third state travels in a
SECOND function -- the shape this module already chose for the project pool,
whose comment says so in as many words: "A second function, not one more key."
"""

from __future__ import annotations

import pytest

from brain_v42.dream_killswitches import (
    non_canonical_killswitches,
    parse_killswitches,
)

_DRY_KEYS = {
    "BRAIN_DREAM_REORG_DRY_RUN": "reorg_dry",
    "BRAIN_DREAM_EXTRACT_DRY_RUN": "extract_dry",
    "BRAIN_DREAM_ROADMAP_DRY_RUN": "roadmap_dry",
    "BRAIN_DREAM_SWEEP_DRY_RUN": "sweep_dry",
}


def _drop_in(key: str, value: str) -> str:
    return f"[Service]\nEnvironment={key}={value}\n"


# ── the divergence, closed ───────────────────────────────────────────────────


@pytest.mark.parametrize(("env", "flag"), sorted(_DRY_KEYS.items()))
@pytest.mark.parametrize("value", ["", "False", "FALSE", "0", "flase", "no"])
def test_a_non_canonical_dry_flag_reads_as_dry_like_the_rail(
    env: str, flag: str, value: str
) -> None:
    """`dream.sh` runs these DRY, so the briefing must say DRY."""
    flags = parse_killswitches(_drop_in(env, value))

    assert flags[flag] is True


@pytest.mark.parametrize(("env", "flag"), sorted(_DRY_KEYS.items()))
def test_an_explicit_false_is_the_only_wet(env: str, flag: str) -> None:
    assert parse_killswitches(_drop_in(env, "false"))[flag] is False


@pytest.mark.parametrize(("env", "flag"), sorted(_DRY_KEYS.items()))
def test_an_explicit_true_is_dry(env: str, flag: str) -> None:
    assert parse_killswitches(_drop_in(env, "true"))[flag] is True


# ── the enabled flags were already right, and must stay right ────────────────


@pytest.mark.parametrize(
    ("env", "flag"),
    [
        ("BRAIN_DREAM_PROMOTE_ENABLED", "promote"),
        ("BRAIN_DREAM_REORG_ENABLED", "reorg"),
        ("BRAIN_DREAM_EXTRACT_ENABLED", "extract"),
        ("BRAIN_DREAM_ROADMAP_ENABLED", "roadmap"),
        ("BRAIN_DREAM_SWEEP_ENABLED", "sweep"),
    ],
)
@pytest.mark.parametrize("value", ["false", "", "flase", "0"])
def test_a_non_true_enabled_flag_still_reads_as_disabled(env: str, flag: str, value: str) -> None:
    """`dream.sh` reads these `!= "true"` and SKIPS. Both sides already agreed.

    Pinned rather than assumed: this lot changes the neighbouring coercion, and
    flipping these by accident would announce a phase that never ran.
    """
    assert parse_killswitches(_drop_in(env, value))[flag] is False


@pytest.mark.parametrize(
    ("env", "flag"),
    [("BRAIN_DREAM_PROMOTE_ENABLED", "promote"), ("BRAIN_DREAM_REORG_ENABLED", "reorg")],
)
def test_an_explicit_true_enabled_flag_reads_as_enabled(env: str, flag: str) -> None:
    assert parse_killswitches(_drop_in(env, "true"))[flag] is True


# ── the third state, which no boolean can carry ──────────────────────────────


def test_a_canonical_drop_in_reports_nothing_non_canonical() -> None:
    """Today's live shape. A report that fires every night stops being read."""
    content = (
        "[Service]\n"
        "Environment=BRAIN_DREAM_REORG_DRY_RUN=false\n"
        "Environment=BRAIN_DREAM_EXTRACT_DRY_RUN=false\n"
        "Environment=BRAIN_DREAM_ROADMAP_DRY_RUN=true\n"
        "Environment=BRAIN_DREAM_SWEEP_DRY_RUN=false\n"
        "Environment=BRAIN_DREAM_REORG_ENABLED=true\n"
    )

    assert non_canonical_killswitches(content) == {}


@pytest.mark.parametrize("value", ["", "False", "0", "flase"])
def test_a_non_canonical_value_is_named_with_what_it_contained(value: str) -> None:
    """The value is what tells an operator it was a typo and not a decision.

    Asserting the CAUSE by name rather than a bare flag (learning e2e5a550).
    """
    found = non_canonical_killswitches(_drop_in("BRAIN_DREAM_SWEEP_DRY_RUN", value))

    assert found == {"BRAIN_DREAM_SWEEP_DRY_RUN": value}


def test_it_reports_every_offending_key_not_only_the_first() -> None:
    content = (
        "[Service]\n"
        "Environment=BRAIN_DREAM_REORG_DRY_RUN=flase\n"
        "Environment=BRAIN_DREAM_SWEEP_DRY_RUN=0\n"
        "Environment=BRAIN_DREAM_ROADMAP_DRY_RUN=true\n"
    )

    assert non_canonical_killswitches(content) == {
        "BRAIN_DREAM_REORG_DRY_RUN": "flase",
        "BRAIN_DREAM_SWEEP_DRY_RUN": "0",
    }


def test_a_key_this_module_does_not_own_is_ignored() -> None:
    """The project pool is list-valued and has its own parser, on purpose."""
    content = "[Service]\nEnvironment=BRAIN_DREAM_PROJECT_POOL=red,brain-v42\n"

    assert non_canonical_killswitches(content) == {}


def test_a_missing_key_is_not_a_non_canonical_one() -> None:
    """Absent means "use the default", which is a decision, not a typo."""
    assert non_canonical_killswitches("[Service]\n") == {}
