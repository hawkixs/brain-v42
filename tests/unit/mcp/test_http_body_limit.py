"""Body limits buffer safely before entering the MCP exception boundary."""

from typing import Any

import httpx
import pytest
from fastmcp import FastMCP
from pydantic import ValidationError
from starlette.types import Message, Receive, Scope, Send

from brain_v42.config import Settings
from brain_v42.mcp import server
from brain_v42.mcp.http_security import RequestBodyLimitGuard


async def _drive(
    chunks: list[Message],
    headers: list[tuple[bytes, bytes]] | None = None,
    *,
    cap: int = 4,
    scope_type: str = "http",
) -> tuple[list[Message], list[Message]]:
    sent: list[Message] = []
    received: list[Message] = []

    async def receive() -> Message:
        assert chunks, "unexpected body read"
        return chunks.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    async def inner(scope: Scope, recv: Receive, send: Send) -> None:
        if scope["type"] == "http":
            received.append(await recv())
            received.append(await recv())
        else:
            received.append({"type": scope["type"]})

    await RequestBodyLimitGuard(inner, max_body_bytes=cap)(
        {"type": scope_type, "headers": headers or []}, receive, send
    )
    return sent, received


@pytest.mark.asyncio
async def test_declared_oversize_is_refused_413_without_reading_the_body() -> None:
    sent, received = await _drive([], [(b"content-length", b"5")])
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b'{"detail":"Request body too large"}'
    assert not received


@pytest.mark.asyncio
async def test_streamed_oversize_without_content_length_is_refused_413() -> None:
    sent, received = await _drive(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"45", "more_body": False},
        ]
    )
    assert sent[0]["status"] == 413
    assert not received


@pytest.mark.asyncio
async def test_body_at_exact_cap_reaches_app_and_then_delegates_disconnect() -> None:
    sent, received = await _drive(
        [
            {"type": "http.request", "body": b"12", "more_body": True},
            {"type": "http.request", "body": b"34", "more_body": False},
            {"type": "http.disconnect"},
        ],
        [(b"content-length", b"4")],
    )
    assert not sent
    assert received == [
        {"type": "http.request", "body": b"1234", "more_body": False},
        {"type": "http.disconnect"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"abc", b"-1", b"+5", b"5_0", b" 1 ", b"\xb2"])
async def test_invalid_content_length_is_refused_400(value: bytes) -> None:
    sent, received = await _drive([], [(b"content-length", value)])
    assert sent[0]["status"] == 400
    assert sent[1]["body"] == b'{"detail":"Invalid Content-Length"}'
    assert not received


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_health_body_methods_are_refused_without_reading(method: str) -> None:
    from unittest.mock import AsyncMock

    from brain_v42.mcp.http_security import BearerTokenGuard

    receive, send, inner = AsyncMock(), AsyncMock(), AsyncMock()
    receive.return_value = {"type": "http.request", "body": b"12345", "more_body": True}
    app = BearerTokenGuard(RequestBodyLimitGuard(inner, max_body_bytes=4), token="secret")
    await app(
        {
            "type": "http",
            "path": "/health",
            "method": method,
            "headers": [(b"transfer-encoding", b"chunked")],
        },
        receive,
        send,
    )
    assert send.call_args_list[0].args[0]["status"] == 405
    receive.assert_not_called()
    inner.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_health_non_body_methods_pass_through_without_buffering(method: str) -> None:
    from unittest.mock import AsyncMock

    receive, send, inner = AsyncMock(), AsyncMock(), AsyncMock()
    scope = {"type": "http", "path": "/health", "method": method, "headers": []}
    await RequestBodyLimitGuard(inner, max_body_bytes=4)(scope, receive, send)
    receive.assert_not_called()
    inner.assert_awaited_once_with(scope, receive, send)


@pytest.mark.asyncio
async def test_disconnect_while_buffering_sends_nothing() -> None:
    assert await _drive([{"type": "http.disconnect"}]) == ([], [])


@pytest.mark.asyncio
async def test_non_http_scope_passes_through() -> None:
    assert await _drive([], scope_type="lifespan") == ([], [{"type": "lifespan"}])


@pytest.mark.parametrize("value", [65_535, 67_108_865])
def test_max_body_bytes_setting_bounds(value: int) -> None:
    assert Settings.model_fields["mcp_http_max_body_bytes"].default == 2_097_152
    with pytest.raises(ValidationError):
        Settings(
            postgres_url="postgresql+asyncpg://unused:unused@localhost/unused",
            mcp_http_max_body_bytes=value,
            _env_file=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authenticated", "enforcement", "status"),
    [
        (True, False, 413),
        (False, False, 401),
        (True, True, 413),
    ],
)
async def test_http_plan_body_and_auth_order(
    authenticated: bool, enforcement: bool, status: int
) -> None:
    from tests.unit.mcp.test_dream_capability_http import FakeProjectResolver, _registry_json

    settings = Settings(
        postgres_url="postgresql+asyncpg://unused:unused@localhost/unused",
        mcp_http_token="admin-token",
        mcp_http_max_body_bytes=65_536,
        brain_dream_capability_enforcement=enforcement,
        mcp_http_dream_tokens=_registry_json(),
        _env_file=None,
    )
    mcp = FastMCP("body-order")
    middleware = server._configure_http_security(
        mcp, settings, project_resolver=FakeProjectResolver()
    )
    app = mcp.http_app(stateless_http=True, middleware=middleware)
    headers = {"Authorization": "Bearer admin-token"} if authenticated else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        async with app.router.lifespan_context(app):
            response = await client.post("/mcp", content=b"x" * 65_537, headers=headers)
    assert response.status_code == status


@pytest.mark.asyncio
async def test_uvicorn_configuration_has_no_unsupported_body_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = FastMCP("uvicorn-options")
    captured: dict[str, Any] = {}

    async def run(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(mcp, "run_http_async", run)
    settings = Settings(
        postgres_url="postgresql+asyncpg://unused:unused@localhost/unused",
        brain_mcp_transport="http",
        mcp_http_token="token",
        mcp_http_stateless=True,
        _env_file=None,
    )
    await server._run_mcp(mcp, settings)
    assert captured["uvicorn_config"] == {
        "timeout_graceful_shutdown": 10,
        "proxy_headers": False,
        "forwarded_allow_ips": "",
    }
