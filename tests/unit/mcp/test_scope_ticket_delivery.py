"""The writer's declared project is independent of a cross-project destination."""

from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import AuthorizationError

from brain_v42.mcp import tool_families
from brain_v42.mcp.project_scope import SCOPE_ARGUMENTS
from brain_v42.mcp.tool_families import FamilyAuthorizationMiddleware
from tests.unit.mcp.test_tool_families import _scope_context, _scoped_access


@pytest.mark.parametrize(
    "tool",
    [
        "brain_session_start",
        "brain_ticket_create",
        "brain_ticket_reply",
        "brain_ticket_transition",
        "brain_ticket_plan",
        *[name for name in SCOPE_ARGUMENTS if name.startswith("brain_delivery_")],
    ],
)
@pytest.mark.parametrize(
    "actor,target", [("brain-v42", "brain-v42"), ("brain-v42", "outside"), ("outside", "brain-v42")]
)
async def test_actor_and_declared_writer_project_are_independent(monkeypatch, tool, actor, target):
    monkeypatch.setattr(
        tool_families,
        "get_access_token",
        lambda: _scoped_access("write", "delivery", projects=["brain-v42"]),
    )
    monkeypatch.setattr(tool_families, "get_current_actor_project", lambda: actor)
    args = {SCOPE_ARGUMENTS[tool][0]: target, "to_project": "outside"}
    handler = AsyncMock()
    middleware = FamilyAuthorizationMiddleware(scope_mode="enforce")
    if actor == "outside" or target == "outside":
        with pytest.raises(AuthorizationError, match="^scope_denied$"):
            await middleware.on_call_tool(_scope_context(tool, args), handler)
        handler.assert_not_awaited()
    else:
        await middleware.on_call_tool(_scope_context(tool, args), handler)
        handler.assert_awaited_once()


@pytest.mark.parametrize("actor", ["brain-v42", "outside"])
async def test_gate_report_checks_actor_not_named_gate_owner(monkeypatch, actor):
    monkeypatch.setattr(
        tool_families, "get_access_token", lambda: _scoped_access("write", projects=["brain-v42"])
    )
    monkeypatch.setattr(tool_families, "get_current_actor_project", lambda: actor)
    middleware = FamilyAuthorizationMiddleware(scope_mode="enforce")
    handler = AsyncMock()
    context = _scope_context(
        "brain_programme_gate_report", {"owner": "outside", "coordinator": "outside"}
    )
    if actor == "outside":
        with pytest.raises(AuthorizationError, match="^scope_denied$"):
            await middleware.on_call_tool(context, handler)
        handler.assert_not_awaited()
    else:
        await middleware.on_call_tool(context, handler)
        handler.assert_awaited_once()


@pytest.mark.parametrize(
    "tool", ["brain_ticket_reply", "brain_ticket_transition", "brain_ticket_plan"]
)
@pytest.mark.parametrize("compact", [False, True])
async def test_project_issuer_cannot_override_ticket_author_scope(tool, compact):
    from brain_v42.mcp import server
    from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
    from tests.unit.mcp.test_credentials_http_wiring import settings
    from tests.unit.mcp.test_project_scope_clients import _http_call, _registry

    mcp = server.create_mcp_instance()
    reached = []

    @mcp.tool(name=tool)
    async def writer(author_project: str) -> str:
        reached.append(author_project)
        return "written"

    if compact:
        apply_tool_catalog_profile(mcp, profile="compact")
    config = settings()
    config.brain_credential_project_scope_mode = "enforce"
    arguments = {"author_project": "brain-v42"}
    result = await _http_call(
        mcp,
        config,
        "brain_call_tool" if compact else tool,
        {"name": tool, "arguments": arguments} if compact else arguments,
        registry=await _registry("scoped-ticket", ["write"], ["@project"], projects=["brain-v42"]),
        agent="outside",
    )
    assert result["isError"] and "scope_denied" in str(result["content"])
    assert reached == []
