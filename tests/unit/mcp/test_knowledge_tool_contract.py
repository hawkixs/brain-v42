"""Protocol contracts for the 43 non-session Brain MCP tools."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

READ_ONLY_TOOLS = frozenset(
    {
        "brain_consolidation_candidates",
        "brain_decay_status",
        "brain_focus_history",
        "brain_get",
        "brain_get_clusters",
        "brain_get_neighbors",
        "brain_get_roadmap",
        "brain_get_runbook",
        "brain_get_supersession_chain",
        "brain_graph_path",
        "brain_list",
        "brain_list_adrs",
        "brain_list_curation_proposals",
        "brain_list_orphans_for_classification",
        "brain_list_project_groups",
        "brain_list_projects",
        "brain_search",
        "brain_ticket_get",
        "brain_ticket_list",
        "brain_workflow_guide",
    }
)
IDEMPOTENT_ADDITIVE_TOOLS = frozenset({"brain_assign_domain"})
ADDITIVE_WRITE_TOOLS = frozenset(
    {
        "brain_backfill_links_batch",
        "brain_create_runbook",
        "brain_feature_create",
        "brain_learn",
        "brain_log_decision",
        "brain_propose_adr",
        "brain_save_snippet",
        "brain_ticket_create",
        "brain_ticket_reply",
    }
)
IDEMPOTENT_DESTRUCTIVE_TOOLS = frozenset(
    {
        "brain_delete",
        # Rejection is TERMINAL (no resurrection at review) and idempotent
        # (re-rejecting returns "already rejected").
        "brain_reject_curation_proposals",
    }
)
DESTRUCTIVE_TOOLS = frozenset(
    {
        "brain_accept_adr",
        "brain_deprecate_adr",
        "brain_execute_runbook",
        "brain_feature_update",
        "brain_merge_entities",
        # A curation apply mutates the FEATURE (merge archives the loser, rename
        # overwrites the title) — the same family as brain_feature_update.
        "brain_apply_curation_proposal",
        "brain_refresh_entity",
        "brain_reindex_plans",
        "brain_set_project_context",
        "brain_supersede_decision",
        "brain_ticket_transition",
        "brain_update",
        "brain_update_project_focus",
        "brain_use_snippet",
        "brain_validate_learning",
    }
)


def _registered_knowledge_server() -> FastMCP:
    from brain_v42.mcp.tools.brain_tools import register_tools
    from brain_v42.mcp.tools.crud_tools import register_crud_tools
    from brain_v42.mcp.tools.decay_tools import register_decay_tools
    from brain_v42.mcp.tools.dream_tools import register_dream_tools
    from brain_v42.mcp.tools.plan_tools import register_plan_tools
    from brain_v42.mcp.tools.roadmap_tools import register_roadmap_tools
    from brain_v42.mcp.tools.ticket_tools import register_ticket_tools

    service = MagicMock()
    server = FastMCP("knowledge-tool-contract")
    register_tools(
        server,
        decision_svc=service,
        learning_svc=service,
        snippet_svc=service,
        runbook_svc=service,
        adr_svc=service,
        project_context_svc=service,
        brain_svc=service,
        roadmap_svc=service,
        graph_svc=service,
    )
    register_roadmap_tools(server, service, service, service)
    register_decay_tools(server, service, service)
    register_plan_tools(server, service)
    register_crud_tools(
        server,
        decision_svc=service,
        learning_svc=service,
        snippet_svc=service,
        runbook_svc=service,
        adr_svc=service,
        session_factory=service,
    )
    register_dream_tools(
        server,
        session_factory=service,
        auto_linker=service,
        graph_service=service,
    )
    register_ticket_tools(server, service)
    return server


async def test_all_knowledge_tools_publish_exact_safety_annotations() -> None:
    """Missing or wrong hints must not let a client misclassify a tool call."""
    server = _registered_knowledge_server()
    groups = (
        READ_ONLY_TOOLS,
        IDEMPOTENT_ADDITIVE_TOOLS,
        ADDITIVE_WRITE_TOOLS,
        IDEMPOTENT_DESTRUCTIVE_TOOLS,
        DESTRUCTIVE_TOOLS,
    )
    expected_names = frozenset().union(*groups)
    assert len(expected_names) == 47
    assert sum(len(group) for group in groups) == len(expected_names)
    assert {tool.name for tool in await server.list_tools()} == expected_names

    expected = {
        **{
            name: ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            )
            for name in READ_ONLY_TOOLS
        },
        **{
            name: ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            )
            for name in IDEMPOTENT_ADDITIVE_TOOLS
        },
        **{
            name: ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=False,
            )
            for name in ADDITIVE_WRITE_TOOLS
        },
        **{
            name: ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=True,
                openWorldHint=False,
            )
            for name in IDEMPOTENT_DESTRUCTIVE_TOOLS
        },
        **{
            name: ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=False,
            )
            for name in DESTRUCTIVE_TOOLS
        },
    }

    for name, expected_annotations in expected.items():
        tool = await server.get_tool(name)
        assert tool is not None, name
        assert tool.annotations == expected_annotations, name


def _schema_nodes(schema: dict[str, Any], root: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [schema]
    if ref := schema.get("$ref"):
        target: Any = root
        for part in ref.removeprefix("#/").split("/"):
            target = target[part]
        nodes.extend(_schema_nodes(target, root))
    for key in ("anyOf", "oneOf"):
        for branch in schema.get(key, []):
            nodes.extend(_schema_nodes(branch, root))
    if isinstance(schema.get("items"), dict):
        nodes.extend(_schema_nodes(schema["items"], root))
    return nodes


def _property_nodes(parameters: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return _schema_nodes(parameters["properties"][name], parameters)


async def test_domain_types_are_published_in_tool_input_schemas() -> None:
    """Domain constraints must be discoverable before a client sends an invalid call."""
    server = _registered_knowledge_server()
    knowledge_types = {"decision", "learning", "snippet", "runbook", "adr", "plan"}
    mutable_types = knowledge_types - {"plan"}

    for name in ("brain_get", "brain_delete", "brain_list", "brain_refresh_entity"):
        tool = await server.get_tool(name)
        assert tool is not None
        enums = {
            value
            for node in _property_nodes(tool.parameters, "entity_type")
            for value in node.get("enum", [])
        }
        assert enums == knowledge_types, name

    for name in ("brain_update", "brain_merge_entities"):
        tool = await server.get_tool(name)
        assert tool is not None
        enums = {
            value
            for node in _property_nodes(tool.parameters, "entity_type")
            for value in node.get("enum", [])
        }
        assert enums == mutable_types, name

    search = await server.get_tool("brain_search")
    learn = await server.get_tool("brain_learn")
    execute = await server.get_tool("brain_execute_runbook")
    list_adrs = await server.get_tool("brain_list_adrs")
    listing = await server.get_tool("brain_list")
    save_snippet = await server.get_tool("brain_save_snippet")
    transition = await server.get_tool("brain_ticket_transition")
    propose_adr = await server.get_tool("brain_propose_adr")
    assert all(
        tool is not None
        for tool in (
            search,
            learn,
            execute,
            list_adrs,
            listing,
            save_snippet,
            transition,
            propose_adr,
        )
    )

    def enum_values(tool: Any, property_name: str) -> set[str]:
        return {
            value
            for node in _property_nodes(tool.parameters, property_name)
            for value in node.get("enum", [])
        }

    assert enum_values(search, "types") == knowledge_types
    assert enum_values(learn, "source_type") == {
        "experience",
        "documentation",
        "code_review",
        "bug",
        "external",
        "article",
        "video",
        "book",
        "conversation",
        "research",
        "automated",
    }
    assert enum_values(learn, "confidence") == {"low", "medium", "high"}
    assert enum_values(execute, "status") == {"success", "failed", "partial", "skipped"}
    assert enum_values(list_adrs, "status") == {
        "proposed",
        "accepted",
        "deprecated",
        "superseded",
    }
    assert enum_values(listing, "confidence") == {"low", "medium", "high"}
    assert enum_values(transition, "action") == {
        "start",
        "resolve",
        "resolve_pending",
        "wontfix",
        "confirm",
        "reopen",
        "ack",
        "cancel",
    }

    for tool, property_name in ((save_snippet, "language"), (listing, "language")):
        max_lengths = {
            node["maxLength"]
            for node in _property_nodes(tool.parameters, property_name)
            if "maxLength" in node
        }
        assert max_lengths == {50}

    alternative_objects = [
        node
        for node in _property_nodes(propose_adr.parameters, "alternatives_considered")
        if node.get("type") == "object"
    ]
    assert any(
        set(node.get("properties", {})) == {"title", "description", "reason_rejected"}
        for node in alternative_objects
    )
