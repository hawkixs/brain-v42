"""Bounded tool-catalog exposure profiles for the Brain MCP server."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.server.transforms import GetToolNext
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.tools.base import Tool
from fastmcp.utilities.versions import VersionSpec

from brain_v42.mcp.dream_capabilities import (
    dream_phase_tool_allowlist,
    resolve_dream_capability_principal,
)

type ToolCatalogProfile = Literal["compact", "native"]

SESSION_LIFECYCLE_TOOLS = (
    "brain_session_start",
    "brain_session_capture",
    "brain_session_heartbeat",
    "brain_session_end",
    "brain_session_list",
    "brain_session_resume",
    "brain_session_abandon",
)

_TOOL_PROFILE_HEADER = "x-brain-tool-profile"


class _RequestAwareBM25SearchTransform(BM25SearchTransform):
    """Render a native catalog on request while keeping compact as the default."""

    def __init__(self, *, compact_by_default: bool, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._compact_by_default = compact_by_default

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
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
            compact_by_default=profile == "compact",
            max_results=5,
            always_visible=list(SESSION_LIFECYCLE_TOOLS),
            search_tool_name="brain_find_tool",
            call_tool_name="brain_call_tool",
        )
    )
    return mcp
