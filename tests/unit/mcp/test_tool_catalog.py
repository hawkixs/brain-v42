"""Behavioral tests for bounded Brain MCP tool exposure."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken

from brain_v42.mcp import tool_catalog
from brain_v42.mcp.tool_catalog import SESSION_LIFECYCLE_TOOLS, apply_tool_catalog_profile
from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools


class Workflow(StrEnum):
    PLAN = "plan"
    BUILD = "build"


class ManyValues(StrEnum):
    VALUE_1 = "v1"
    VALUE_2 = "v2"
    VALUE_3 = "v3"
    VALUE_4 = "v4"
    VALUE_5 = "v5"
    VALUE_6 = "v6"
    VALUE_7 = "v7"
    VALUE_8 = "v8"
    VALUE_9 = "v9"


def _build_test_server() -> FastMCP:
    server = FastMCP("brain-catalog-test")

    @server.tool
    async def brain_session_start(project_key: str) -> str:
        """Start a user-controlled Brain session."""
        return f"started:{project_key}"

    @server.tool
    async def brain_search(query: str) -> str:
        """Search decisions and learnings in Brain memory."""
        return f"memory:{query}"

    return server


def _build_resolution_server() -> FastMCP:
    server = FastMCP("brain-catalog-resolution-test", mask_error_details=True)

    @server.tool
    async def brain_session_start(project_key: str) -> str:
        """Start a user-controlled Brain session."""
        return project_key

    @server.tool
    async def brain_search(query: str) -> str:
        """Search decisions and learnings in Brain memory."""
        return f"memory:{query}"

    @server.tool
    async def brain_get(entity_id: str, entity_type: str = "learning") -> str:
        """Get one typed memory entry."""
        return f"{entity_type}:{entity_id}"

    @server.tool
    async def brain_get_runbook(entity_id: str) -> str:
        """Get a runbook using its native tool."""
        return f"runbook:{entity_id}"

    @server.tool
    async def brain_focus_history(project_key: str) -> str:
        """Read project focus history."""
        return project_key

    @server.tool
    async def brain_list_projects() -> str:
        """List projects."""
        return "projects"

    return server


def _build_capability_test_server() -> FastMCP:
    server = FastMCP("brain-capability-catalog-test")

    @server.tool
    async def brain_session_start(project_key: str) -> str:
        """Start a user-controlled Brain session."""
        return f"started:{project_key}"

    @server.tool
    async def brain_search(query: str) -> str:
        """Search decisions and learnings in Brain memory."""
        return f"memory:{query}"

    @server.tool
    async def brain_list(project_key: str) -> str:
        """List Brain entities."""
        return project_key

    @server.tool
    async def brain_update(entity_id: str) -> str:
        """Update a Brain entity."""
        return entity_id

    @server.tool
    async def brain_delete(entity_id: str) -> str:
        """Delete a Brain entity."""
        return entity_id

    return server


def _catalog_access(*, principal: str, phase: str = "scan") -> AccessToken:
    if principal == "admin":
        return AccessToken(
            token="admin-super-secret",
            client_id="brain-admin",
            scopes=["brain:admin"],
            claims={"type": "admin"},
        )
    return AccessToken(
        token="profile-super-secret",
        client_id=f"dream-codex-{phase}",
        scopes=["brain:dream"],
        claims={
            "type": "scoped",
            "agent": f"dream-codex-{phase}",
            "phase": phase,
            "project_key": "brain-v42",
        },
    )


def _install_capability_middleware(server: FastMCP) -> None:
    from brain_v42.mcp import dream_capabilities

    middleware_type = getattr(dream_capabilities, "DreamCapabilityMiddleware", None)
    assert middleware_type is not None, "the Dream list firewall must be public"
    server.add_middleware(middleware_type())


async def test_native_profile_keeps_direct_tool_catalog() -> None:
    server = _build_test_server()

    configured = apply_tool_catalog_profile(server, "native")

    assert configured is server
    async with Client(configured) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"brain_session_start", "brain_search"}


async def test_compact_profile_bounds_the_visible_catalog() -> None:
    server = _build_test_server()

    configured = apply_tool_catalog_profile(server, "compact")

    assert configured is server
    async with Client(configured) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"brain_session_start", "brain_find_tool", "brain_call_tool"}


async def test_compact_profile_can_render_native_catalog_for_dream(
    monkeypatch,
) -> None:
    server = _build_test_server()
    monkeypatch.setattr(
        tool_catalog,
        "get_http_headers",
        lambda: {"x-brain-tool-profile": "native"},
        raising=False,
    )

    configured = apply_tool_catalog_profile(server, "compact")

    async with Client(configured) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"brain_session_start", "brain_search"}


async def test_compact_profile_finds_and_calls_a_hidden_tool() -> None:
    server = _build_test_server()
    configured = apply_tool_catalog_profile(server, "compact")

    async with Client(configured) as client:
        found = await client.call_tool(
            "brain_find_tool",
            {"query": "search decisions in memory"},
        )
        called = await client.call_tool(
            "brain_call_tool",
            {"name": "brain_search", "arguments": {"query": "session lifecycle"}},
        )

    assert [tool["name"] for tool in found.data] == ["brain_search"]
    assert called.data == "memory:session lifecycle"


async def test_find_tool_returns_compact_summary_with_required_and_enum_types() -> None:
    server = FastMCP("brain-catalog-summary-test")

    @server.tool
    async def brain_search(
        query: str,
        kind: Literal["decision", "learning"],
        workflow: Workflow,
        many: ManyValues,
        nullable: str | None,
    ) -> str:
        """
        Search memory entries.

        Additional detail stays out of summaries.
        """
        return query + kind + workflow + many + (nullable or "")

    async with Client(apply_tool_catalog_profile(server, "compact")) as client:
        found = await client.call_tool("brain_find_tool", {"query": "search memory"})

    assert isinstance(found.data, list)
    result = next(item for item in found.data if item["name"] == "brain_search")
    assert set(result) == {"name", "description", "required", "optional"}
    assert result["description"] == "Search memory entries."
    assert "\n" not in result["description"]
    assert result["required"] == {
        "query": "string",
        "kind": "decision|learning",
        "workflow": "plan|build",
        "many": "v1|v2|v3|v4|v5|v6|v7|v8|…",
        "nullable": "string",
    }
    assert result["optional"] == []
    assert "outputSchema" not in json.dumps(found.data)
    assert "output_schema" not in json.dumps(found.data)


async def test_find_tool_exact_name_returns_full_input_schema_only() -> None:
    server = apply_tool_catalog_profile(_build_resolution_server(), "compact")
    async with Client(server) as client:
        found = await client.call_tool("brain_find_tool", {"query": " brain_get "})
        aliased = await client.call_tool("brain_find_tool", {"query": "brain_recall"})

    assert len(found.data) == 1
    result = found.data[0]
    assert result["name"] == "brain_get"
    assert result["description"] == "Get one typed memory entry."
    assert result["input_schema"]["properties"]["entity_id"] == {"type": "string"}
    assert "output_schema" not in result
    assert aliased.data[0]["name"] == "brain_search"
    assert "input_schema" in aliased.data[0]


async def test_find_tool_exact_name_does_not_leak_family_hidden_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp import tool_families
    from brain_v42.mcp.tool_families import FamilyAuthorizationMiddleware

    server = FastMCP("brain-family-exact-catalog-test", mask_error_details=True)

    @server.tool
    async def brain_search(query: str) -> str:
        """Search memory."""
        return query

    @server.tool
    async def brain_learn(content: str) -> str:
        """Save a learning."""
        return content

    server.add_middleware(FamilyAuthorizationMiddleware())
    access = AccessToken(
        token="",
        client_id="reader",
        scopes=["read"],
        claims={"credential_id": "00000000-0000-0000-0000-000000000001", "issuers": ["actor.*"]},
    )
    monkeypatch.setattr(tool_families, "get_access_token", lambda: access)
    async with Client(apply_tool_catalog_profile(server, "compact")) as client:
        hidden = await client.call_tool("brain_find_tool", {"query": "brain_learn"})
        allowed = await client.call_tool("brain_find_tool", {"query": "brain_search"})
    assert all(item["name"] != "brain_learn" for item in hidden.data)
    assert all("input_schema" not in item for item in hidden.data)
    assert allowed.data[0]["name"] == "brain_search"
    assert "input_schema" in allowed.data[0]


async def test_find_tool_summaries_are_bounded_for_large_tool_schemas() -> None:
    server = FastMCP("brain-catalog-size-test")
    long_text = "Detailed description. " * 100
    for index in range(5):

        async def tool(required_value: str) -> dict[str, str]:
            """Placeholder."""
            return {"value": required_value}

        tool.__name__ = f"brain_large_{index}"
        tool.__doc__ = long_text
        server.tool(tool)

    async with Client(apply_tool_catalog_profile(server, "compact")) as client:
        found = await client.call_tool("brain_find_tool", {"query": "large tool"})

    assert len(found.data) == 5
    assert len(json.dumps(found.data)) < 1500


@pytest.mark.parametrize(
    ("name", "arguments", "guidance"),
    [
        ("brain_call_tool", {}, 'call brain_call_tool(name="<target tool>", arguments={...}) once'),
        ("brain_find_tool", {}, "call brain_find_tool(query=...) directly"),
    ],
)
async def test_nested_catalog_gateways_return_actionable_tool_errors(
    name: str, arguments: dict, guidance: str
) -> None:
    server = apply_tool_catalog_profile(_build_resolution_server(), "compact")
    async with Client(server) as client:
        with pytest.raises(ToolError, match="cannot be nested") as error:
            await client.call_tool("brain_call_tool", {"name": name, "arguments": arguments})
    assert guidance in str(error.value)


async def test_unknown_tool_suggests_similar_catalog_tools() -> None:
    server = apply_tool_catalog_profile(_build_resolution_server(), "compact")
    async with Client(server) as client:
        with pytest.raises(ToolError) as error:
            await client.call_tool("brain_call_tool", {"name": "brain_get_project_focus"})
    message = str(error.value)
    assert "Unknown tool 'brain_get_project_focus'." in message
    assert "brain_focus_history" in message and "brain_list_projects" in message
    assert "Use brain_find_tool(query=...) to search the catalogue." in message
    assert "Project focus:" in message


async def test_typo_prefers_strong_fuzzy_match_over_word_overlap() -> None:
    server = FastMCP("brain-catalog-typo-test", mask_error_details=True)
    for tool_name in (
        "brain_session_start",
        "brain_session_abandon",
        "brain_session_bind",
        "brain_session_capture",
        "brain_session_end",
    ):

        async def tool() -> str:
            return "tool"

        tool.__name__ = tool_name
        server.tool(tool)
    async with Client(apply_tool_catalog_profile(server, "compact")) as client:
        with pytest.raises(ToolError) as error:
            await client.call_tool("brain_call_tool", {"name": "brain_session_statr"})
    suggestions = str(error.value).split("Closest: ", 1)[1].split(".", 1)[0]
    assert suggestions.split(", ")[0] == "brain_session_start"


async def test_unknown_tool_suggestions_exclude_family_hidden_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp import tool_families
    from brain_v42.mcp.tool_families import FamilyAuthorizationMiddleware

    server = FastMCP("brain-family-catalog-test", mask_error_details=True)

    @server.tool
    async def brain_session_start(project_key: str) -> str:
        """Start a user-controlled Brain session."""
        return project_key

    @server.tool
    async def brain_search(query: str) -> str:
        """Search Brain memory."""
        return query

    server.add_middleware(FamilyAuthorizationMiddleware())
    access = AccessToken(
        token="",
        client_id="reader",
        scopes=["read"],
        claims={
            "credential_id": "00000000-0000-0000-0000-000000000001",
            "issuers": ["actor.*"],
        },
    )
    monkeypatch.setattr(tool_families, "get_access_token", lambda: access)
    # Read scope can see brain_search but not the session start write family.
    async with Client(apply_tool_catalog_profile(server, "compact")) as client:
        with pytest.raises(ToolError) as error:
            await client.call_tool("brain_call_tool", {"name": "brain_session_statr"})
    assert "Unknown tool 'brain_session_statr'." in str(error.value)
    assert "brain_session_start" not in str(error.value)


async def test_unrelated_unknown_tool_omits_closest_candidates() -> None:
    server = apply_tool_catalog_profile(_build_resolution_server(), "compact")
    async with Client(server) as client:
        with pytest.raises(ToolError) as error:
            await client.call_tool("brain_call_tool", {"name": "zzzzzzzzzzzz"})
    assert "Closest:" not in str(error.value)
    assert "Use brain_find_tool(query=...) to search the catalogue." in str(error.value)


async def test_brain_prefixed_nonsense_omits_closest_candidates() -> None:
    server = FastMCP("brain-nonsense-catalog-test")

    @server.tool
    async def brain_session_start(project_key: str) -> str:
        """Start a session."""
        return project_key

    @server.tool
    async def brain_search(query: str) -> str:
        """Search memory."""
        return query

    async with Client(apply_tool_catalog_profile(server, "compact")) as client:
        with pytest.raises(ToolError) as error:
            await client.call_tool("brain_call_tool", {"name": "brain_qwzx"})
    assert "Closest:" not in str(error.value)


async def test_plural_project_query_suggests_focus_history() -> None:
    server = _build_resolution_server()

    @server.tool
    async def brain_update_project_focus(project_key: str) -> str:
        """Update project focus."""
        return project_key

    @server.tool
    async def brain_list_project_groups() -> str:
        """List project groups."""
        return "groups"

    async with Client(apply_tool_catalog_profile(server, "compact")) as client:
        with pytest.raises(ToolError) as error:
            await client.call_tool("brain_call_tool", {"name": "brain_get_project_focus"})
    assert "brain_focus_history" in str(error.value)


async def test_alias_to_family_hidden_tool_is_treated_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp import tool_families
    from brain_v42.mcp.tool_families import FamilyAuthorizationMiddleware

    server = _build_resolution_server()
    server.add_middleware(FamilyAuthorizationMiddleware())
    access = AccessToken(
        token="",
        client_id="writer",
        scopes=["write"],
        claims={
            "credential_id": "00000000-0000-0000-0000-000000000001",
            "issuers": ["actor.*"],
        },
    )
    monkeypatch.setattr(tool_families, "get_access_token", lambda: access)
    async with Client(apply_tool_catalog_profile(server, "compact")) as client:
        with pytest.raises(ToolError, match="Unknown tool 'brain_recall'"):
            await client.call_tool(
                "brain_call_tool", {"name": "brain_recall", "arguments": {"query": "x"}}
            )


async def test_recall_alias_dispatches() -> None:
    server = apply_tool_catalog_profile(_build_resolution_server(), "compact")
    async with Client(server) as client:
        recalled = await client.call_tool(
            "brain_call_tool", {"name": "brain_recall", "arguments": {"query": "x"}}
        )
    assert recalled.data == "memory:x"


async def test_typed_getter_alias_dispatches() -> None:
    server = apply_tool_catalog_profile(_build_resolution_server(), "compact")
    async with Client(server) as client:
        typed = await client.call_tool(
            "brain_call_tool", {"name": "brain_get_decision", "arguments": {"entity_id": "id"}}
        )
    assert typed.data == "decision:id"


async def test_real_tool_name_takes_precedence_over_alias() -> None:
    server = apply_tool_catalog_profile(_build_resolution_server(), "compact")
    async with Client(server) as client:
        real = await client.call_tool(
            "brain_call_tool", {"name": "brain_get_runbook", "arguments": {"entity_id": "id"}}
        )
    assert real.data == "runbook:id"


async def test_typed_alias_refuses_conflicting_entity_type() -> None:
    server = apply_tool_catalog_profile(_build_resolution_server(), "compact")
    async with Client(server) as client:
        with pytest.raises(ToolError, match="conflicts"):
            await client.call_tool(
                "brain_call_tool",
                {
                    "name": "brain_get_decision",
                    "arguments": {"entity_id": "id", "entity_type": "learning"},
                },
            )


async def test_call_gateway_dump_matches_base_transform() -> None:
    from fastmcp.server.transforms.search import BM25SearchTransform

    configured = apply_tool_catalog_profile(_build_resolution_server(), "compact")
    base_tool = BM25SearchTransform(
        max_results=5,
        always_visible=list(SESSION_LIFECYCLE_TOOLS),
        search_tool_name="brain_find_tool",
        call_tool_name="brain_call_tool",
    )._make_call_tool()
    async with Client(configured) as client:
        gateway = next(tool for tool in await client.list_tools() if tool.name == "brain_call_tool")
    assert gateway.model_dump(
        by_alias=True, exclude_none=True
    ) == base_tool.to_mcp_tool().model_dump(by_alias=True, exclude_none=True)


async def test_search_gateway_documents_exact_name_schema_lookup() -> None:
    from fastmcp.server.transforms.search import BM25SearchTransform

    configured = apply_tool_catalog_profile(_build_resolution_server(), "compact")
    base_tool = BM25SearchTransform(
        max_results=5,
        always_visible=list(SESSION_LIFECYCLE_TOOLS),
        search_tool_name="brain_find_tool",
        call_tool_name="brain_call_tool",
    )._make_search_tool()
    async with Client(configured) as client:
        gateway = next(tool for tool in await client.list_tools() if tool.name == "brain_find_tool")
    base_gateway = base_tool.to_mcp_tool()
    assert gateway.inputSchema == base_gateway.inputSchema
    assert gateway.outputSchema == base_gateway.outputSchema
    assert gateway.description == (
        "Search for tools using natural language.\n\n"
        "Returns up to 5 matching tools ranked by relevance, each as a summary: name, one-line "
        "description, required parameters with types, optional parameter names. Pass an exact "
        "tool name to get that tool's full input schema."
    )


async def test_compact_profile_keeps_all_registered_lifecycle_tools_visible() -> None:
    server = FastMCP("brain-complete-lifecycle-catalog")
    register_session_lifecycle_tools(server, MagicMock(), AsyncMock())

    @server.tool
    async def brain_search(query: str) -> str:
        """Hidden non-lifecycle tool used to prove the catalog is bounded."""
        return query

    configured = apply_tool_catalog_profile(server, "compact")

    async with Client(configured) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert names == {
        *SESSION_LIFECYCLE_TOOLS,
        "brain_find_tool",
        "brain_call_tool",
    }


def test_all_user_controlled_session_commands_are_pinned() -> None:
    assert SESSION_LIFECYCLE_TOOLS == (
        "brain_session_start",
        "brain_session_capture",
        "brain_session_heartbeat",
        "brain_session_end",
        "brain_session_list",
        "brain_session_resume",
        "brain_session_abandon",
        "brain_session_bind",
        "brain_session_relay",
    )


@pytest.mark.parametrize("profile", ["compact", "native"])
@pytest.mark.parametrize(
    ("headers", "case"),
    [
        ({}, "absent"),
        ({"x-brain-tool-profile": "compact"}, "compact"),
        ({"x-brain-tool-profile": "native"}, "native"),
        (
            {
                "x-brain-tool-profile": "forged-native",
                "x-brain-agent": "dream-codex-clean",
            },
            "forged",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
async def test_scoped_principal_always_receives_exact_native_phase_catalog(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    headers: dict[str, str],
    case: str,
) -> None:
    del case
    from brain_v42.mcp import dream_capabilities

    server = _build_capability_test_server()
    access = _catalog_access(principal="scoped", phase="scan")
    monkeypatch.setattr(tool_catalog, "get_http_headers", lambda: headers, raising=False)
    monkeypatch.setattr(tool_catalog, "get_access_token", lambda: access, raising=False)
    monkeypatch.setattr(dream_capabilities, "get_access_token", lambda: access, raising=False)
    _install_capability_middleware(server)

    configured = apply_tool_catalog_profile(server, profile)  # type: ignore[arg-type]

    assert configured is server
    async with Client(configured) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"brain_search", "brain_list"}


@pytest.mark.parametrize(
    ("profile", "header", "expected"),
    [
        (
            "compact",
            {},
            {"brain_session_start", "brain_find_tool", "brain_call_tool"},
        ),
        (
            "compact",
            {"x-brain-tool-profile": "native"},
            {
                "brain_session_start",
                "brain_search",
                "brain_list",
                "brain_update",
                "brain_delete",
            },
        ),
        (
            "compact",
            {"x-brain-tool-profile": "forged-native"},
            {"brain_session_start", "brain_find_tool", "brain_call_tool"},
        ),
        (
            "native",
            {"x-brain-tool-profile": "compact"},
            {
                "brain_session_start",
                "brain_search",
                "brain_list",
                "brain_update",
                "brain_delete",
            },
        ),
    ],
)
async def test_admin_principal_preserves_historical_catalog_profiles(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    header: dict[str, str],
    expected: set[str],
) -> None:
    from brain_v42.mcp import dream_capabilities

    server = _build_capability_test_server()
    access = _catalog_access(principal="admin")
    monkeypatch.setattr(tool_catalog, "get_http_headers", lambda: header, raising=False)
    monkeypatch.setattr(tool_catalog, "get_access_token", lambda: access, raising=False)
    monkeypatch.setattr(dream_capabilities, "get_access_token", lambda: access, raising=False)
    _install_capability_middleware(server)

    apply_tool_catalog_profile(server, profile)  # type: ignore[arg-type]

    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == expected


async def test_scoped_list_middleware_filters_a_bypassed_transform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp import dream_capabilities

    server = _build_capability_test_server()
    access = _catalog_access(principal="scoped", phase="scan")
    monkeypatch.setattr(dream_capabilities, "get_access_token", lambda: access, raising=False)
    _install_capability_middleware(server)

    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert names == {"brain_search", "brain_list"}


async def test_invalid_scoped_claims_receive_no_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp import dream_capabilities

    server = _build_capability_test_server()
    invalid_access = AccessToken(
        token="profile-super-secret",
        client_id="dream-codex-scan",
        scopes=["brain:dream"],
        claims={"type": "scoped", "phase": "scan"},
    )
    monkeypatch.setattr(
        dream_capabilities, "get_access_token", lambda: invalid_access, raising=False
    )
    _install_capability_middleware(server)

    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert names == set()


@pytest.mark.parametrize("mode", ["disabled-http", "stdio", "admin-http"])
async def test_project_authorization_preserves_exact_public_input_schemas(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    server = _build_capability_test_server()
    if mode == "admin-http":
        from brain_v42.mcp import dream_capabilities

        access = _catalog_access(principal="admin")
        monkeypatch.setattr(
            dream_capabilities,
            "get_access_token",
            lambda: access,
            raising=False,
        )
        _install_capability_middleware(server)

    async with Client(server) as client:
        tools = await client.list_tools()

    schemas = {tool.name: tool.inputSchema for tool in tools}
    assert schemas == {
        "brain_session_start": {
            "additionalProperties": False,
            "properties": {"project_key": {"type": "string"}},
            "required": ["project_key"],
            "type": "object",
        },
        "brain_search": {
            "additionalProperties": False,
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "type": "object",
        },
        "brain_list": {
            "additionalProperties": False,
            "properties": {"project_key": {"type": "string"}},
            "required": ["project_key"],
            "type": "object",
        },
        "brain_update": {
            "additionalProperties": False,
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "type": "object",
        },
        "brain_delete": {
            "additionalProperties": False,
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "type": "object",
        },
    }
