from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated, Any, Literal, cast

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from brain_v42.mcp.server import create_mcp_instance
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile


def _server() -> FastMCP:
    server = FastMCP("argument-guidance", mask_error_details=True)
    from brain_v42.mcp.argument_guidance import ArgumentGuidanceMiddleware

    server.add_middleware(ArgumentGuidanceMiddleware())
    return server


async def test_unexpected_and_missing_arguments_show_safe_expected_signature() -> None:
    server = _server()

    @server.tool()
    async def lookup_entity(entity_type: str, entity_id: str) -> str:
        return entity_type + entity_id

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(
                "lookup_entity", {"id": "sentinel-private-value", "type": "decision"}
            )

    message = str(caught.value)
    assert "unexpected 'id' (did you mean 'entity_id'?)" in message
    assert "unexpected 'type' (did you mean 'entity_type'?)" in message
    assert "missing 'entity_type', 'entity_id'" in message
    assert "Expected: lookup_entity(entity_type: string, entity_id: string)" in message
    assert "sentinel-private-value" not in message
    assert "pydantic.dev" not in message


async def test_parameterless_tool_reports_unexpected_argument_and_empty_signature() -> None:
    server = _server()

    @server.tool()
    async def brain_fact_list() -> str:
        return "ok"

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("brain_fact_list", {"foo": 1})

    assert str(caught.value) == (
        "Invalid arguments for brain_fact_list: unexpected 'foo'. Expected: brain_fact_list()"
    )


async def test_signature_renders_enum_and_optional_parameters() -> None:
    server = _server()

    @server.tool()
    async def lookup(kind: Literal["decision", "learning"], limit: int = 5) -> str:
        return kind

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("lookup", {})

    assert 'kind: "decision"|"learning"' in str(caught.value)
    assert "limit?: integer" in str(caught.value)


async def test_shape_error_reports_path_without_echoing_value() -> None:
    server = _server()

    @server.tool()
    async def collect(steps: list[dict[str, str]]) -> str:
        return "ok"

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("collect", {"steps": ["private-sentinel"]})

    assert "'steps.0': expected object, got string" in str(caught.value)
    assert "missing" not in str(caught.value)
    assert "private-sentinel" not in str(caught.value)


async def test_validation_error_from_tool_body_has_fields_without_signature() -> None:
    server = _server()

    class Payload(BaseModel):
        count: int

    @server.tool()
    async def create(payload: dict[str, object]) -> str:
        Payload.model_validate(payload)
        return "ok"

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("create", {"payload": {"count": "private-sentinel"}})

    assert "count" in str(caught.value)
    assert "missing" not in str(caught.value)
    assert "private-sentinel" not in str(caught.value)
    assert "Expected:" not in str(caught.value)


async def test_compact_gateway_reports_inner_tool_guidance() -> None:
    server = _server()

    @server.tool()
    async def lookup_entity(entity_type: str, entity_id: str) -> str:
        return entity_type + entity_id

    apply_tool_catalog_profile(server, "compact")
    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(
                "brain_call_tool",
                {
                    "name": "lookup_entity",
                    "arguments": {"id": "private-sentinel", "type": "decision"},
                },
            )

    assert "Expected: lookup_entity(entity_type: string, entity_id: string)" in str(caught.value)
    assert "private-sentinel" not in str(caught.value)


async def test_wrong_enum_lists_allowed_values_without_echoing_input() -> None:
    server = _server()

    @server.tool()
    async def lookup(kind: Literal["decision", "learning"]) -> str:
        return kind

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("lookup", {"kind": "SECRET-VALUE"})
    assert '"decision"|"learning"' in str(caught.value)
    assert "SECRET-VALUE" not in str(caught.value)


async def test_le_limit_is_rendered() -> None:
    server = _server()

    @server.tool()
    async def lookup(limit: Annotated[int, Field(le=10)]) -> str:
        return str(limit)

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("lookup", {"limit": 11})
    assert "must be <= 10" in str(caught.value)


async def test_max_length_limit_is_rendered() -> None:
    server = _server()

    @server.tool()
    async def lookup(topic: Annotated[str, Field(max_length=50)]) -> str:
        return topic

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("lookup", {"topic": "x" * 51})
    assert "at most 50 characters" in str(caught.value)


async def test_union_error_omits_branch_names() -> None:
    server = _server()

    @server.tool()
    async def lookup(tags: str | list[str]) -> str:
        return str(tags)

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("lookup", {"tags": [1]})
    message = str(caught.value)
    assert "tags" in message
    assert "list[str]" not in message
    assert "tags.str" not in message


async def test_non_identifier_unexpected_key_is_redacted() -> None:
    server = _server()

    @server.tool()
    async def lookup(value: str) -> str:
        return value

    key = "SECRET-KEY-http://x.y"
    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("lookup", {key: "secret"})
    assert "unexpected '<key>'" in str(caught.value)
    assert key not in str(caught.value)


async def test_non_validation_error_remains_masked() -> None:
    server = _server()

    @server.tool()
    async def x() -> str:
        raise RuntimeError("private-sentinel")

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("x", {})

    assert "Error calling tool 'x'" in str(caught.value)
    assert "private-sentinel" not in str(caught.value)


async def test_guidance_is_capped_for_many_optional_parameters() -> None:
    server = _server()
    parameters = ", ".join(f"optional_{index}: str = 'value'" for index in range(60))
    namespace: dict[str, object] = {}
    exec(f"async def large_signature({parameters}) -> str:\n    return 'ok'", namespace)
    tool = cast(Any, namespace["large_signature"])
    server.tool()(tool)

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("large_signature", {"unexpected": "private-sentinel"})

    assert len(str(caught.value)) <= 600
    assert str(caught.value).endswith(("…", ")"))
    assert "(+" in str(caught.value)


async def test_published_parameters_swallows_lookup_failure() -> None:
    from brain_v42.mcp.argument_guidance import ArgumentGuidanceMiddleware

    async def fail_lookup(name: str) -> None:
        raise RuntimeError("lookup unavailable")

    fake_context = SimpleNamespace(
        fastmcp_context=SimpleNamespace(fastmcp=SimpleNamespace(get_tool=fail_lookup))
    )

    assert await ArgumentGuidanceMiddleware()._published_parameters(fake_context, "x") is None


async def test_guidance_without_published_schema_omits_expected_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server()

    @server.tool()
    async def lookup(value: str) -> str:
        return value

    middleware = next(
        item
        for item in server.middleware
        if item.__class__.__name__ == "ArgumentGuidanceMiddleware"
    )

    async def no_published_parameters(context: Any, name: str) -> None:
        return None

    monkeypatch.setattr(middleware, "_published_parameters", no_published_parameters)
    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("lookup", {"unexpected": "private-sentinel"})

    assert "unexpected 'unexpected'" in str(caught.value)
    assert "Expected:" not in str(caught.value)
    assert "private-sentinel" not in str(caught.value)


def test_create_mcp_instance_installs_exactly_one_guidance_middleware() -> None:
    from brain_v42.mcp.argument_guidance import ArgumentGuidanceMiddleware

    server = create_mcp_instance()
    assert sum(isinstance(item, ArgumentGuidanceMiddleware) for item in server.middleware) == 1


def test_schema_path_unwraps_optional_nested_models_and_maps_dict_values() -> None:
    from brain_v42.mcp.argument_guidance import _schema_path

    root = {
        "properties": {
            "steps": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
            "meta": {"type": "object", "additionalProperties": {"type": "integer"}},
        }
    }
    assert _schema_path(root, ("steps", 0)) == (["steps", 0], {"type": "string"})
    assert _schema_path(root, ("meta", "count")) == (["meta", "count"], {"type": "integer"})
    assert _schema_path(root, ("steps", "unknown")) == (["steps"], None)


def test_limits_patterns_enum_and_signature_edge_rendering() -> None:
    from brain_v42.mcp.argument_guidance import _limit_message, _signature

    assert _limit_message("string_too_short", {"min_length": 74}) == "at least 74 characters"
    assert _limit_message("too_short", {"min_items": 3}) == "at least 3 items"
    assert (
        _limit_message("string_pattern_mismatch", {"pattern": "secret"})
        == "does not match the expected format"
    )
    optional_schema = {"properties": {f"optional_{i}": {"type": "string"} for i in range(6)}}
    assert _signature("tiny", optional_schema, 30) == ""
    assert _signature("tiny", optional_schema, 45) == "tiny(optional_0?: string, … (+5 more))"
    assert (
        _signature(
            "long",
            {
                "properties": {"x": {"enum": [str(i) for i in range(9)]}},
                "required": ["x"],
            },
            600,
        )
        == 'long(x: "0"|"1"|"2"|"3"|"4"|"5"|"6"|"7"|…)'
    )


def test_signature_respects_cap_for_parameterless_tool() -> None:
    from brain_v42.mcp.argument_guidance import _signature

    schema = {"properties": {}}
    assert _signature("tool_x", schema, len("tool_x()") - 1) == ""
    assert _signature("tool_x", schema, len("tool_x()")) == "tool_x()"


def test_limit_message_uses_item_units_for_list_length_errors() -> None:
    from brain_v42.mcp.argument_guidance import _limit_message

    assert _limit_message("too_short", {"min_length": 1}) == "at least 1 item"
    assert _limit_message("too_long", {"max_length": 2}) == "at most 2 items"
    assert _limit_message("string_too_short", {"min_length": 1}) == "at least 1 character"
    assert _limit_message("string_too_long", {"max_length": 2}) == "at most 2 characters"


def test_guidance_caps_large_required_signature_with_one_final_period() -> None:
    from pydantic import ValidationError

    from brain_v42.mcp.argument_guidance import _guidance

    error = ValidationError.from_exception_data(
        "Synthetic",
        [{"type": "unexpected_keyword_argument", "loc": ("unknown",), "input": 1}],
    )
    schema = {
        "properties": {f"required_{i}": {"type": "string"} for i in range(40)},
        "required": [f"required_{i}" for i in range(40)],
    }
    message = _guidance("synthetic", error, schema, {})
    assert len(message) <= 600
    assert message.endswith(".")
    assert not message.endswith("..")


def test_guidance_omits_signature_when_unknown_keys_leave_no_room() -> None:
    from pydantic import ValidationError

    from brain_v42.mcp.argument_guidance import _guidance

    fields = [
        {"type": "unexpected_keyword_argument", "loc": (f"unknown_{i:032d}",), "input": 1}
        for i in range(15)
    ]
    error = ValidationError.from_exception_data("Synthetic", fields)
    schema = {"properties": {"lit": {"type": "string"}}, "required": ["lit"]}
    message = _guidance("synthetic", error, schema, {})
    assert len(message) <= 600
    assert message.endswith("Expected: synthetic(lit: string)")
    assert "…" in message

    message = _guidance("x" * 590, error, schema, {})
    assert "Expected:" not in message
    assert len(message) <= 600


def test_argument_error_docs_describe_all_echoed_caller_names() -> None:
    from pathlib import Path

    docs = Path(__file__).resolve().parents[3] / "docs" / "MCP_TOOLS.md"
    assert (
        "caller-supplied names (parameter names and dictionary keys) are echoed only when they "
        "look like identifiers" in docs.read_text()
    )


def test_no_signature_keeps_fitting_unexpected_clauses_and_marks_dropped_ones() -> None:
    from pydantic import ValidationError

    from brain_v42.mcp.argument_guidance import _guidance

    error = ValidationError.from_exception_data(
        "x",
        [
            {"type": "unexpected_keyword_argument", "loc": (f"unknown_{i}",), "input": None}
            for i in range(30)
        ],
    )
    parameters = {
        "type": "object",
        "properties": {"optional_" + "x" * 700: {"type": "string"}},
    }
    message = _guidance("x", error, parameters, {})

    assert "unexpected 'unknown_0'" in message
    assert "unexpected 'unknown_29'" not in message
    assert message.endswith("….")
    assert len(message) <= 600
    assert "Expected:" not in message
    assert "invalid value shape" not in message
    assert ": ; " not in message


def test_guidance_caps_unexpected_clauses_with_long_tool_names() -> None:
    from pydantic import ValidationError

    from brain_v42.mcp.argument_guidance import _guidance

    error = ValidationError.from_exception_data(
        "Synthetic",
        [
            *[
                {"type": "unexpected_keyword_argument", "loc": (f"u{i:02d}",), "input": 1}
                for i in range(29)
            ],
            {"type": "missing_argument", "loc": ("a",), "input": None},
        ],
    )
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b" * 20: {"type": "integer"}},
        "required": ["a"],
    }

    for name_length in (3, 12, 21, 30, 39):
        message = _guidance("t" * name_length, error, schema, {})
        assert len(message) <= 600
        assert "…. Expected: " in message


def test_guidance_never_cuts_a_signature_when_unexpected_clauses_are_truncated() -> None:
    from pydantic import ValidationError

    from brain_v42.mcp.argument_guidance import _guidance

    error = ValidationError.from_exception_data(
        "Synthetic",
        [
            {"type": "missing_argument", "loc": ("a",), "input": None},
            *[
                {"type": "unexpected_keyword_argument", "loc": (key,), "input": None}
                for key in ("u1", "u2", "u3")
            ],
        ],
    )
    for name_length in range(1, 600):
        long_name = "p" * name_length
        schema = {
            "properties": {"a": {"type": "string"}, long_name: {"type": "string"}},
            "required": ["a", long_name],
        }
        message = _guidance("tool_x", error, schema, {})
        assert len(message) <= 600
        if " Expected: " in message:
            assert message.endswith(")")

        if name_length == 520:
            assert " Expected: " in message
            assert message.endswith(")")


def test_guidance_shortens_a_single_overlong_clause() -> None:
    from pydantic import ValidationError

    from brain_v42.mcp.argument_guidance import _guidance

    error = ValidationError.from_exception_data(
        "x",
        [{"type": "missing_argument", "loc": ("required_" + "x" * 900,), "input": None}],
    )
    message = _guidance("x", error, {"type": "object", "properties": {}}, {})

    assert len(message) <= 600
    assert "missing 'required_" in message
    assert "…" in message
