from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from brain_v42.mcp.argument_guidance import ArgumentGuidanceMiddleware
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile


def _server() -> FastMCP:
    server = FastMCP("parameter-aliases", mask_error_details=True)
    server.add_middleware(ArgumentGuidanceMiddleware())
    return server


async def test_aliases_are_normalized_for_real_tool() -> None:
    server = _server()
    seen: dict[str, Any] = {}

    @server.tool()
    async def brain_get(entity_type: str, entity_id: str) -> str:
        seen.update(entity_type=entity_type, entity_id=entity_id)
        return "ok"

    async with Client(server) as client:
        await client.call_tool("brain_get", {"id": "123", "type": "decision"})
    assert seen == {"entity_type": "decision", "entity_id": "123"}


async def test_alias_and_canonical_are_ambiguous_without_echoing_values() -> None:
    server = _server()

    @server.tool()
    async def brain_get(entity_type: str, entity_id: str) -> str:
        return "ok"

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(
                "brain_get", {"id": "PRIVATE", "entity_id": "OTHER", "type": "decision"}
            )
    assert "Ambiguous arguments for brain_get: 'id' and 'entity_id' name the same parameter" in str(
        caught.value
    )
    assert "PRIVATE" not in str(caught.value) and "OTHER" not in str(caught.value)


async def test_two_aliases_for_one_canonical_are_ambiguous() -> None:
    server = _server()

    @server.tool()
    async def brain_save_snippet(intention: str) -> str:
        return intention

    async with Client(server) as client:
        with pytest.raises(ToolError, match="Ambiguous arguments for brain_save_snippet") as caught:
            await client.call_tool("brain_save_snippet", {"description": "one", "purpose": "two"})
    message = str(caught.value)
    assert "'description'" in message and "'purpose'" in message
    assert "pass only 'intention'" in message


async def test_alias_works_through_compact_gateway() -> None:
    server = _server()
    seen: dict[str, Any] = {}

    @server.tool()
    async def brain_get(entity_type: str, entity_id: str) -> str:
        seen.update(entity_type=entity_type, entity_id=entity_id)
        return "ok"

    apply_tool_catalog_profile(server, "compact")
    async with Client(server) as client:
        await client.call_tool(
            "brain_call_tool", {"name": "brain_get", "arguments": {"id": "123", "type": "decision"}}
        )
    assert seen == {"entity_type": "decision", "entity_id": "123"}


async def test_alias_is_scoped_to_its_tool() -> None:
    server = _server()

    @server.tool()
    async def another(entity_id: str) -> str:
        return entity_id

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("another", {"id": "literal"})
    assert "unexpected 'id'" in str(caught.value)
    assert "missing 'entity_id'" in str(caught.value)


async def test_real_catalogue_parameters_and_aliases_are_declared() -> None:
    from brain_v42.mcp.parameter_vocabulary import PARAMETER_ALIASES, VOCABULARY
    from tests.unit.mcp.test_tool_input_bounds import _registered_knowledge_server

    # This catalogue walk covers the register functions listed in its fixture.
    tools = {tool.name: tool for tool in await _registered_knowledge_server().list_tools()}
    for name, tool in tools.items():
        assert set(tool.parameters.get("properties", {})) <= VOCABULARY, name
    for name, aliases in PARAMETER_ALIASES.items():
        assert name in tools
        parameters = set(tools[name].parameters.get("properties", {}))
        for alias, canonical in aliases.items():
            assert canonical in parameters, (name, canonical)
            assert alias not in parameters, (name, alias)


async def test_guidance_trims_unexpected_clause_before_required_details() -> None:
    from pydantic import ValidationError

    from brain_v42.mcp.argument_guidance import _guidance

    error = ValidationError.from_exception_data(
        "x",
        [
            {"type": "unexpected_keyword_argument", "loc": (f"unknown_{i}",), "input": None}
            for i in range(50)
        ]
        + [{"type": "missing_argument", "loc": ("needed",), "input": None}],
    )
    message = _guidance(
        "x",
        error,
        {"type": "object", "properties": {"needed": {"type": "string"}}, "required": ["needed"]},
        {},
    )
    assert "missing 'needed'" in message
    assert "…" in message


def test_signature_squeezed_has_no_empty_separator() -> None:
    from brain_v42.mcp.argument_guidance import _signature

    result = _signature(
        "many",
        {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
        20,
    )
    assert ", ," not in result
    assert result == ""
