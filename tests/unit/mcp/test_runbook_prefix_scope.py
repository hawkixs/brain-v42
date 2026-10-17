"""Authorization resolves the same short ids as the runbook handler."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastmcp import Client, FastMCP

from brain_v42.mcp import tool_families
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from brain_v42.mcp.tool_families import FamilyAuthorizationMiddleware
from brain_v42.mcp.tools.runbook_tools import register_runbook_tools
from tests.unit.mcp.test_tool_families import _scoped_access
from tests.unit.mcp.tools.test_runbook_tools import make_runbook
from tests.unit.repositories.test_pg_scope_lookup import lookup_fixture


@pytest.mark.parametrize("mode,scoped", [("enforce", True), ("off", True), ("enforce", False)])
@pytest.mark.parametrize("outcome", ["allowed", "foreign", "missing", "ambiguous"])
@pytest.mark.parametrize("compact", [False, True])
async def test_runbook_prefix_scope_and_legacy_errors(monkeypatch, mode, scoped, outcome, compact):
    uid = UUID("a1b2c3d4-0000-4000-8000-000000000000")
    matches = [] if outcome == "missing" else [uid, uuid4()] if outcome == "ambiguous" else [uid]
    lookup, _ = lookup_fixture(
        MagicMock(scalars=lambda: MagicMock(all=lambda: matches)),
        MagicMock(one_or_none=lambda: ("outside" if outcome == "foreign" else "brain-v42",)),
    )

    async def resolve(kind, entity_id):
        return await lookup(kind, entity_id)

    resolver = AsyncMock(side_effect=resolve)
    monkeypatch.setattr(
        tool_families,
        "get_access_token",
        lambda: _scoped_access("write", projects=["brain-v42"] if scoped else None),
    )
    mcp = FastMCP(mask_error_details=True)
    mcp.add_middleware(
        FamilyAuthorizationMiddleware(scope_mode=mode, row_project_resolver=resolver)
    )
    service = MagicMock(
        resolve_id_prefix=AsyncMock(return_value=matches),
        record_execution=AsyncMock(return_value=make_runbook(id=uid)),
    )
    access = MagicMock()
    register_runbook_tools(mcp, service, access_logger=access)
    if compact:
        apply_tool_catalog_profile(mcp, profile="compact")
    args = {"runbook_id": uid.hex[:8]}
    async with Client(mcp) as client:
        result = await client.call_tool(
            "brain_call_tool" if compact else "brain_execute_runbook",
            {"name": "brain_execute_runbook", "arguments": args} if compact else args,
            raise_on_error=False,
        )
    denied = scoped and mode == "enforce" and outcome != "allowed"
    if denied:
        assert result.is_error and "scope_denied" in str(result.content)
        service.record_execution.assert_not_awaited()
        service.resolve_id_prefix.assert_not_awaited()
        access.log_access.assert_not_called()
    elif outcome in {"missing", "ambiguous"}:
        expected = (
            "No runbook found for id prefix" if outcome == "missing" else "Ambiguous id prefix"
        )
        assert expected in str(result.content)
        service.record_execution.assert_not_awaited()
    else:
        assert "Runbook executed" in str(result.content)
        service.record_execution.assert_awaited_once_with(uid, "success")
    if not scoped or mode == "off":
        resolver.assert_not_awaited()
