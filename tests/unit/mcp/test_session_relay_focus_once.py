"""Start/relay render the base once and offer a bounded, opt-in briefing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastmcp import FastMCP
from pydantic import ValidationError

from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools
from brain_v42.mcp.tools.session_tools import (
    COMPACT_BRIEFING_MAX_CHARS,
    make_session_briefing_loader,
)
from brain_v42.models.brain_session import (
    BrainSession,
    BrainSessionResumeResult,
    BrainSessionStartResult,
)
from brain_v42.models.focus_slot import BrainSessionRelayResult, FocusSlotView, SlotBriefing
from brain_v42.models.ticket import Ticket, TicketGroups, TicketKind
from brain_v42.services.feature_service import RoadmapAliveFeature
from tests.unit.mcp.test_delivery_briefing import _page, _services, _view

FOCUS = "F" * 10_000
# JSON field names, UUIDs, timestamps, lifecycle metadata and escaped newlines.
ANSWER_ENVELOPE_ALLOWANCE = 2_500
TOOLS = ("brain_session_start", "brain_session_relay")


@pytest.fixture
def briefing_tools(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Reuse the delivery briefing service fakes behind the real MCP adapters/loader."""
    context, decisions, learnings, dream, feature, sessions = _services()
    ctx = context.get_by_key.return_value
    ctx.current_focus = FOCUS
    ctx.blockers = ["blocker body"]
    decisions.list_all.return_value = [SimpleNamespace(id=uuid4(), title="decision title")]
    learnings.list_all.return_value = [
        SimpleNamespace(id=uuid4(), topic="learning topic", insight="recap body")
    ]
    roadmap = RoadmapAliveFeature(
        name="roadmap title",
        status="in_progress",
        pinned=False,
        artifact_count=0,
        last_artifact_at=None,
    )
    feature.roadmap_alive.return_value = [roadmap]
    tickets = [
        Ticket(
            kind=TicketKind.REQUEST,
            title=f"ticket {i} " + "T" * 190,
            body="ticket body",
            from_project="red-rail",
            to_project="brain-v42",
        )
        for i in range(8)
    ]
    ticket_svc = SimpleNamespace(
        list_grouped=AsyncMock(return_value=TicketGroups(a_traiter=tickets)),
        release_lots=AsyncMock(return_value=[]),
    )
    now = datetime.now(UTC)
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr("brain_v42.mcp.tools.session_tools.datetime", clock)
    slots = [
        FocusSlotView(
            id=uuid4(),
            project_key="brain-v42",
            title="slot title",
            body="slot body",
            revision=2,
            opened_at=now,
            body_updated_at=now,
            anchors=[],
        )
    ]
    slot_view = SlotBriefing(open_slots=slots)
    delivery_page = _page(
        *(_view(uuid4(), health=health) for health in ("fresh", "stale", "stale"))
    )
    delivery = SimpleNamespace(list=AsyncMock(return_value=delivery_page))
    loader = make_session_briefing_loader(
        context,
        decisions,
        learnings,
        dream,
        feature,
        sessions,
        ticket_svc=ticket_svc,
        delivery_svc=delivery,
        focus_slot_svc=SimpleNamespace(briefing=AsyncMock(return_value=slot_view)),
    )
    monkeypatch.setattr(
        "brain_v42.mcp.tools.session_tools.get_settings",
        lambda: SimpleNamespace(graph_enabled=False, brain_dream_cross_project_enabled=False),
    )
    session = BrainSession(
        id=uuid4(),
        project_key="brain-v42",
        client_key="successor",
        started_focus=FOCUS,
        started_focus_revision=17,
        started_at=now,
        updated_at=now,
    )
    sessions.start = AsyncMock(
        return_value=BrainSessionStartResult(session=session, replayed=False, open_session_count=1)
    )
    sessions.relay = AsyncMock(
        return_value=BrainSessionRelayResult(
            ended_session_id=uuid4(),
            session=session,
            focus_revision=17,
            replayed=False,
        )
    )
    server = FastMCP("focus-once")
    register_session_lifecycle_tools(server, sessions, loader)
    return SimpleNamespace(
        server=server,
        loader=loader,
        session=session,
        sessions=sessions,
        context=ctx,
        delivery_page=delivery_page,
        tickets=tickets,
        slot_view=slot_view,
    )


async def _answer(fakes: SimpleNamespace, name: str, **kwargs: Any) -> Any:
    tool = await fakes.server.get_tool(name)
    if name == "brain_session_start":
        return await tool.fn(project_key="brain-v42", client_key="successor", **kwargs)
    return await tool.fn(
        session_id=uuid4(),
        expected_client_key="predecessor",
        summary="work completed",
        handover=FOCUS,
        new_client_key="successor",
        initiator="operator",
        expected_focus_revision=16,
        **kwargs,
    )


def _serialized(answer: Any) -> str:
    return json.dumps(answer.model_dump(mode="json"), ensure_ascii=False)


@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize("mode", [None, "full", "compact"])
async def test_started_focus_occurs_exactly_once(
    briefing_tools: SimpleNamespace, name: str, mode: str | None
) -> None:
    answer = await _answer(briefing_tools, name, **({} if mode is None else {"briefing": mode}))

    assert _serialized(answer).count(FOCUS) == 1
    assert answer.session.started_focus == FOCUS
    assert answer.session.started_focus_revision == 17
    assert "### Focus\n(see session.started_focus, revision 17, 10000 chars)" in answer.briefing


@pytest.mark.parametrize("name", TOOLS)
async def test_compact_bounds_the_briefing_and_the_serialized_answer(
    briefing_tools: SimpleNamespace, name: str
) -> None:
    answer = await _answer(briefing_tools, name, briefing="compact")

    assert COMPACT_BRIEFING_MAX_CHARS == 4_000
    assert len(answer.briefing) <= COMPACT_BRIEFING_MAX_CHARS
    assert len(_serialized(answer)) <= (
        len(FOCUS) + COMPACT_BRIEFING_MAX_CHARS + ANSWER_ENVELOPE_ALLOWANCE
    )
    assert "### Livraison\n3 contracts (1 fresh)" in answer.briefing
    assert str(briefing_tools.tickets[0].id) in answer.briefing
    assert str(briefing_tools.slot_view.open_slots[0].id) in answer.briefing
    assert "slot title" in answer.briefing
    for body in ("ticket body", "slot body", "recap body", "blocker body", "T" * 190):
        assert body not in answer.briefing
    view = briefing_tools.delivery_page.items[0]
    for detail in (view.assessment.assessment_id, view.assessment.delivery_digest, "docs/proof.md"):
        assert detail not in answer.briefing
    ticket_lines = [
        line for line in answer.briefing.splitlines() if str(briefing_tools.tickets[0].id) in line
    ]
    assert len(ticket_lines) == 1 and len(ticket_lines[0]) <= 180


@pytest.mark.parametrize("name", TOOLS)
async def test_full_default_changes_only_the_focus_section(
    briefing_tools: SimpleNamespace, name: str
) -> None:
    original = await briefing_tools.loader("brain-v42", briefing_tools.session.id)
    answer = await _answer(briefing_tools, name)

    reference = "(see session.started_focus, revision 17, 10000 chars)"
    assert answer.briefing == original.replace(f"### Focus\n{FOCUS}", f"### Focus\n{reference}")
    for section in ("Tickets", "Roadmap", "Slots", "Blockers", "Recap", "Livraison"):
        assert f"### {section}" in answer.briefing
    assert "recap body" in answer.briefing
    assert briefing_tools.delivery_page.items[0].assessment.assessment_id in answer.briefing
    assert FOCUS in await briefing_tools.loader("brain-v42", briefing_tools.session.id)


@pytest.mark.parametrize("name", TOOLS)
async def test_reference_describes_the_returned_snapshot(
    briefing_tools: SimpleNamespace, name: str
) -> None:
    briefing_tools.context.current_focus = "a concurrent focus edit"
    answer = await _answer(briefing_tools, name)

    assert answer.session.started_focus == FOCUS
    assert "revision 17, 10000 chars" in answer.briefing
    assert "a concurrent focus edit" not in answer.briefing


@pytest.mark.parametrize("name", TOOLS)
async def test_compact_truncates_with_a_visible_marker(
    briefing_tools: SimpleNamespace, name: str
) -> None:
    slot = briefing_tools.slot_view.open_slots[0]
    briefing_tools.slot_view.open_slots = [
        slot.model_copy(update={"id": uuid4(), "title": "S" * 200}) for _ in range(100)
    ]
    answer = await _answer(briefing_tools, name, briefing="compact")

    assert len(answer.briefing) == COMPACT_BRIEFING_MAX_CHARS
    assert answer.briefing.endswith("[compact briefing truncated]")
    assert len(_serialized(answer)) <= (
        len(FOCUS) + COMPACT_BRIEFING_MAX_CHARS + ANSWER_ENVELOPE_ALLOWANCE
    )


@pytest.mark.parametrize("name", TOOLS)
async def test_mode_is_published_and_validated(briefing_tools: SimpleNamespace, name: str) -> None:
    tool = await briefing_tools.server.get_tool(name)
    mode = tool.parameters["properties"]["briefing"]
    assert mode["enum"] == ["full", "compact"]
    assert mode["default"] == "full"
    arguments = (
        {"project_key": "brain-v42", "client_key": "successor"}
        if name == "brain_session_start"
        else {
            "session_id": str(uuid4()),
            "expected_client_key": "predecessor",
            "summary": "work completed",
            "handover": FOCUS,
            "new_client_key": "successor",
            "initiator": "operator",
            "expected_focus_revision": 16,
        }
    )
    with pytest.raises(ValidationError, match="briefing"):
        await tool.run({**arguments, "briefing": "invalid"})
    briefing_tools.sessions.start.assert_not_awaited()
    briefing_tools.sessions.relay.assert_not_awaited()


@pytest.mark.parametrize("name", TOOLS)
async def test_full_keeps_focus_prose_without_a_started_snapshot(
    briefing_tools: SimpleNamespace, name: str
) -> None:
    briefing_tools.session.started_focus = None
    answer = await _answer(briefing_tools, name)

    assert f"### Focus\n{FOCUS}" in answer.briefing
    assert "see session.started_focus" not in answer.briefing


async def test_resume_keeps_its_existing_briefing(briefing_tools: SimpleNamespace) -> None:
    briefing_tools.sessions.resume = AsyncMock(
        return_value=BrainSessionResumeResult(
            session=briefing_tools.session,
            open_session_count=1,
            current_focus=FOCUS,
            current_focus_revision=17,
        )
    )
    tool = await briefing_tools.server.get_tool("brain_session_resume")
    answer = await tool.fn(session_id=briefing_tools.session.id, expected_client_key="successor")

    original = await briefing_tools.loader("brain-v42", briefing_tools.session.id)
    assert answer.briefing == original
    assert f"### Focus\n{FOCUS}" in answer.briefing
