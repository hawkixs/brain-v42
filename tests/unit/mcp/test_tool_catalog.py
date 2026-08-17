"""Behavioral tests for bounded Brain MCP tool exposure."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken

from brain_v42.mcp import tool_catalog
from brain_v42.mcp.tool_catalog import SESSION_LIFECYCLE_TOOLS, apply_tool_catalog_profile
from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools


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
