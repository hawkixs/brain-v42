"""Original authors come from verified credentials on every creation path."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken

from brain_v42.mcp import client_principal, tool_families
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from brain_v42.mcp.tools import formatters
from brain_v42.mcp.tools.brain_tools import register_tools
from brain_v42.mcp.tools.crud_tools import register_crud_tools
from brain_v42.mcp.tools.focus_slot_tools import register_focus_slot_tools
from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools
from brain_v42.mcp.tools.session_tools import _section_bound_slot, _section_slots
from brain_v42.models.adr import ADR
from brain_v42.models.brain_session import (
    BrainSession,
    BrainSessionCheckpointResult,
    BrainSessionStartResult,
)
from brain_v42.models.decision import Decision
from brain_v42.models.focus_slot import (
    BrainSessionRelayResult,
    FocusSlot,
    FocusSlotOpenResult,
    FocusSlotView,
    SlotBriefing,
)
from brain_v42.models.learning import Learning
from brain_v42.models.runbook import Runbook
from brain_v42.models.snippet import Snippet

MODELS = {
    "decision": Decision,
    "learning": Learning,
    "snippet": Snippet,
    "runbook": Runbook,
    "adr": ADR,
}
DETAIL = {
    "decision": formatters.format_decision_detail,
    "learning": formatters.format_learning_detail,
    "snippet": formatters.format_snippet_detail,
    "runbook": formatters.format_runbook,
    "adr": formatters.format_adr_detail,
}
LIST = {
    "decision": formatters.format_decisions,
    "learning": formatters.format_learnings,
    "snippet": formatters.format_snippets,
    "runbook": formatters.format_runbooks,
    "adr": formatters.format_adrs,
}
DECISION_ARGS = {
    "title": "Choice",
    "context": "Context",
    "decision_made": "Choice",
    "reasoning": "Reason",
}
ADR_ARGS = {
    "title": "Choice",
    "context": "Context",
    "decision": "Choice",
    "consequences": "Effects",
    "project_key": "brain-v42",
}
RUNBOOK_ARGS = {
    "title": "Deploy",
    "description": "Deploy safely",
    "project_key": "brain-v42",
    "trigger": "release",
    "steps": ["Build"],
}
WRITES = [
    ("brain_log_decision", "decision", "create", DECISION_ARGS),
    (
        "brain_learn",
        "learning",
        "create",
        {"topic": "Lesson", "insight": "Insight", "source": "test"},
    ),
    (
        "brain_save_snippet",
        "snippet",
        "create",
        {"title": "Parse", "intention": "Parse data", "code": "pass", "language": "python"},
    ),
    ("brain_create_runbook", "runbook", "create", RUNBOOK_ARGS),
    ("brain_propose_adr", "adr", "create", ADR_ARGS),
    (
        "brain_slot_open",
        "slot",
        "open",
        {
            "project_key": "brain-v42",
            "title": "Topic",
            "body": "Work",
            "anchors": [{"kind": "lot", "target_release": "1.0.0"}],
        },
    ),
    (
        "brain_supersede_decision",
        "decision",
        "supersede",
        dict(**DECISION_ARGS, old_decision_id=str(uuid4())),
    ),
    (
        "brain_promote_adr",
        "adr",
        "create_with_promotion",
        dict(**ADR_ARGS, source_learning_id=str(uuid4())),
    ),
    (
        "brain_promote_runbook",
        "runbook",
        "create_with_promotion",
        dict(**RUNBOOK_ARGS, source_learning_id=str(uuid4())),
    ),
]


def entity(kind, author=None):
    now = datetime.now(UTC)
    data = {
        "id": uuid4(),
        "title": "Title",
        "description": "Description",
        "reasoning": "Reason",
        "topic": "Topic",
        "insight": "Insight",
        "source": "test",
        "intention": "Intent",
        "code": "pass",
        "language": "python",
        "context": "Context",
        "decision": "Choice",
        "consequences": "Effects",
        "number": 1,
        "project_key": "brain-v42",
        "trigger": "release",
        "steps": [{"order": 1, "title": "Build"}],
        "created_at": now,
        "updated_at": now,
        "author_client_id": author,
    }
    return MODELS[kind].model_validate(data)


def server_and_services():
    services = {f"{kind}_svc": MagicMock() for kind in MODELS}
    for kind in MODELS:
        service = services[f"{kind}_svc"]
        for method in ("create", "create_with_promotion", "supersede", "get_by_id", "update"):
            setattr(service, method, AsyncMock(return_value=entity(kind)))
    server = FastMCP("author-client-id")
    register_tools(server, **services, project_context_svc=MagicMock(), brain_svc=MagicMock())
    register_crud_tools(server, **services, session_factory=MagicMock())
    slot_service = MagicMock()
    now = datetime.now(UTC)
    slot_service.open = AsyncMock(
        return_value=FocusSlotOpenResult(
            slot=FocusSlot(
                id=uuid4(),
                project_key="brain-v42",
                title="Topic",
                body="Work",
                revision=0,
                opened_at=now,
                body_updated_at=now,
            ),
            replayed=False,
        )
    )
    register_focus_slot_tools(server, slot_service)
    services["slot_svc"] = slot_service
    return server, services


async def test_author_is_absent_from_registered_tool_input_and_output_schemas() -> None:
    server, _ = server_and_services()
    for tool in await server.list_tools():
        assert "author_client_id" not in json.dumps([tool.parameters, tool.output_schema]), (
            tool.name
        )


@pytest.mark.parametrize("profile", ["native", "compact"])
async def test_slot_tool_result_hides_the_stored_author(profile: str) -> None:
    server, services = server_and_services()
    stored = services["slot_svc"].open.return_value
    stored.slot.author_client_id = "original-client"
    apply_tool_catalog_profile(server, profile)
    name, _, _, arguments = WRITES[5]
    async with Client(server) as client:
        if profile == "compact":
            result = await client.call_tool(
                "brain_call_tool", {"name": name, "arguments": arguments}
            )
        else:
            result = await client.call_tool(name, arguments)
    assert stored.slot.author_client_id == "original-client"
    assert result.structured_content is not None
    assert "author_client_id" not in json.dumps(result.structured_content)
    assert "author_client_id" not in json.dumps(
        [block.model_dump(mode="json") for block in result.content]
    )


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize("operation", ["start", "relay", "checkpoint"])
async def test_session_lifecycle_results_and_schemas_hide_the_slot_author(
    profile: str, operation: str
) -> None:
    server, services = server_and_services()
    slot = services["slot_svc"].open.return_value.slot
    slot.author_client_id = "original-client"
    assert slot.closed_at is None
    now = datetime.now(UTC)
    session = BrainSession(
        id=uuid4(),
        project_key="brain-v42",
        client_key="session-client",
        started_focus_revision=0,
        started_at=now,
        updated_at=now,
    )
    successor = session.model_copy(update={"id": uuid4(), "client_key": "successor"})
    service = MagicMock(
        start=AsyncMock(
            return_value=BrainSessionStartResult(
                session=session, replayed=False, open_session_count=1
            )
        ),
        relay=AsyncMock(
            return_value=BrainSessionRelayResult(
                ended_session_id=session.id, session=successor, slot=slot, replayed=False
            )
        ),
        checkpoint=AsyncMock(
            return_value=BrainSessionCheckpointResult(
                session_id=session.id, seq=1, created_at=now, replayed=False, checkpoint_count=1
            )
        ),
    )
    slot_view = SlotBriefing(
        open_slots=[
            FocusSlotView.model_validate(
                dict(slot.model_dump(), author_client_id=slot.author_client_id)
            )
        ],
        bound_slot=slot,
    )
    loader = AsyncMock(
        return_value="\n".join([_section_slots(slot_view, now=now), _section_bound_slot(slot_view)])
    )
    register_session_lifecycle_tools(server, service, loader)
    apply_tool_catalog_profile(server, profile)
    arguments = {
        "start": {"project_key": "brain-v42", "client_key": session.client_key},
        "relay": {
            "session_id": str(session.id),
            "expected_client_key": session.client_key,
            "summary": "Progress",
            "handover": slot.body,
            "expected_slot_revision": slot.revision,
            "new_client_key": successor.client_key,
            "initiator": "operator",
        },
        "checkpoint": {
            "session_id": str(session.id),
            "expected_client_key": session.client_key,
            "seq": 1,
            "progress": "Progress",
            "next_step": "Continue",
        },
    }
    name = f"brain_session_{operation}"
    async with Client(server) as client:
        if profile == "compact" and operation == "checkpoint":
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert name not in tools
            gateway = tools["brain_call_tool"]
            assert "author_client_id" not in json.dumps([gateway.inputSchema, gateway.outputSchema])
            result = await client.call_tool(
                "brain_call_tool", {"name": name, "arguments": arguments[operation]}
            )
        else:
            tool = next(tool for tool in await client.list_tools() if tool.name == name)
            assert tool.outputSchema is not None
            assert "author_client_id" not in json.dumps(tool.outputSchema)
            result = await client.call_tool(name, arguments[operation])

    assert slot.author_client_id == "original-client"
    assert result.structured_content is not None
    payload = json.dumps(
        [result.structured_content, [block.model_dump(mode="json") for block in result.content]]
    )
    assert "author_client_id" not in payload
    assert "original-client" not in payload
    getattr(service, operation).assert_awaited_once()
    if operation in ("start", "relay"):
        loader.assert_awaited_once()
        assert slot.title in result.structured_content["briefing"]
    if operation == "relay":
        assert result.structured_content["slot"]["id"] == str(slot.id)


@pytest.fixture(params=["credential", "stdio", "shared", "unverified"])
def author_context(request, monkeypatch):
    expected = "client-a" if request.param == "credential" else None
    claims = {"credential_id": str(uuid4()), "issuers": []} if expected else {}
    if request.param == "unverified":
        claims = {"credential_id": "unverified", "issuers": []}
    token = (
        None
        if request.param == "stdio"
        else AccessToken(
            token="opaque",
            client_id="client-a",
            scopes=["write"],
            claims=claims,
        )
    )
    monkeypatch.setattr(client_principal, "get_access_token", lambda: token, raising=False)
    monkeypatch.setattr(tool_families, "get_access_token", lambda: token)
    monkeypatch.setattr(
        tool_families, "get_http_headers", lambda **kw: {"mcp-session-id": "author-test"}
    )
    return expected


def test_author_helper_accepts_only_verified_credentials(author_context):
    assert client_principal.current_author_client_id() == author_context


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize("name,kind,method,arguments", WRITES)
async def test_every_write_uses_the_verified_author(
    author_context, name, kind, method, arguments, profile
):
    server, services = server_and_services()
    if author_context is not None:
        server.add_middleware(
            tool_families.FamilyAuthorizationMiddleware(
                elevation_checker=AsyncMock(return_value=True)
            )
        )
    apply_tool_catalog_profile(server, profile)
    async with Client(server) as client:
        if profile == "compact":
            await client.call_tool("brain_call_tool", {"name": name, "arguments": arguments})
        else:
            await client.call_tool(name, arguments)
    call = getattr(services[f"{kind}_svc"], method).await_args
    assert call is not None
    data = call.kwargs.get("data") or (call.args[-1] if call.args else None)
    author = call.kwargs.get("author_client_id") if kind == "slot" else data.author_client_id
    assert author == author_context


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize("name,kind,method,arguments", WRITES)
async def test_caller_cannot_spoof_author(author_context, name, kind, method, arguments, profile):
    server, services = server_and_services()
    apply_tool_catalog_profile(server, profile)
    forged_arguments = dict(arguments, author_client_id="forged")
    async with Client(server) as client:
        if profile == "compact":
            result = await client.call_tool(
                "brain_call_tool",
                {"name": name, "arguments": forged_arguments},
                raise_on_error=False,
            )
        else:
            result = await client.call_tool(name, forged_arguments, raise_on_error=False)
    call = getattr(services[f"{kind}_svc"], method).await_args
    if call is None:
        assert result.is_error
    else:
        data = call.kwargs.get("data") or (call.args[-1] if call.args else None)
        author = call.kwargs.get("author_client_id") if kind == "slot" else data.author_client_id
        assert author == author_context


@pytest.mark.parametrize("profile", ["native", "compact"])
@pytest.mark.parametrize("name,kind,method,arguments", WRITES[:6])
async def test_http_verified_credential_reaches_the_inner_write(
    name, kind, method, arguments, profile
):
    from dataclasses import replace

    import httpx

    from brain_v42.credentials.verifier import CredentialVerifier
    from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
    from brain_v42.mcp.server import plan_http_transport
    from tests.unit.mcp.test_credentials_http import NOW, TOKEN, Registry
    from tests.unit.mcp.test_credentials_http_wiring import settings

    class WriterRegistry(Registry):
        async def active_rows(self, now):
            return [replace(row, families=["write"]) for row in await super().active_rows(now)]

    registry = CredentialVerifier(WriterRegistry(), clock=lambda: NOW, monotonic=lambda: 0.0)
    assert await registry.refresh()
    server, services = server_and_services()
    server.add_middleware(ProvenanceMiddleware())
    server.add_middleware(tool_families.FamilyAuthorizationMiddleware())
    apply_tool_catalog_profile(server, profile)
    plan = plan_http_transport(server, settings(), credential_verifier=registry)
    app = server.http_app(middleware=plan.middleware, stateless_http=False, json_response=True)
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client,
    ):
        initialized = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "author-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        headers["Mcp-Session-Id"] = initialized.headers["Mcp-Session-Id"]
        await client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        params = {"name": name, "arguments": arguments}
        if profile == "compact":
            params = {"name": "brain_call_tool", "arguments": params}
        response = await client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params},
        )
        assert response.status_code == 200
        assert not response.json()["result"].get("isError"), response.text
    call = getattr(services[f"{kind}_svc"], method).await_args
    assert call is not None
    author = call.kwargs["author_client_id"] if kind == "slot" else call.args[0].author_client_id
    assert author == "red-rail"


@pytest.mark.parametrize("kind", MODELS)
@pytest.mark.parametrize("author", [None, "client-a"])
async def test_get_appends_author_after_unchanged_detail(kind, author):
    server, services = server_and_services()
    row = entity(kind, author)
    services[f"{kind}_svc"].get_by_id.return_value = row
    result = await (await server.get_tool("brain_get")).fn(entity_type=kind, entity_id=str(row.id))
    assert row.author_client_id == author
    assert result == DETAIL[kind](row) + (
        f"\nauthor_client_id: {author}" if author is not None else ""
    )


@pytest.mark.parametrize("kind", MODELS)
def test_all_knowledge_formatters_ignore_even_non_null_author(kind):
    historical = entity(kind)
    attributed = historical.model_copy(update={"author_client_id": "client-a"})
    assert DETAIL[kind](historical) == DETAIL[kind](attributed)
    assert LIST[kind]([historical]) == LIST[kind]([attributed])


@pytest.mark.parametrize("kind", MODELS)
@pytest.mark.parametrize("author", [None, "original-client"])
async def test_updates_preserve_original_author(author_context, kind, author):
    server, services = server_and_services()
    original = entity(kind, author)
    service = services[f"{kind}_svc"]
    service.update.return_value = original
    tool = await server.get_tool("brain_update")
    await tool.fn(entity_type=kind, entity_id=str(original.id), fields={"tags": ["edited"]})
    update = service.update.await_args.args[1]
    assert "author_client_id" not in update.model_dump()
    assert original.author_client_id == author
    async with Client(server) as client:
        await client.call_tool(
            "brain_update",
            {
                "entity_type": kind,
                "entity_id": str(original.id),
                "fields": {"author_client_id": "forged"},
            },
            raise_on_error=False,
        )
    assert service.update.await_count == 1


@pytest.mark.parametrize("author", [None, "original-client"])
async def test_slot_reuse_preserves_the_original_author(monkeypatch, author):
    from contextlib import asynccontextmanager

    from brain_v42.models.focus_slot import SlotAnchor
    from brain_v42.repositories import pg_focus_slot

    now = datetime.now(UTC)
    anchors = [SlotAnchor(kind="lot", target_release="1.0.0")]
    existing = FocusSlot(
        id=uuid4(),
        project_key="brain-v42",
        title="Topic",
        body="Work",
        revision=0,
        opened_at=now,
        body_updated_at=now,
        anchors=anchors,
        author_client_id=author,
    )
    repo = pg_focus_slot.PgFocusSlotRepo(MagicMock())
    session = AsyncMock()

    @asynccontextmanager
    async def transaction():
        yield session

    monkeypatch.setattr(repo, "transaction", transaction)
    monkeypatch.setattr(pg_focus_slot, "_require_project", AsyncMock())
    monkeypatch.setattr(repo, "_open_by_title", AsyncMock(return_value=existing))
    replay = await repo.open(
        "brain-v42", "Topic", "Work", anchors, author_client_id="second-client"
    )
    assert replay.replayed
    assert replay.slot.author_client_id == author
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("kind", ["decision", "learning"])
async def test_proposal_acceptance_keeps_author_null_even_with_a_request_context(
    author_context, kind
):
    from brain_v42.services.proposal_service import ProposalService

    service = object.__new__(ProposalService)
    service._learning_service = MagicMock(create=AsyncMock(return_value=entity("learning")))
    service._decision_service = MagicMock(create=AsyncMock(return_value=entity("decision")))
    payload = {
        "title": "Choice",
        "description": "Context",
        "reasoning": "Reason",
        "topic": "Lesson",
        "insight": "Insight",
        "author_client_id": "forged",
    }
    _, data = await service._create_ticket_entity(
        AsyncMock(),
        {
            "payload": payload,
            "ticket_id": uuid4(),
            "target_project": "brain-v42",
            "target_type": kind,
        },
    )
    assert data.author_client_id is None
