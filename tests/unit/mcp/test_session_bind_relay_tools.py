from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from brain_v42.mcp.tool_catalog import SESSION_LIFECYCLE_TOOLS
from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools


def _server() -> tuple[FastMCP, MagicMock, AsyncMock]:
    service, loader = MagicMock(), AsyncMock(return_value="## briefing")
    server = FastMCP("bind-relay")
    register_session_lifecycle_tools(server, service, loader)
    return server, service, loader


async def test_bind_forwards_and_is_an_idempotent_write() -> None:
    server, service, _ = _server()
    service.bind = AsyncMock(return_value=MagicMock())
    tool = await server.get_tool("brain_session_bind")
    session_id, slot_id = uuid4(), uuid4()
    await tool.fn(session_id=session_id, expected_client_key="k", slot_id=slot_id)
    service.bind.assert_awaited_once_with(
        session_id=session_id, expected_client_key="k", slot_id=slot_id
    )
    assert tool.annotations == ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
    assert tool.version == "4.0"


def test_bind_is_always_visible() -> None:
    assert "brain_session_bind" in SESSION_LIFECYCLE_TOOLS


async def test_relay_forwards_then_adds_the_successor_briefing() -> None:
    server, service, loader = _server()
    result = MagicMock()
    result.session.project_key = "brain-v42"
    result.model_copy = MagicMock(return_value="with briefing")
    service.relay = AsyncMock(return_value=result)
    tool = await server.get_tool("brain_session_relay")
    session_id = uuid4()
    knowledge_id = uuid4()
    outcome = await tool.fn(
        session_id=session_id,
        expected_client_key="old",
        summary="s",
        handover="h",
        expected_slot_revision=2,
        new_client_key="new",
        initiator="guard_mod",
        knowledge_ids=[knowledge_id],
        nothing_to_capture_reason="no findings to retain",
    )
    service.relay.assert_awaited_once_with(
        session_id=session_id,
        expected_client_key="old",
        summary="s",
        handover="h",
        expected_slot_revision=2,
        new_client_key="new",
        initiator="guard_mod",
        knowledge_ids=[knowledge_id],
        nothing_to_capture_reason="no findings to retain",
    )
    loader.assert_awaited_once_with("brain-v42", result.session.id)
    result.model_copy.assert_called_once_with(update={"briefing": "## briefing"})
    assert outcome == "with briefing"


async def test_relay_schema_bounds_and_annotations() -> None:
    server, _, _ = _server()
    tool = await server.get_tool("brain_session_relay")
    properties = tool.parameters["properties"]
    assert properties["handover"]["maxLength"] == 4000
    assert properties["summary"]["maxLength"] == 10_000
    assert properties["new_client_key"]["maxLength"] == 128
    assert properties["initiator"]["enum"] == ["operator", "guard_mod"]
    assert tool.annotations == ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    )
    assert "brain_session_relay" in SESSION_LIFECYCLE_TOOLS


async def test_relay_refuses_oversized_summary_and_handover() -> None:
    server, _, _ = _server()
    tool = await server.get_tool("brain_session_relay")
    arguments = {
        "session_id": uuid4(),
        "expected_client_key": "old",
        "summary": "s" * 10_001,
        "handover": "h",
        "expected_slot_revision": 2,
        "new_client_key": "new",
        "initiator": "operator",
    }
    with pytest.raises(ValidationError):
        await tool.run(arguments)
    arguments["summary"] = "s"
    arguments["handover"] = "h" * 4_001
    with pytest.raises(ValidationError):
        await tool.run(arguments)
