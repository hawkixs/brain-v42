"""Delivery-only transport protection before FastMCP validation/error logging."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult
from fastmcp.tools.function_tool import FunctionTool
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from brain_v42.models.delivery import DeliveryError


class _DeliveryFunctionTool(FunctionTool):
    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Sanitize before FastMCP logs validation input or exception tracebacks.

        A missing argument's Pydantic error includes the complete input mapping,
        even when a separate claim_token argument is SecretStr. Catching inside
        the function body would be too late for that validation error.
        """
        try:
            return await super().run(arguments)
        except ValidationError:
            raise ToolError("invalid_arguments: delivery arguments are invalid") from None
        except ToolError:
            raise
        except DeliveryError as exc:
            # Also safe before the server's idempotent transport prelude runs.
            raise ToolError(str(exc)) from None
        except Exception:
            raise ToolError("delivery_unavailable: delivery operation could not complete") from None


class _DeliveryRegistry:
    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp

    def tool(
        self, *, version: str, annotations: ToolAnnotations
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Keep native schemas/registration while protecting the validation boundary."""

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._mcp.add_tool(
                _DeliveryFunctionTool.from_function(
                    fn, version=version, annotations=annotations, tags={"delivery"}
                )
            )
            return fn

        return register
