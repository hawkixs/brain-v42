"""Unit tests for HostOriginGuard ASGI middleware (DNS-rebinding protection).

Tests run entirely in-process via httpx.ASGITransport against a trivial dummy
inner ASGI app — no real socket, no FastMCP instance needed.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.types import ASGIApp, Receive, Scope, Send

from brain_v42.mcp.http_security import HostOriginGuard, _bare_host

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


def _client(inner: ASGIApp = _ok_app) -> httpx.AsyncClient:
    """Build an httpx async client that drives inner via ASGITransport."""
    guarded: ASGIApp = HostOriginGuard(inner)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=guarded),
        base_url="http://testserver",
    )


# ---------------------------------------------------------------------------
# _bare_host unit tests
# ---------------------------------------------------------------------------


def test_bare_host_plain() -> None:
    assert _bare_host("127.0.0.1") == "127.0.0.1"


def test_bare_host_with_port() -> None:
    assert _bare_host("127.0.0.1:8765") == "127.0.0.1"


def test_bare_host_ipv6_bracketed() -> None:
    assert _bare_host("[::1]:9000") == "::1"


def test_bare_host_ipv6_no_port() -> None:
    assert _bare_host("[::1]") == "::1"


def test_bare_host_localhost() -> None:
    assert _bare_host("localhost") == "localhost"


def test_bare_host_evil() -> None:
    # single-label hostname, no colon → returned as-is
    assert _bare_host("evil.com") == "evil.com"


# ---------------------------------------------------------------------------
# HostOriginGuard — HTTP scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evil_host_returns_421() -> None:
    """Non-loopback Host header must be rejected with 421."""
    async with _client() as c:
        resp = await c.get("/", headers={"Host": "evil.com"})
    assert resp.status_code == 421


@pytest.mark.asyncio
async def test_loopback_host_with_port_passes() -> None:
    """127.0.0.1:PORT is loopback — the guard must strip the port and allow it."""
    async with _client() as c:
        resp = await c.get("/", headers={"Host": "127.0.0.1:8765"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_evil_origin_returns_403() -> None:
    """Loopback Host + non-loopback Origin must be rejected with 403."""
    async with _client() as c:
        resp = await c.get(
            "/",
            headers={"Host": "127.0.0.1", "Origin": "http://evil.com"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_no_origin_passes() -> None:
    """Loopback Host + no Origin (CLI/non-browser) must be allowed."""
    async with _client() as c:
        resp = await c.get("/", headers={"Host": "127.0.0.1"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ipv6_loopback_host_passes() -> None:
    """IPv6 loopback [::1]:PORT must be allowed."""
    async with _client() as c:
        resp = await c.get("/", headers={"Host": "[::1]:9000"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_loopback_origin_passes() -> None:
    """Loopback Host + loopback Origin must be allowed."""
    async with _client() as c:
        resp = await c.get(
            "/",
            headers={"Host": "127.0.0.1", "Origin": "http://127.0.0.1:8765"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_localhost_origin_passes() -> None:
    """Loopback Host + localhost Origin must be allowed."""
    async with _client() as c:
        resp = await c.get(
            "/",
            headers={"Host": "localhost", "Origin": "http://localhost:3000"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_non_http_scope_passes_through() -> None:
    """Non-http scopes (lifespan) must not be filtered."""
    guard = HostOriginGuard(_ok_app)
    received: list[dict] = []

    async def _recv() -> dict:
        return {}

    async def _send(msg: object) -> None:
        received.append(msg)  # type: ignore[arg-type]

    # lifespan scope — guard should delegate without crashing
    await guard({"type": "lifespan"}, _recv, _send)


@pytest.mark.asyncio
async def test_custom_allowed_host() -> None:
    """Custom allowed_hosts set overrides the default loopback set."""
    guard = HostOriginGuard(_ok_app, allowed_hosts={"myhost.local"})
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=guard),
        base_url="http://testserver",
    )
    async with client as c:
        resp = await c.get("/", headers={"Host": "myhost.local"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_custom_allowed_host_blocks_loopback() -> None:
    """When custom allowed_hosts excludes loopback, loopback is blocked."""
    guard = HostOriginGuard(_ok_app, allowed_hosts={"myhost.local"})
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=guard),
        base_url="http://testserver",
    )
    async with client as c:
        resp = await c.get("/", headers={"Host": "127.0.0.1"})
    assert resp.status_code == 421


# ---------------------------------------------------------------------------
# Adversarial / fail-closed tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uppercase_localhost_rejected() -> None:
    """Host: LOCALHOST → 421.

    The allowlist is intentionally case-sensitive lowercase.  This test locks
    in that behaviour so a future maintainer cannot silently weaken the guard
    by adding .lower() to the comparison.
    """
    async with _client() as c:
        resp = await c.get("/", headers={"Host": "LOCALHOST"})
    assert resp.status_code == 421


@pytest.mark.asyncio
async def test_origin_lookalike_rejected() -> None:
    """Origin: http://127.0.0.1.evil.com → 403.

    A DNS-rebinding lookalike that embeds the loopback address as a subdomain
    label must NOT pass.  urlparse().netloc gives '127.0.0.1.evil.com' and
    _bare_host strips no port, so the full string is compared — not in
    allowed_origin_hosts → 403.
    """
    async with _client() as c:
        resp = await c.get(
            "/",
            headers={
                "Host": "127.0.0.1",
                "Origin": "http://127.0.0.1.evil.com",
            },
        )
    assert resp.status_code == 403
