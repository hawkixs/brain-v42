"""Published input limits, refusal before services, and the explicit census."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from brain_v42.config import Settings
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from brain_v42.models.input_bounds import (
    KNOWLEDGE_TEXT_MAX_LENGTH,
    LIST_MAX_ITEMS,
    RELATIONS_MAX_ITEMS,
    SEARCH_QUERY_MAX_LENGTH,
    SHORT_TEXT_MAX_LENGTH,
    TAG_MAX_LENGTH,
)
from brain_v42.models.learning import Learning


def _registered_knowledge_server() -> FastMCP:
    from brain_v42.mcp.tools.brain_tools import register_tools
    from brain_v42.mcp.tools.claim_tools import register_claim_tools
    from brain_v42.mcp.tools.crud_tools import register_crud_tools
    from brain_v42.mcp.tools.decay_tools import register_decay_tools
    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools
    from brain_v42.mcp.tools.dream_tools import register_dream_tools
    from brain_v42.mcp.tools.fact_tools import register_fact_tools
    from brain_v42.mcp.tools.focus_slot_tools import register_focus_slot_tools
    from brain_v42.mcp.tools.plan_tools import register_plan_tools
    from brain_v42.mcp.tools.roadmap_tools import register_roadmap_tools
    from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools
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
    register_focus_slot_tools(server, service)
    register_session_lifecycle_tools(server, service, service)
    register_delivery_tools(server, service)
    register_fact_tools(server, service)
    register_claim_tools(server, service, service)
    return server


# UUID/short-id arguments are exempt by suffix; other exceptions are explicit.
UNBOUNDED_BY_DESIGN = {
    ("brain_claim_list", "project_key"): "Canonicalized project key",
    ("brain_log_decision", "claims"): "MAX_CLAIMS_PER_WRITE validates claims",
    ("brain_learn", "claims"): "MAX_CLAIMS_PER_WRITE validates claims",
    ("brain_propose_adr", "claims"): "MAX_CLAIMS_PER_WRITE validates claims",
    ("brain_save_snippet", "claims"): "MAX_CLAIMS_PER_WRITE validates claims",
    ("brain_create_runbook", "claims"): "MAX_CLAIMS_PER_WRITE validates claims",
    ("brain_update", "claims"): "MAX_CLAIMS_PER_WRITE validates claims",
    ("brain_update", "fields"): "Bounded Update models validate object properties",
    ("brain_reject_curation_proposals", "proposal_ids"): "Integer identifiers; HTTP body cap",
    ("brain_consolidation_candidates", "entity_type"): "Service dispatch vocabulary",
    ("brain_feature_create", "name"): "Writer model varchar(200)",
    ("brain_feature_create", "description"): "FeatureCreate description max_length=10000",
    ("brain_feature_create", "project_key"): "Canonicalized project key",
    ("brain_get_roadmap", "project_key"): "Canonicalized project key",
    ("brain_feature_update", "feature"): "Feature key resolved by service",
    ("brain_feature_update", "status"): "Service status vocabulary",
    ("brain_feature_update", "project_key"): "Canonicalized project key",
    ("brain_create_runbook", "title"): "Writer model varchar(200), or runbook lookup",
    ("brain_create_runbook", "project_key"): "Canonicalized project key",
    ("brain_create_runbook", "estimated_duration"): "Writer model max_length=50",
    ("brain_promote_runbook", "title"): "Writer model varchar(200), or runbook lookup",
    ("brain_promote_runbook", "project_key"): "Canonicalized project key",
    ("brain_promote_runbook", "estimated_duration"): "Writer model max_length=50",
    ("brain_get_runbook", "title"): "Writer model varchar(200), or runbook lookup",
    ("brain_get_runbook", "project_key"): "Canonicalized project key",
    ("brain_ticket_create", "from_project"): "Canonicalized project key",
    ("brain_ticket_create", "to_project"): "Canonicalized project key",
    ("brain_ticket_create", "kind"): "Ticket kind vocabulary",
    ("brain_ticket_create", "title"): "Writer model varchar(200), or runbook lookup",
    ("brain_ticket_create", "extraction"): "Ticket extraction vocabulary",
    ("brain_ticket_reply", "author_project"): "Canonicalized project key",
    ("brain_ticket_transition", "author_project"): "Canonicalized project key",
    ("brain_ticket_plan", "author_project"): "Canonicalized project key",
    ("brain_ticket_plan", "target_release"): "Release identifier",
    ("brain_ticket_list", "project_key"): "Canonicalized project key",
    ("brain_ticket_list", "target_release"): "Release identifier",
    ("brain_focus_history", "project_key"): "Canonicalized project key",
    ("brain_project_archive", "project_key"): "Canonicalized project key",
    ("brain_project_unarchive", "project_key"): "Canonicalized project key",
    ("brain_list_projects", "project_group"): "Canonicalized project group",
    ("brain_update_project_focus", "project_key"): "Canonicalized project key",
    ("brain_update", "expected_active_claim_ids"): "Claim identifiers checked by service; body cap",
    ("brain_list", "project_key"): "Canonicalized project key",
    ("brain_list", "status"): "Service status vocabulary",
    ("brain_reindex_plans", "project_key"): "Canonicalized project key",
    ("brain_log_decision", "title"): "Writer model varchar(200), or runbook lookup",
    ("brain_log_decision", "project_key"): "Canonicalized project key",
    ("brain_supersede_decision", "title"): "Writer model varchar(200), or runbook lookup",
    ("brain_supersede_decision", "project_key"): "Canonicalized project key",
    ("brain_learn", "topic"): "Writer model varchar(200)",
    ("brain_learn", "project_key"): "Canonicalized project key",
    ("brain_propose_adr", "title"): "Writer model varchar(200), or runbook lookup",
    ("brain_propose_adr", "project_key"): "Canonicalized project key",
    ("brain_promote_adr", "title"): "Writer model varchar(200), or runbook lookup",
    ("brain_promote_adr", "project_key"): "Canonicalized project key",
    ("brain_search", "project_key"): "Canonicalized project key",
    ("brain_search", "project_group"): "Canonicalized project group",
    ("brain_backfill_links_batch", "entity_type"): "Service dispatch vocabulary",
    ("brain_assign_domain", "domain_name"): "Domain allowlist",
    ("brain_list_curation_proposals", "project_key"): "Canonicalized project key",
    ("brain_list_curation_proposals", "status"): "Service status vocabulary",
    ("brain_reject_curation_proposals", "project_key"): "Canonicalized project key",
    ("brain_apply_curation_proposal", "project_key"): "Canonicalized project key",
    ("brain_save_snippet", "title"): "Writer model varchar(200), or runbook lookup",
    ("brain_save_snippet", "project_key"): "Canonicalized project key",
}
BOUNDED_ARGUMENTS = (
    ("brain_fact_get", "name", "maxLength", 200),
    ("brain_claim_verify", "idempotency_key", "maxLength", 200),
    ("brain_log_decision", "context", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_log_decision", "decision_made", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_log_decision", "reasoning", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_log_decision", "consequences", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_log_decision", "alternatives", "maxItems", LIST_MAX_ITEMS),
    ("brain_log_decision", "alternatives", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_log_decision", "tags", "maxItems", LIST_MAX_ITEMS),
    ("brain_log_decision", "tags", "itemMaxLength", TAG_MAX_LENGTH),
    ("brain_log_decision", "related_to", "maxItems", RELATIONS_MAX_ITEMS),
    ("brain_supersede_decision", "context", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_supersede_decision", "decision_made", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_supersede_decision", "reasoning", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_supersede_decision", "consequences", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_supersede_decision", "alternatives", "maxItems", LIST_MAX_ITEMS),
    ("brain_supersede_decision", "alternatives", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_supersede_decision", "tags", "maxItems", LIST_MAX_ITEMS),
    ("brain_supersede_decision", "tags", "itemMaxLength", TAG_MAX_LENGTH),
    ("brain_learn", "insight", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_learn", "source", "maxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_learn", "tags", "maxItems", LIST_MAX_ITEMS),
    ("brain_learn", "tags", "itemMaxLength", TAG_MAX_LENGTH),
    ("brain_learn", "related_to", "maxItems", RELATIONS_MAX_ITEMS),
    ("brain_propose_adr", "context", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_propose_adr", "decision", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_propose_adr", "consequences", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_propose_adr", "alternatives_considered", "maxItems", LIST_MAX_ITEMS),
    ("brain_propose_adr", "tags", "maxItems", LIST_MAX_ITEMS),
    ("brain_propose_adr", "tags", "itemMaxLength", TAG_MAX_LENGTH),
    ("brain_promote_adr", "context", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_promote_adr", "decision", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_promote_adr", "consequences", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_promote_adr", "alternatives_considered", "maxItems", LIST_MAX_ITEMS),
    ("brain_promote_adr", "tags", "maxItems", LIST_MAX_ITEMS),
    ("brain_promote_adr", "tags", "itemMaxLength", TAG_MAX_LENGTH),
    ("brain_deprecate_adr", "reason", "maxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_search", "query", "maxLength", SEARCH_QUERY_MAX_LENGTH),
    ("brain_search", "tags", "maxItems", LIST_MAX_ITEMS),
    ("brain_search", "tags", "itemMaxLength", TAG_MAX_LENGTH),
    ("brain_get_neighbors", "rel_types", "maxItems", LIST_MAX_ITEMS),
    ("brain_get_neighbors", "rel_types", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_graph_path", "rel_types", "maxItems", LIST_MAX_ITEMS),
    ("brain_graph_path", "rel_types", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_save_snippet", "intention", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_save_snippet", "code", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_save_snippet", "usage_example", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_save_snippet", "gotchas", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_save_snippet", "dependencies", "maxItems", LIST_MAX_ITEMS),
    ("brain_save_snippet", "dependencies", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_save_snippet", "tags", "maxItems", LIST_MAX_ITEMS),
    ("brain_save_snippet", "tags", "itemMaxLength", TAG_MAX_LENGTH),
    ("brain_save_snippet", "related_to", "maxItems", RELATIONS_MAX_ITEMS),
    ("brain_create_runbook", "description", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_create_runbook", "trigger", "maxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_create_runbook", "steps", "maxItems", LIST_MAX_ITEMS),
    ("brain_create_runbook", "rollback_steps", "maxItems", LIST_MAX_ITEMS),
    ("brain_create_runbook", "prerequisites", "maxItems", LIST_MAX_ITEMS),
    ("brain_create_runbook", "prerequisites", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_create_runbook", "tags", "maxItems", LIST_MAX_ITEMS),
    ("brain_create_runbook", "tags", "itemMaxLength", TAG_MAX_LENGTH),
    ("brain_promote_runbook", "description", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_promote_runbook", "trigger", "maxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_promote_runbook", "steps", "maxItems", LIST_MAX_ITEMS),
    ("brain_promote_runbook", "rollback_steps", "maxItems", LIST_MAX_ITEMS),
    ("brain_promote_runbook", "prerequisites", "maxItems", LIST_MAX_ITEMS),
    ("brain_promote_runbook", "prerequisites", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_promote_runbook", "tags", "maxItems", LIST_MAX_ITEMS),
    ("brain_promote_runbook", "tags", "itemMaxLength", TAG_MAX_LENGTH),
    ("brain_ticket_create", "body", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_ticket_reply", "body", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_ticket_reply", "corrects_body", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_ticket_transition", "message", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_set_project_context", "description", "maxLength", KNOWLEDGE_TEXT_MAX_LENGTH),
    ("brain_set_project_context", "code_style", "maxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_set_project_context", "git_workflow", "maxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_set_project_context", "test_strategy", "maxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_set_project_context", "current_phase", "maxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_set_project_context", "languages", "maxItems", LIST_MAX_ITEMS),
    ("brain_set_project_context", "languages", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_set_project_context", "frameworks", "maxItems", LIST_MAX_ITEMS),
    ("brain_set_project_context", "frameworks", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_set_project_context", "databases", "maxItems", LIST_MAX_ITEMS),
    ("brain_set_project_context", "databases", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_set_project_context", "blockers", "maxItems", LIST_MAX_ITEMS),
    ("brain_set_project_context", "blockers", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_set_project_context", "related_projects", "maxItems", LIST_MAX_ITEMS),
    ("brain_set_project_context", "related_projects", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_set_project_context", "plan_scan_paths", "maxItems", LIST_MAX_ITEMS),
    ("brain_set_project_context", "plan_scan_paths", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_update_project_focus", "blockers", "maxItems", LIST_MAX_ITEMS),
    ("brain_update_project_focus", "blockers", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_update_project_focus", "unpin", "maxItems", LIST_MAX_ITEMS),
    ("brain_update_project_focus", "unpin", "itemMaxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_project_archive", "reason", "maxLength", SHORT_TEXT_MAX_LENGTH),
    ("brain_update", "related_to", "maxItems", RELATIONS_MAX_ITEMS),
    ("brain_list", "tags", "maxItems", LIST_MAX_ITEMS),
    ("brain_list", "tags", "itemMaxLength", TAG_MAX_LENGTH),
)


def _nodes(schema: dict[str, Any], root: dict[str, Any]) -> list[dict[str, Any]]:
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return _nodes(target, root)
    if "anyOf" in schema:
        return [
            node
            for branch in schema["anyOf"]
            for node in _nodes(branch, root)
            if node.get("type") != "null"
        ]
    return [schema]


async def test_every_string_and_array_argument_is_bounded_or_exempted() -> None:
    for tool in await _registered_knowledge_server().list_tools():
        for prop, schema in tool.parameters["properties"].items():
            missing = False
            for node in _nodes(schema, tool.parameters):
                if node.get("type") == "string":
                    missing |= not any(
                        k in node for k in ("maxLength", "enum", "const", "pattern", "format")
                    )
                if node.get("type") == "array":
                    items = _nodes(node.get("items", {}), tool.parameters)
                    enum_items = all("enum" in item or "const" in item for item in items)
                    missing |= "maxItems" not in node and not enum_items
                    missing |= any(
                        item.get("type") == "string"
                        and not any(
                            k in item for k in ("maxLength", "enum", "const", "pattern", "format")
                        )
                        for item in items
                    )
            if missing:
                assert prop.endswith("_id") or (tool.name, prop) in UNBOUNDED_BY_DESIGN, (
                    tool.name,
                    prop,
                )


async def test_no_stale_exemption() -> None:
    tools = {tool.name: tool for tool in await _registered_knowledge_server().list_tools()}
    for (name, prop), reason in UNBOUNDED_BY_DESIGN.items():
        assert reason
        assert name in tools and prop in tools[name].parameters["properties"]


@pytest.mark.parametrize(("name", "prop", "keyword", "cap"), BOUNDED_ARGUMENTS)
async def test_bounded_arguments_publish_the_shared_constants(
    name: str, prop: str, keyword: str, cap: int
) -> None:
    tool = await _registered_knowledge_server().get_tool(name)
    assert tool is not None
    nodes = _nodes(tool.parameters["properties"][prop], tool.parameters)
    if keyword == "itemMaxLength":
        nodes = [item for node in nodes for item in _nodes(node["items"], tool.parameters)]
        keyword = "maxLength"
    assert {node.get(keyword) for node in nodes} == {cap}


@pytest.mark.parametrize("kind", ["fact", "claim"])
async def test_fact_and_claim_identifier_bounds_preserve_business_errors(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brain_v42.mcp.fact_errors import FactToolError
    from brain_v42.mcp.tools import claim_tools
    from brain_v42.mcp.tools.fact_tools import register_fact_tools
    from brain_v42.models.claim_verdict import ClaimVerificationError

    service = MagicMock()
    server = FastMCP("identifier-bounds")
    if kind == "fact":
        service.measure = AsyncMock(side_effect=ReachedService)
        service.names.return_value = []
        register_fact_tools(server, service)
        tool = await server.get_tool("brain_fact_get")
        args, prop, error = {}, "name", FactToolError
        method = service.measure
    else:
        service.verify = AsyncMock(side_effect=ReachedService)
        monkeypatch.setattr(claim_tools, "get_current_actor", lambda: "test-actor")
        claim_tools.register_claim_tools(server, service)
        tool = await server.get_tool("brain_claim_verify")
        args, prop, error = {"claim_id": str(uuid4())}, "idempotency_key", ClaimVerificationError
        method = service.verify
    assert tool is not None
    with pytest.raises(error):
        await tool.run({**args, prop: "x" * 201})
    method.assert_not_called()
    with pytest.raises(ReachedService):
        await tool.run({**args, prop: "x" * 200})
    method.assert_awaited_once()


class ReachedService(Exception):
    """Stop after proving validation allowed a call, before formatting a mock."""


def _invocation(name: str) -> tuple[FastMCP, AsyncMock, dict[str, Any]]:
    from brain_v42.mcp.tools.brain_tools import register_tools
    from brain_v42.mcp.tools.crud_tools import register_crud_tools
    from brain_v42.mcp.tools.ticket_tools import register_ticket_tools

    svc = MagicMock()
    for method in ("create", "reply", "get_or_create", "search", "update"):
        setattr(svc, method, AsyncMock(side_effect=ReachedService))
    server = FastMCP("input-bound-write")
    register_tools(
        server,
        decision_svc=svc,
        learning_svc=svc,
        snippet_svc=svc,
        runbook_svc=svc,
        adr_svc=svc,
        project_context_svc=svc,
        brain_svc=svc,
        roadmap_svc=svc,
    )
    register_ticket_tools(server, svc)
    register_crud_tools(
        server,
        decision_svc=svc,
        learning_svc=svc,
        snippet_svc=svc,
        runbook_svc=svc,
        adr_svc=svc,
        session_factory=None,
    )
    calls = {
        "brain_learn": ("create", {"topic": "bound", "insight": "ok"}),
        "brain_log_decision": (
            "create",
            {"title": "bound", "context": "context", "decision_made": "choice", "reasoning": "why"},
        ),
        "brain_save_snippet": (
            "create",
            {"title": "bound", "intention": "example", "code": "pass", "language": "python"},
        ),
        "brain_create_runbook": (
            "create",
            {
                "title": "bound",
                "description": "example",
                "project_key": "brain-v42",
                "trigger": "manual",
                "steps": [],
            },
        ),
        "brain_ticket_create": (
            "create",
            {
                "title": "bound",
                "body": "ok",
                "kind": "request",
                "from_project": "brain-v42",
                "to_project": "brain-v42",
            },
        ),
        "brain_ticket_reply": (
            "reply",
            {"ticket_id": str(uuid4()), "author_project": "brain-v42", "body": "ok"},
        ),
        "brain_set_project_context": (
            "get_or_create",
            {"project_key": "brain-v42", "name": "Brain", "description": "ok"},
        ),
        "brain_search": ("search", {"query": "ok"}),
        "brain_update": (
            "update",
            {"entity_type": "learning", "entity_id": str(uuid4()), "fields": {}},
        ),
    }
    method, args = calls[name]
    return server, getattr(svc, method), args


REFUSALS = (
    ("brain_learn", "insight", KNOWLEDGE_TEXT_MAX_LENGTH, "text"),
    ("brain_log_decision", "reasoning", KNOWLEDGE_TEXT_MAX_LENGTH, "text"),
    ("brain_save_snippet", "code", KNOWLEDGE_TEXT_MAX_LENGTH, "text"),
    ("brain_create_runbook", "steps", LIST_MAX_ITEMS, "steps"),
    ("brain_ticket_create", "body", KNOWLEDGE_TEXT_MAX_LENGTH, "text"),
    ("brain_ticket_reply", "body", KNOWLEDGE_TEXT_MAX_LENGTH, "text"),
    ("brain_set_project_context", "description", KNOWLEDGE_TEXT_MAX_LENGTH, "text"),
    ("brain_search", "query", SEARCH_QUERY_MAX_LENGTH, "text"),
    ("brain_learn", "tags", LIST_MAX_ITEMS, "tags"),
    ("brain_learn", "tags", TAG_MAX_LENGTH, "tag"),
)


@pytest.mark.parametrize(("name", "prop", "cap", "kind"), REFUSALS)
async def test_one_over_the_bound_is_refused_before_the_service(
    name: str, prop: str, cap: int, kind: str
) -> None:
    server, service, args = _invocation(name)
    tool = await server.get_tool(name)
    assert tool is not None

    def value(size: int) -> Any:
        if kind == "steps":
            return [{"title": "step"} for _ in range(size)]
        if kind == "tags":
            return ["tag"] * size
        if kind == "tag":
            return ["x" * size]
        return "x" * size

    with pytest.raises(ValidationError):
        await tool.run({**args, prop: value(cap + 1)})
    service.assert_not_called()
    with pytest.raises(ReachedService):
        await tool.run({**args, prop: value(cap)})
    service.assert_awaited_once()


async def test_the_bound_counts_characters_not_bytes() -> None:
    server, service, args = _invocation("brain_learn")
    tool = await server.get_tool("brain_learn")
    assert tool is not None
    with pytest.raises(ReachedService):
        await tool.run({**args, "insight": "é" * KNOWLEDGE_TEXT_MAX_LENGTH})
    service.assert_awaited_once()


def test_measured_payloads_fit_with_headroom() -> None:
    text_maxima = {
        "insight": 17047,
        "decision": 14010,
        "steps_json": 15445,
        "ticket": 10093,
        "reply": 8609,
        "description": 6869,
        "code": 6025,
        "feature": 4479,
        "runbook": 3112,
        "context": 2767,
        "reasoning": 2775,
        "gotchas": 1678,
        "intention": 1527,
    }
    assert all(KNOWLEDGE_TEXT_MAX_LENGTH >= 2 * n for n in text_maxima.values())
    short_maxima = {
        "prerequisite": 711,
        "test_strategy": 609,
        "alternative": 581,
        "git_workflow": 423,
        "trigger": 410,
        "source": 403,
        "blocker": 312,
    }
    assert all(SHORT_TEXT_MAX_LENGTH >= 2.5 * n for n in short_maxima.values())
    assert TAG_MAX_LENGTH >= 2 * 44
    assert LIST_MAX_ITEMS >= 2.5 * max(34, 12, 16, 7, 7, 8)
    assert RELATIONS_MAX_ITEMS >= 2.5 * 9


def test_a_maximal_field_fits_under_the_http_body_cap() -> None:
    assert (
        KNOWLEDGE_TEXT_MAX_LENGTH * 6 + 65_536
        < Settings.model_fields["mcp_http_max_body_bytes"].default
    )


async def test_the_bound_holds_through_brain_call_tool() -> None:
    server, service, args = _invocation("brain_learn")
    apply_tool_catalog_profile(server, "compact")
    async with Client(server) as client:
        result = await client.call_tool(
            "brain_call_tool",
            {
                "name": "brain_learn",
                "arguments": {**args, "insight": "x" * (KNOWLEDGE_TEXT_MAX_LENGTH + 1)},
            },
            raise_on_error=False,
        )
    assert result.is_error
    assert "string_too_long" in str(result.content)
    service.assert_not_called()


async def test_brain_update_refuses_an_oversized_field() -> None:
    server, service, args = _invocation("brain_update")
    tool = await server.get_tool("brain_update")
    assert tool is not None
    with pytest.raises(ToolError, match="Invalid fields"):
        await tool.run({**args, "fields": {"insight": "x" * (KNOWLEDGE_TEXT_MAX_LENGTH + 1)}})
    service.assert_not_called()
    with pytest.raises(ReachedService):
        await tool.run({**args, "fields": {"insight": "x" * KNOWLEDGE_TEXT_MAX_LENGTH}})
    service.assert_awaited_once()


def test_read_models_stay_unbounded() -> None:
    assert Learning(topic="history", insight="x" * (KNOWLEDGE_TEXT_MAX_LENGTH + 1))


def test_documented_input_bounds_and_auth_refusal() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    tools = (root / "docs/MCP_TOOLS.md").read_text()
    operations = (root / "docs/OPERATIONS.md").read_text()
    architecture = (root / "docs/ARCHITECTURE.md").read_text()
    runbook = (root / "deploy/systemd/MCP_HTTP_RUNBOOK.md").read_text()
    assert tools.index("## UUID error contracts") < tools.index("## Input bounds")
    for constant, value in (
        ("KNOWLEDGE_TEXT_MAX_LENGTH", KNOWLEDGE_TEXT_MAX_LENGTH),
        ("SHORT_TEXT_MAX_LENGTH", SHORT_TEXT_MAX_LENGTH),
        ("TAG_MAX_LENGTH", TAG_MAX_LENGTH),
        ("LIST_MAX_ITEMS", LIST_MAX_ITEMS),
        ("RELATIONS_MAX_ITEMS", RELATIONS_MAX_ITEMS),
        ("SEARCH_QUERY_MAX_LENGTH", SEARCH_QUERY_MAX_LENGTH),
    ):
        assert f"`{constant}` | {value:,}" in tools
    assert "## MCP HTTP input bounds and residuals" in operations
    assert "MCP_HTTP_ALLOW_UNAUTHENTICATED" in operations
    assert "HttpAuthConfigurationError" in operations
    assert "body-read deadline" in operations
    assert 'Direct dev HTTP can leave `MCP_HTTP_TOKEN=""`' not in architecture
    assert "may omit the bearer" not in architecture
    assert "MCP_HTTP_ALLOW_UNAUTHENTICATED" in architecture
    assert "non-empty `MCP_HTTP_TOKEN`" in architecture
    assert "never belong" in architecture
    assert "MCP_HTTP_ALLOW_UNAUTHENTICATED" in runbook
    assert "header file" in runbook and "413" in runbook


@pytest.mark.parametrize(
    ("module", "class_name", "field", "cap", "kind"),
    [
        ("decision", "DecisionUpdate", field, KNOWLEDGE_TEXT_MAX_LENGTH, "text")
        for field in ("description", "reasoning", "consequences")
    ]
    + [
        ("learning", "LearningUpdate", "insight", KNOWLEDGE_TEXT_MAX_LENGTH, "text"),
        ("learning", "LearningUpdate", "source", SHORT_TEXT_MAX_LENGTH, "text"),
        *[
            ("snippet", "SnippetUpdate", field, KNOWLEDGE_TEXT_MAX_LENGTH, "text")
            for field in ("intention", "code", "usage_example", "gotchas")
        ],
        ("runbook", "RunbookUpdate", "description", KNOWLEDGE_TEXT_MAX_LENGTH, "text"),
        ("runbook", "RunbookUpdate", "trigger", SHORT_TEXT_MAX_LENGTH, "text"),
        *[
            ("adr", "ADRUpdate", field, KNOWLEDGE_TEXT_MAX_LENGTH, "text")
            for field in ("context", "decision", "consequences")
        ],
        *[
            (module, cls, "tags", LIST_MAX_ITEMS, "tags")
            for module, cls in (
                ("decision", "DecisionUpdate"),
                ("learning", "LearningUpdate"),
                ("snippet", "SnippetUpdate"),
                ("runbook", "RunbookUpdate"),
                ("adr", "ADRUpdate"),
            )
        ],
        ("decision", "DecisionUpdate", "alternatives", LIST_MAX_ITEMS, "tags"),
        ("snippet", "SnippetUpdate", "dependencies", LIST_MAX_ITEMS, "tags"),
        ("runbook", "RunbookUpdate", "prerequisites", LIST_MAX_ITEMS, "tags"),
        ("runbook", "RunbookUpdate", "steps", LIST_MAX_ITEMS, "steps"),
        ("runbook", "RunbookUpdate", "rollback_steps", LIST_MAX_ITEMS, "steps"),
        ("adr", "ADRUpdate", "alternatives_considered", LIST_MAX_ITEMS, "alternatives"),
    ],
)
def test_update_models_refuse_one_over_the_bound(
    module: str, class_name: str, field: str, cap: int, kind: str
) -> None:
    from brain_v42.models import adr, decision, learning, runbook, snippet

    models = {
        "adr": adr,
        "decision": decision,
        "learning": learning,
        "runbook": runbook,
        "snippet": snippet,
    }
    cls = getattr(models[module], class_name)

    def value(size: int) -> Any:
        if kind == "tags":
            return ["item"] * size
        if kind == "steps":
            return [{"title": "step"}] * size
        if kind == "alternatives":
            return [{"title": "option", "description": "why"}] * size
        return "x" * size

    with pytest.raises(ValidationError):
        cls(**{field: value(cap + 1)})
    assert cls(**{field: value(cap)})
