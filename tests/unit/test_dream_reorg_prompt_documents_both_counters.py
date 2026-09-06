"""`phase_reorg.md` must document BOTH counters the summary row now prints,
consistently across the WHOLE file -- including the operative guardrail.

Ticket `1597c36d` (PR #100, merge `40f6b32`) changed `_summary_access` to render
`access:{human} | reads:{machine}` -- both counters, labelled -- because the
guardrail was reading `access_count`, the counter the nightly scans themselves
inflate, instead of `access_count_human`.

The code changed. This prompt's Part 2 explanation caught up, but the
"Guardrails" section -- the most authoritative part of the file, restated
across all parts -- kept `access_count > 5` verbatim: a bare reference to the
MACHINE counter used as the archive threshold, which is the exact bug PR #100
fixed in code. A model reading the guardrail literally and the explanation four
paragraphs above gets two different columns for the same decision.

Two kinds of assertion live in this file:

- Prose-anchored: independently hardcoded literals that pin specific sentences
  (the stale "live access_count" phrase must be gone; the explanation must name
  `access_count_human`). These do not derive from code and cannot catch every
  drift by construction -- they are a targeted prose contract, not a generic
  guard.
- Code-derived: `test_the_prompt_template_line_shows_both_labels` renders
  `_summary_access` and extracts the two labels (`access`, `reads`) from the
  ACTUAL output, then checks the prompt's row template uses the same labels in
  the same order. If `_summary_access` ever changes its labels or their order,
  this assertion changes with it instead of silently going stale.

Neither kind alone would have caught the guardrail bug: the prose tests only
grepped the explanation paragraph, and the code-derived test only looks at the
template line. `test_no_bare_access_count_used_as_a_threshold_anywhere` and
`test_the_guardrails_section_pins_the_threshold_to_the_human_counter` close
that gap by inspecting the Guardrails section itself, not just the paragraph
that explains it.
"""

from __future__ import annotations

import re
from pathlib import Path

from brain_v42.mcp.tools.formatters import _summary_access

REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE_REORG_PROMPT = REPO_ROOT / "scripts" / "dream" / "phase_reorg.md"
GUARDRAILS_HEADING = "## Guardrails"


class _FakeEntity:
    """Mirrors the incident's own numbers: human 5, machine 13."""

    access_count = 13
    access_count_human = 5


def _prompt_text() -> str:
    return PHASE_REORG_PROMPT.read_text(encoding="utf-8")


def _rendered_labels() -> list[str]:
    """Labels the summary row actually carries, in rendered order."""
    rendered = _summary_access(_FakeEntity())
    return [segment.split(":", 1)[0] for segment in rendered.split(" | ")]


def _guardrails_section(text: str) -> str:
    """Slice from the '## Guardrails' heading to end of file.

    It is the last section in `phase_reorg.md`; if that ever stops being true,
    `.index()` still finds the heading and this still returns "everything from
    there on", which remains the guardrail text this test cares about.
    """
    start = text.index(GUARDRAILS_HEADING)
    return text[start:]


def test_the_rendered_row_carries_two_labelled_counters() -> None:
    """Sanity check on the fixture itself: both labels are in the real output."""
    rendered = _summary_access(_FakeEntity())
    assert rendered == "access:5 | reads:13"


def test_the_prompt_template_line_shows_both_labels() -> None:
    """The row shape documented for the phase must match what it will see.

    Derived from code: the expected template is built from the labels
    `_summary_access` actually renders, not a second hardcoded copy of them.
    """
    labels = _rendered_labels()
    assert labels == ["access", "reads"]
    template = f"{labels[0]}:N | {labels[1]}:"
    text = _prompt_text()
    assert template in text, (
        f"phase_reorg.md does not document the row template `{template}`, "
        f"which is the shape `_summary_access` actually renders."
    )


def test_the_prompt_no_longer_calls_access_the_live_access_count() -> None:
    """`access:` is the HUMAN counter (1597c36d) -- not `access_count` itself."""
    text = _prompt_text()
    assert "is the live access_count" not in text, (
        "this phrase describes `access:N` as the machine counter, which is the "
        "exact bug PR #100 fixed in code; the prompt must not re-teach it."
    )


def test_the_prompt_names_the_human_counter_and_the_machine_counter() -> None:
    """The explanation must say which field is which, not just show them."""
    text = _prompt_text()
    assert "access_count_human" in text


def test_no_bare_access_count_used_as_a_threshold_anywhere() -> None:
    """The machine counter must never be used as a bare threshold, anywhere.

    `access_count` (unlabelled, no `_human` suffix) is the counter the nightly
    scans themselves inflate. `access_count > 5` as a threshold is the exact
    bug PR #100 fixed in code; nothing in this prompt may reintroduce it, in
    any section -- not just the explanatory paragraph checked above.
    """
    text = _prompt_text()
    assert re.search(r"access_count\s*>", text) is None, (
        "found a bare `access_count > ...` threshold; the guardrail must read "
        "the human counter shown as `access:N` (`access_count_human`), never "
        "the raw machine counter `access_count`."
    )


def test_the_guardrails_section_pins_the_threshold_to_the_human_counter() -> None:
    """The OPERATIVE guardrail, not just the prose above it, must name the
    field the summary row actually carries -- matching Part 2 step (b), which
    already reads `access:N` for this same decision.
    """
    guardrails = _guardrails_section(_prompt_text())
    assert "access:N > 5" in guardrails, (
        "the Guardrails section must state the archive threshold on `access:N` "
        "(the human counter shown in the summary row), matching Part 2 step (b)."
    )
