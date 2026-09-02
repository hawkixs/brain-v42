"""The briefing says how much is left before `next_focus`' cap.

`brain_session_end` requires `next_focus`, capped at 10,000 characters, and that
value REPLACES `current_focus` when the compare-and-swap succeeds. The other
writer of the same column, `brain_update_project_focus`, has **no bound at all**:
neither its argument (`current_focus: str`, a bare `str`), nor the model, nor the
service, nor the column (`text`, unlimited). The MCP cap is the ONLY length bound
on the write path — so the unbounded writer can put the project into a state the
bounded writer cannot represent.

This is not theoretical. Measured on 2026-08-22, reconstructed from the
`brain_sessions.started_focus` snapshots: revision 217 carried **12,157
characters for sixteen hours**, seen by seven sessions; its closure brought the
focus back to 8,522 — **3,635 characters, 30 % of the focus, removed in one
write**.

What this line changes: today one discovers the wall **at closing time, AFTER the
work**, through a validation refusal. A line read first moves it earlier.

What it does NOT change: no contract, no behaviour, no write. It is an
observation, like the schema measurement just above it.

The margin is counted in CHARACTERS, because that is what Pydantic's `maxLength`
counts. The same focus measured that day was 9,977 characters for **10,285
bytes**: a bound counting bytes would already be crossed, and displaying both
numbers would make illegible the one line that must be read under pressure.
Whoever reopens the subject will have to say which of the two they count.
"""

from __future__ import annotations

import pytest

from brain_v42.mcp.tools.session_lifecycle_tools import NEXT_FOCUS_MAX_LENGTH
from brain_v42.mcp.tools.session_tools import _section_technical_state
from tests.unit.mcp.test_session_tools import _no_activity_ks


def _line(focus_length: int) -> str:
    out = _section_technical_state("046", focus_tracked=True, focus_length=focus_length)
    lines = [line for line in out.splitlines() if line.startswith("- Focus :")]
    assert len(lines) == 1, out
    return lines[0]


def test_a_comfortable_margin_states_both_numbers_and_the_margin() -> None:
    line = _line(4200)

    assert "4200" in line
    assert str(NEXT_FOCUS_MAX_LENGTH) in line
    assert "marge 5800" in line


def test_the_measured_focus_of_the_day_shows_twenty_three_left() -> None:
    """9,977: the real focus of 2026-08-22, pressed against the cap."""
    assert "marge 23" in _line(9977)


def test_a_null_margin_is_LOUD() -> None:
    """THE WITNESS THAT MATTERS — first half.

    At zero margin, a single extra character makes any closure impossible. That is
    the precise moment someone needs this line, hence the moment it must not look
    like one more statistic.
    """
    line = _line(NEXT_FOCUS_MAX_LENGTH)

    assert "marge 0" not in line, "à marge nulle, un nombre discret ne suffit pas"
    assert "NULLE" in line
    assert "refus" in line.lower(), "la CONSÉQUENCE doit être dite, pas seulement l'état"


@pytest.mark.parametrize("excess", [1, 240, 2157])
def test_an_exceeded_cap_is_LOUD_and_names_the_two_outcomes(excess: int) -> None:
    """THE WITNESS THAT MATTERS — second half.

    Above the cap, closing has only two outcomes and the operator must know them
    BEFORE writing: compress — hence lose text they chose, with no diff and no
    trace — or be refused. A line saying only "margin -240" would suggest a
    counting detail.
    """
    line = _line(NEXT_FOCUS_MAX_LENGTH + excess)

    assert "DÉPASSÉ" in line
    assert str(excess) in line
    assert "compress" in line.lower(), "la compression, et ce qu'elle coûte"
    assert "refus" in line.lower(), "l'autre issue"
    assert "marge -" not in line, (
        "une marge négative écrite nue se lirait comme un détail de comptage"
    )


def test_the_cap_is_not_a_parallel_literal() -> None:
    """The line and the validator must cite the SAME bound.

    Two 10,000 literals would diverge at the first change, and the briefing would
    announce a margin validation would not recognize — exactly the defect this
    batch measures, reproduced one layer up.
    """
    from pydantic import TypeAdapter, ValidationError

    from brain_v42.mcp.tools.session_lifecycle_tools import FocusArg

    adapter = TypeAdapter(FocusArg)
    adapter.validate_python("x" * NEXT_FOCUS_MAX_LENGTH)
    with pytest.raises(ValidationError):
        adapter.validate_python("x" * (NEXT_FOCUS_MAX_LENGTH + 1))


def test_an_unmeasured_focus_invents_nothing() -> None:
    """No focus, no line — "not measured" is not "full margin"."""
    out = _section_technical_state("046", focus_tracked=True, focus_length=None)

    assert "- Focus :" not in out
    assert "- Schéma : 046" in out


def test_the_nominal_path_stays_green_and_quiet() -> None:
    """The second required witness: the line is added, it replaces nothing.

    Without it, we would have made the margin visible by breaking what it
    accompanies.
    """
    before = _section_technical_state("046", focus_tracked=True)
    after = _section_technical_state("046", focus_tracked=True, focus_length=4200)

    assert "### État technique (mesuré)" in after
    assert "- Schéma : 046" in after
    assert "- Focus écrit :" in before and "- Focus écrit :" in after
    assert before.splitlines()[:2] == after.splitlines()[:2], (
        "les lignes existantes gardent leur place et leur ordre"
    )


def test_the_line_reaches_the_REAL_briefing_not_just_its_helper() -> None:
    """The witness the three green, inert batches of 08-21/22 were missing.

    A line computed by a tested helper, and never passed through the composer,
    reads exactly like an absent line. This test follows the path every session
    takes: `_format_session_briefing` must derive the `current_focus` length from
    the context, without the caller having to supply it.
    """
    from types import SimpleNamespace

    from brain_v42.mcp.tools.session_tools import _format_session_briefing

    focus = "j" * 9977
    ctx = SimpleNamespace(
        project_key="brain-v42",
        current_focus=focus,
        focus_updated_at=None,
        blockers=[],
    )

    briefing = _format_session_briefing(
        ctx, [], [], _no_activity_ks(), None, [], [], schema_revision="046"
    )

    # 9,977 ASCII "j"s: characters and bytes coincide — the composer derives BOTH
    # from the context, without the caller having to supply them.
    assert "- Focus : 9977 / 10000 caractères (marge 23 ; 9977 octets)" in briefing
    assert briefing.index("- Focus :") < briefing.index("### Focus"), (
        "la mesure doit précéder la prose qu'elle borne"
    )


def test_the_LOUD_form_survives_the_composer_too() -> None:
    """The end-to-end golden exercises ONLY the calm case — 17 characters.

    Flagged by the orchestrator while re-reading the CI log, and rightly so: the
    briefing's only end-to-end rendering carries a margin of 9,983, so the shape
    that matters — the one read under pressure — never passes through it. A golden
    is not twisted to exercise limits; this test carries them, but on the COMPOSED
    path, not on the helper alone.
    """
    from types import SimpleNamespace

    from brain_v42.mcp.tools.session_tools import _format_session_briefing

    ctx = SimpleNamespace(
        project_key="brain-v42",
        current_focus="d" * (NEXT_FOCUS_MAX_LENGTH + 240),
        focus_updated_at=None,
        blockers=[],
    )

    briefing = _format_session_briefing(
        ctx, [], [], _no_activity_ks(), None, [], [], schema_revision="046"
    )

    assert "DÉPASSÉ de 240" in briefing
    assert "compress" in briefing.lower()
    assert "refus" in briefing.lower()


class TestTheByteFigureOnTheNominalLineOnly:
    """The subject reopened on 2026-08-29 — saying which one is counted, as required.

    The bound counts CHARACTERS (Pydantic's `maxLength`); the same focus measured
    on 2026-08-22 was 9,977 characters for 10,285 BYTES — any bound counting bytes
    would already be crossed. The nominal line now carries both numbers so nobody
    confuses them; the NOISY branches (zero margin, overshoot) stay pure — the
    original decision, "the one line read under pressure is not diluted", holds
    exactly where it was argued.
    """

    def _line_with_octets(self, focus_length: int, focus_octets: int) -> str:
        out = _section_technical_state(
            "048",
            focus_tracked=True,
            focus_length=focus_length,
            focus_octets=focus_octets,
        )
        lines = [line for line in out.splitlines() if line.startswith("- Focus :")]
        assert len(lines) == 1, out
        return lines[0]

    def test_the_nominal_line_says_both_and_names_both_units(self) -> None:
        line = self._line_with_octets(9977, 10285)

        assert "9977" in line
        assert "caractères" in line
        assert "10285 octets" in line

    def test_the_loud_lines_stay_pure(self) -> None:
        at_cap = self._line_with_octets(NEXT_FOCUS_MAX_LENGTH, NEXT_FOCUS_MAX_LENGTH + 300)
        exceeded = self._line_with_octets(NEXT_FOCUS_MAX_LENGTH + 40, NEXT_FOCUS_MAX_LENGTH + 400)

        assert "octets" not in at_cap
        assert "octets" not in exceeded

    def test_a_legacy_caller_without_octets_renders_unchanged(self) -> None:
        assert "octets" not in _line(4200)
