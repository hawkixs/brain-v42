"""Tests for the tickets-section silenced-count contract (ticket 259cfbe5).

Scope: `_section_tickets` in `session_tools.py` must surface how many
tickets the `_TICKETS_CAP` hides, instead of the current uncounted
"→ brain_ticket_list pour le reste" line. No priority/deadline/pinned
signal is introduced — see ticket 259cfbe5 for the constraint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from brain_v42.mcp.tools.session_tools import _TICKETS_CAP, _section_tickets
from brain_v42.models.ticket import Ticket, TicketGroups, TicketKind, TicketStatus


def _ticket(title: str, *, kind: TicketKind = TicketKind.REQUEST, **kwargs) -> Ticket:
    return Ticket(
        kind=kind,
        title=title,
        body="b",
        from_project=kwargs.pop("from_project", "red-shrik"),
        to_project=kwargs.pop("to_project", "p"),
        **kwargs,
    )


class TestTicketsSilencedCount:
    def test_under_cap_has_no_silenced_message(self):
        groups = TicketGroups(a_traiter=[_ticket("t1"), _ticket("t2")])
        section = _section_tickets(groups)
        assert "tu" not in section.split("\n")[-1]

    def test_over_cap_reports_exact_silenced_count(self):
        # 7 a_traiter tickets, cap is 5 -> 2 silenced, 0 a_confirmer.
        a_traiter = [_ticket(f"t{i}") for i in range(_TICKETS_CAP + 2)]
        groups = TicketGroups(a_traiter=a_traiter)
        section = _section_tickets(groups)
        assert "2 tickets tus par le cap" in section

    def test_silenced_count_combines_both_categories(self):
        # 4 a_traiter (all shown) + 4 a_confirmer (budget=1 left -> 1 shown,
        # 3 silenced). Total silenced = 0 + 3 = 3.
        a_traiter = [_ticket(f"t{i}") for i in range(4)]
        a_confirmer = [
            _ticket(f"c{i}", from_project="p", to_project="red-data", status=TicketStatus.RESOLVED)
            for i in range(4)
        ]
        groups = TicketGroups(a_traiter=a_traiter, a_confirmer=a_confirmer)
        section = _section_tickets(groups)
        assert "3 tickets tus par le cap" in section

    def test_singular_silenced_ticket_uses_singular_wording(self):
        a_traiter = [_ticket(f"t{i}") for i in range(_TICKETS_CAP + 1)]
        groups = TicketGroups(a_traiter=a_traiter)
        section = _section_tickets(groups)
        assert "1 ticket tu par le cap" in section
        assert "1 ticket tus" not in section

    def test_no_signal_regression_no_deadline_or_priority_field(self):
        # Guard against the reflex fix the ticket explicitly rules out.
        groups = TicketGroups(a_traiter=[_ticket("t1")])
        section = _section_tickets(groups)
        assert "deadline" not in section.lower()
        assert "priority" not in section.lower()
        assert "pinned" not in section.lower()


class TestDatedSilencedSurfacing:
    """The title convention becomes operative BEYOND the cap (259cfbe5, mission re-demonstrated).

    Measured on 2026-08-29: `191b2dba` carries "REVUE 2026-09-03" in its title — the
    notebook's only dated deadline, five days out — and it sits at rank 49 out of
    62, invisible from the briefing. Sorting by recency punishes precisely the
    tickets one does not touch while their date approaches: the title convention only
    works as long as the ticket is displayed, that is, never when it matters.

    The zero-cost shape: among the tickets SILENCED by the cap, those whose title
    carries an ISO date are named under the counter line, sorted by date, capped at
    2. A DATE is falsifiable and self-expiring — no hand-set rank, no flag to police
    (the `pinned` counter-example is documented in the ticket), no column: the
    deadline migration (batch C12) stays an operator arbitration, which this line
    does not exhaust.
    """

    def test_a_dated_silenced_ticket_is_named_with_its_date(self):
        a_traiter = [_ticket(f"t{i}") for i in range(_TICKETS_CAP)]
        a_traiter.append(_ticket("REVUE 2099-09-03 — poison pill extract"))
        groups = TicketGroups(a_traiter=a_traiter)

        section = _section_tickets(groups)

        assert "1 ticket tu par le cap" in section
        assert "daté hors cap" in section
        assert "2099-09-03" in section

    def test_an_undated_silenced_ticket_is_not_named(self):
        a_traiter = [_ticket(f"t{i}") for i in range(_TICKETS_CAP + 2)]
        groups = TicketGroups(a_traiter=a_traiter)

        section = _section_tickets(groups)

        assert "daté hors cap" not in section

    def test_a_dated_but_shown_ticket_is_not_duplicated(self):
        a_traiter = [_ticket("REVUE 2099-09-03 — visible au rang 1")]
        groups = TicketGroups(a_traiter=a_traiter)

        section = _section_tickets(groups)

        assert "daté hors cap" not in section

    def test_dated_silenced_tickets_come_nearest_first_and_capped_at_two(self):
        a_traiter = [_ticket(f"t{i}") for i in range(_TICKETS_CAP)]
        a_traiter.extend(
            (
                _ticket("REVUE 2099-12-01 — la plus lointaine"),
                _ticket("REVUE 2099-09-03 — la plus proche"),
                _ticket("REVUE 2099-10-15 — la médiane"),
            )
        )
        groups = TicketGroups(a_traiter=a_traiter)

        section = _section_tickets(groups)
        dated_lines = [line for line in section.splitlines() if "daté hors cap" in line]

        assert len(dated_lines) == 2
        assert "2099-09-03" in dated_lines[0]
        assert "2099-10-15" in dated_lines[1]
        assert "2099-12-01" not in section

    def test_a_past_date_is_said_overdue_not_hidden(self):
        """A passed deadline stays visible as long as the ticket is open: touching
        it (reviewing it) lifts it back up in recency and switches it off here —
        expiry is the review gesture itself, not a timer."""
        a_traiter = [_ticket(f"t{i}") for i in range(_TICKETS_CAP)]
        a_traiter.append(_ticket("REVUE 2020-01-01 — oubliée depuis longtemps"))
        groups = TicketGroups(a_traiter=a_traiter)

        section = _section_tickets(groups)

        assert "2020-01-01" in section
        assert "dépassée" in section

    def test_an_invalid_date_shape_is_ignored_silently(self):
        a_traiter = [_ticket(f"t{i}") for i in range(_TICKETS_CAP)]
        a_traiter.append(_ticket("faux motif 2099-13-99 dans le titre"))
        groups = TicketGroups(a_traiter=a_traiter)

        section = _section_tickets(groups)

        assert "daté hors cap" not in section


class TestDatedSelectionPrefersNearestToToday:
    """Which dated tickets win the two slots — re-demonstrated on a live case.

    The 2026-08-21 triage asked for the damage to be re-demonstrated on a live
    case before a deadline column could cost a migration head. Measured on the
    production notebook on 2026-09-02 (37 open tickets to brain-v42, four with an
    ISO date in the title), the two slots went to the two STALEST breaches:

        shown    #5281f0ef  2026-08-23  overdue by 10d
        shown    #af5dc328  2026-08-25  overdue by 8d
        hidden   #58711012  2026-08-27  overdue by 6d
        hidden   #191b2dba  2026-09-03  DUE TOMORROW

    So the mechanism hid the only deadline that had not passed yet, one day out.
    Sorting by absolute date means permanently-overdue tickets squat the slots
    forever: that is open question 2 of the ticket ("must a passed deadline stay
    displayed indefinitely? Otherwise we recreate the permanent alarm one stops
    reading") arriving in production.

    The rule that fixes it is not a column — a `deadline` column sorted the same
    way reproduces this exactly. A deadline's claim on attention peaks ON its date
    and decays in BOTH directions, so the slots go to the dates NEAREST today,
    breaches first on a tie. Still no hand-placed rank and still self-expiring:
    an ancient breach now loses its slot by arithmetic rather than by a timer.
    """

    @staticmethod
    def _dated_lines(*offsets: int) -> list[str]:
        """Render the section with one silenced dated ticket per day-offset from today."""
        today = datetime.now(UTC).date()
        a_traiter = [_ticket(f"t{i}") for i in range(_TICKETS_CAP)]
        a_traiter.extend(
            _ticket(f"REVUE {today + timedelta(days=offset)} — offset {offset}")
            for offset in offsets
        )
        section = _section_tickets(TicketGroups(a_traiter=a_traiter))
        return [line for line in section.splitlines() if "daté hors cap" in line]

    def test_a_deadline_due_tomorrow_beats_two_staler_overdue_ones(self):
        today = datetime.now(UTC).date()
        lines = self._dated_lines(-10, -8, -6, 1)

        assert len(lines) == 2
        assert str(today + timedelta(days=1)) in lines[0]
        assert str(today + timedelta(days=-6)) in lines[1]
        assert str(today + timedelta(days=-10)) not in "\n".join(lines)

    def test_a_long_forgotten_breach_yields_its_slot_to_a_closer_one(self):
        today = datetime.now(UTC).date()
        lines = self._dated_lines(-2000, -3)

        assert str(today + timedelta(days=-3)) in lines[0]
        assert str(today + timedelta(days=-2000)) in lines[1]

    def test_an_equally_distant_breach_outranks_an_upcoming_deadline(self):
        today = datetime.now(UTC).date()
        lines = self._dated_lines(3, -3)

        assert str(today + timedelta(days=-3)) in lines[0]
        assert str(today + timedelta(days=3)) in lines[1]
