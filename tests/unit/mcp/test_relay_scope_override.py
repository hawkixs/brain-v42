"""Scoped credentials cannot claim the operator's relay overrides."""

from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import AuthorizationError

from brain_v42.credentials.author_binding import author_binding_counts, reset_author_binding_counts
from brain_v42.mcp import tool_families
from brain_v42.mcp.tool_families import FamilyAuthorizationMiddleware
from tests.unit.mcp.test_tool_families import _scope_context, _scoped_access


@pytest.mark.parametrize(
    "override",
    [
        {"initiator": "operator"},
        {"allow_focus_shrink": True},
        {"allow_focus_shrink": 1},
        {"allow_focus_shrink": "true"},
        {"allow_focus_shrink": "yes"},
    ],
)
@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
@pytest.mark.parametrize("projects", [None, ["brain-v42"]])
async def test_relay_override_precedes_capture(monkeypatch, override, mode, projects):
    reset_author_binding_counts()
    monkeypatch.setattr(
        tool_families, "get_access_token", lambda: _scoped_access("write", projects=projects)
    )
    resolver = AsyncMock(return_value="brain-v42")
    middleware = FamilyAuthorizationMiddleware(scope_mode=mode, row_project_resolver=resolver)
    handler = AsyncMock()
    args = {"session_id": "opaque-session", **override}
    if projects is not None and mode == "enforce":
        with pytest.raises(AuthorizationError, match="^relay_override_refused$"):
            await middleware.on_call_tool(_scope_context("brain_session_relay", args), handler)
        handler.assert_not_awaited()
    else:
        await middleware.on_call_tool(_scope_context("brain_session_relay", args), handler)
        handler.assert_awaited_once()
    assert author_binding_counts() == (
        {"brain_session_relay:scope_mismatch": 1} if projects is not None and mode != "off" else {}
    )


@pytest.mark.parametrize("override", [{"initiator": "operator"}, {"allow_focus_shrink": True}])
async def test_compact_relay_refuses_before_handler(monkeypatch, override):
    from fastmcp import Client, FastMCP

    from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile

    monkeypatch.setattr(
        tool_families, "get_access_token", lambda: _scoped_access("write", projects=["brain-v42"])
    )
    mcp = FastMCP()
    reached = []
    mcp.add_middleware(
        FamilyAuthorizationMiddleware(
            scope_mode="enforce", row_project_resolver=AsyncMock(return_value="brain-v42")
        )
    )

    @mcp.tool()
    async def brain_session_relay(
        initiator: str = "guard", allow_focus_shrink: bool = False
    ) -> str:
        reached.append(True)
        return "relayed"

    apply_tool_catalog_profile(mcp, profile="compact")
    async with Client(mcp) as client:
        result = await client.call_tool(
            "brain_call_tool",
            {"name": "brain_session_relay", "arguments": override},
            raise_on_error=False,
        )
    assert result.is_error and "relay_override_refused" in str(result.content)
    assert reached == []
