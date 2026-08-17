"""Unit tests for BearerTokenGuard ASGI middleware (optional bearer auth).

Tests run entirely in-process via httpx.ASGITransport against a trivial dummy
inner ASGI app — no real socket, no FastMCP instance needed.

TDD Red phase: these tests MUST fail before BearerTokenGuard is implemented.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.types import ASGIApp, Receive, Scope, Send

from brain_v42.mcp.http_security import BearerTokenGuard

# ---------------------------------------------------------------------------
# Dummy inner ASGI app
# ---------------------------------------------------------------------------


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Trivial ASGI app that always returns 200 OK."""
    if scope["type"] == "http":
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"2")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _client(token: str, inner: ASGIApp = _ok_app) -> httpx.AsyncClient:
    """Build an httpx async client that drives inner via BearerTokenGuard."""
    guarded: ASGIApp = BearerTokenGuard(inner, token=token)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=guarded),
        base_url="http://testserver",
    )


# ---------------------------------------------------------------------------
# Token configured — auth enforcement active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_correct_token_passes() -> None:
    """Correct Authorization: Bearer <token> must be allowed."""
    async with _client("mysecret") as c:
        resp = await c.get("/", headers={"Host": "127.0.0.1", "Authorization": "Bearer mysecret"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_bearer_missing_header_returns_401() -> None:
    """No Authorization header when token is configured must return 401."""
    async with _client("mysecret") as c:
        resp = await c.get("/", headers={"Host": "127.0.0.1"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_wrong_token_returns_401() -> None:
    """Wrong token must return 401, not 403 (uniform rejection)."""
    async with _client("mysecret") as c:
        resp = await c.get("/", headers={"Host": "127.0.0.1", "Authorization": "Bearer wrongtoken"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_wrong_scheme_returns_401() -> None:
    """Authorization with wrong scheme (e.g. Basic) must return 401."""
    async with _client("mysecret") as c:
        resp = await c.get(
            "/", headers={"Host": "127.0.0.1", "Authorization": "Basic dXNlcjpwYXNz"}
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_401_includes_www_authenticate() -> None:
    """401 response must include WWW-Authenticate: Bearer header."""
    async with _client("mysecret") as c:
        resp = await c.get("/", headers={"Host": "127.0.0.1"})
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_bearer_health_exempt_without_token() -> None:
    """/health must be exempt from bearer auth (watchdog / red-monitor)."""
    async with _client("mysecret") as c:
        resp = await c.get("/health", headers={"Host": "127.0.0.1"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_bearer_health_also_passes_with_token() -> None:
    """/health with a valid token must also pass (belt-and-suspenders)."""
    async with _client("mysecret") as c:
        resp = await c.get(
            "/health",
            headers={"Host": "127.0.0.1", "Authorization": "Bearer mysecret"},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Token NOT configured — guard is transparent (fleet non-regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_token_configured_passes_without_auth() -> None:
    """Empty token = disabled mode: any request passes through unchanged."""
    async with _client("") as c:
        resp = await c.get("/", headers={"Host": "127.0.0.1"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_no_token_configured_passes_with_any_bearer() -> None:
    """Empty token = disabled mode: even a gibberish bearer passes through."""
    async with _client("") as c:
        resp = await c.get("/", headers={"Host": "127.0.0.1", "Authorization": "Bearer anything"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Non-HTTP scopes — must be delegated unchanged (same as HostOriginGuard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_http_scope_passes_through() -> None:
    """Non-http scopes (lifespan) must not be filtered by BearerTokenGuard."""
    guard = BearerTokenGuard(_ok_app, token="secret")
    received: list[dict] = []

    async def _recv() -> dict:
        return {}

    async def _send(msg: object) -> None:
        received.append(msg)  # type: ignore[arg-type]

    # lifespan scope — guard should delegate without crashing
    await guard({"type": "lifespan"}, _recv, _send)


# ---------------------------------------------------------------------------
# Constant-time comparison — timing-safe behaviour is structural
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_token_is_case_sensitive() -> None:
    """Token comparison is case-sensitive: 'Secret' != 'secret'."""
    async with _client("secret") as c:
        resp = await c.get("/", headers={"Host": "127.0.0.1", "Authorization": "Bearer Secret"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Non-ASCII Authorization value — must be a 401, never a TypeError/500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_non_ascii_authorization_returns_401_not_500() -> None:
    """Latin-1 obs-text in the Authorization value is legal HTTP/1.1 content.

    str-based hmac.compare_digest raises TypeError on non-ASCII operands,
    which would turn a wrong token into an unhandled 500 on the security
    boundary. The guard must compare bytes and answer a plain 401.
    Driven via a raw ASGI scope: httpx refuses to send non-ASCII header
    values, but a hostile client on the wire can.
    """
    guard = BearerTokenGuard(_ok_app, token="secret")
    sent: list[dict] = []

    async def _send(message: dict) -> None:
        sent.append(message)

    async def _receive() -> dict:
        return {"type": "http.request"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp",
        "headers": [(b"authorization", "Bearer \xe9secret".encode("latin-1"))],
    }
    await guard(scope, _receive, _send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401
