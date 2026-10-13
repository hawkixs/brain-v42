"""Bounded tool-catalog exposure profiles for the Brain MCP server."""

from __future__ import annotations

from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Annotated, Any, Literal, get_args

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.server.transforms import GetToolNext
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.utilities.versions import VersionSpec

from brain_v42.mcp.dream_capabilities import (
    dream_phase_tool_allowlist,
    resolve_dream_capability_principal,
)
from brain_v42.mcp.tool_families import FamilyAuthorizationMiddleware
from brain_v42.models.brain import KnowledgeType

type ToolCatalogProfile = Literal["compact", "native"]

SESSION_LIFECYCLE_TOOLS = (
    "brain_session_start",
    "brain_session_capture",
    "brain_session_heartbeat",
    "brain_session_end",
    "brain_session_list",
    "brain_session_resume",
    "brain_session_abandon",
    "brain_session_bind",
    "brain_session_relay",
)

_TOOL_PROFILE_HEADER = "x-brain-tool-profile"

# Recall is an unambiguous request to search memory.
TOOL_NAME_ALIASES = {"brain_recall": "brain_search"}
# Typed getters preserve the requested entity type without duplicating the catalogue.
TOOL_NAME_ALIASES.update({f"brain_get_{kind}": "brain_get" for kind in get_args(KnowledgeType)})


class _RequestAwareBM25SearchTransform(BM25SearchTransform):
    """Render a native catalog on request while keeping compact as the default."""

    def __init__(self, *, mcp: FastMCP, compact_by_default: bool, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._mcp = mcp
        self._compact_by_default = compact_by_default

    def _family_authorizer(self) -> FamilyAuthorizationMiddleware | None:
        # HTTP planning installs this only in credentials mode, after transforms
        # may have been created. Looking it up avoids a second mode/rights state.
        return next(
            (m for m in self._mcp.middleware if isinstance(m, FamilyAuthorizationMiddleware)),
            None,
        )

    def _make_call_tool(self) -> Tool:
        """Keep FastMCP's proxy schema while resolving safe names before dispatch."""
        transform = self

        async def call_tool(
            name: Annotated[str, "The name of the tool to call"],
            arguments: Annotated[dict[str, Any] | None, "Arguments to pass to the tool"] = None,
            ctx: Context = None,  # type: ignore[assignment]
        ) -> ToolResult:
            """Call a tool by name with the given arguments.

            Use this to execute tools discovered via search_tools.
            """
            if name in {transform._call_tool_name, transform._search_tool_name}:
                if name == transform._search_tool_name:
                    raise ToolError(
                        f"Gateway {transform._search_tool_name} cannot be nested; call "
                        f"{transform._search_tool_name}(query=...) directly."
                    )
                raise ToolError(
                    f"Gateway {transform._call_tool_name} cannot be nested; call "
                    f'{transform._call_tool_name}(name="<target tool>", arguments={{...}}) once.'
                )

            raw_names = {tool.name for tool in await transform._mcp._list_tools()}
            if name in raw_names:
                return await ctx.fastmcp.call_tool(name, arguments)

            available_names = {tool.name for tool in await transform._available_tools(ctx)}
            target = TOOL_NAME_ALIASES.get(name)
            if target is None or target not in available_names:
                raise ToolError(transform._unknown_tool_message(name, available_names))
            resolved_arguments = dict(arguments or {})
            if name in TOOL_NAME_ALIASES and name.startswith("brain_get_"):
                kind = name.removeprefix("brain_get_")
                passed_type = resolved_arguments.get("entity_type")
                if passed_type is not None and passed_type != kind:
                    raise ToolError(
                        f"Alias {name} requires entity_type={kind!r}, which conflicts with entity_type={passed_type!r}."
                    )
                resolved_arguments["entity_type"] = kind
            return await ctx.fastmcp.call_tool(target, resolved_arguments)

        return Tool.from_function(fn=call_tool, name=self._call_tool_name)

    async def _available_tools(self, ctx: Context) -> Sequence[Tool]:
        return await self.get_tool_catalog(ctx)

    @staticmethod
    def _unknown_tool_message(name: str, available_names: set[str]) -> str:
        stop_words = {"brain", "get", "list"}

        def normalized_tokens(value: str) -> set[str]:
            return {
                token[:-1] if token.endswith("s") and len(token) > 1 else token
                for token in value.split("_")
                if token not in stop_words
            }

        tokens = normalized_tokens(name)
        prefix = "brain_"
        if name.startswith(prefix):
            query = name.removeprefix(prefix)
            candidate_names = {
                candidate: candidate.removeprefix(prefix)
                if candidate.startswith(prefix)
                else candidate
                for candidate in available_names
            }
        else:
            query = name
            candidate_names = {candidate: candidate for candidate in available_names}
        ratios = {
            candidate: SequenceMatcher(None, query, candidate_name).ratio()
            for candidate, candidate_name in candidate_names.items()
        }
        strong = sorted(
            (candidate for candidate, ratio in ratios.items() if ratio >= 0.85),
            key=lambda candidate: (-ratios[candidate], candidate),
        )
        overlap = sorted(
            (
                candidate
                for candidate in available_names
                if tokens.intersection(normalized_tokens(candidate))
            ),
            key=lambda candidate: (
                -len(tokens.intersection(normalized_tokens(candidate))),
                candidate,
            ),
        )
        weak = sorted(
            (candidate for candidate, ratio in ratios.items() if 0.6 <= ratio < 0.85),
            key=lambda candidate: (-ratios[candidate], candidate),
        )
        suggestions = list(dict.fromkeys(strong + overlap + weak))[:3]
        message = f"Unknown tool {name!r}."
        if suggestions:
            message += f" Closest: {', '.join(suggestions)}."
        message += " Use brain_find_tool(query=...) to search the catalogue."
        if "project" in name:
            message += (
                " Project focus: the brain_session_start briefing, or "
                "brain_focus_history(project_key=...); all projects: brain_list_projects."
            )
        return message

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        authorizer = self._family_authorizer()
        if authorizer is not None:
            tools = await authorizer.filter_tools(tools)
            if not tools:
                return ()
            if (
                not self._compact_by_default
                or get_http_headers().get(_TOOL_PROFILE_HEADER) == "native"
            ):
                return tools
            return await super().transform_tools(tools)
        principal = resolve_dream_capability_principal(get_access_token())
        if principal.kind == "scoped" and principal.phase is not None:
            allowed = frozenset(dream_phase_tool_allowlist(principal.phase))
            return tuple(tool for tool in tools if tool.name in allowed)
        if principal.kind == "invalid":
            return ()
        if not self._compact_by_default or get_http_headers().get(_TOOL_PROFILE_HEADER) == "native":
            return tools
        return await super().transform_tools(tools)

    async def get_tool(
        self,
        name: str,
        call_next: GetToolNext,
        *,
        version: VersionSpec | None = None,
    ) -> Tool | None:
        """Expose compact gateways only to unrestricted compact principals."""
        authorizer = self._family_authorizer()
        if authorizer is not None:
            if not await authorizer.allows_current_tool(name):
                return None
            if not self._compact_by_default:
                return await call_next(name, version=version)
            return await super().get_tool(name, call_next, version=version)
        principal = resolve_dream_capability_principal(get_access_token())
        if not self._compact_by_default or principal.kind in {"scoped", "invalid"}:
            return await call_next(name, version=version)
        return await super().get_tool(name, call_next, version=version)


def apply_tool_catalog_profile(
    mcp: FastMCP,
    profile: ToolCatalogProfile,
) -> FastMCP:
    """Apply the selected tool exposure profile and return the same server."""
    mcp.add_transform(
        _RequestAwareBM25SearchTransform(
            mcp=mcp,
            compact_by_default=profile == "compact",
            max_results=5,
            always_visible=list(SESSION_LIFECYCLE_TOOLS),
            search_tool_name="brain_find_tool",
            call_tool_name="brain_call_tool",
        )
    )
    return mcp
