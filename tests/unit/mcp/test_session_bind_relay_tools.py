from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

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
