from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from brain_v42.mcp.tools.focus_slot_tools import register_focus_slot_tools
from brain_v42.models.focus_slot import SlotAnchor


async def _server() -> tuple[FastMCP, MagicMock]:
    service = MagicMock()
    for method in ("open", "list", "close"):
        setattr(service, method, AsyncMock(return_value=MagicMock()))
    server = FastMCP("focus-slot-tools")
    register_focus_slot_tools(server, service)
    return server, service


async def test_the_three_tools_are_registered_with_their_safety_annotations() -> None:
    server, _ = await _server()
    expected = {
        "brain_slot_open": ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        "brain_slot_list": ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        "brain_slot_close": ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
        ),
    }
    for name, annotations in expected.items():
        tool = await server.get_tool(name)
        assert tool is not None and tool.annotations == annotations, name


async def test_input_bounds_are_the_spec_bounds() -> None:
    server, _ = await _server()
    open_schema = (await server.get_tool("brain_slot_open")).parameters["properties"]
    assert open_schema["title"]["maxLength"] == 120
    assert open_schema["body"]["maxLength"] == 4000
    assert (open_schema["anchors"]["minItems"], open_schema["anchors"]["maxItems"]) == (1, 10)
    close_schema = (await server.get_tool("brain_slot_close")).parameters["properties"]
    assert close_schema["note"]["maxLength"] == 2000
    assert close_schema["expected_revision"]["minimum"] == 0


async def test_open_list_close_forward_to_the_service() -> None:
    server, service = await _server()
    anchor = SlotAnchor(kind="lot", target_release="0.6.4")
    await (await server.get_tool("brain_slot_open")).fn(
        project_key="brain-v42", title="t", body="b", anchors=[anchor]
    )
    service.open.assert_awaited_once_with(
        project_key="brain-v42", title="t", body="b", anchors=[anchor]
    )
    await (await server.get_tool("brain_slot_list")).fn(project_key="brain-v42")
    service.list.assert_awaited_once_with(
        project_key="brain-v42", status="open", limit=20, offset=0
    )
    slot_id = uuid4()
    await (await server.get_tool("brain_slot_close")).fn(
        slot_id=slot_id, expected_revision=2, note="done"
    )
    service.close.assert_awaited_once_with(slot_id=slot_id, expected_revision=2, note="done")


async def test_open_and_close_docstrings_say_explicit_user_command() -> None:
    server, _ = await _server()
    for name in ("brain_slot_open", "brain_slot_close"):
        description = (await server.get_tool(name)).description or ""
        assert "explicit user command" in " ".join(description.split()), name
