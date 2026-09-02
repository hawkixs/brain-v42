"""Unit tests for ticket models and the pure transition table."""

import pytest
from pydantic import ValidationError

from brain_v42.models.ticket import (
    SELF_TRANSITIONS,
    TERMINAL_STATUSES,
    TRANSITIONS,
    ExtractionStatus,
    Ticket,
    TicketAction,
    TicketCreate,
    TicketKind,
    TicketMessage,
    TicketStatus,
    allowed_actions,
)


class TestTicketCreate:
    def test_canonicalizes_both_project_keys(self):
        t = TicketCreate(
            kind=TicketKind.REQUEST,
            title="t",
            body="b",
            from_project="brain_v42",  # confusable connu → brain-v42
            to_project="red-data",
        )
        assert t.from_project == "brain-v42"
        assert t.to_project == "red-data"

    def test_rejects_non_kebab_project_key(self):
        with pytest.raises(ValidationError):
            TicketCreate(
                kind=TicketKind.FYI,
                title="t",
                body="b",
                from_project="Red Data",
                to_project="red-shrik",
            )

    def test_title_max_200(self):
        with pytest.raises(ValidationError):
            TicketCreate(
                kind=TicketKind.REQUEST,
                title="x" * 201,
                body="b",
                from_project="red-shrik",
                to_project="red-data",
            )

    def test_extraction_status_defaults_none(self):
        t = TicketCreate(
            kind=TicketKind.FYI,
            title="t",
            body="b",
            from_project="red-lab-factory",
            to_project="brain-v42",
        )
        assert t.extraction_status is None

    def test_accepts_skipped_opt_out(self):
        t = TicketCreate(
            kind=TicketKind.FYI,
            title="job factory terminé",
            body="b",
            from_project="red-lab-factory",
            to_project="brain-v42",
            extraction_status=ExtractionStatus.SKIPPED,
        )
        assert t.extraction_status is ExtractionStatus.SKIPPED


class TestTicketDefaults:
    def test_new_ticket_defaults(self):
        t = Ticket(
            kind=TicketKind.REQUEST,
            title="t",
            body="b",
            from_project="red-shrik",
            to_project="red-data",
        )
        assert t.status is TicketStatus.OPEN
        assert t.extraction_status is None
        assert t.resolved_at is None
        assert t.closed_at is None

    def test_message_status_to_optional(self):
        m = TicketMessage(
            ticket_id=Ticket(
                kind=TicketKind.FYI,
                title="t",
                body="b",
                from_project="a-b",
                to_project="c-d",
            ).id,
            author_project="a-b",
            body="hello",
        )
        assert m.status_to is None


class TestTransitionTable:
    def test_terminal_statuses(self):
        assert TERMINAL_STATUSES == frozenset({TicketStatus.CLOSED, TicketStatus.ACKED})

    @pytest.mark.parametrize(
        ("kind", "status", "action", "role", "new_status"),
        [
            (
                TicketKind.REQUEST,
                TicketStatus.OPEN,
                TicketAction.START,
                "executor",
                TicketStatus.IN_PROGRESS,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.OPEN,
                TicketAction.RESOLVE,
                "executor",
                TicketStatus.RESOLVED,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.OPEN,
                TicketAction.WONTFIX,
                "executor",
                TicketStatus.WONTFIX,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.IN_PROGRESS,
                TicketAction.RESOLVE,
                "executor",
                TicketStatus.RESOLVED,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.IN_PROGRESS,
                TicketAction.WONTFIX,
                "executor",
                TicketStatus.WONTFIX,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.RESOLVED,
                TicketAction.CONFIRM,
                "requester",
                TicketStatus.CLOSED,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.WONTFIX,
                TicketAction.CONFIRM,
                "requester",
                TicketStatus.CLOSED,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.RESOLVED,
                TicketAction.REOPEN,
                "requester",
                TicketStatus.OPEN,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.WONTFIX,
                TicketAction.REOPEN,
                "requester",
                TicketStatus.OPEN,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.OPEN,
                TicketAction.CANCEL,
                "requester",
                TicketStatus.CLOSED,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.IN_PROGRESS,
                TicketAction.CANCEL,
                "requester",
                TicketStatus.CLOSED,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.RESOLVED,
                TicketAction.CANCEL,
                "requester",
                TicketStatus.CLOSED,
            ),
            (
                TicketKind.REQUEST,
                TicketStatus.WONTFIX,
                TicketAction.CANCEL,
                "requester",
                TicketStatus.CLOSED,
            ),
            (TicketKind.FYI, TicketStatus.OPEN, TicketAction.ACK, "executor", TicketStatus.ACKED),
            (
                TicketKind.FYI,
                TicketStatus.OPEN,
                TicketAction.CANCEL,
                "requester",
                TicketStatus.CLOSED,
            ),
        ],
    )
    def test_legal_transitions(self, kind, status, action, role, new_status):
        assert TRANSITIONS[(kind, status, action)] == (role, new_status)

    def test_exactly_15_legal_transitions(self):
        # Pins the whole surface: the 15 legal ones are enumerated above and
        # this count guarantees NO other combination (kind × status × action,
        # 84 in total) is legal — the illegal matrix is covered by construction
        # (spec §8), the cases below are documentation only.
        assert len(TRANSITIONS) == 15

    @pytest.mark.parametrize(
        ("kind", "status", "action"),
        [
            (TicketKind.REQUEST, TicketStatus.CLOSED, TicketAction.REOPEN),  # terminal
            (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.ACK),  # ack = fyi only
            (TicketKind.REQUEST, TicketStatus.OPEN, TicketAction.CONFIRM),  # nothing to confirm
            (TicketKind.FYI, TicketStatus.OPEN, TicketAction.RESOLVE),  # fyi does not resolve
            (TicketKind.FYI, TicketStatus.ACKED, TicketAction.ACK),  # terminal
            (TicketKind.FYI, TicketStatus.OPEN, TicketAction.START),
        ],
    )
    def test_illegal_transitions_absent(self, kind, status, action):
        assert (kind, status, action) not in TRANSITIONS

    def test_allowed_actions_open_request(self):
        assert allowed_actions(TicketKind.REQUEST, TicketStatus.OPEN) == [
            "cancel",
            "resolve",
            "start",
            "wontfix",
        ]

    def test_allowed_actions_terminal_is_empty(self):
        assert allowed_actions(TicketKind.FYI, TicketStatus.ACKED) == []


class TestSelfTransitions:
    """Self-tickets (from_project == to_project) skip the role check entirely and
    consult SELF_TRANSITIONS instead of TRANSITIONS (spec §4.1)."""

    def test_resolve_pending_absent_from_cross_project_table(self):
        assert not any(a is TicketAction.RESOLVE_PENDING for (_k, _s, a) in TRANSITIONS)

    def test_allowed_actions_differ_by_self_ticket_flag(self):
        cross = allowed_actions(TicketKind.REQUEST, TicketStatus.OPEN, self_ticket=False)
        self_ = allowed_actions(TicketKind.REQUEST, TicketStatus.OPEN, self_ticket=True)
        assert cross == ["cancel", "resolve", "start", "wontfix"]
        assert self_ == ["cancel", "resolve", "resolve_pending", "start", "wontfix"]
        assert cross != self_

    def test_self_transitions_cover_every_reachable_non_terminal_state(self):
        # fyi included: without a self entry for (FYI, OPEN), an fyi self-ticket
        # would become untransitionable (spec §4.1).
        reachable = {(k, s) for (k, s, _a) in TRANSITIONS}
        for kind, status in reachable:
            assert status not in TERMINAL_STATUSES
            assert (kind, status) in {(k, s) for (k, s, _a) in SELF_TRANSITIONS}


class TestExtractionStatus:
    def test_values(self):
        assert {e.value for e in ExtractionStatus} == {"pending", "proposed", "skipped", "done"}
