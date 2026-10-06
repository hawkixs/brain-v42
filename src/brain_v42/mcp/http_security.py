"""Host/Origin ASGI middleware for DNS-rebinding protection and fail-closed bearer auth.

jlowin/fastmcp does not wire any Host/Origin guard, so a browser on the same
machine could DNS-rebind 127.0.0.1:PORT and call every brain tool
unauthenticated.  This middleware blocks such attacks at the ASGI layer:

- Any request whose Host header resolves to a non-loopback name → 421
- Any request that carries an Origin header whose host is non-loopback → 403
- Requests with no Origin header are allowed (CLI / non-browser clients)

BearerTokenGuard adds fail-closed token authentication on top:
- Empty tokens refuse requests unless MCP_HTTP_ALLOW_UNAUTHENTICATED is opted into
- Exempts /health so systemd watchdog and red-monitor can probe without headers
- Uses constant-time comparison (hmac.compare_digest) to prevent timing attacks

Intended use:
    from starlette.middleware import Middleware
    from brain_v42.mcp.http_security import HostOriginGuard, BearerTokenGuard
    mcp.run_http_async(..., middleware=[
        Middleware(HostOriginGuard),
        Middleware(BearerTokenGuard, token=settings.mcp_http_token),
    ])
"""

from __future__ import annotations

import hmac
from urllib.parse import urlparse

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def _bare_host(value: str) -> str:
    """Strip the port component from a Host header value.

    Handles three forms:
    - ``127.0.0.1``         → ``127.0.0.1``
    - ``127.0.0.1:8765``    → ``127.0.0.1``
    - ``[::1]:9000``        → ``::1``
    - ``[::1]``             → ``::1``
    """
    v = value.strip()
    # Bracketed IPv6: "[::1]" or "[::1]:port"
    if v.startswith("["):
        end = v.find("]")
        return v[1:end] if end != -1 else v
    # Plain host with exactly one colon: "host:port"
    if ":" in v and v.count(":") == 1:
        return v.rsplit(":", 1)[0]
    return v


class HostOriginGuard:
    """ASGI middleware that rejects DNS-rebinding attempts.

    Rules:
    - ``Host`` not in ``allowed_hosts`` → **421 Misdirected Request**
    - ``Origin`` present and its host not in ``allowed_origin_hosts`` → **403 Forbidden**
    - No ``Origin`` header → pass through (CLI / programmatic clients)
    - Non-HTTP scopes (websocket, lifespan) → delegated unchanged

    Header access uses ``starlette.datastructures.Headers`` rather than
    ``dict(scope["headers"])``. Note: ``Headers.get("host")`` returns only the
    first value when multiple Host headers are present. Duplicate-Host protection
    in practice comes from h11 (the underlying HTTP/1.1 parser used by uvicorn),
    which rejects malformed requests with duplicate Host headers before they reach
    this middleware. This guard validates the *value* of the single Host header
    that survives parsing.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: set[str] | None = None,
        allowed_origin_hosts: set[str] | None = None,
    ) -> None:
        self.app = app
        self.allowed_hosts: frozenset[str] = frozenset(
            allowed_hosts if allowed_hosts is not None else _LOOPBACK_HOSTS
        )
        self.allowed_origin_hosts: frozenset[str] = frozenset(
            allowed_origin_hosts if allowed_origin_hosts is not None else _LOOPBACK_HOSTS
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)

            host_raw = headers.get("host", "")
            if _bare_host(host_raw) not in self.allowed_hosts:
                await PlainTextResponse("Invalid Host header", status_code=421)(
                    scope, receive, send
                )
                return

            origin_raw = headers.get("origin", "")
            if origin_raw:
                origin_host = _bare_host(urlparse(origin_raw).netloc)
                if origin_host not in self.allowed_origin_hosts:
                    await PlainTextResponse("Invalid Origin", status_code=403)(scope, receive, send)
                    return

        await self.app(scope, receive, send)


_HEALTH_PATH = "/health"


class RequestBodyLimitGuard:
    """Buffer a bounded body before the SDK's exception-catching receive path."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == _HEALTH_PATH:
            if scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
                await self.app(scope, receive, send)
            else:
                await JSONResponse({"detail": "Method not allowed"}, status_code=405)(
                    scope, receive, send
                )
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if not (declared.isascii() and declared.isdigit()):
                    raise ValueError
                length = int(declared)
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(
                    scope, receive, send
                )
                return
            if length > self.max_body_bytes:
                await JSONResponse({"detail": "Request body too large"}, status_code=413)(
                    scope, receive, send
                )
                return

        buffered = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(buffered) + len(chunk) > self.max_body_bytes:
                await JSONResponse({"detail": "Request body too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            buffered.extend(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(buffered), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


class HttpAuthConfigurationError(RuntimeError):
    """The HTTP transport has no consistent authentication configuration."""


class BearerTokenGuard:
    """ASGI middleware that enforces fail-closed bearer-token authentication.

    Activation:
        An empty or blank token refuses all requests except /health unless
        ``allow_unauthenticated`` explicitly enables development access.

    Rules (when active):
        - ``/health`` is always exempt so systemd watchdog and red-monitor can
          probe liveness without carrying an Authorization header.
        - All other HTTP requests must carry ``Authorization: Bearer <token>``.
        - Missing or wrong token → **401 Unauthorized** +
          ``WWW-Authenticate: Bearer`` response header.
        - Token comparison uses ``hmac.compare_digest`` (constant-time) to
          resist timing-based token enumeration attacks.
        - Non-HTTP scopes (websocket, lifespan) → delegated unchanged.

    IMPORTANT — enabling this token is a coordinated deployment operation.
    Every fleet .mcp.json client must inject the Authorization header before
    MCP_HTTP_TOKEN is set on the server.  Activating the server guard without
    updating the clients will break all MCP calls silently.
    """

    def __init__(self, app: ASGIApp, *, token: str, allow_unauthenticated: bool = False) -> None:
        self.app = app
        self._token = token
        self._refuse_all = not token.strip() and not allow_unauthenticated
        self._active = bool(token.strip()) or self._refuse_all

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._active:
            path: str = scope.get("path", "")
            if path != _HEALTH_PATH:
                headers = Headers(scope=scope)
                auth = headers.get("authorization", "")
                # Normalise: "Bearer <token>" → "<token>"
                if auth.lower().startswith("bearer "):
                    presented = auth[7:]
                else:
                    presented = ""
                # Constant-time comparison on BYTES: str operands raise
                # TypeError on non-ASCII input (latin-1 obs-text is legal
                # HTTP/1.1 field-value content and reaches us as non-ASCII
                # str) — that would turn a wrong token into a 500 instead of
                # a 401. surrogateescape keeps arbitrary client bytes safe.
                if self._refuse_all or not hmac.compare_digest(
                    presented.encode("utf-8", "surrogateescape"),
                    self._token.encode("utf-8"),
                ):
                    await PlainTextResponse(
                        "Unauthorized",
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )(scope, receive, send)
                    return

        await self.app(scope, receive, send)
