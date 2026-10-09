"""The hook's principal grants elevations without replacing the MCP actor."""

from typing import Any

import httpx

from brain_v42.provenance import (
    get_current_actor,
    get_current_principal,
    set_current_actor,
    set_current_principal,
)
from tests.unit.mcp.test_admin_elevations import SESSION_ID, TOKEN, counters, route  # noqa: F401


async def test_hook_request_does_not_reinterpret_actor_or_attach_to_mcp_session(route: Any) -> None:  # noqa: F811
    actor, principal = get_current_actor(), get_current_principal()
    set_current_actor("operator-actor")
    set_current_principal("workstation-claude")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(route[0]), base_url="http://localhost"
        ) as client:
            response = await client.post(
                "/admin/elevations",
                headers={
                    "Authorization": "Bearer " + TOKEN,
                    "X-Brain-Agent": "untrusted-declaration",
                    "Mcp-Session-Id": "unrelated-connection",
                },
                json={"session_id": str(SESSION_ID), "reason": "maintenance"},
            )
        assert response.status_code == 200
        assert route[1].calls[0][1]["requested_by_client_id"] == "operator-hook"
        assert get_current_actor() == "operator-actor"
        assert get_current_principal() == "workstation-claude"
        assert route[2].verify.await_count == 1
    finally:
        set_current_actor(actor)
        set_current_principal(principal)
