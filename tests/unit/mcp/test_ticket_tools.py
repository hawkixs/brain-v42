"""Unit tests for the 5 brain_ticket_* MCP tools (mocked service)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastmcp import Client, FastMCP

from brain_v42.mcp.tools.ticket_tools import register_ticket_tools
from brain_v42.models.ticket import (
    ExtractionStatus,
    Ticket,
    TicketGroups,
    TicketKind,
    TicketMessage,
    TicketStatus,
)
from brain_v42.services.ticket_service import (
    IllegalTransitionError,
    TicketService,
    UnknownProjectError,
)
from tests.unit.mcp._tool_error_adapter import capture_tool_errors

# asyncio_mode = "auto" — no pytestmark (the repo's unit test style).

FROM, TO = "red-shrik", "red-data"


def _ticket(**kw) -> Ticket:
    defaults = {
        "kind": TicketKind.REQUEST,
        "title": "exposer ndjson",
        "body": "détail",
        "from_project": FROM,
        "to_project": TO,
    }
    defaults.update(kw)
    return Ticket(**defaults)


async def _tool(mcp, name):
    tool = await mcp.get_tool(name)
    assert tool is not None
    return SimpleNamespace(
        description=tool.description,
        fn=capture_tool_errors(tool.fn),
    )


def _mcp_with(svc):
    mcp = FastMCP("test")
    register_ticket_tools(mcp, ticket_svc=svc)
    return mcp


class TestRegistration:
    async def test_all_five_tools_registered(self):
        mcp = _mcp_with(MagicMock())
        for name in (
            "brain_ticket_create",
            "brain_ticket_reply",
            "brain_ticket_transition",
            "brain_ticket_list",
            "brain_ticket_get",
        ):
            assert await mcp.get_tool(name) is not None


class TestCreate:
    async def test_description_documents_note_to_self_tickets(self):
        tool = await _tool(_mcp_with(MagicMock()), "brain_ticket_create")

        assert "note-to-self" in tool.description

    async def test_create_note_to_self_uses_same_project_for_both_roles(self):
        svc = MagicMock()
        svc.create = AsyncMock(
            return_value=_ticket(from_project="brain-v42", to_project="brain-v42")
        )
        tool = await _tool(_mcp_with(svc), "brain_ticket_create")

        result = await tool.fn(
            from_project="brain-v42",
            to_project="brain-v42",
            kind="request",
            title="stabiliser le focus",
            body="à reprendre dans la prochaine session",
        )

        assert result.startswith("ok ")
        payload = svc.create.await_args.args[0]
        assert payload.from_project == payload.to_project == "brain-v42"

    async def test_create_ok(self):
        svc = MagicMock()
        svc.create = AsyncMock(return_value=_ticket())
        tool = await _tool(_mcp_with(svc), "brain_ticket_create")
        result = await tool.fn(
            from_project=FROM,
            to_project=TO,
            kind="request",
            title="exposer ndjson",
            body="détail",
        )
        assert result.startswith("ok ")
        assert "id:" in result

    async def test_create_invalid_kind(self):
        tool = await _tool(_mcp_with(MagicMock()), "brain_ticket_create")
        result = await tool.fn(
            from_project=FROM,
            to_project=TO,
            kind="bug",
            title="t",
            body="b",
        )
        assert result and result[0].isalnum()
        assert "request" in result and "fyi" in result

    async def test_create_unknown_project_returns_error_str(self):
        svc = MagicMock()
        svc.create = AsyncMock(side_effect=UnknownProjectError("Unknown project 'red-dataz'"))
        tool = await _tool(_mcp_with(svc), "brain_ticket_create")
        result = await tool.fn(
            from_project=FROM,
            to_project="red-dataz",
            kind="fyi",
            title="t",
            body="b",
        )
        assert result and result[0].isalnum()
        assert "red-dataz" in result

    async def test_create_malformed_project_key(self):
        tool = await _tool(_mcp_with(MagicMock()), "brain_ticket_create")
        result = await tool.fn(
            from_project="Red Shrik",
            to_project=TO,
            kind="request",
            title="t",
            body="b",
        )
        assert result and result[0].isalnum()

    async def test_create_with_extraction_skipped(self):
        svc = MagicMock()
        svc.create = AsyncMock(return_value=_ticket())
        tool = await _tool(_mcp_with(svc), "brain_ticket_create")
        result = await tool.fn(
            from_project=FROM,
            to_project=TO,
            kind="fyi",
            title="job done",
            body="b",
            extraction="skipped",
        )
        assert result.startswith("ok ")
        data = svc.create.await_args.args[0]
        assert data.extraction_status is ExtractionStatus.SKIPPED

    async def test_create_invalid_extraction_rejected(self):
        tool = await _tool(_mcp_with(MagicMock()), "brain_ticket_create")
        result = await tool.fn(
            from_project=FROM,
            to_project=TO,
            kind="fyi",
            title="t",
            body="b",
            extraction="garbage",
        )
        assert result and result[0].isalnum()
        assert "skipped" in result


class TestTransition:
    async def test_transition_ok(self):
        svc = MagicMock()
        svc.transition = AsyncMock(return_value=_ticket(status=TicketStatus.RESOLVED))
        tool = await _tool(_mcp_with(svc), "brain_ticket_transition")
        result = await tool.fn(
            ticket_id=str(uuid4()),
            author_project=TO,
            action="resolve",
        )
        assert result.startswith("ok ")
        assert "resolved" in result

    async def test_transition_illegal_is_error_str(self):
        svc = MagicMock()
        svc.transition = AsyncMock(side_effect=IllegalTransitionError("'ack' is illegal"))
        tool = await _tool(_mcp_with(svc), "brain_ticket_transition")
        result = await tool.fn(
            ticket_id=str(uuid4()),
            author_project=TO,
            action="ack",
        )
        assert result == "'ack' is illegal"

    async def test_transition_invalid_uuid(self):
        tool = await _tool(_mcp_with(MagicMock()), "brain_ticket_transition")
        result = await tool.fn(ticket_id="nope", author_project=TO, action="resolve")
        assert result and result[0].isalnum()
        assert "UUID" in result

    async def test_transition_conflict_is_bounded_user_facing_error(self):
        repo = MagicMock()
        repo.get_by_id = AsyncMock(return_value=_ticket(status=TicketStatus.OPEN))
        repo.apply_transition = AsyncMock(return_value=None)
        repo.add_message = AsyncMock()
        svc = TicketService(repo=repo, project_context_repo=MagicMock())
        tool = await _tool(_mcp_with(svc), "brain_ticket_transition")

        result = await tool.fn(
            ticket_id=str(uuid4()),
            author_project=TO,
            action="resolve",
            message="done",
        )

        assert result == "Ticket changed concurrently; reload and retry"
        assert len(result) < 100
        repo.add_message.assert_not_awaited()


class TestListAndGet:
    async def test_list_description_documents_pagination_and_recent_activity_order(self):
        tool = await _tool(_mcp_with(MagicMock()), "brain_ticket_list")

        assert "Pagination is applied independently to every category" in tool.description
        assert "updated_at DESC, created_at DESC" in tool.description
        assert "including deadlines" in tool.description

    async def test_list_grouped_rendering(self):
        svc = MagicMock()
        svc.list_grouped = AsyncMock(
            return_value=TicketGroups(
                a_traiter=[_ticket()],
                a_confirmer=[_ticket(status=TicketStatus.RESOLVED)],
                en_attente=[],
            )
        )
        tool = await _tool(_mcp_with(svc), "brain_ticket_list")
        result = await tool.fn(project_key=TO)
        assert "À traiter (1)" in result
        assert "À confirmer (1)" in result

    async def test_list_renders_awaiting_requester_confirmation_section_and_total(self):
        # spec 2026-08-03-ticket-briefing-fourth-quadrant §2.4, test 7: the total
        # includes the new group (so no "no ticket" when it alone is non-empty),
        # and a complete section is rendered.
        svc = MagicMock()
        svc.list_grouped = AsyncMock(
            return_value=TicketGroups(
                awaiting_requester_confirmation=[
                    _ticket(title="wontfix par nous", status=TicketStatus.WONTFIX)
                ],
            )
        )
        tool = await _tool(_mcp_with(svc), "brain_ticket_list")

        result = await tool.fn(project_key=TO)

        assert "aucun ticket" not in result
        assert "(1)" in result
        assert "wontfix par nous" in result

    async def test_list_default_page_reports_exact_omission_and_next_call(self):
        tickets = [_ticket(title=f"ticket-{index}") for index in range(12)]
        svc = MagicMock()
        svc.list_grouped = AsyncMock(return_value=TicketGroups(a_traiter=tickets))
        tool = await _tool(_mcp_with(svc), "brain_ticket_list")

        result = await tool.fn(project_key=TO)

        assert "À traiter (12)" in result
        assert "ticket-0" in result and "ticket-9" in result
        assert "ticket-10" not in result
        assert "2 omis sur cette page; 0 avant, 2 après" in result
        assert "brain_ticket_list(project_key='red-data', limit=10, offset=10)" in result

    async def test_list_offset_reaches_later_tickets_in_every_category(self):
        incoming = [_ticket(title=f"in-{index}") for index in range(12)]
        confirmable = [
            _ticket(title=f"confirm-{index}", status=TicketStatus.RESOLVED) for index in range(12)
        ]
        waiting = [_ticket(title=f"wait-{index}") for index in range(12)]
        svc = MagicMock()
        svc.list_grouped = AsyncMock(
            return_value=TicketGroups(
                a_traiter=incoming,
                a_confirmer=confirmable,
                en_attente=waiting,
            )
        )
        tool = await _tool(_mcp_with(svc), "brain_ticket_list")

        result = await tool.fn(project_key=TO, limit=5, offset=10)

        assert "in-10" in result and "in-11" in result
        assert "confirm-10" in result and "confirm-11" in result
        assert "wait-10" in result and "wait-11" in result
        assert "in-9" not in result
        assert "confirm-9" not in result
        assert "wait-9" not in result
        assert result.count("10 omis sur cette page; 10 avant, 0 après") == 3

    async def test_list_clamps_oversized_limit_to_one_hundred(self):
        tickets = [_ticket(title=f"ticket-{index}") for index in range(102)]
        svc = MagicMock()
        svc.list_grouped = AsyncMock(return_value=TicketGroups(a_traiter=tickets))
        tool = await _tool(_mcp_with(svc), "brain_ticket_list")

        result = await tool.fn(project_key=TO, limit=1_000)

        assert "« ticket-99 »" in result
        assert "« ticket-100 »" not in result
        assert "2 omis sur cette page; 0 avant, 2 après" in result
        assert "limit=100, offset=100" in result

    async def test_list_clamps_zero_limit_to_one(self):
        tickets = [_ticket(title=f"ticket-{index}") for index in range(2)]
        svc = MagicMock()
        svc.list_grouped = AsyncMock(return_value=TicketGroups(a_traiter=tickets))
        tool = await _tool(_mcp_with(svc), "brain_ticket_list")

        result = await tool.fn(project_key=TO, limit=0)

        assert "« ticket-0 »" in result
        assert "« ticket-1 »" not in result
        assert "1 omis sur cette page; 0 avant, 1 après" in result
        assert "limit=1, offset=1" in result

    async def test_list_normalizes_negative_offset_to_first_page(self):
        tickets = [_ticket(title=f"ticket-{index}") for index in range(3)]
        svc = MagicMock()
        svc.list_grouped = AsyncMock(return_value=TicketGroups(a_traiter=tickets))
        tool = await _tool(_mcp_with(svc), "brain_ticket_list")

        result = await tool.fn(project_key=TO, limit=2, offset=-5)

        assert "« ticket-0 »" in result and "« ticket-1 »" in result
        assert "« ticket-2 »" not in result
        assert "1 omis sur cette page; 0 avant, 1 après" in result
        assert "limit=2, offset=2" in result

    async def test_list_empty(self):
        svc = MagicMock()
        svc.list_grouped = AsyncMock(return_value=TicketGroups())
        tool = await _tool(_mcp_with(svc), "brain_ticket_list")
        result = await tool.fn(project_key=TO)
        assert "aucun ticket" in result

    async def test_get_renders_thread_and_allowed_actions(self):
        t = _ticket()
        svc = MagicMock()
        svc.get_with_thread = AsyncMock(
            return_value=(
                t,
                [
                    TicketMessage(
                        ticket_id=t.id,
                        author_project=TO,
                        body="je regarde",
                        created_at=datetime.now(UTC),
                    )
                ],
            )
        )
        tool = await _tool(_mcp_with(svc), "brain_ticket_get")
        result = await tool.fn(ticket_id=str(t.id))
        assert "je regarde" in result
        assert "resolve" in result  # actions possibles depuis open/request

    async def test_get_not_found(self):
        svc = MagicMock()
        svc.get_with_thread = AsyncMock(return_value=None)
        tool = await _tool(_mcp_with(svc), "brain_ticket_get")
        result = await tool.fn(ticket_id=str(uuid4()))
        assert result and result[0].isalnum()

    async def test_get_self_ticket_lists_resolve_pending(self):
        # spec §4.2/§4.3: on a self-ticket (from == to), resolve_pending must be
        # discoverable — the self_ticket flag must reach allowed_actions() here.
        t = _ticket(from_project="brain-v42", to_project="brain-v42")
        svc = MagicMock()
        svc.get_with_thread = AsyncMock(return_value=(t, []))
        tool = await _tool(_mcp_with(svc), "brain_ticket_get")
        result = await tool.fn(ticket_id=str(t.id))
        assert "resolve_pending" in result


class TestIdPrefixResolution:
    async def test_get_resolves_unique_prefix(self):
        t = _ticket()
        svc = MagicMock()
        svc.resolve_id_prefix = AsyncMock(return_value=[t.id])
        svc.get_with_thread = AsyncMock(return_value=(t, []))
        tool = await _tool(_mcp_with(svc), "brain_ticket_get")

        result = await tool.fn(ticket_id=t.id.hex[:8])

        svc.resolve_id_prefix.assert_awaited_once_with(t.id.hex[:8])
        svc.get_with_thread.assert_awaited_once_with(t.id)
        assert "exposer ndjson" in result

    async def test_get_ambiguous_prefix_lists_matches(self):
        a, b = uuid4(), uuid4()
        svc = MagicMock()
        svc.resolve_id_prefix = AsyncMock(return_value=[a, b])
        svc.get_with_thread = AsyncMock()
        tool = await _tool(_mcp_with(svc), "brain_ticket_get")

        result = await tool.fn(ticket_id="61b0fa47")

        assert "Ambiguous" in result
        assert str(a) in result
        svc.get_with_thread.assert_not_awaited()

    async def test_get_garbage_id_stays_invalid_uuid(self):
        svc = MagicMock()
        svc.resolve_id_prefix = AsyncMock()
        tool = await _tool(_mcp_with(svc), "brain_ticket_get")

        result = await tool.fn(ticket_id="pas-un-uuid")

        assert "Invalid UUID" in result
        svc.resolve_id_prefix.assert_not_awaited()

    async def test_transition_keeps_exact_uuid_only(self):
        """Writes stay exact-UUID in v1 — a prefix on transition is rejected."""
        svc = MagicMock()
        svc.resolve_id_prefix = AsyncMock()
        svc.transition = AsyncMock()
        tool = await _tool(_mcp_with(svc), "brain_ticket_transition")

        result = await tool.fn(ticket_id="61b0fa47", author_project=FROM, action="ack")

        assert "Invalid UUID" in result
        svc.resolve_id_prefix.assert_not_awaited()
        svc.transition.assert_not_awaited()


class TestReplyCanCorrectTheTicketBody:
    """`cabb7503` — fix a stale body WITHOUT adding a tool to the catalogue.

    The public MCP contract has no free room left: its floor already had to be
    renegotiated by measurement (10,000 → 9,500). A sixth tool would have been the
    obvious solution; that is why it was not taken.
    """

    async def test_the_ticket_catalog_still_has_exactly_five_tools(self):
        """Architecture guard: no tool added, the fix goes through `reply`.

        This test does not measure bytes — it pins the DECISION. A sixth tool here
        would be an operator decision, not a worker's batch, and this red is what
        forces it to be put rather than taken in passing.
        """
        mcp = _mcp_with(MagicMock())

        async with Client(mcp) as client:
            names = [tool.name for tool in await client.list_tools()]
        ticket_tools = sorted(n for n in names if n.startswith("brain_ticket_"))

        assert ticket_tools == [
            "brain_ticket_create",
            "brain_ticket_get",
            "brain_ticket_list",
            "brain_ticket_reply",
            "brain_ticket_transition",
        ], f"le catalogue ticket a changé de taille : {ticket_tools}"

    async def test_corrects_body_reaches_the_service(self):
        svc = MagicMock()
        svc.reply = AsyncMock()
        tool = await _tool(_mcp_with(svc), "brain_ticket_reply")

        result = await tool.fn(
            ticket_id=str(uuid4()),
            author_project=TO,
            body="la prémisse est morte : décision 218028c7",
            corrects_body="GitHub est l'unique rail",
        )

        assert svc.reply.await_args.kwargs["corrects_body"] == "GitHub est l'unique rail"
        assert "body corrected" in result, (
            "la confirmation doit dire que le corps a changé — sinon l'appelant "
            f"ne distingue pas une correction d'une simple réponse : {result!r}"
        )

    async def test_a_plain_reply_forwards_none_and_says_nothing_about_the_body(self):
        """Negative witness at tool level: nothing changes for an ordinary reply."""
        svc = MagicMock()
        svc.reply = AsyncMock()
        tool = await _tool(_mcp_with(svc), "brain_ticket_reply")

        result = await tool.fn(
            ticket_id=str(uuid4()),
            author_project=TO,
            body="juste une remarque",
        )

        assert svc.reply.await_args.kwargs["corrects_body"] is None
        assert "body corrected" not in result, (
            f"une réponse ordinaire annonce une correction de corps : {result!r}"
        )
