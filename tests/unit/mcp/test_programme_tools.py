"""brain_programme_* tools: registration, annotations, schema bounds, forwarding.

No tool takes an argument naming its writer: the service reads the caller from the
credential (contract docs/contracts/programmes_v1.md §6).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from brain_v42.mcp.business_errors import SURFACED_BUSINESS_ERRORS
from brain_v42.mcp.tool_families import TOOL_FAMILIES
from brain_v42.mcp.tools.programme_tools import ProgrammeProgress, register_programme_tools
from brain_v42.models.programme import ProgrammeError

WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
TOOLS = {
    "brain_programme_upsert": WRITE,
    "brain_programme_gate_upsert": WRITE,
    "brain_programme_gate_report": WRITE,
    "brain_programme_request": WRITE,
    "brain_programme_list": READ,
}
IDENTITY_WORDS = ("caller", "author", "updated_by", "by", "actor", "project_key", "coordinator")


async def _server() -> tuple[FastMCP, MagicMock]:
    service = MagicMock()
    for method in ("upsert_programme", "upsert_gate", "report_gate", "request", "list_programmes"):
        setattr(service, method, AsyncMock(return_value={"key": "move"}))
    server = FastMCP("programme-tools")
    register_programme_tools(server, service)
    return server, service


async def test_the_five_tools_are_registered_with_their_annotations_and_families() -> None:
    server, _ = await _server()
    for name, expected in TOOLS.items():
        tool = await server.get_tool(name)
        assert tool is not None and tool.annotations == expected, name
        assert TOOL_FAMILIES[name] == ("read" if expected.readOnlyHint else "write"), name


async def test_no_tool_lets_the_caller_name_the_writer() -> None:
    server, _ = await _server()
    for name in TOOLS:
        properties = (await server.get_tool(name)).parameters["properties"]
        assert not set(properties) & set(IDENTITY_WORDS), (name, sorted(properties))


async def test_schema_bounds_follow_the_contract() -> None:
    server, _ = await _server()
    gate = (await server.get_tool("brain_programme_gate_upsert")).parameters["properties"]
    assert gate["depends_on"]["anyOf"][0]["maxItems"] == 12
    report = (await server.get_tool("brain_programme_gate_report")).parameters["properties"]
    assert report["note"]["anyOf"][0]["maxLength"] == 200
    request = (await server.get_tool("brain_programme_request")).parameters["properties"]
    assert request["ask"]["anyOf"][0]["maxLength"] == 200


async def test_each_tool_forwards_to_the_service() -> None:
    server, service = await _server()
    await (await server.get_tool("brain_programme_upsert")).fn(key="move", title="Move")
    service.upsert_programme.assert_awaited_once_with(
        key="move", title="Move", state=None, position=None
    )
    await (await server.get_tool("brain_programme_gate_upsert")).fn(
        programme="move", gate="G7", title="t", owner="red-backup", depends_on=["G6"], weight=4
    )
    service.upsert_gate.assert_awaited_once_with(
        programme="move",
        gate="G7",
        title="t",
        owner="red-backup",
        depends_on=["G6"],
        weight=4,
        position=None,
        state=None,
    )
    await (await server.get_tool("brain_programme_gate_report")).fn(
        programme="move",
        gate="G7",
        state="in_progress",
        progress=ProgrammeProgress(done=6, total=15, unit="tasks"),
    )
    service.report_gate.assert_awaited_once_with(
        programme="move",
        gate="G7",
        state="in_progress",
        progress={"done": 6, "total": 15, "unit": "tasks"},
        clear_progress=False,
        note=None,
        clear_note=False,
    )
    await (await server.get_tool("brain_programme_request")).fn(programme="move", gate="G7")
    service.request.assert_awaited_once_with(programme="move", gate="G7", ask=None)
    result = await (await server.get_tool("brain_programme_list")).fn()
    service.list_programmes.assert_awaited_once_with(include_done=False, key=None)
    assert result == {"programmes": {"key": "move"}}


def test_programme_refusals_reach_the_caller_unmasked() -> None:
    assert ProgrammeError in SURFACED_BUSINESS_ERRORS
