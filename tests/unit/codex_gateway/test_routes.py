"""JSON route contract for Codex management operations."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest

from brain_v42.services.feature_service import FeatureStateConflictError
from brain_v42.services.proposal_service import (
    ProposalApplyError,
    ProposalNotFoundError,
    ProposalNotProposedError,
    ProposalStateConflictError,
)
from tests.unit.codex_gateway._support import GATEWAY_TOKEN, build_gateway_fixture

_AUTH = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}


async def _request(app: Any, method: str, path: str, payload: dict[str, Any] | None = None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        return await client.request(method, path, json=payload, headers=_AUTH)


@pytest.mark.asyncio
async def test_ticket_create_reply_and_legal_transition_use_ticket_service() -> None:
    fixture = build_gateway_fixture()
    created = await _request(
        fixture.app,
        "POST",
        "/api/tickets",
        {
            "kind": "request",
            "title": "Gateway",
            "body": "Ship it",
            "from_project": "red-codex",
            "to_project": "brain-v42",
        },
    )
    ticket_id = created.json()["id"]

    replied = await _request(
        fixture.app,
        "POST",
        f"/api/tickets/{ticket_id}/reply",
        {"actor_project": "red-codex", "body": "Any update?"},
    )
    transitioned = await _request(
        fixture.app,
        "POST",
        f"/api/tickets/{ticket_id}/transition",
        {"actor_project": "brain-v42", "action": "start", "message": "Starting"},
    )

    assert created.status_code == 201
    assert created.json()["status"] == "open"
    assert replied.status_code == 200
    assert replied.json()["author_project"] == "red-codex"
    assert transitioned.status_code == 200
    assert transitioned.json()["status"] == "in_progress"


@pytest.mark.asyncio
async def test_illegal_ticket_transition_returns_runtime_allowed_actions() -> None:
    fixture = build_gateway_fixture()
    ticket = await fixture.seed_ticket()

    response = await _request(
        fixture.app,
        "POST",
        f"/api/tickets/{ticket.id}/transition",
        {"actor_project": "red-codex", "action": "confirm"},
    )

    assert response.status_code == 409
    assert response.json()["allowed_actions"] == ["cancel", "resolve", "start", "wontfix"]


@pytest.mark.asyncio
async def test_ticket_errors_map_to_404_and_422() -> None:
    fixture = build_gateway_fixture()
    unknown = uuid4()

    not_found = await _request(
        fixture.app,
        "POST",
        f"/api/tickets/{unknown}/reply",
        {"actor_project": "red-codex", "body": "hello"},
    )
    invalid_uuid = await _request(
        fixture.app,
        "POST",
        "/api/tickets/not-a-uuid/reply",
        {"actor_project": "red-codex", "body": "hello"},
    )
    unknown_project = await _request(
        fixture.app,
        "POST",
        "/api/tickets",
        {
            "kind": "request",
            "title": "Typo",
            "body": "No phantom project",
            "from_project": "red-codex",
            "to_project": "unknown-project",
        },
    )

    assert not_found.status_code == 404
    assert invalid_uuid.status_code == 422
    assert unknown_project.status_code == 422


@pytest.mark.asyncio
async def test_learning_entity_feature_and_killswitch_routes_return_json() -> None:
    fixture = build_gateway_fixture()

    learning = await _request(fixture.app, "POST", f"/api/learnings/{fixture.learning_id}/validate")
    entity = await _request(
        fixture.app,
        "POST",
        f"/api/entities/learning/{fixture.entity_id}/refresh",
    )
    feature = await _request(
        fixture.app,
        "PATCH",
        f"/api/features/{fixture.feature_id}",
        {"status": "building", "pinned": False},
    )
    killswitches = await _request(fixture.app, "GET", "/api/killswitches")

    assert learning.status_code == 200
    assert learning.json()["id"] == str(fixture.learning_id)
    assert entity.status_code == 200
    assert entity.json()["freshness_status"] == "fresh"
    assert feature.status_code == 200
    assert fixture.feature_service.last_patch == {
        "status": "building",
        "pinned": False,
        "archived": None,
    }
    assert killswitches.status_code == 200
    assert killswitches.json()["extract_dry"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_family", "expected_status"),
    [
        ("/api/proposals/ticket-extraction/11/apply", "ticket-extraction", "applied"),
        ("/api/proposals/ticket-extraction/11/reject", "ticket-extraction", "rejected"),
        ("/api/proposals/roadmap-curation/12/apply", "roadmap-curation", "applied"),
        ("/api/proposals/roadmap-curation/12/reject", "roadmap-curation", "rejected"),
    ],
)
async def test_proposal_routes_dispatch_one_id(
    path: str,
    expected_family: str,
    expected_status: str,
) -> None:
    fixture = build_gateway_fixture()

    response = await _request(fixture.app, "POST", path)

    assert response.status_code == 200
    assert response.json()["family"] == expected_family
    assert response.json()["status"] == expected_status


class _FailingProposalService:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def apply_ticket_extraction(
        self,
        proposal_id: int,
        *,
        project_group: str | None = None,
    ):
        assert project_group == "red"
        raise self._error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (
            ProposalNotFoundError(
                "missing",
                family="ticket-extraction",
                proposal_id=99,
                operation="apply",
            ),
            404,
        ),
        (
            ProposalNotProposedError(
                "already applied",
                family="ticket-extraction",
                proposal_id=99,
                operation="apply",
                status="applied",
            ),
            409,
        ),
        (
            ProposalStateConflictError(
                "feature is no longer live",
                family="roadmap-curation",
                proposal_id=99,
                operation="merge",
            ),
            409,
        ),
        (
            ProposalApplyError(
                "database detail must stay private",
                family="ticket-extraction",
                proposal_id=99,
                operation="apply",
            ),
            500,
        ),
    ],
)
async def test_proposal_service_errors_are_mapped(error: Exception, expected_status: int) -> None:
    fixture = build_gateway_fixture(proposal_service=_FailingProposalService(error))

    response = await _request(
        fixture.app,
        "POST",
        "/api/proposals/ticket-extraction/99/apply",
    )

    assert response.status_code == expected_status
    if expected_status == 500:
        assert "database detail" not in response.text


@pytest.mark.asyncio
async def test_management_routes_return_404_for_unknown_ids() -> None:
    fixture = build_gateway_fixture()
    unknown = uuid4()

    responses = [
        await _request(fixture.app, "POST", f"/api/learnings/{unknown}/validate"),
        await _request(fixture.app, "POST", f"/api/entities/learning/{unknown}/refresh"),
        await _request(
            fixture.app,
            "PATCH",
            f"/api/features/{unknown}",
            {"pinned": True},
        ),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]


class _ConflictingFeatureService:
    async def patch(self, *args: Any, **kwargs: Any) -> None:
        raise FeatureStateConflictError("merged feature cannot be reactivated")


@pytest.mark.asyncio
async def test_feature_state_conflict_maps_to_409() -> None:
    fixture = build_gateway_fixture(feature_service=_ConflictingFeatureService())

    response = await _request(
        fixture.app,
        "PATCH",
        f"/api/features/{uuid4()}",
        {"status": "building"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Feature state changed; review required"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/entities/feature/00000000-0000-0000-0000-000000000000/refresh", None),
        ("/api/features/00000000-0000-0000-0000-000000000000", {}),
        (
            "/api/features/00000000-0000-0000-0000-000000000000",
            {"status": "in_progress"},
        ),
        (
            "/api/features/00000000-0000-0000-0000-000000000000",
            {"status": "building", "archived": True},
        ),
    ],
)
async def test_management_payload_validation_returns_422(
    path: str,
    payload: dict[str, Any] | None,
) -> None:
    fixture = build_gateway_fixture()
    method = "PATCH" if path.startswith("/api/features") else "POST"

    response = await _request(fixture.app, method, path, payload)

    assert response.status_code == 422


class _FailingLearningService:
    async def validate(self, learning_id, *, project_group=None):
        assert project_group == "red"
        raise RuntimeError("private database detail")


@pytest.mark.asyncio
async def test_unexpected_failures_are_masked_as_json() -> None:
    fixture = build_gateway_fixture(learning_service=_FailingLearningService())
    transport = httpx.ASGITransport(app=fixture.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        response = await client.post(
            f"/api/learnings/{fixture.learning_id}/validate",
            headers=_AUTH,
        )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Internal Server Error"}
    assert "private database detail" not in response.text
