"""HTTP authentication is refused eagerly unless explicitly opted out."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastmcp import FastMCP

from brain_v42.config import Settings
from brain_v42.mcp import server
from brain_v42.mcp.dream_capabilities import DreamCapabilityConfigurationError
from brain_v42.mcp.http_security import BearerTokenGuard, HostOriginGuard, RequestBodyLimitGuard


@pytest.fixture(autouse=True)
def _isolate_auth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MCP_HTTP_TOKEN",
        "BRAIN_MCP_HTTP_TOKEN",
        "MCP_HTTP_ALLOW_UNAUTHENTICATED",
        "BRAIN_MCP_HTTP_ALLOW_UNAUTHENTICATED",
    ):
        monkeypatch.delenv(name, raising=False)


def _settings(**kwargs: object) -> Settings:
    return Settings(
        postgres_url="postgresql+asyncpg://unused:unused@localhost/unused",
        mcp_http_token="",
        _env_file=None,
        **kwargs,
    )


@pytest.mark.parametrize("token", ["", "   "])
def test_http_security_refuses_an_empty_or_blank_token(token: str) -> None:
    from brain_v42.mcp.http_security import HttpAuthConfigurationError

    mcp = FastMCP("refusal")
    settings = _settings()
    settings.mcp_http_token = token
    with pytest.raises(HttpAuthConfigurationError) as caught:
        server._configure_http_security(mcp, settings)
    assert "MCP_HTTP_TOKEN" in str(caught.value)
    assert "MCP_HTTP_ALLOW_UNAUTHENTICATED" in str(caught.value)
    assert mcp not in server._http_security_configured_servers


def test_plan_http_transport_refuses_before_touching_process_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp.http_security import HttpAuthConfigurationError

    idle, termination = Mock(), Mock()
    monkeypatch.setattr(server, "_install_session_idle_timeout", idle)
    monkeypatch.setattr(server, "_install_transport_termination_hook", termination)
    with pytest.raises(HttpAuthConfigurationError):
        server.plan_http_transport(FastMCP("plan-refusal"), _settings())
    idle.assert_not_called()
    termination.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "   "])
async def test_the_named_opt_out_serves_without_auth_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    warning = Mock()
    monkeypatch.setattr(server.logger, "warning", warning)
    mcp = FastMCP("opt-out")
    settings = _settings(mcp_http_allow_unauthenticated=True)
    settings.mcp_http_token = token
    middleware = server._configure_http_security(mcp, settings)
    assert [entry.cls for entry in middleware] == [
        HostOriginGuard,
        BearerTokenGuard,
        RequestBodyLimitGuard,
    ]
    assert middleware[1].kwargs == {"token": "", "allow_unauthenticated": True}
    warning.assert_called_once_with("brain_v42.server.http_auth", auth="disabled_by_opt_in")
    app = mcp.http_app(stateless_http=True, middleware=middleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        async with app.router.lifespan_context(app):
            assert (await client.post("/mcp", json={})).status_code != 401


def test_the_opt_out_with_a_token_is_refused_as_contradictory() -> None:
    from brain_v42.mcp.http_security import HttpAuthConfigurationError

    settings = _settings(mcp_http_allow_unauthenticated=True)
    settings.mcp_http_token = "secret-not-for-errors"
    with pytest.raises(HttpAuthConfigurationError) as caught:
        server._configure_http_security(FastMCP("contradiction"), settings)
    assert settings.mcp_http_token not in str(caught.value)


@pytest.mark.parametrize("token_name", ["MCP_HTTP_TOKEN", "BRAIN_MCP_HTTP_TOKEN"])
def test_opt_out_refuses_a_raw_token_even_when_settings_masks_it(
    monkeypatch: pytest.MonkeyPatch, token_name: str
) -> None:
    from brain_v42.mcp.http_security import HttpAuthConfigurationError

    monkeypatch.setenv(token_name, "secret-not-for-errors")
    if token_name == "MCP_HTTP_TOKEN":
        monkeypatch.setenv("BRAIN_MCP_HTTP_TOKEN", "")
    settings = _settings(mcp_http_allow_unauthenticated=True)
    assert settings.mcp_http_token == ""
    with pytest.raises(HttpAuthConfigurationError, match="contradictory") as caught:
        server._configure_http_security(FastMCP("masked-token"), settings)
    assert "secret-not-for-errors" not in str(caught.value)


def test_entrypoint_auth_refusal_precedes_every_lifecycle_side_effect() -> None:
    import ast
    from contextlib import asynccontextmanager
    from pathlib import Path

    from brain_v42.mcp.http_security import HttpAuthConfigurationError

    entered = Mock()

    @asynccontextmanager
    async def lifecycle(*args: object) -> AsyncIterator[None]:
        entered()
        yield

    built = server.BuiltServer(
        FastMCP("startup-refusal"), {}, _settings(brain_mcp_transport="http"), None
    )
    tree = ast.parse(Path(server.__file__).read_text())
    entrypoint = next(
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )
    namespace = dict(
        vars(server), __name__="__main__", build_server=lambda: built, app_lifecycle=lifecycle
    )
    for name in (
        "_configure_stdio_logging",
        "_setup_parent_death_signal",
        "_apply_http_server_arg",
        "log_server_starting",
    ):
        namespace[name] = Mock()
    with pytest.raises(HttpAuthConfigurationError):
        exec(
            compile(ast.Module(body=[entrypoint], type_ignores=[]), server.__file__, "exec"),
            namespace,
        )
    entered.assert_not_called()


def test_the_opt_out_is_refused_under_capability_enforcement() -> None:
    with pytest.raises(
        DreamCapabilityConfigurationError, match="incompatible with Dream capability enforcement"
    ):
        server._configure_http_security(
            FastMCP("enforcement"),
            _settings(mcp_http_allow_unauthenticated=True, brain_dream_capability_enforcement=True),
        )


@pytest.mark.asyncio
async def test_stdio_transport_needs_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = FastMCP("stdio")
    run = AsyncMock()
    monkeypatch.setattr(mcp, "run_async", run)
    await server._run_mcp(mcp, _settings(brain_mcp_transport="stdio"))
    run.assert_awaited_once()
