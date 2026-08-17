"""Compatibility adapter for unit tests that call MCP functions directly."""

from __future__ import annotations

from functools import wraps
from typing import Any

from fastmcp.exceptions import ToolError


def capture_tool_errors(function: Any) -> Any:
    """Expose rich business-error text to focused direct-call unit tests.

    The real MCP transport contract remains covered through an unwrapped
    ``FastMCP`` client in ``test_business_error_contract.py``.
    """

    @wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await function(*args, **kwargs)
        except ToolError as exc:
            return str(exc)

    return wrapped
