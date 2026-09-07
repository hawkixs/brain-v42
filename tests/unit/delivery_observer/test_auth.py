"""Dedicated machine authentication without reading operator credentials."""

from __future__ import annotations

import asyncio
import os
import traceback
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import DeliveryError

from .test_transport import _transport


def _provider(settings, transport, **kwargs):
    from brain_v42.delivery_observer.auth import GitHubAuthProvider

    return GitHubAuthProvider(settings, transport, **kwargs)


def _private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


async def test_dedicated_pat_file_is_used_without_contacting_token_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "unrelated-interactive-token")
    path = _private(
        tmp_path / "observer.env", "BRAIN_DELIVERY_GITHUB_TOKEN=dedicated-fixture-token\n"
    )
    settings = DeliverySettings(observer_env_path=path)
    seen = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: seen.append(req))
    ) as http:
        auth = _provider(settings, _transport(http, settings=settings))
        headers = await auth.authorization_headers()
    assert headers["Authorization"] == "Bearer dedicated-fixture-token"
    assert seen == []
    assert "dedicated-fixture-token" not in repr(auth)


@pytest.mark.parametrize(
    "condition",
    [
        "missing",
        "mode",
        "symlink",
        "directory",
        "owner",
        "oversized",
        "duplicate",
        "empty",
        "malformed",
    ],
)
async def test_private_pat_file_errors_fail_closed_and_redacted(tmp_path, monkeypatch, condition):
    path = _private(tmp_path / "observer.env", "BRAIN_DELIVERY_GITHUB_TOKEN=private-file-marker\n")
    if condition == "missing":
        path.unlink()
    elif condition == "mode":
        path.chmod(0o644)
    elif condition == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    elif condition == "directory":
        path.unlink()
        path.mkdir()
    elif condition == "owner":
        actual_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: actual_uid + 1)
    elif condition == "oversized":
        path.write_text("x" * 65537)
    elif condition == "duplicate":
        path.write_text("BRAIN_DELIVERY_GITHUB_TOKEN=a\nBRAIN_DELIVERY_GITHUB_TOKEN=b\n")
    elif condition == "empty":
        path.write_text("BRAIN_DELIVERY_GITHUB_TOKEN=\n")
    elif condition == "malformed":
        path.write_text("GH_TOKEN=private-file-marker\n")
    settings = DeliverySettings(observer_env_path=path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as http:
        with pytest.raises(DeliveryError) as caught:
            await _provider(settings, _transport(http)).authorization_headers()
    assert "private-file-marker" not in "".join(traceback.format_exception(caught.value))


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def test_app_jwt_is_signed_and_installation_refresh_is_single_flight(tmp_path, rsa_key):
    key_path = _private(
        tmp_path / "app.pem",
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )
    settings = DeliverySettings(
        github_app_id=123, github_installation_id=456, github_private_key_path=key_path
    )
    instant = 1700000000.0
    requests = []

    async def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/app/installations/456/access_tokens"
        token = request.headers["Authorization"].removeprefix("Bearer ")
        payload = jwt.decode(
            token,
            rsa_key.public_key(),
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert payload["iss"] == "123"
        assert payload["iat"] == int(instant) - 60
        assert payload["exp"] == int(instant) + 540
        await asyncio.sleep(0.01)
        return httpx.Response(
            201,
            json={
                "token": f"installation-fixture-{len(requests)}",
                "expires_at": datetime.fromtimestamp(instant + 3600, UTC).isoformat(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = _provider(settings, _transport(http), now=lambda: instant)
        results = await asyncio.gather(*(auth.authorization_headers() for _ in range(5)))
        assert len(requests) == 1
        assert all(result["Authorization"] == "Bearer installation-fixture-1" for result in results)
        instant += 3541
        assert (await auth.authorization_headers())[
            "Authorization"
        ] == "Bearer installation-fixture-2"
        assert len(requests) == 2


async def test_auth_exchange_and_data_share_one_budget_without_recursive_deadlock(
    tmp_path, rsa_key
):
    key_path = _private(
        tmp_path / "app.pem",
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )
    settings = DeliverySettings(
        github_app_id=123,
        github_installation_id=456,
        github_private_key_path=key_path,
        request_budget_per_minute=1,
        max_concurrent_requests=1,
    )
    instant = 0.0
    starts = []

    async def sleep(delay):
        nonlocal instant
        instant += delay
        await asyncio.sleep(0)

    def handler(request):
        starts.append(instant)
        return (
            httpx.Response(
                201, json={"token": "installation-fixture", "expires_at": "2026-01-01T01:00:00Z"}
            )
            if request.method == "POST"
            else httpx.Response(200, json={"ok": True})
        )

    now = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transport = _transport(http, settings=settings, monotonic=lambda: instant, sleep=sleep)
        auth = _provider(settings, transport, now=lambda: now)
        headers = await asyncio.wait_for(auth.authorization_headers(), 1)
        assert await asyncio.wait_for(
            transport.request_json("GET", "/data", headers=headers), 1
        ) == {"ok": True}
    assert starts == [0.0, 60.0]


@pytest.mark.parametrize(
    "response",
    [
        {"token": "fixture"},
        {"token": "fixture", "expires_at": "invalid"},
        {"token": "", "expires_at": "2026-01-01T01:00:00Z"},
        {"token": "fixture", "expires_at": "2020-01-01T00:00:00Z"},
    ],
)
async def test_app_rejects_invalid_installation_token_response(tmp_path, rsa_key, response):
    path = _private(
        tmp_path / "app.pem",
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )
    settings = DeliverySettings(
        github_app_id=1, github_installation_id=2, github_private_key_path=path
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(201, json=response))
    ) as http:
        with pytest.raises(DeliveryError, match="provider_invalid_response"):
            await _provider(settings, _transport(http)).authorization_headers()


async def test_pat_file_rejects_symlinked_parent_directory(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    _private(actual / "observer.env", "BRAIN_DELIVERY_GITHUB_TOKEN=fixture\n")
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    settings = DeliverySettings(observer_env_path=alias / "observer.env")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as http:
        with pytest.raises(DeliveryError, match="provider_forbidden"):
            await _provider(settings, _transport(http)).authorization_headers()
