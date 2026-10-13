from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from brain_v42.mcp.tool_families import TOOL_FAMILIES
from brain_v42.mcp.tools.project_focus_read_tools import register_project_focus_read_tools
from brain_v42.mcp.tools.tool_annotations import _READ_ANNOTATIONS


def _setup(row: Any = None) -> tuple[Any, AsyncMock]:
    service = AsyncMock()
    service.get_by_key.return_value = row
    server = FastMCP("test")
    register_project_focus_read_tools(server, service)
    return server, service


@pytest.mark.asyncio
async def test_returns_focus_revision_timestamp_and_character_count() -> None:
    updated = datetime(2026, 10, 13, 12, 30, tzinfo=UTC)
    server, service = _setup(
        SimpleNamespace(
            project_key="brain-v42", current_focus="été", focus_revision=7, focus_updated_at=updated
        )
    )

    tool = await server.get_tool("brain_project_focus_get")
    result = await tool.fn(project_key="brain-v42")

    assert result.model_dump() == {
        "project_key": "brain-v42",
        "focus": "été",
        "focus_revision": 7,
        "focus_updated_at": "2026-10-13T12:30:00Z",
        "chars": 3,
    }
    service.get_by_key.assert_awaited_once_with("brain-v42")


@pytest.mark.asyncio
async def test_empty_focus_has_null_text_and_zero_characters() -> None:
    server, _ = _setup(
        SimpleNamespace(
            project_key="brain-v42", current_focus=None, focus_revision=0, focus_updated_at=None
        )
    )

    tool = await server.get_tool("brain_project_focus_get")
    result = await tool.fn(project_key="brain-v42")

    assert result.focus is None
    assert result.chars == 0


@pytest.mark.asyncio
async def test_unknown_project_names_key_and_list_hint() -> None:
    server, _ = _setup()

    with pytest.raises(ToolError, match="missing-project.*brain_list_projects"):
        tool = await server.get_tool("brain_project_focus_get")
        await tool.fn(project_key="missing-project")


@pytest.mark.asyncio
async def test_invalid_key_is_refused_before_service_call() -> None:
    server, service = _setup()

    with pytest.raises(ToolError):
        tool = await server.get_tool("brain_project_focus_get")
        await tool.fn(project_key="bad key!")

    service.get_by_key.assert_not_awaited()


async def test_registered_tool_publishes_read_only_five_field_output_schema() -> None:
    server = FastMCP("schema")
    register_project_focus_read_tools(server, AsyncMock())
    tool = await server.get_tool("brain_project_focus_get")

    assert tool.output_schema is not None
    assert set(tool.output_schema["properties"]) == {
        "project_key",
        "focus",
        "focus_revision",
        "focus_updated_at",
        "chars",
    }
    properties = tool.output_schema["properties"]
    assert properties["focus_revision"]["type"] == "integer"
    assert properties["chars"]["type"] == "integer"
    assert properties["project_key"]["type"] == "string"
    assert properties["focus"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert properties["focus_updated_at"]["anyOf"] == [
        {"type": "string"},
        {"type": "null"},
    ]
    assert tool.annotations == _READ_ANNOTATIONS
    assert tool.version == "1.0"
    assert TOOL_FAMILIES["brain_project_focus_get"] == "read"


@pytest.mark.asyncio
async def test_alias_project_key_is_canonicalized_before_service_call() -> None:
    server, service = _setup(
        SimpleNamespace(
            project_key="brain-v42", current_focus="focus", focus_revision=1, focus_updated_at=None
        )
    )

    tool = await server.get_tool("brain_project_focus_get")
    await tool.fn(project_key="brain_v42")

    service.get_by_key.assert_awaited_once_with("brain-v42")
