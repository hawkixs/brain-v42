"""`phase_reorg.md` must document BOTH counters the summary row now prints.

Ticket `1597c36d` (PR #100, merge `40f6b32`) changed `_summary_access` to render
`access:{human} | reads:{machine}` -- both counters, labelled -- because the
guardrail was reading `access_count`, the counter the nightly scans themselves
inflate, instead of `access_count_human`.

The code changed. This prompt did not. `phase_reorg.md` still showed the row
template with a single `access:N` field and described it as "the live
access_count" -- the MACHINE counter, by name, in the very prose meant to tell
the phase what the number means. A model reading that sentence and then a live
row showing `access:5 | reads:13` has two disagreeing sources for the same
number, which is exactly the shape that produced the incident this ticket is
named after: two independent readers of a REORG log concluding the briefing was
wrong when entity `test_origin_check` showed `access:13` (its OLD, pre-fix
rendering) although `access_count_human` was `5` in the database.

This test pins prose to code so the two can't drift apart silently again: it
reads the ACTUAL rendered row from `_summary_access` and asserts the prompt's
own description contains the same two labels, and that the stale phrase
"live access_count" for the `access:` field is gone.
"""

from __future__ import annotations

from pathlib import Path

from brain_v42.mcp.tools.formatters import _summary_access

REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE_REORG_PROMPT = REPO_ROOT / "scripts" / "dream" / "phase_reorg.md"


class _FakeEntity:
    """Mirrors the incident's own numbers: human 5, machine 13."""

    access_count = 13
    access_count_human = 5


def _prompt_text() -> str:
    return PHASE_REORG_PROMPT.read_text(encoding="utf-8")


def test_the_rendered_row_carries_two_labelled_counters() -> None:
    """Sanity check on the fixture itself: both labels are in the real output."""
    rendered = _summary_access(_FakeEntity())
    assert rendered == "access:5 | reads:13"


def test_the_prompt_template_line_shows_both_labels() -> None:
    """The row shape documented for the phase must match what it will see."""
    text = _prompt_text()
    assert "access:N | reads:" in text, (
        "phase_reorg.md still documents the row as a single `access:N` field; "
        "the live row carries `access:N | reads:M`."
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
    assert "reads:" in text
