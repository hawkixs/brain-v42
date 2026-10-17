"""Persisted ownership is checked on direct and compact dispatch alike."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP

from brain_v42.credentials.author_binding import author_binding_counts, reset_author_binding_counts
from brain_v42.mcp import tool_families
from brain_v42.mcp.project_scope import ROW_SOURCES, SCOPE_EXEMPT, scope_decision
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from brain_v42.mcp.tool_families import FamilyAuthorizationMiddleware
from tests.unit.mcp.test_project_scope import principal
from tests.unit.mcp.test_tool_families import _scoped_access


def arguments_for(tool: str) -> dict:
    _, argument = ROW_SOURCES[tool]
    arguments = {argument: str(uuid4())}
    if tool in {"brain_update", "brain_refresh_entity"}:
        arguments["entity_type"] = "learning"
    if tool == "brain_supersede_decision":
        arguments["project_key"] = "brain-v42"
    return arguments


@pytest.mark.parametrize("tool", sorted(ROW_SOURCES))
@pytest.mark.parametrize("project", ["brain-v42", "outside", None])
@pytest.mark.parametrize("mode", ["enforce", "observe", "off"])
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
async def test_row_ownership_on_both_dispatch_paths(
    monkeypatch, tool, project, mode, compact, scoped
):
    reset_author_binding_counts()
    monkeypatch.setattr(
        tool_families,
        "get_access_token",
        lambda: _scoped_access("write", projects=["brain-v42"] if scoped else None),
    )
    resolver = AsyncMock(return_value=project)
    reached = []
    mcp = FastMCP()
    mcp.add_middleware(
        FamilyAuthorizationMiddleware(scope_mode=mode, row_project_resolver=resolver)
    )

    async def handler(**kwargs):
        from brain_v42.mcp.project_scope import KNOWLEDGE_ROW_TOOLS
        from brain_v42.provenance import get_checked_claim_source, get_checked_project

        expected = "brain-v42" if scoped and mode == "enforce" and project == "brain-v42" else None
        assert get_checked_project() == (expected if tool in KNOWLEDGE_ROW_TOOLS else None)
        assert get_checked_claim_source() == (expected if tool == "brain_claim_verify" else None)
        reached.append(True)
        return "written"

    # Preserve arguments in the protocol schema rather than relying on **kwargs parsing.
    import inspect

    arguments = arguments_for(tool)
    handler.__signature__ = inspect.Signature(
        [inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY) for key in arguments]
    )
    mcp.tool(handler, name=tool)
    if compact:
        apply_tool_catalog_profile(mcp, profile="compact")
    async with Client(mcp) as client:
        result = await client.call_tool(
            "brain_call_tool" if compact else tool,
            {"name": tool, "arguments": arguments} if compact else arguments,
            raise_on_error=False,
        )
    denied = scoped and mode == "enforce" and project != "brain-v42"
    assert bool(reached) is not denied
    if denied:
        assert "scope_denied" in str(result.content)
    if mode == "off" or not scoped:
        resolver.assert_not_awaited()
    else:
        assert resolver.await_count == (2 if tool == "brain_claim_verify" else 1)
    cause = "scope_unresolved" if project is None else "scope_mismatch"
    assert author_binding_counts() == (
        {f"{tool}:{cause}": 1} if scoped and mode != "off" and project != "brain-v42" else {}
    )


@pytest.mark.parametrize("tool", sorted(ROW_SOURCES))
async def test_unscoped_never_resolves_rows(monkeypatch, tool):
    from tests.unit.mcp.test_tool_families import _scope_context

    monkeypatch.setattr(
        tool_families, "get_access_token", lambda: _scoped_access("write", projects=None)
    )
    resolver = AsyncMock(side_effect=AssertionError("unscoped lookup"))
    handler = AsyncMock()
    await FamilyAuthorizationMiddleware(
        scope_mode="enforce", row_project_resolver=resolver
    ).on_call_tool(_scope_context(tool, arguments_for(tool)), handler)
    resolver.assert_not_awaited()
    handler.assert_awaited_once()


@pytest.mark.parametrize("entity_type", ["decision", "learning", "snippet", "runbook", "adr"])
def test_update_checks_move_target(entity_type):
    args = {"entity_type": entity_type, "fields": {"project_key": "outside"}}
    assert (
        scope_decision("brain_update", args, principal(), None, ["brain-v42"]).reason
        == "scope_denied"
    )
    args["fields"]["project_key"] = "allowed"
    assert (
        scope_decision(
            "brain_update", args, principal(("brain-v42", "allowed")), None, ["brain-v42"]
        )
        is None
    )


def test_supersede_omitted_project_is_null_not_inherited():
    decision = scope_decision("brain_supersede_decision", {}, principal(), None, ["brain-v42"])
    assert decision.reason == "null_project_refused"


def test_delivery_reads_remain_exempt():
    assert {
        "brain_delivery_get",
        "brain_delivery_list",
        "brain_delivery_attestation_list",
    } <= SCOPE_EXEMPT


@pytest.mark.parametrize("tool", sorted(ROW_SOURCES))
def test_absent_and_null_row_are_uniform(tool):
    for rows in (None, [None], ["outside"]):
        decision = scope_decision(tool, arguments_for(tool), principal(), None, rows)
        assert decision.reason == "scope_denied"


@pytest.mark.parametrize("tool", ["brain_update", "brain_refresh_entity"])
@pytest.mark.parametrize(
    "entity_type", ["decision", "learning", "snippet", "runbook", "adr", "plan"]
)
@pytest.mark.parametrize("owner", ["brain-v42", "outside"])
async def test_dynamic_type_is_resolved_before_dispatch(monkeypatch, tool, entity_type, owner):
    from fastmcp.exceptions import AuthorizationError

    from tests.unit.mcp.test_tool_families import _scope_context

    if entity_type == "plan" and tool == "brain_update":
        return  # The update handler's schema deliberately excludes immutable plans.
    monkeypatch.setattr(
        tool_families, "get_access_token", lambda: _scoped_access("write", projects=["brain-v42"])
    )
    resolver = AsyncMock(return_value=owner)
    arguments = {"entity_type": entity_type, "entity_id": str(uuid4())}
    middleware = FamilyAuthorizationMiddleware(scope_mode="enforce", row_project_resolver=resolver)
    handler = AsyncMock()
    if owner == "outside":
        with pytest.raises(AuthorizationError, match="^scope_denied$"):
            await middleware.on_call_tool(_scope_context(tool, arguments), handler)
        handler.assert_not_awaited()
    else:
        await middleware.on_call_tool(_scope_context(tool, arguments), handler)
        handler.assert_awaited_once()
    resolver.assert_awaited_once_with(entity_type, arguments["entity_id"])


@pytest.mark.parametrize(
    "row,source", [("brain-v42", "outside"), ("outside", "brain-v42"), ("brain-v42", None)]
)
def test_claim_checks_its_project_and_source_independently(row, source):
    assert (
        scope_decision("brain_claim_verify", {}, principal(), None, [row, source]).reason
        == "scope_denied"
    )


def test_claim_requires_both_resolutions():
    decision = scope_decision("brain_claim_verify", {}, principal(), None, ["brain-v42"])
    assert decision.reason == "scope_denied"


@pytest.mark.parametrize("old,new", [("brain-v42", "outside"), ("outside", "brain-v42")])
def test_supersede_checks_old_and_new_independently(old, new):
    assert (
        scope_decision(
            "brain_supersede_decision", {"project_key": new}, principal(), None, [old]
        ).reason
        == "scope_denied"
    )


@pytest.mark.parametrize("tool", ["brain_update", "brain_refresh_entity"])
@pytest.mark.parametrize("entity_type", ["session", "slot", "claim", "unknown", None, []])
@pytest.mark.parametrize("mode", ["enforce", "observe"])
async def test_non_entity_kinds_never_reach_the_row_resolver(monkeypatch, tool, entity_type, mode):
    from fastmcp.exceptions import AuthorizationError

    from tests.unit.mcp.test_tool_families import _scope_context

    monkeypatch.setattr(
        tool_families, "get_access_token", lambda: _scoped_access("write", projects=["brain-v42"])
    )
    resolver = AsyncMock(return_value="brain-v42")
    handler = AsyncMock()
    middleware = FamilyAuthorizationMiddleware(scope_mode=mode, row_project_resolver=resolver)
    context = _scope_context(tool, {"entity_type": entity_type, "entity_id": str(uuid4())})
    if mode == "enforce":
        with pytest.raises(AuthorizationError, match="^scope_denied$"):
            await middleware.on_call_tool(context, handler)
        handler.assert_not_awaited()
    else:
        await middleware.on_call_tool(context, handler)
        handler.assert_awaited_once()
    resolver.assert_not_awaited()
