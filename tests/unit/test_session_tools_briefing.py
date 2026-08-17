"""Tests for the tickets-section silenced-count contract (ticket 259cfbe5).

Scope: `_section_tickets` in `session_tools.py` must surface how many
tickets the `_TICKETS_CAP` hides, instead of the current uncounted
"→ brain_ticket_list pour le reste" line. No priority/deadline/pinned
signal is introduced — see ticket 259cfbe5 for the constraint.
"""

from __future__ import annotations

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
