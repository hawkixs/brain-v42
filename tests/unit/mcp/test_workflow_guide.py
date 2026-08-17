"""Contract tests for the bounded, read-only workflow guidance MCP tool."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP

from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from brain_v42.mcp.tools.workflow_guide_tools import (
    CATALOG_REVISION,
    GUIDE_VERSION,
    WORKFLOW_CATALOG,
    format_workflow_guidance_briefing,
    register_workflow_guide_tools,
)


async def _guide(
    server: FastMCP,
    **arguments: object,
) -> dict[str, Any]:
    tool = await server.get_tool("brain_workflow_guide")
    assert tool is not None
    result = await tool.run(arguments)
    return cast(dict[str, Any], result.structured_content)


@pytest.fixture
def server() -> FastMCP:
    instance = FastMCP("workflow-guide-contract")
    register_workflow_guide_tools(instance)
    return instance


@pytest.mark.parametrize(
    ("workflow", "tool_name"),
    [
        ("session.lifecycle", "brain_session_start"),
        ("project.context", "brain_set_project_context"),
        ("knowledge.decision", "brain_log_decision"),
        ("runbook.lifecycle", "brain_create_runbook"),
        ("ticket.lifecycle", "brain_ticket_create"),
    ],
)
async def test_every_initial_workflow_family_returns_a_bounded_prepare_guide(
    server: FastMCP,
    workflow: str,
    tool_name: str,
) -> None:
    """A removed family or an unbounded catalog response must fail this test."""
    guide = await _guide(server, project_key="brain-v42", workflow=workflow)

    assert guide["status"] == "unknown"
    assert guide["refresh_required"] is True
    assert guide["tool_name"] == tool_name
    assert guide["next_action"].count("\n") == 0
    assert 1 <= len(guide["prerequisites"]) <= 5
    assert 1 <= len(guide["fields_to_prepare"]) <= 12
    assert len(guide["success_criteria"]) == 3
    assert 1 <= len(guide["recovery"]) <= 3
    assert set(guide) == {
        "guide_version",
        "catalog_revision",
        "status",
        "refresh_required",
        "refresh_reason",
        "refresh_action",
        "objective",
        "prerequisites",
        "next_action",
        "tool_name",
        "fields_to_prepare",
        "success_criteria",
        "recovery",
    }


async def test_matching_known_versions_mark_the_guide_current(server: FastMCP) -> None:
    """A matching client version must not spuriously require a refresh."""
    guide = await _guide(
        server,
        project_key="brain-v42",
        workflow="runbook.lifecycle",
        known_guide_version=GUIDE_VERSION,
        known_catalog_revision=CATALOG_REVISION,
    )

    assert guide["status"] == "current"
    assert guide["refresh_required"] is False
    assert guide["refresh_reason"] == "versions_match"


async def test_current_guide_carries_no_refresh_action(server: FastMCP) -> None:
    """A guide needing no refresh must not tell the client to reload it.

    refresh_action was built unconditionally, so a client that reads it without
    first checking refresh_required would reload a guide that is already
    current — forever. The key stays present so the response shape is stable
    for typed clients; only the value becomes honest.
    """
    guide = await _guide(
        server,
        project_key="brain-v42",
        workflow="runbook.lifecycle",
        known_guide_version=GUIDE_VERSION,
        known_catalog_revision=CATALOG_REVISION,
    )

    assert guide["refresh_required"] is False
    assert "refresh_action" in guide
    assert guide["refresh_action"] is None


async def test_old_client_version_requires_an_explicit_refresh(server: FastMCP) -> None:
    """Changing a returned guide version to an older one must flag stale guidance."""
    guide = await _guide(
        server,
        project_key="brain-v42",
        workflow="runbook.lifecycle",
        known_guide_version="0.9",
        known_catalog_revision=CATALOG_REVISION,
    )

    assert guide["status"] == "outdated"
    assert guide["refresh_required"] is True
    assert guide["refresh_reason"] == "guide_version_mismatch"
    assert "brain_workflow_guide" in guide["refresh_action"]
    assert "reload" in guide["refresh_action"].lower()


@pytest.mark.parametrize(
    ("known_guide_version", "known_catalog_revision", "status", "refresh_required"),
    [
        (None, None, "unknown", True),
        (GUIDE_VERSION, None, "unknown", True),
        (None, CATALOG_REVISION, "unknown", True),
        (GUIDE_VERSION, CATALOG_REVISION, "current", False),
        ("0.9", None, "outdated", True),
        (None, "2026-01-01.1", "outdated", True),
        ("0.9", CATALOG_REVISION, "outdated", True),
        (GUIDE_VERSION, "2026-01-01.1", "outdated", True),
    ],
)
async def test_freshness_requires_complete_matching_version_evidence(
    server: FastMCP,
    known_guide_version: str | None,
    known_catalog_revision: str | None,
    status: str,
    refresh_required: bool,
) -> None:
    """Partial client evidence must never establish freshness."""
    guide = await _guide(
        server,
        project_key="brain-v42",
        workflow="runbook.lifecycle",
        known_guide_version=known_guide_version,
        known_catalog_revision=known_catalog_revision,
    )

    assert guide["status"] == status
    assert guide["refresh_required"] is refresh_required


async def test_error_context_selects_bounded_recovery_without_echoing_it(server: FastMCP) -> None:
    """Raw validation errors must not be reflected into the guide response."""
    raw_error = "title missing; internal request id=do-not-echo"
    guide = await _guide(
        server,
        project_key="brain-v42",
        workflow="runbook.lifecycle",
        phase="recover",
        error_context=raw_error,
    )

    assert (
        guide["next_action"]
        == "Correct the required runbook fields, then call brain_create_runbook once."
    )
    assert guide["tool_name"] == "brain_create_runbook"
    assert len(guide["recovery"]) <= 3
    assert raw_error not in repr(guide)


async def test_unknown_workflow_is_rejected_by_the_tool_enum(server: FastMCP) -> None:
    """An arbitrary workflow must never silently map to a catalog entry."""
    tool = await server.get_tool("brain_workflow_guide")
    assert tool is not None

    with pytest.raises(Exception, match="workflow"):
        await tool.run({"project_key": "brain-v42", "workflow": "unknown.workflow"})


async def test_compact_profile_discovers_and_calls_the_hidden_guide() -> None:
    """Cold agents use the compact gateways without making the guide always visible."""
    server = FastMCP("workflow-guide-compact")
    register_workflow_guide_tools(server)
    apply_tool_catalog_profile(server, "compact")

    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
        found = await client.call_tool("brain_find_tool", {"query": "workflow guide runbook"})
        called = await client.call_tool(
            "brain_call_tool",
            {
                "name": "brain_workflow_guide",
                "arguments": {"project_key": "brain-v42", "workflow": "runbook.lifecycle"},
            },
        )

    assert "brain_workflow_guide" not in names
    assert "brain_workflow_guide" in [tool["name"] for tool in found.data]
    assert called.data["status"] == "unknown"


def _registered_operational_server() -> FastMCP:
    """Register the real target tool schemas without touching a database."""
    from brain_v42.mcp.tools.brain_tools import register_tools
    from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools
    from brain_v42.mcp.tools.ticket_tools import register_ticket_tools

    service = MagicMock()
    operational = FastMCP("workflow-guide-operational-contract")
    register_tools(
        operational,
        decision_svc=service,
        learning_svc=service,
        snippet_svc=service,
        runbook_svc=service,
        adr_svc=service,
        project_context_svc=service,
        brain_svc=service,
    )
    register_ticket_tools(operational, ticket_svc=service)
    register_session_lifecycle_tools(operational, service, AsyncMock())
    return operational


@pytest.mark.parametrize(
    ("workflow", "phase", "tool_name", "required_fields"),
    [
        ("session.lifecycle", "prepare", "brain_session_start", {"project_key", "client_key"}),
        (
            "session.lifecycle",
            "recover",
            "brain_session_resume",
            {"session_id", "expected_client_key"},
        ),
        (
            "project.context",
            "prepare",
            "brain_set_project_context",
            {"project_key", "name", "description"},
        ),
        (
            "project.context",
            "recover",
            "brain_set_project_context",
            {"project_key", "name", "description"},
        ),
        (
            "knowledge.decision",
            "prepare",
            "brain_log_decision",
            {"title", "context", "decision_made", "reasoning"},
        ),
        (
            "knowledge.decision",
            "recover",
            "brain_log_decision",
            {"title", "context", "decision_made", "reasoning"},
        ),
        (
            "runbook.lifecycle",
            "prepare",
            "brain_create_runbook",
            {"title", "description", "project_key", "trigger", "steps"},
        ),
        (
            "runbook.lifecycle",
            "recover",
            "brain_create_runbook",
            {"title", "description", "project_key", "trigger", "steps"},
        ),
        (
            "ticket.lifecycle",
            "prepare",
            "brain_ticket_create",
            {"from_project", "to_project", "kind", "title", "body"},
        ),
        (
            "ticket.lifecycle",
            "recover",
            "brain_ticket_create",
            {"from_project", "to_project", "kind", "title", "body"},
        ),
    ],
)
async def test_guide_payloads_match_real_fastmcp_target_tool_schemas(
    server: FastMCP,
    workflow: str,
    phase: str,
    tool_name: str,
    required_fields: set[str],
) -> None:
    """A guide may only prepare exactly the required fields of its target tool."""
    guide = await _guide(
        server,
        project_key="brain-v42",
        workflow=workflow,
        phase=phase,
    )
    operational = _registered_operational_server()
    target_tool = await operational.get_tool(tool_name)
    assert target_tool is not None
    schema = target_tool.parameters

    assert guide["tool_name"] == tool_name
    assert set(guide["fields_to_prepare"]) == required_fields
    assert set(schema["required"]) == required_fields
    assert set(guide["fields_to_prepare"]) <= set(schema["properties"])


def test_catalog_is_typed_versioned_and_guidance_freshness_is_not_a_heartbeat() -> None:
    """Guide freshness is a catalog contract, independent from session presence."""
    assert set(WORKFLOW_CATALOG) == {
        "session.lifecycle",
        "project.context",
        "knowledge.decision",
        "runbook.lifecycle",
        "ticket.lifecycle",
    }
    briefing = format_workflow_guidance_briefing()
    assert CATALOG_REVISION in briefing
    assert "brain_workflow_guide" in briefing
    assert "heartbeat" in briefing.lower()
    assert "does not establish guidance freshness" in briefing.lower()
