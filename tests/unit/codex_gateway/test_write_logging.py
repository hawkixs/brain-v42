"""Audit context must be emitted for every successful gateway write."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from tests.unit.codex_gateway._support import GATEWAY_TOKEN, build_gateway_fixture

_AUTH = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}


@pytest.mark.asyncio
async def test_every_write_logs_codex_origin_and_effective_actor_project() -> None:
    fixture = build_gateway_fixture()
    ticket = await fixture.seed_ticket()
    requests: list[tuple[str, str, dict[str, Any] | None]] = [
        (
            "POST",
            "/api/tickets",
            {
                "kind": "fyi",
                "title": "Notice",
                "body": "Contract changed",
                "from_project": "red-codex",
                "to_project": "brain-v42",
            },
        ),
        (
            "POST",
            f"/api/tickets/{ticket.id}/reply",
            {"actor_project": "red-codex", "body": "ping"},
        ),
        (
            "POST",
            f"/api/tickets/{ticket.id}/transition",
            {"actor_project": "brain-v42", "action": "start"},
        ),
        ("POST", f"/api/learnings/{fixture.learning_id}/validate", None),
        ("POST", f"/api/entities/learning/{fixture.entity_id}/refresh", None),
        ("PATCH", f"/api/features/{fixture.feature_id}", {"pinned": True}),
        ("POST", "/api/proposals/ticket-extraction/11/apply", None),
        ("POST", "/api/proposals/ticket-extraction/11/reject", None),
        ("POST", "/api/proposals/roadmap-curation/12/apply", None),
        ("POST", "/api/proposals/roadmap-curation/12/reject", None),
    ]
    transport = httpx.ASGITransport(app=fixture.app)

    with capture_logs() as logs:
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            responses = [
                await client.request(method, path, json=payload, headers=_AUTH)
                for method, path, payload in requests
            ]

    assert all(response.is_success for response in responses)
    writes = [entry for entry in logs if entry.get("event") == "codex_gateway.write"]
    assert [entry["operation"] for entry in writes] == [
        "ticket.create",
        "ticket.reply",
        "ticket.transition",
        "learning.validate",
        "entity.refresh",
        "feature.patch",
        "proposal.ticket-extraction.apply",
        "proposal.ticket-extraction.reject",
        "proposal.roadmap-curation.apply",
        "proposal.roadmap-curation.reject",
    ]
    assert all(entry["origin"] == "codex" for entry in writes)
    assert [entry["actor_project"] for entry in writes[:3]] == [
        "red-codex",
        "red-codex",
        "brain-v42",
    ]
    assert all(entry["actor_project"] == "red-codex" for entry in writes[3:])
