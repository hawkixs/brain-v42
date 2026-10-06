"""Authorize credential tools from one reviewed table, including compact inner calls."""

from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import AuthorizationError
from fastmcp.prompts.base import Prompt, PromptResult
from fastmcp.resources.base import Resource, ResourceResult
from fastmcp.resources.template import ResourceTemplate
from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult

from brain_v42.credentials.families import ELEVATION_FAMILY
from brain_v42.credentials.reasons import emit_refusal
from brain_v42.mcp.client_principal import ClientPrincipal, resolve_client_principal
from brain_v42.provenance import get_current_actor, get_current_peer

NEUTRAL_TOOLS = frozenset({"brain_find_tool", "brain_call_tool"})

# Explicit names make the registration census fail when a new tool needs review.
# Telemetry and elevate deliberately own no tools; admin is never a stored scope.
TOOL_FAMILIES: dict[str, str] = {
    "brain_search": "read",
    "brain_get": "read",
    "brain_list": "read",
    "brain_get_supersession_chain": "read",
    "brain_get_neighbors": "read",
    "brain_graph_path": "read",
    "brain_get_runbook": "read",
    "brain_get_roadmap": "read",
    "brain_focus_history": "read",
    "brain_list_projects": "read",
    "brain_list_project_groups": "read",
    "brain_workflow_guide": "read",
    "brain_ticket_list": "read",
    "brain_ticket_get": "read",
    "brain_slot_list": "read",
    "brain_fact_get": "read",
    "brain_fact_list": "read",
    "brain_claim_list": "read",
    "brain_claim_history": "read",
    "brain_decay_status": "read",
    "brain_consolidation_candidates": "read",
    "brain_learn": "write",
    "brain_log_decision": "write",
    "brain_supersede_decision": "write",
    "brain_save_snippet": "write",
    "brain_create_runbook": "write",
    "brain_propose_adr": "write",
    "brain_ticket_create": "write",
    "brain_ticket_reply": "write",
    "brain_ticket_transition": "write",
    "brain_ticket_plan": "write",
    "brain_session_start": "write",
    "brain_session_list": "write",
    "brain_session_resume": "write",
    "brain_session_capture": "write",
    "brain_session_checkpoint": "write",
    "brain_session_heartbeat": "write",
    "brain_session_bind": "write",
    "brain_session_end": "write",
    "brain_session_relay": "write",
    "brain_session_abandon": "write",
    "brain_slot_open": "write",
    "brain_slot_close": "write",
    "brain_claim_verify": "write",
    "brain_update": "write",
    "brain_validate_learning": "write",
    "brain_use_snippet": "write",
    "brain_execute_runbook": "write",
    "brain_refresh_entity": "write",
    "brain_delivery_contract_set": "delivery",
    "brain_delivery_bind_pr": "delivery",
    "brain_delivery_get": "delivery",
    "brain_delivery_list": "delivery",
    "brain_delivery_refresh": "delivery",
    "brain_delivery_claim": "delivery",
    "brain_delivery_claim_renew": "delivery",
    "brain_delivery_claim_release": "delivery",
    "brain_delivery_accept": "delivery",
    "brain_delivery_attest": "delivery",
    "brain_delivery_attestation_list": "delivery",
    "brain_delete": "admin",
    "brain_merge_entities": "admin",
    "brain_update_project_focus": "admin",
    "brain_set_project_context": "admin",
    "brain_feature_create": "admin",
    "brain_feature_update": "admin",
    "brain_reindex_plans": "admin",
    "brain_backfill_links_batch": "admin",
    "brain_get_clusters": "admin",
    "brain_list_orphans_for_classification": "admin",
    "brain_assign_domain": "admin",
    "brain_list_curation_proposals": "admin",
    "brain_reject_curation_proposals": "admin",
    "brain_apply_curation_proposal": "admin",
    "brain_promote_adr": "admin",
    "brain_promote_runbook": "admin",
    "brain_accept_adr": "admin",
    "brain_deprecate_adr": "admin",
    "brain_project_archive": "admin",
    "brain_project_unarchive": "admin",
}


def family_of(name: str) -> str:
    """Fail closed until a new registration's family has been reviewed."""
    return TOOL_FAMILIES.get(name, ELEVATION_FAMILY)


async def apply_tool_families(mcp: FastMCP) -> None:
    """Tag raw tools centrally without replacing functions, versions or schemas."""
    for tool in await mcp._list_tools():
        if tool.name not in NEUTRAL_TOOLS:
            tool.tags.add(f"family:{family_of(tool.name)}")


class FamilyAuthorizationMiddleware(Middleware):
    """Installed only in credentials mode, after provenance binds the current actor."""

    def __init__(
        self,
        elevation_checker: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._elevation_checker = elevation_checker
        self._catalog_elevations: ContextVar[dict[tuple[str, str], bool] | None] = ContextVar(
            "brain_v42_catalog_elevations", default=None
        )

    async def allows_tool(
        self,
        principal: ClientPrincipal | None,
        name: str,
        *,
        elevations: dict[tuple[str, str], bool] | None = None,
    ) -> bool:
        """Calls check afresh; a catalogue's filter passes share one request snapshot."""
        if principal is None:
            return False
        # Neither auxiliary family grants a catalogue, even with an elevation.
        if not principal.families.intersection(TOOL_FAMILIES.values()):
            return False
        if name in NEUTRAL_TOOLS:
            return True
        family = family_of(name)
        if family != ELEVATION_FAMILY:
            return family in principal.families
        if self._elevation_checker is None:
            return False
        connection_id = get_http_headers(include={"mcp-session-id"}).get("mcp-session-id")
        if not connection_id:
            return False
        pair = (connection_id, principal.client_id)
        if elevations is not None and pair in elevations:
            return elevations[pair]
        try:
            allowed = await self._elevation_checker(*pair)
        except Exception:
            # A repository failure grants nothing and must not expose its message.
            allowed = False
        if elevations is not None:
            elevations[pair] = allowed
        return allowed

    async def filter_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        """Also filter BM25's real ranking pool, which bypasses catalogue transforms."""
        principal = resolve_client_principal(get_access_token())
        elevations = self._catalog_elevations.get()
        if elevations is None:
            # Direct catalogue/search filters also resolve once, without retaining rights.
            elevations = {}
        return tuple(
            [
                tool
                for tool in tools
                if await self.allows_tool(principal, tool.name, elevations=elevations)
            ]
        )

    async def allows_current_tool(self, name: str) -> bool:
        """Use the same current-request token resolver for transforms and calls."""
        return await self.allows_tool(resolve_client_principal(get_access_token()), name)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        # The transform runs inside call_next, before this middleware's final pass.
        token = self._catalog_elevations.set({})
        try:
            return await self.filter_tools(await call_next(context))
        finally:
            self._catalog_elevations.reset(token)

    def _refuse(self, *, tool: str | None = None, path: str | None = None) -> None:
        principal = resolve_client_principal(get_access_token())
        emit_refusal(
            "family_denied",
            status=403,
            client_id=None if principal is None else principal.client_id,
            declared_agent=get_current_actor(),
            peer=get_current_peer(),
            tool=tool,
            path=path,
        )

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        if not await self.allows_current_tool(name):
            self._refuse(tool=name)
            raise AuthorizationError("family_denied")
        return await call_next(context)

    async def on_list_resources(
        self,
        context: MiddlewareContext[mt.ListResourcesRequest],
        call_next: CallNext[mt.ListResourcesRequest, Sequence[Resource]],
    ) -> Sequence[Resource]:
        return []

    async def on_list_resource_templates(
        self,
        context: MiddlewareContext[mt.ListResourceTemplatesRequest],
        call_next: CallNext[mt.ListResourceTemplatesRequest, Sequence[ResourceTemplate]],
    ) -> Sequence[ResourceTemplate]:
        return []

    async def on_list_prompts(
        self,
        context: MiddlewareContext[mt.ListPromptsRequest],
        call_next: CallNext[mt.ListPromptsRequest, Sequence[Prompt]],
    ) -> Sequence[Prompt]:
        return []

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, ResourceResult],
    ) -> ResourceResult:
        self._refuse(path=str(context.message.uri))
        raise AuthorizationError("family_denied")

    async def on_get_prompt(
        self,
        context: MiddlewareContext[mt.GetPromptRequestParams],
        call_next: CallNext[mt.GetPromptRequestParams, PromptResult],
    ) -> PromptResult:
        self._refuse(tool=context.message.name)
        raise AuthorizationError("family_denied")
