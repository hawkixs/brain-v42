"""The production server must surface business errors on every registered tool.

The whitelist in ``brain_v42.mcp.business_errors`` is inert unless something
applies it. Ticket 40ab2ced was a *production* defect, so the contract worth
pinning is the wiring, not just the helper: a tool registered tomorrow must be
covered without anyone remembering to decorate it.

``_run_mcp`` is the seam — the same one ``test_http_transport_routing`` uses —
because it is the single async choke point both transports pass through.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from brain_v42.services.ticket_service import UnknownProjectError

_MESSAGE = "Unknown project 'projet-qui-nexiste-pas' — create it first (brain_set_project_context)"


def test_run_mcp_surfaces_business_errors_on_registered_tools(monkeypatch: Any) -> None:
    """Booting the stdio transport must leave every tool business-error aware."""
    from brain_v42.config import Settings
    from brain_v42.mcp import server

    app = FastMCP("wiring", mask_error_details=True)

    @app.tool()
    async def registered_before_boot() -> str:
        raise UnknownProjectError(_MESSAGE)

    async def fake_run_async(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(app, "run_async", fake_run_async)
    settings = Settings(postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain")
    monkeypatch.setattr(settings, "brain_mcp_transport", "stdio", raising=False)

    async def scenario() -> str:
        await server._run_mcp(app, settings)
        async with Client(app) as client:
            try:
                await client.call_tool("registered_before_boot", {})
            except ToolError as exc:
                return str(exc)
        return ""

    surfaced = asyncio.run(scenario())

    assert "projet-qui-nexiste-pas" in surfaced, (
        f"_run_mcp did not wire business-error surfacing; caller saw {surfaced!r}"
    )
