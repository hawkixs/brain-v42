"""Fact-only MCP registration helper kept apart from the delivery transport boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from mcp.types import ToolAnnotations


class _FactsRegistry:
    """Register facts with their own tag, not the delivery-specific transport class."""

    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp

    def tool(
        self, *, version: str, annotations: ToolAnnotations
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Preserve FastMCP's native schema while marking the closed facts catalogue."""

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._mcp.add_tool(
                FunctionTool.from_function(
                    fn, version=version, annotations=annotations, tags={"facts"}
                )
            )
            return fn

        return register
