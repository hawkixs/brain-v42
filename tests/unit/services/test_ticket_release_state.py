from unittest.mock import AsyncMock, MagicMock

from fastmcp import FastMCP

from brain_v42.facts.sources import running_release_sha
from brain_v42.mcp.tools.ticket_tools import register_ticket_tools
from brain_v42.services.ticket_service import TicketService
from tests.unit.services.test_ticket_service import _svc, _ticket

L = "a" * 40


async def test_release_state_judges_deployment_against_the_running_release() -> None:
    svc, _, _ = _svc(ticket=_ticket())
    svc._repo.release_state = AsyncMock(return_value=(("v0.6.3",), (L,)))
    svc._repo.deployed_deliverables = AsyncMock(return_value=(1, 1))
    svc.set_running_sha_provider(lambda: L)
    state = await svc.release_state(_ticket().id)
    assert state is not None
    assert state.rendered_parts() == ["shipped v0.6.3", "deployed"]


async def test_release_state_counts_the_deliverables_live_in_the_running_release() -> None:
    ticket = _ticket()
    svc, _, _ = _svc(ticket=ticket)
    svc._repo.release_state = AsyncMock(return_value=((), (L,)))
    svc._repo.deployed_deliverables = AsyncMock(return_value=(1, 2))
    svc.set_running_sha_provider(lambda: L)
    state = await svc.release_state(ticket.id)
    assert state is not None
    assert state.rendered_parts() == ["partly deployed (1/2)"]
    svc._repo.deployed_deliverables.assert_awaited_once_with(ticket.id, L)


async def test_release_state_is_none_without_any_observer_measurement() -> None:
    svc, _, _ = _svc(ticket=_ticket())
    svc._repo.release_state = AsyncMock(return_value=((), ()))
    svc.set_running_sha_provider(lambda: L)
    assert await svc.release_state(_ticket().id) is None


def test_registering_the_tools_binds_the_process_release_probe() -> None:
    svc = TicketService(MagicMock(), MagicMock())
    register_ticket_tools(FastMCP("test"), ticket_svc=svc)
    assert svc._running_sha_provider is running_release_sha
