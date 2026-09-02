"""Instrument the registered tools, without mutating ``mcp.tool`` (ticket c352eaaa).

The historical wiring replaced ``FastMCP.tool`` with a wrapper at registration
time (``brain_tools.register_tools``). Three defects, all named by spec §1:
coupling to a ``metrics_collector`` passed to ONE particular registration
function, dependence on declaration order (a tool registered before the patch
was not counted), and mutation of a third-party object's method.

Wrapping the tools AFTER registration removes all three. Same mechanism as
``brain_v42.mcp.business_errors``: ``FunctionTool.run`` reads ``self.fn`` on
every call, so replacing ``fn`` with a signature-transparent wrapper
(``functools.wraps``) instruments the tool without touching the published
schema.

Why not an ``on_call_tool`` middleware
--------------------------------------
The ticket proposed a middleware, the Q2 re-measurement having shown that it
also sees the real tool name behind the compact gateway. That is true, but
insufficient, and MEASURED: ``FastMCP.call_tool`` applies the middlewares THEN
re-enters ``call_tool(run_middleware=False)``, which carries the masking
``try/except``. A middleware therefore sits above the masking and receives only
the generic ``ToolError`` — the ticket's constraint 2 ("preserve EXACTLY the
AuthorizationError capture") would be met by luck (``AuthorizationError`` is a
``FastMCPError``, re-raised as is) but the ``exception_type`` log would
degenerate into "ToolError" for every other failure, losing the diagnosis it
exists to give. Wrapping the function sees the exception BEFORE masking.

Constraint 1 ("ignore gateway names, otherwise double counting") is satisfied BY
CONSTRUCTION, with no denylist to maintain: ``_list_tools()`` returns the raw
registry, where ``brain_call_tool`` and ``brain_find_tool`` do not exist — they
live only in ``list_tools()``'s transformed view. A future rename of the
gateways therefore cannot reopen the double counting.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.instrument import instrument_tool

_INSTRUMENTED_MARKER = "__brain_metrics_instrumented__"


def _wrap(fn: Any, collector: MetricsCollector, tool_name: str) -> Any:
    """Apply ``instrument_tool`` and mark the function as already processed.

    Only the POINT OF APPLICATION changes: the decorator itself is reused as is,
    which literally satisfies the ticket's constraint 2 ("preserve EXACTLY the
    AuthorizationError capture and the latency measurement") rather than
    re-implementing it identically and hoping.
    """
    instrumented = instrument_tool(collector, tool_name)(fn)
    setattr(instrumented, _INSTRUMENTED_MARKER, True)
    return instrumented


async def instrument_registered_tools(
    mcp: FastMCP,
    collector: MetricsCollector,
) -> tuple[str, ...]:
    """Instrument every registered tool and return the names actually processed.

    Idempotent: an already instrumented tool is left as is, so a double call
    does not stack wrappers and does not double the counters.
    """
    instrumented: list[str] = []
    for tool in await mcp._list_tools():
        if not isinstance(tool, FunctionTool) or getattr(tool.fn, _INSTRUMENTED_MARKER, False):
            continue
        tool.fn = _wrap(tool.fn, collector, tool.name)
        instrumented.append(tool.name)
    return tuple(instrumented)
