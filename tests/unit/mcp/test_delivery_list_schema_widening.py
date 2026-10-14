"""The ticket filter only widens the published brain_delivery_list input schema.

The base fixture is the schema published before the filter existed. Regenerate it by
running this module's ``__main__`` with the base sources first on the import path, then
set ``source_ref`` to the base commit.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastmcp import Client

BASE_FIXTURE = Path(__file__).parents[2] / "fixtures/mcp_delivery_list_input_schema_base.json"


async def _published_list_schema() -> dict:
    from unittest.mock import AsyncMock

    from brain_v42.mcp.server import create_mcp_instance, prepare_tools_for_transport
    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools

    app = create_mcp_instance()
    register_delivery_tools(app, delivery_svc=AsyncMock())
    await prepare_tools_for_transport(app, None)
    async with Client(app) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    return tools["brain_delivery_list"].inputSchema


def published_delivery_list_schema() -> dict:
    return asyncio.run(_published_list_schema())


def test_delivery_list_schema_only_adds_an_optional_ticket_id():
    base = json.loads(BASE_FIXTURE.read_text())["schema"]
    current = published_delivery_list_schema()
    assert {key: value for key, value in current.items() if key != "properties"} == {
        key: value for key, value in base.items() if key != "properties"
    }
    for key, value in base["properties"].items():
        assert current["properties"][key] == value
    assert set(current["properties"]) - set(base["properties"]) == {"ticket_id"}
    ticket_id = current["properties"]["ticket_id"]
    assert ticket_id["default"] is None
    assert {"type": "null"} in ticket_id["anyOf"]
    assert "ticket_id" not in current["required"]


if __name__ == "__main__":
    print(json.dumps({"schema": published_delivery_list_schema()}))
