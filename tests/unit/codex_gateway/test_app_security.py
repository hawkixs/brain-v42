"""Liveness and Bearer-boundary behavior for the Codex gateway."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from brain_v42.codex_gateway.auth import BearerAuthenticator
from tests.unit.codex_gateway._support import GATEWAY_TOKEN, build_gateway_fixture


@pytest.mark.asyncio
async def test_health_is_public_while_every_api_route_requires_bearer() -> None:
    fixture = build_gateway_fixture()
    transport = httpx.ASGITransport(app=fixture.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        health = await client.get("/health")
        ready = await client.get("/ready")
        unauthorized = await client.get("/api/killswitches")
        authorized = await client.get(
            "/api/killswitches",
            headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"},
        )

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failure", "timeout"])
async def test_readiness_reports_dependency_failures_without_breaking_liveness(mode: str) -> None:
    async def readiness() -> None:
        if mode == "failure":
            raise RuntimeError("database unavailable")
        await asyncio.Event().wait()

    fixture = build_gateway_fixture(
        readiness=readiness,
        readiness_timeout_s=0.01,
    )
    transport = httpx.ASGITransport(app=fixture.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        ready = await client.get("/ready")
        health = await client.get("/health")

    assert ready.status_code == 503
    assert ready.json() == {"status": "unavailable"}
    assert health.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer wrong",
        "Basic Z2F0ZXdheTpzZWNyZXQ=",
        "bearer gateway-secret",
    ],
)
async def test_auth_rejects_wrong_or_non_bearer_credentials(authorization: str) -> None:
    fixture = build_gateway_fixture()
    transport = httpx.ASGITransport(app=fixture.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        response = await client.get(
            "/api/killswitches",
            headers={"Authorization": authorization},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.asyncio
async def test_auth_rejects_non_ascii_bearer_without_type_error() -> None:
    authenticator = BearerAuthenticator(SecretStr(GATEWAY_TOKEN))

    with pytest.raises(HTTPException) as caught:
        await authenticator("Bearer échec-non-ascii")

    assert caught.value.status_code == 401


def test_gateway_refuses_to_start_with_an_empty_token() -> None:
    with pytest.raises(RuntimeError, match="BRAIN_CODEX_GATEWAY_TOKEN"):
        build_gateway_fixture(token="")


@pytest.mark.parametrize("token", ["x", "REPLACE_WITH_RANDOM_TOKEN"])
def test_gateway_refuses_to_start_with_a_weak_or_placeholder_token(token: str) -> None:
    with pytest.raises(RuntimeError, match="at least 32"):
        build_gateway_fixture(token=token)


@pytest.mark.asyncio
async def test_gateway_does_not_publish_interactive_docs() -> None:
    fixture = build_gateway_fixture()
    transport = httpx.ASGITransport(app=fixture.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        responses = [await client.get(path) for path in ("/docs", "/redoc", "/openapi.json")]

    assert all(response.status_code == 404 for response in responses)
