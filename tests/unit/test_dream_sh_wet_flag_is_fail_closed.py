"""A DRY_RUN killswitch arms writing only on an explicit `false`.

Measured on 2026-09-03 while writing the 7511c210 test, and it is the reason
this lot exists. `dream.sh` translated three of its four DRY_RUN killswitches
with `!= "true"`:

    if [[ "$BRAIN_DREAM_EXTRACT_DRY_RUN" != "true" ]]; then
      roadmap_args+=(--wet)

So `false` arms the wet run — and so do `False`, `0`, `flase`, and an EMPTY
value. The fourth killswitch, REORG, used the mirror idiom `== "true"` in two
places, which falls through to the global `DRY_RUN`, itself defaulting to
`false`. Two opposite spellings, one destination: every value nobody
anticipated ends up WET.

That is fail-OPEN on the only switch standing between a proposer and a writer.
A typo in a systemd drop-in is not an exotic event, and the drop-in is edited by
hand at 5am. The direction is now reversed: writing requires the one value that
can only be typed on purpose, and everything else falls back to DRY *loudly*,
naming the variable and the value it received (learnings 083d74e5, e2e5a550) —
a silent fallback would trade a dangerous night for an invisible one.

WHY THIS TEST RUNS BASH INSTEAD OF READING THE FILE. The test in
`test_dream_roadmap_configured_primary_is_guarded.py` asserts the SHAPE of the
comparison, which is all a text reader can do; it cannot tell you what bash does
with `0` or with an empty string. Those semantics are the whole subject here, so
this file extracts the function and EXECUTES it (learning 187f107c: a test that
merely re-reads the implementation shares its blind spots).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DREAM_SH = Path(__file__).resolve().parents[2] / "scripts" / "dream.sh"

#: The helper under test. Extracted rather than sourced: sourcing `dream.sh`
#: would run its whole preamble, which resolves paths, parses argv and would
#: eventually touch the log directory of a real night.
_FUNCTION = "dream_wants_wet"

#: The killswitches this helper serves, with the phase each one gates.
#: BRAIN_DREAM_ROADMAP_DRY_RUN was a fourth until 2026-09-10, when the nightly
#: roadmap phase was removed from the rail (ADR 45671595). The live drop-in may
#: still set it; nothing in dream.sh reads it, so it gates nothing.
KILLSWITCHES = (
    "BRAIN_DREAM_REORG_DRY_RUN",
    "BRAIN_DREAM_EXTRACT_DRY_RUN",
    "BRAIN_DREAM_SWEEP_DRY_RUN",
)


def _extract_function(name: str = _FUNCTION) -> str:
    source = DREAM_SH.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\)\s*\{{.*?^\}}", source, re.DOTALL | re.MULTILINE)
    assert match, f"{name}() not found in {DREAM_SH.name}"
    return match.group(0)


def _run(value: str | None, *, var: str = "BRAIN_DREAM_EXTRACT_DRY_RUN", body: str | None = None):
    """Execute the helper in a clean bash, with `log` stubbed onto stdout.

    `value=None` means the variable is UNSET, which `set -u` would otherwise
    turn into a crash — the call site uses `${VAR-}` so the helper receives an
    empty string, and that is the case reproduced here.
    """
    function = _extract_function() if body is None else body
    script = (
        "set -euo pipefail\n"
        'log() { echo "$*"; }\n'
        f"{function}\n"
        + ("" if value is None else f"export {var}={value!r}\n")
        + f'if {_FUNCTION} {var} "${{{var}-}}"; then echo "VERDICT=wet"; '
        'else echo "VERDICT=dry"; fi\n'
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, check=False
    )


def _verdict(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, f"the helper must never abort the night: {result.stderr}"
    match = re.search(r"VERDICT=(wet|dry)", result.stdout)
    assert match, f"no verdict printed; stdout={result.stdout!r} stderr={result.stderr!r}"
    return match.group(1)


# ── guards on the harness itself ─────────────────────────────────────────────


def test_the_script_still_parses() -> None:
    """`bash -n` on the real file: a refactor that breaks it loses the whole night."""
    result = subprocess.run(
        ["bash", "-n", str(DREAM_SH)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_the_extraction_returns_a_function_and_not_the_whole_file() -> None:
    """A sed-style extraction that over-matched would test something else entirely."""
    body = _extract_function()

    assert body.startswith(f"{_FUNCTION}()")
    assert body.count("\n") < 40, "the extraction swallowed more than the function"


# ── the semantics, executed ──────────────────────────────────────────────────


def test_an_explicit_false_is_the_only_way_to_arm_a_wet_run() -> None:
    assert _verdict(_run("false")) == "wet"


def test_an_explicit_true_stays_dry() -> None:
    assert _verdict(_run("true")) == "dry"


@pytest.mark.parametrize("value", ["", "False", "FALSE", "0", "flase", "no", "n0"])
def test_every_unanticipated_value_falls_back_to_dry(value: str) -> None:
    """The reversal. Each of these armed `--wet` before this lot.

    `False` and `0` are the plausible ones — a human writing Python or C by
    reflex. `flase` is the one that actually happens at 5am.
    """
    assert _verdict(_run(value)) == "dry"


def test_an_unset_variable_stays_dry() -> None:
    assert _verdict(_run(None)) == "dry"


# ── the fallback is loud, and names its cause ────────────────────────────────


@pytest.mark.parametrize("value", ["", "False", "0", "flase"])
def test_the_fallback_names_the_variable_and_the_value(value: str) -> None:
    """A silent downgrade trades a dangerous night for an invisible one.

    The operator reading the log at 07:00 needs both halves: WHICH switch was
    misread, and WHAT it contained — the value is what tells them it was a typo
    rather than a decision (learning e2e5a550).
    """
    result = _run(value, var="BRAIN_DREAM_SWEEP_DRY_RUN")

    printed = result.stdout
    assert "BRAIN_DREAM_SWEEP_DRY_RUN" in printed
    assert repr(value) in printed or f"'{value}'" in printed or f"{value!r}" in printed


@pytest.mark.parametrize("value", ["true", "false"])
def test_a_canonical_value_says_nothing(value: str) -> None:
    """A warning printed every night stops being read."""
    result = _run(value)

    assert "BRAIN_DREAM_EXTRACT_DRY_RUN" not in result.stdout.replace("VERDICT=wet", "").replace(
        "VERDICT=dry", ""
    )


# ── the call sites, and the defaults that keep `set -u` out of it ────────────


def test_every_killswitch_reaches_the_helper() -> None:
    """No phase may keep a hand-rolled comparison beside the shared one."""
    source = DREAM_SH.read_text(encoding="utf-8")

    for variable in KILLSWITCHES:
        assert f"{_FUNCTION} {variable}" in source, f"{variable} does not use {_FUNCTION}"


def test_no_killswitch_is_compared_by_hand_any_more() -> None:
    """The idioms this lot removes, asserted absent rather than assumed gone.

    Both directions: `!= "true"` armed the wet run on any unknown value, and
    `== "true"` let an unknown value fall through to the global `DRY_RUN`, which
    defaults to `false`. They failed in the same direction by opposite means.
    """
    source = DREAM_SH.read_text(encoding="utf-8")

    for variable in KILLSWITCHES:
        assert f'"${variable}" != "true"' not in source
        assert f'"${variable}" == "true"' not in source


@pytest.mark.parametrize("variable", KILLSWITCHES)
def test_each_killswitch_still_carries_an_explicit_default(variable: str) -> None:
    """`set -euo pipefail` turns an unset variable into a dead night.

    The defaults are what keep "absent" from ever reaching the helper as unset,
    so they are part of this contract rather than incidental.
    """
    source = DREAM_SH.read_text(encoding="utf-8")

    assert re.search(rf'^{variable}="\$\{{{variable}:-(true|false)\}}"$', source, re.MULTILINE)


# ── the night that runs tonight must not change ──────────────────────────────


def test_todays_live_values_are_all_canonical() -> None:
    """The lot is a no-op on the live configuration, and this is what proves it.

    Every live killswitch is an explicit `true` or `false`, so each one takes the
    same branch before and after. Skips off the server, where the drop-in cannot
    exist.
    """
    drop_in = Path.home() / ".config/systemd/user/brain-v42-dream.service.d/killswitches.conf"
    if not drop_in.exists():
        pytest.skip(f"{drop_in} absent — not the server")

    settings = {}
    for line in drop_in.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith("Environment="):
            continue
        key, _, value = stripped[len("Environment=") :].partition("=")
        settings[key.strip()] = value.strip()

    for variable in KILLSWITCHES:
        assert variable in settings, f"{variable} unset in the live drop-in — re-read the report"
        assert settings[variable] in {"true", "false"}, (
            f"{variable}={settings[variable]!r} is not canonical; this lot would CHANGE "
            "tonight's behaviour and must not be merged before that is resolved"
        )


def test_shutil_finds_bash() -> None:
    """Guard on every test above: a missing bash would make them all vacuous."""
    assert shutil.which("bash")
