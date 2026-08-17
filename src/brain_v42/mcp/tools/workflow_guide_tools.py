"""Versioned, bounded, read-only guidance for Brain MCP workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from brain_v42.mcp.tools.tool_annotations import _READ_ANNOTATIONS

GUIDE_VERSION = "1.0"
CATALOG_REVISION = "2026-08-02.1"

type WorkflowName = Literal[
    "session.lifecycle",
    "project.context",
    "knowledge.decision",
    "runbook.lifecycle",
    "ticket.lifecycle",
]
type WorkflowPhase = Literal["prepare", "recover"]
type GuidanceStatus = Literal["current", "outdated", "unknown"]

ProjectKeyArg = Annotated[str, Field(min_length=1, max_length=50)]
KnownVersionArg = Annotated[str, Field(min_length=1, max_length=64)]
ErrorContextArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=300,
        description="Short local validation context. It is never reflected in the guide response.",
    ),
]


@dataclass(frozen=True, slots=True)
class WorkflowGuideDefinition:
    """Static, deliberately small description of one supported workflow family."""

    objective: str
    prepare_tool_name: str
    recover_tool_name: str
    prerequisites: tuple[str, ...]
    prepare_fields: tuple[str, ...]
    recover_fields: tuple[str, ...]
    success_criteria: tuple[str, ...]
    prepare_next_action: str
    recover_next_action: str
    recovery: tuple[str, ...]


WORKFLOW_CATALOG: dict[WorkflowName, WorkflowGuideDefinition] = {
    "session.lifecycle": WorkflowGuideDefinition(
        objective="Open or resume an explicitly user-controlled Brain session.",
        prepare_tool_name="brain_session_start",
        recover_tool_name="brain_session_resume",
        prerequisites=(
            "The user explicitly requested the lifecycle action.",
            "The project key is known.",
        ),
        prepare_fields=("project_key", "client_key"),
        recover_fields=("session_id", "expected_client_key"),
        success_criteria=(
            "The response contains an open session.",
            "The client key matches the intended session.",
            "The returned briefing is read before the next operation.",
        ),
        prepare_next_action="Call brain_session_start once with the project key and stable client key.",
        recover_next_action=(
            "Verify the client key and session identifier, then call brain_session_resume once."
        ),
        recovery=(
            "Use the original stable client key.",
            "Do not treat a heartbeat as a semantic checkpoint.",
        ),
    ),
    "project.context": WorkflowGuideDefinition(
        objective="Create or update the explicit context for one Brain project.",
        prepare_tool_name="brain_set_project_context",
        recover_tool_name="brain_set_project_context",
        prerequisites=(
            "The project key is authoritative.",
            "The user supplied the context to record.",
        ),
        prepare_fields=("project_key", "name", "description"),
        recover_fields=("project_key", "name", "description"),
        success_criteria=(
            "The response identifies the requested project.",
            "The intended focus is present.",
            "No unrelated project was updated.",
        ),
        prepare_next_action="Call brain_set_project_context once with the confirmed project context.",
        recover_next_action=(
            "Correct the project context fields, then call brain_set_project_context once."
        ),
        recovery=(
            "Recheck the canonical project key.",
            "Keep context text scoped to the requested project.",
        ),
    ),
    "knowledge.decision": WorkflowGuideDefinition(
        objective="Record a durable technical decision with its rationale.",
        prepare_tool_name="brain_log_decision",
        recover_tool_name="brain_log_decision",
        prerequisites=(
            "A decision was actually made.",
            "The alternatives and rationale are available.",
        ),
        prepare_fields=(
            "title",
            "context",
            "decision_made",
            "reasoning",
        ),
        recover_fields=("title", "context", "decision_made", "reasoning"),
        success_criteria=(
            "The response confirms the stored decision.",
            "The rationale explains why the choice was made.",
            "The project scope matches the intended project.",
        ),
        prepare_next_action="Call brain_log_decision once with the decision and its rationale.",
        recover_next_action="Correct the decision fields, then call brain_log_decision once.",
        recovery=(
            "Separate the decision from a pure learning or code snippet.",
            "Provide the reasoning rather than only the outcome.",
        ),
    ),
    "runbook.lifecycle": WorkflowGuideDefinition(
        objective="Create a reproducible operational runbook for one project.",
        prepare_tool_name="brain_create_runbook",
        recover_tool_name="brain_create_runbook",
        prerequisites=(
            "The procedure is reproducible.",
            "The project key and runbook title are known.",
        ),
        prepare_fields=("title", "description", "project_key", "trigger", "steps"),
        recover_fields=("title", "description", "project_key", "trigger", "steps"),
        success_criteria=(
            "The response confirms the new runbook.",
            "Each step is actionable.",
            "The runbook is scoped to the intended project.",
        ),
        prepare_next_action="Call brain_create_runbook once with the complete procedure.",
        recover_next_action="Correct the required runbook fields, then call brain_create_runbook once.",
        recovery=(
            "Supply every required runbook field.",
            "Keep steps executable and ordered.",
        ),
    ),
    "ticket.lifecycle": WorkflowGuideDefinition(
        objective="Create a scoped Brain coordination ticket for a requested action.",
        prepare_tool_name="brain_ticket_create",
        recover_tool_name="brain_ticket_create",
        prerequisites=(
            "The requested action and target project are known.",
            "The ticket body contains the necessary handoff context.",
        ),
        prepare_fields=("from_project", "to_project", "kind", "title", "body"),
        recover_fields=("from_project", "to_project", "kind", "title", "body"),
        success_criteria=(
            "The response identifies the created ticket.",
            "The target project matches the intended owner.",
            "The ticket kind matches the requested coordination.",
        ),
        prepare_next_action="Call brain_ticket_create once with the scoped coordination request.",
        recover_next_action="Correct the ticket fields, then call brain_ticket_create once.",
        recovery=(
            "Verify both project keys before retrying.",
            "Keep the body actionable for the target project.",
        ),
    ),
}


def _guidance_status(
    known_guide_version: str | None,
    known_catalog_revision: str | None,
) -> tuple[GuidanceStatus, bool, str]:
    """Derive client guidance freshness without consulting mutable session state."""
    if known_guide_version not in {None, GUIDE_VERSION}:
        if known_catalog_revision not in {None, CATALOG_REVISION}:
            return "outdated", True, "guide_and_catalog_revision_mismatch"
        return "outdated", True, "guide_version_mismatch"
    if known_catalog_revision not in {None, CATALOG_REVISION}:
        return "outdated", True, "catalog_revision_mismatch"
    if known_guide_version is None or known_catalog_revision is None:
        return "unknown", True, "partial_version_evidence"
    return "current", False, "versions_match"


def _refresh_action(workflow: WorkflowName) -> str:
    return (
        "Reload the guide and local workflow context: call brain_workflow_guide again "
        f"for workflow '{workflow}' and replace cached guidance with this response."
    )


def format_workflow_guidance_briefing() -> str:
    """Render the fixed, non-mutating session-briefing guidance signal."""
    return (
        "### Workflow guidance\n"
        f"- Catalog revision: {CATALOG_REVISION} (guide {GUIDE_VERSION})\n"
        "- A session heartbeat does not establish guidance freshness.\n"
        "- Refresh cached guidance with brain_workflow_guide before continuing when versions differ."
    )


def _guide_payload(
    workflow: WorkflowName,
    phase: WorkflowPhase,
    known_guide_version: str | None,
    known_catalog_revision: str | None,
    error_context: str | None,
) -> dict[str, Any]:
    """Build a stable response without echoing client-supplied error text."""
    del error_context
    definition = WORKFLOW_CATALOG[workflow]
    status, refresh_required, refresh_reason = _guidance_status(
        known_guide_version,
        known_catalog_revision,
    )
    recovering = phase == "recover"
    return {
        "guide_version": GUIDE_VERSION,
        "catalog_revision": CATALOG_REVISION,
        "status": status,
        "refresh_required": refresh_required,
        "refresh_reason": refresh_reason,
        # Only instruct a reload when one is actually required. A client that
        # reads refresh_action without first checking refresh_required would
        # otherwise reload an already-current guide forever. The key stays
        # present so the response shape is stable for typed clients.
        "refresh_action": _refresh_action(workflow) if refresh_required else None,
        "objective": definition.objective,
        "prerequisites": list(definition.prerequisites),
        "next_action": (
            definition.recover_next_action if recovering else definition.prepare_next_action
        ),
        "tool_name": (definition.recover_tool_name if recovering else definition.prepare_tool_name),
        "fields_to_prepare": list(
            definition.recover_fields if recovering else definition.prepare_fields
        ),
        "success_criteria": list(definition.success_criteria),
        "recovery": list(definition.recovery),
    }


def register_workflow_guide_tools(mcp: FastMCP) -> None:
    """Register the catalog-only workflow guide without injecting any persistence service."""

    @mcp.tool(version=GUIDE_VERSION, output_schema=None, annotations=_READ_ANNOTATIONS)
    async def brain_workflow_guide(
        project_key: ProjectKeyArg,
        workflow: WorkflowName,
        phase: WorkflowPhase = "prepare",
        known_guide_version: KnownVersionArg | None = None,
        known_catalog_revision: KnownVersionArg | None = None,
        error_context: ErrorContextArg | None = None,
    ) -> dict[str, Any]:
        """Return a bounded, read-only guide for one supported Brain workflow family."""
        del project_key
        return _guide_payload(
            workflow,
            phase,
            known_guide_version,
            known_catalog_revision,
            error_context,
        )
