"""Let the expected business errors through ``mask_error_details``.

The server is built with ``mask_error_details=True``: any exception escaping a
tool is flattened into ``Error calling tool 'X'``. That is the right default for
internal failures (DSNs, paths, traces), but it also destroys the text of the
*fail-closed* guards, which are written precisely to be actionable. Measured in
production on 2026-08-06 (brain ticket 40ab2ced):

    brain_learn(project_key="projet-qui-nexiste-pas", ...)
    serveur : Unknown project 'projet-qui-nexiste-pas' — create it first
              (brain_set_project_context) or check the key (brain_list_projects)
    client  : Error calling tool 'brain_learn'

The caller learns neither that the key is the problem, nor which one they
passed, nor what to do — so they retry identically, or give up on capitalizing.

``format_error`` (``mcp.tools.formatters``) is the already sanctioned channel,
but it covers only the failures a tool catches *explicitly*. Guards such as
``require_known_project`` raise from deep inside a service and escape without
being converted; the session lifecycle does the same. This module closes that
hole at a single point, for every registered tool.

Why not a middleware
--------------------
MEASURED, not assumed: ``FastMCP.call_tool`` applies the middleware chain then
re-enters ``call_tool(run_middleware=False)``, and it is that internal pass which
carries the masking ``try/except``. An ``on_call_tool`` is therefore structurally
ABOVE the masking: it never sees the raw exception, only the generic ``ToolError``
already produced. Interception must happen inside ``tool._run``, hence inside the
tool function itself.

``FunctionTool.run`` reads ``self.fn`` on every call and derives its type adapter
from it, so wrapping ``fn`` with ``functools.wraps`` preserves the published input
schema (guaranteed by ``test_wrapping_preserves_tool_input_schema``).

Explicit choice: ALLOWLIST, not pass-through
--------------------------------------------
Surfacing an arbitrary exception message would disclose paths, connection
identifiers or internal structure. Only the families below pass through, chosen
because their message is (a) written for the caller and (b) free of internals.
Everything else stays masked — the default is refusal.

Deliberate exclusions:

- ``PlanScanPathError`` — describes a filesystem scan refusal.
- The ops/admin errors (``LegacyGraphImportBlocked``, ``RepairSafetyError``,
  ``AutomationCleanupError``, ``OwnershipLostError``) — never on an LLM tool
  path, and their text describes internals.

Out of scope because FastMCP already handles them: every subclass of
``FastMCPError`` passes through intact by construction (the
``except FastMCPError: raise`` branch). That is the case for
``DreamProjectAuthorizationError``, which derives from ``AuthorizationError`` and
deliberately carries a generic message, the precise reason staying in an
attribute — a detailed authorization refusal would be an enumeration oracle.
"""

from __future__ import annotations

import functools
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool

from brain_v42.models.brain_session import BrainSessionError
from brain_v42.services.consolidation import ConsolidationEntityNotFoundError
from brain_v42.services.entity_maintenance_service import UnknownEntityTypeError
from brain_v42.services.feature_creation_service import FeatureCreationError
from brain_v42.services.feature_service import FeatureStateConflictError
from brain_v42.services.proposal_service import ProposalServiceError
from brain_v42.services.roadmap_service import ProjectFocusError
from brain_v42.services.ticket_service import TicketError

#: Exception families whose message is meant for the caller. Adding an entry is
#: a disclosure decision: check that the text can contain neither a path, nor a
#: DSN, nor a schema detail.
SURFACED_BUSINESS_ERRORS: tuple[type[Exception], ...] = (
    TicketError,
    BrainSessionError,
    FeatureCreationError,
    FeatureStateConflictError,
    ProposalServiceError,
    ProjectFocusError,
    UnknownEntityTypeError,
    ConsolidationEntityNotFoundError,
)

_SURFACED_MARKER = "__brain_business_errors_surfaced__"


def _wrap(fn: Any) -> Any:
    """Convert the allowlisted families into a readable ``ToolError``."""

    @functools.wraps(fn)
    async def surfaced(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except SURFACED_BUSINESS_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    setattr(surfaced, _SURFACED_MARKER, True)
    return surfaced


async def surface_business_errors(mcp: FastMCP) -> tuple[str, ...]:
    """Wrap every registered tool and return the names actually processed.

    Idempotent: an already wrapped tool is left as is, so a double call (or a
    re-registration) does not stack layers.

    ``_list_tools()`` is used rather than ``list_tools()`` because it returns the
    raw tools without going through the catalogue transforms — MEASURED: under
    the ``compact`` profile, ``list_tools()`` would expose only the gateways and
    leave nearly every real tool unconverted.

    Tools that are not ``FunctionTool``s expose no Python function to wrap and
    are ignored.
    """
    wrapped: list[str] = []
    for tool in await mcp._list_tools():
        if not isinstance(tool, FunctionTool) or getattr(tool.fn, _SURFACED_MARKER, False):
            continue
        tool.fn = _wrap(tool.fn)
        wrapped.append(tool.name)
    return tuple(wrapped)
