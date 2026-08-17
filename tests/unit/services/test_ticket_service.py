"""Unit tests for TicketService — state machine enforcement with mocked repo."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.ticket import (
    ExtractionStatus,
    Ticket,
    TicketCreate,
    TicketKind,
    TicketStatus,
)
from brain_v42.services.ticket_service import (
    IllegalTransitionError,
    NotAllowedError,
    TicketError,
    TicketNotFoundError,
    TicketService,
    UnknownProjectError,
)

# PAS de pytestmark : pyproject a asyncio_mode = "auto", les unit tests du
# repo écrivent des `async def test_*` nus (cf. tests/unit existants).

FROM, TO = "red-shrik", "red-data"
SELF_PROJECT = "brain-v42"


def _ticket(kind=TicketKind.REQUEST, status=TicketStatus.OPEN, **kw) -> Ticket:
    return Ticket(
        kind=kind,
        title="t",
        body="b",
        from_project=FROM,
        to_project=TO,
        status=status,
        **kw,
    )


def _self_ticket(kind=TicketKind.REQUEST, status=TicketStatus.OPEN, **kw) -> Ticket:
    return Ticket(
        kind=kind,
        title="t",
        body="b",
        from_project=SELF_PROJECT,
        to_project=SELF_PROJECT,
        status=status,
        **kw,
    )


def _svc(ticket=None, known_projects=(FROM, TO)):
    repo = MagicMock()
    repo.get_by_id = AsyncMock(return_value=ticket)
    repo.create = AsyncMock(side_effect=lambda data: _ticket(kind=data.kind))
    repo.add_message = AsyncMock()

    # apply_transition renvoie le ticket muté (echo simplifié pour les tests)
    async def _apply(
        ticket_id,
        new_status,
        *,
        expected_status,
        resolved_at,
        closed_at,
        extraction_status,
        message_author,
        message_body,
    ):
        return _ticket(
            kind=ticket.kind if ticket else TicketKind.REQUEST,
            status=new_status,
            resolved_at=resolved_at,
            closed_at=closed_at,
            extraction_status=extraction_status,
        )

    repo.apply_transition = AsyncMock(side_effect=_apply)
    ctx_repo = MagicMock()
    ctx_repo.get_by_key = AsyncMock(
        side_effect=lambda key: MagicMock() if key in known_projects else None
    )
    return TicketService(repo=repo, project_context_repo=ctx_repo), repo, ctx_repo


class TestCreate:
    async def test_create_validates_both_projects_exist(self):
        svc, repo, ctx_repo = _svc()
        data = TicketCreate(
            kind=TicketKind.REQUEST,
            title="t",
            body="b",
            from_project=FROM,
            to_project=TO,
        )
        await svc.create(data)
        assert ctx_repo.get_by_key.await_count == 2
        repo.create.assert_awaited_once()

    async def test_create_rejects_unknown_to_project(self):
        svc, repo, _ = _svc(known_projects=(FROM,))
        data = TicketCreate(
            kind=TicketKind.REQUEST,
            title="t",
            body="b",
            from_project=FROM,
            to_project=TO,
        )
        with pytest.raises(UnknownProjectError, match="red-data"):
            await svc.create(data)
        repo.create.assert_not_awaited()


class TestTransition:
    async def test_resolve_by_executor_sets_resolved_at(self):
        svc, repo, _ = _svc(ticket=_ticket())
        updated = await svc.transition(uuid4(), TO, "resolve")
        assert updated.status is TicketStatus.RESOLVED
        kwargs = repo.apply_transition.await_args.kwargs
        assert kwargs["expected_status"] is TicketStatus.OPEN
        assert kwargs["resolved_at"] is not None
        assert kwargs["closed_at"] is None
        assert kwargs["extraction_status"] is None

    async def test_resolve_by_requester_forbidden(self):
        svc, _, _ = _svc(ticket=_ticket())
        with pytest.raises(NotAllowedError, match="executor"):
            await svc.transition(uuid4(), FROM, "resolve")

    async def test_confirm_by_requester_closes_and_marks_extraction(self):
        svc, repo, _ = _svc(ticket=_ticket(status=TicketStatus.RESOLVED))
        updated = await svc.transition(uuid4(), FROM, "confirm")
        assert updated.status is TicketStatus.CLOSED
        kwargs = repo.apply_transition.await_args.kwargs
        assert kwargs["closed_at"] is not None
        assert kwargs["extraction_status"] is ExtractionStatus.PENDING

    async def test_confirm_by_executor_forbidden(self):
        svc, _, _ = _svc(ticket=_ticket(status=TicketStatus.RESOLVED))
        with pytest.raises(NotAllowedError, match="requester"):
            await svc.transition(uuid4(), TO, "confirm")

    async def test_reopen_clears_resolved_at(self):
        svc, repo, _ = _svc(
            ticket=_ticket(status=TicketStatus.RESOLVED, resolved_at=datetime.now(UTC))
        )
        updated = await svc.transition(uuid4(), FROM, "reopen")
        assert updated.status is TicketStatus.OPEN
        assert repo.apply_transition.await_args.kwargs["resolved_at"] is None

    async def test_ack_fyi_marks_extraction_pending(self):
        svc, repo, _ = _svc(ticket=_ticket(kind=TicketKind.FYI))
        updated = await svc.transition(uuid4(), TO, "ack")
        assert updated.status is TicketStatus.ACKED
        assert (
            repo.apply_transition.await_args.kwargs["extraction_status"] is ExtractionStatus.PENDING
        )

    async def test_illegal_action_lists_allowed(self):
        svc, _, _ = _svc(ticket=_ticket(kind=TicketKind.FYI))
        with pytest.raises(IllegalTransitionError, match="ack"):
            await svc.transition(uuid4(), TO, "resolve")

    async def test_unknown_action_rejected(self):
        svc, _, _ = _svc(ticket=_ticket())
        with pytest.raises(IllegalTransitionError, match="unknown action"):
            await svc.transition(uuid4(), TO, "explode")

    async def test_terminal_state_has_no_actions(self):
        svc, _, _ = _svc(ticket=_ticket(status=TicketStatus.CLOSED))
        with pytest.raises(IllegalTransitionError, match="terminal"):
            await svc.transition(uuid4(), FROM, "reopen")

    async def test_not_found(self):
        svc, _, _ = _svc(ticket=None)
        with pytest.raises(TicketNotFoundError):
            await svc.transition(uuid4(), FROM, "cancel")

    async def test_transition_with_message_is_one_atomic_repository_call(self):
        svc, repo, _ = _svc(ticket=_ticket())
        await svc.transition(uuid4(), TO, "resolve", message="c'est déployé")

        repo.apply_transition.assert_awaited_once()
        kwargs = repo.apply_transition.await_args.kwargs
        assert kwargs["expected_status"] is TicketStatus.OPEN
        assert kwargs["message_author"] == TO
        assert kwargs["message_body"] == "c'est déployé"
        repo.add_message.assert_not_awaited()

    async def test_transition_without_message_writes_no_row(self):
        svc, repo, _ = _svc(ticket=_ticket())
        await svc.transition(uuid4(), TO, "resolve")
        kwargs = repo.apply_transition.await_args.kwargs
        assert kwargs["message_author"] is None
        assert kwargs["message_body"] is None
        repo.add_message.assert_not_awaited()

    async def test_lost_compare_and_swap_raises_reloadable_domain_conflict(self):
        svc, repo, _ = _svc(ticket=_ticket())
        repo.apply_transition.side_effect = None
        repo.apply_transition.return_value = None

        with pytest.raises(TicketError, match="changed concurrently; reload and retry") as exc:
            await svc.transition(uuid4(), TO, "resolve", message="done")

        assert exc.type.__name__ == "TicketTransitionConflictError"
        repo.add_message.assert_not_awaited()

    async def test_conflict_retry_reloads_state_instead_of_replaying_action(self):
        svc, repo, _ = _svc(ticket=_ticket())
        repo.get_by_id.side_effect = [
            _ticket(status=TicketStatus.OPEN),
            _ticket(status=TicketStatus.RESOLVED),
        ]
        repo.apply_transition.side_effect = None
        repo.apply_transition.return_value = None

        with pytest.raises(TicketError, match="reload and retry"):
            await svc.transition(uuid4(), TO, "resolve")
        with pytest.raises(IllegalTransitionError, match="illegal from status 'resolved'"):
            await svc.transition(uuid4(), TO, "resolve")

        repo.apply_transition.assert_awaited_once()

    async def test_third_party_project_rejected(self):
        svc, _, _ = _svc(ticket=_ticket())
        with pytest.raises(NotAllowedError):
            await svc.transition(uuid4(), "red-lab", "resolve")

    async def test_terminal_transition_preserves_preset_skipped(self):
        # Opt-out : un ticket créé 'skipped' ne doit PAS repasser 'pending' en se fermant.
        svc, repo, _ = _svc(
            ticket=_ticket(status=TicketStatus.RESOLVED, extraction_status=ExtractionStatus.SKIPPED)
        )
        updated = await svc.transition(uuid4(), FROM, "confirm")
        assert updated.status is TicketStatus.CLOSED
        assert (
            repo.apply_transition.await_args.kwargs["extraction_status"] is ExtractionStatus.SKIPPED
        )

    async def test_ack_preserves_preset_skipped(self):
        svc, repo, _ = _svc(
            ticket=_ticket(kind=TicketKind.FYI, extraction_status=ExtractionStatus.SKIPPED)
        )
        await svc.transition(uuid4(), TO, "ack")
        assert (
            repo.apply_transition.await_args.kwargs["extraction_status"] is ExtractionStatus.SKIPPED
        )


class TestSelfTicketTransition:
    """from_project == to_project: role check skipped, SELF_TRANSITIONS used
    (docs/superpowers/specs/2026-08-03-self-ticket-lifecycle-design.md §5)."""

    async def test_resolve_from_open_closes_directly(self):
        svc, repo, _ = _svc(ticket=_self_ticket())
        updated = await svc.transition(uuid4(), SELF_PROJECT, "resolve")
        assert updated.status is TicketStatus.CLOSED
        assert repo.apply_transition.await_args.kwargs["closed_at"] is not None

    async def test_resolve_from_in_progress_closes_directly(self):
        svc, repo, _ = _svc(ticket=_self_ticket(status=TicketStatus.IN_PROGRESS))
        updated = await svc.transition(uuid4(), SELF_PROJECT, "resolve")
        assert updated.status is TicketStatus.CLOSED

    async def test_resolve_pending_from_open_stays_resolved(self):
        svc, repo, _ = _svc(ticket=_self_ticket())
        updated = await svc.transition(uuid4(), SELF_PROJECT, "resolve_pending")
        assert updated.status is TicketStatus.RESOLVED
        assert repo.apply_transition.await_args.kwargs["closed_at"] is None

    async def test_wontfix_from_open_closes_directly(self):
        svc, repo, _ = _svc(ticket=_self_ticket())
        updated = await svc.transition(uuid4(), SELF_PROJECT, "wontfix")
        assert updated.status is TicketStatus.CLOSED

    async def test_confirm_from_resolved_closes(self):
        # Les 11 self-tickets 'resolved' existants restent fermables (spec §1.4, §6).
        svc, repo, _ = _svc(ticket=_self_ticket(status=TicketStatus.RESOLVED))
        updated = await svc.transition(uuid4(), SELF_PROJECT, "confirm")
        assert updated.status is TicketStatus.CLOSED

    async def test_reopen_from_resolved_reopens(self):
        svc, repo, _ = _svc(
            ticket=_self_ticket(status=TicketStatus.RESOLVED, resolved_at=datetime.now(UTC))
        )
        updated = await svc.transition(uuid4(), SELF_PROJECT, "reopen")
        assert updated.status is TicketStatus.OPEN
        assert repo.apply_transition.await_args.kwargs["resolved_at"] is None

    async def test_no_role_check_regardless_of_declared_author(self):
        # 'red-lab' n'est ni from_project ni to_project — sur un cross-ticket ce serait
        # un NotAllowedError. Sur un self-ticket la partie unique rend le rôle sans objet.
        svc, _, _ = _svc(ticket=_self_ticket())
        updated = await svc.transition(uuid4(), "red-lab", "resolve")
        assert updated.status is TicketStatus.CLOSED


class TestCrossProjectTransitionNonRegression:
    """Locks that inter-project tickets keep their two-party protocol untouched."""

    async def test_resolve_still_yields_resolved_not_closed(self):
        svc, _, _ = _svc(ticket=_ticket())
        updated = await svc.transition(uuid4(), TO, "resolve")
        assert updated.status is TicketStatus.RESOLVED

    async def test_resolve_pending_is_illegal_cross_project(self):
        svc, _, _ = _svc(ticket=_ticket())
        with pytest.raises(IllegalTransitionError, match="allowed") as exc:
            await svc.transition(uuid4(), TO, "resolve_pending")
        hint = str(exc.value).rsplit("allowed:", maxsplit=1)[1]
        assert "resolve_pending" not in hint
        assert "resolve" in hint

    async def test_role_check_still_rejects_wrong_author(self):
        svc, _, _ = _svc(ticket=_ticket())
        with pytest.raises(NotAllowedError):
            await svc.transition(uuid4(), FROM, "resolve")


class TestReply:
    async def test_reply_by_participant_ok(self):
        svc, repo, _ = _svc(ticket=_ticket())
        await svc.reply(uuid4(), FROM, "des nouvelles ?")
        repo.add_message.assert_awaited_once()

    async def test_reply_by_third_party_rejected(self):
        svc, _, _ = _svc(ticket=_ticket())
        with pytest.raises(NotAllowedError, match="participant"):
            await svc.reply(uuid4(), "red-lab", "hello")

    async def test_reply_allowed_in_terminal_state(self):
        # Le statut contraint les transitions, pas la discussion (spec §3).
        svc, repo, _ = _svc(ticket=_ticket(status=TicketStatus.CLOSED))
        await svc.reply(uuid4(), FROM, "post-mortem")
        repo.add_message.assert_awaited_once()
