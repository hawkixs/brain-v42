from unittest.mock import AsyncMock

import pytest

from brain_v42.models.ticket import TicketStatus
from brain_v42.services.ticket_service import (
    NotAllowedError,
    TicketError,
    TicketPlanConflictError,
)
from tests.unit.services.test_ticket_service import FROM, TO, _self_ticket, _svc, _ticket


def _repo_message(svc, value="ok"):
    svc._repo.set_target_release = AsyncMock(return_value=value)
    return svc._repo.set_target_release


async def test_executor_plans_and_the_message_says_planned_for() -> None:
    svc, _, _ = _svc(ticket=_ticket())
    call = _repo_message(svc)
    await svc.plan(_ticket().id, TO, "0.6.3")
    kwargs = call.await_args.kwargs
    assert (kwargs["expected"], kwargs["new"]) == (None, "0.6.3")
    assert kwargs["message"] == "planned for 0.6.3"
    assert kwargs["author_project"] == TO


async def test_move_and_unplan_messages_name_the_previous_value() -> None:
    svc, _, _ = _svc(ticket=_ticket(target_release="0.6.3"))
    call = _repo_message(svc)
    await svc.plan(_ticket().id, TO, "0.6.4")
    assert call.await_args.kwargs["message"] == "moved from 0.6.3 to 0.6.4"
    await svc.plan(_ticket().id, TO, None)
    assert call.await_args.kwargs["message"] == "unplanned (was 0.6.3)"


async def test_requester_cannot_plan() -> None:
    svc, _, _ = _svc(ticket=_ticket())
    _repo_message(svc)
    with pytest.raises(NotAllowedError, match="reserved to the executor"):
        await svc.plan(_ticket().id, FROM, "0.6.3")


async def test_self_ticket_executor_may_plan() -> None:
    svc, _, _ = _svc(ticket=_self_ticket())
    _repo_message(svc)
    await svc.plan(_self_ticket().id, "brain-v42", "0.6.3")


async def test_same_value_is_refused_not_recorded() -> None:
    svc, _, _ = _svc(ticket=_ticket(target_release="0.6.3"))
    call = _repo_message(svc)
    with pytest.raises(TicketError, match="already planned for 0.6.3"):
        await svc.plan(_ticket().id, TO, "0.6.3")
    call.assert_not_awaited()


@pytest.mark.parametrize("status", [TicketStatus.CLOSED, TicketStatus.ACKED])
async def test_terminal_ticket_cannot_be_planned(status) -> None:
    svc, _, _ = _svc(ticket=_ticket(status=status))
    _repo_message(svc)
    with pytest.raises(TicketError, match="terminal"):
        await svc.plan(_ticket().id, TO, "0.6.3")


async def test_tag_shaped_value_is_refused_with_the_hint() -> None:
    svc, _, _ = _svc(ticket=_ticket())
    _repo_message(svc)
    with pytest.raises(ValueError, match="write 0.6.3, without the v"):
        await svc.plan(_ticket().id, TO, "v0.6.3")


async def test_lost_compare_and_swap_is_a_conflict() -> None:
    svc, _, _ = _svc(ticket=_ticket())
    _repo_message(svc, value=None)
    with pytest.raises(TicketPlanConflictError, match="changed concurrently"):
        await svc.plan(_ticket().id, TO, "0.6.3")
