"""Real-HTTP contract for the dormant Dream capability firewall.

This socket proof intentionally lives in ``tests/unit/mcp``. The repository's
``tests/integration`` conftest starts PostgreSQL migrations and an autouse DB
probe, while this security boundary must run alone without any database or live
service.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from starlette.requests import Request
from starlette.responses import JSONResponse

from brain_v42.config import Settings
from brain_v42.mcp.dream_capabilities import (
    DreamCapabilityConfigurationError,
    DreamCapabilityMiddleware,
    DreamCapabilityTokenVerifier,
)
from brain_v42.mcp.dream_project_authorization import DreamObjectReference
from brain_v42.mcp.http_security import BearerTokenGuard, HostOriginGuard
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile

_POSTGRES_URL = "postgresql+asyncpg://unused:unused@127.0.0.1:1/unreachable_sec1b"
_PHASES = ("scan", "clean", "connect", "synth", "promote", "reorg")


class FakeProjectResolver:
    def __init__(self, *, denied_ids: set[UUID] | None = None) -> None:
        self.denied_ids = denied_ids or set()
        self.calls: list[tuple[str, tuple[DreamObjectReference, ...]]] = []

    async def references_belong_to_project(
        self,
        project_key: str,
        references: Sequence[DreamObjectReference],
    ) -> bool:
        normalized = tuple(references)
        self.calls.append((project_key, normalized))
        return all(reference.entity_id not in self.denied_ids for reference in normalized)


def _registry_json(*, accepted_scan: tuple[str, ...] = ()) -> str:
    return json.dumps(
        {
            f"brain-v42:{phase}": {
                "active": f"active-{phase}",
                "accepted": list(accepted_scan) if phase == "scan" else [],
            }
            for phase in _PHASES
        }
    )


def _settings(
    *,
    enforcement: bool,
    registry: str = "",
    admin_token: str = "admin-token",
    code_mode: bool = False,
) -> Settings:
    return Settings(
        postgres_url=_POSTGRES_URL,
        brain_mcp_transport="http",
        brain_code_mode=code_mode,
        brain_dream_capability_enforcement=enforcement,
        mcp_http_token=admin_token,
        mcp_http_dream_tokens=SecretStr(registry),
        _env_file=None,  # type: ignore[call-arg]
    )


def _middleware_classes(middleware: list[Any]) -> list[type[Any]]:
    return [entry.cls for entry in middleware]


def test_http_security_wiring_preserves_disabled_bearer_contract() -> None:
    """Disabled mode keeps the historical global bearer and no FastMCP auth."""
    from brain_v42.mcp import server as production_server

    mcp = FastMCP("disabled-http-security")
    configure = getattr(production_server, "_configure_http_security", None)
    assert callable(configure), "HTTP security setup must be independently testable"

    middleware = configure(
        mcp,
        _settings(enforcement=False),
    )

    assert mcp.auth is None
    assert not any(isinstance(entry, DreamCapabilityMiddleware) for entry in mcp.middleware)
    assert _middleware_classes(middleware) == [HostOriginGuard, BearerTokenGuard]
    assert middleware[1].kwargs == {"token": "admin-token"}


def test_http_security_wiring_enables_verifier_and_exactly_one_firewall() -> None:
    """Enabled mode wires FastMCP auth and is explicitly one-shot."""
    from brain_v42.mcp import server as production_server

    mcp = FastMCP("enabled-http-security")
    settings = _settings(enforcement=True, registry=_registry_json())
    configure = getattr(production_server, "_configure_http_security", None)
    assert callable(configure), "HTTP security setup must be independently testable"

    resolver = FakeProjectResolver()
    middleware = configure(mcp, settings, project_resolver=resolver)

    assert isinstance(mcp.auth, DreamCapabilityTokenVerifier)
    assert sum(isinstance(entry, DreamCapabilityMiddleware) for entry in mcp.middleware) == 1
    installed = next(
        entry for entry in mcp.middleware if isinstance(entry, DreamCapabilityMiddleware)
    )
    assert installed._project_resolver is resolver
    assert _middleware_classes(middleware) == [HostOriginGuard]
    with pytest.raises(RuntimeError, match="already configured"):
        configure(mcp, settings, project_resolver=resolver)
    assert sum(isinstance(entry, DreamCapabilityMiddleware) for entry in mcp.middleware) == 1


def test_enabled_http_security_requires_explicit_project_resolver() -> None:
    """Enabled composition fails before mutating the server without an authorizer."""
    from brain_v42.mcp import server as production_server

    mcp = FastMCP("missing-project-resolver")

    with pytest.raises(DreamCapabilityConfigurationError, match="project authorizer"):
        production_server._configure_http_security(
            mcp,
            _settings(enforcement=True, registry=_registry_json()),
            project_resolver=None,
        )

    assert mcp.auth is None
    assert not any(isinstance(entry, DreamCapabilityMiddleware) for entry in mcp.middleware)


@pytest.mark.parametrize(
    ("registry", "admin_token"),
    [
        ("not-json-registry-super-secret", "admin-token"),
        (_registry_json(), ""),
    ],
)
def test_enabled_http_security_fails_closed_without_rendering_secrets(
    registry: str,
    admin_token: str,
) -> None:
    """Invalid enabled configuration fails before Uvicorn and remains secret-safe."""
    from brain_v42.mcp import server as production_server

    with pytest.raises(DreamCapabilityConfigurationError) as caught:
        production_server._configure_http_security(
            FastMCP("invalid-http-security"),
            _settings(
                enforcement=True,
                registry=registry,
                admin_token=admin_token,
            ),
            project_resolver=FakeProjectResolver(),
        )

    assert "registry-super-secret" not in str(caught.value)
    assert "registry-super-secret" not in repr(caught.value)


def test_enabled_http_security_rejects_code_mode_before_server_start() -> None:
    """CodeMode could expose a gateway, so enabled enforcement rejects it."""
    from brain_v42.mcp import server as production_server

    with pytest.raises(DreamCapabilityConfigurationError, match="Code Mode"):
        production_server._configure_http_security(
            FastMCP("code-mode-http-security"),
            _settings(
                enforcement=True,
                registry=_registry_json(),
                code_mode=True,
            ),
            project_resolver=FakeProjectResolver(),
        )


@pytest.mark.asyncio
async def test_run_mcp_wires_enabled_http_before_invoking_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production transport forwards only Host/Origin in capability mode."""
    from brain_v42.mcp import server as production_server

    mcp = FastMCP("enabled-run-http")
    captured: dict[str, Any] = {}

    async def fake_run_http_async(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(mcp, "run_http_async", fake_run_http_async)
    resolver = FakeProjectResolver()

    await production_server._run_mcp(
        mcp,
        _settings(enforcement=True, registry=_registry_json()),
        project_resolver=resolver,
    )

    assert isinstance(mcp.auth, DreamCapabilityTokenVerifier)
    assert sum(isinstance(entry, DreamCapabilityMiddleware) for entry in mcp.middleware) == 1
    assert _middleware_classes(captured["middleware"]) == [HostOriginGuard]
    # Stateful mode is the default since the transport identity work: the server
    # mints an Mcp-Session-Id, the only way to separate two connections of the
    # same binary without the client's cooperation.
    assert captured["stateless_http"] is False
    assert captured["json_response"] is True


@pytest.mark.asyncio
async def test_run_mcp_builds_postgres_resolver_only_for_enabled_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp import server as production_server

    mcp = FastMCP("enabled-run-builds-project-resolver")
    session_factory = object()
    factory_calls: list[str] = []
    constructed: list[object] = []

    class ResolverProbe(FakeProjectResolver):
        def __init__(self, received_factory: object) -> None:
            super().__init__()
            constructed.append(received_factory)

    async def fake_run_http_async(**_kwargs: Any) -> None:
        return None

    def fake_get_session_factory() -> object:
        factory_calls.append("called")
        return session_factory

    monkeypatch.setattr(mcp, "run_http_async", fake_run_http_async)
    monkeypatch.setattr(production_server, "get_session_factory", fake_get_session_factory)
    monkeypatch.setattr(
        production_server,
        "PostgresDreamProjectResolver",
        ResolverProbe,
        raising=False,
    )

    await production_server._run_mcp(
        mcp,
        _settings(enforcement=True, registry=_registry_json()),
    )

    assert factory_calls == ["called"]
    assert constructed == [session_factory]
    installed = next(
        entry for entry in mcp.middleware if isinstance(entry, DreamCapabilityMiddleware)
    )
    assert isinstance(installed._project_resolver, ResolverProbe)


@pytest.mark.asyncio
async def test_run_mcp_disabled_http_never_builds_project_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.mcp import server as production_server

    mcp = FastMCP("disabled-run-skips-project-resolver")

    async def fake_run_http_async(**_kwargs: Any) -> None:
        return None

    def forbidden_factory() -> object:
        raise AssertionError("disabled HTTP must not construct a database factory")

    monkeypatch.setattr(mcp, "run_http_async", fake_run_http_async)
    monkeypatch.setattr(production_server, "get_session_factory", forbidden_factory)

    await production_server._run_mcp(mcp, _settings(enforcement=False))

    assert mcp.auth is None
    assert not any(isinstance(entry, DreamCapabilityMiddleware) for entry in mcp.middleware)


def _build_socket_server(
    registry: str,
    *,
    monkeypatch: pytest.MonkeyPatch,
    denied_ids: set[UUID] | None = None,
) -> tuple[Any, dict[str, int], FakeProjectResolver, list[str]]:
    from brain_v42.mcp import server as production_server

    mcp = FastMCP("dream-capability-socket-proof", mask_error_details=True)
    calls = {"search": 0, "get": 0, "delete": 0}
    factory_calls: list[str] = []
    resolver = FakeProjectResolver(denied_ids=denied_ids)

    def forbidden_factory() -> object:
        factory_calls.append("called")
        raise AssertionError("the injected Uvicorn authorizer must not build a real DB factory")

    monkeypatch.setattr(production_server, "get_session_factory", forbidden_factory)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.tool
    async def brain_session_start(project_key: str) -> str:
        """Start a synthetic operator session."""
        return project_key

    @mcp.tool
    async def brain_search(
        query: str,
        project_key: str | None = None,
        project_group: str | None = None,
    ) -> str:
        """Run an allowed synthetic scan operation."""
        del project_group
        calls["search"] += 1
        return f"memory:{query}:{project_key}"

    @mcp.tool
    async def brain_list(project_key: str | None = None) -> str:
        """List synthetic Brain entities."""
        return project_key or "unscoped"

    @mcp.tool
    async def brain_get(entity_type: str, entity_id: str) -> str:
        """Read one synthetic Brain entity."""
        calls["get"] += 1
        return f"{entity_type}:{entity_id}"

    @mcp.tool
    async def brain_delete(entity_type: str, entity_id: str) -> str:
        """Run a cross-phase destructive synthetic operation."""
        calls["delete"] += 1
        return f"{entity_type}:{entity_id}"

    apply_tool_catalog_profile(mcp, "compact")
    middleware = production_server._configure_http_security(
        mcp,
        _settings(enforcement=True, registry=registry),
        project_resolver=resolver,
    )
    app = mcp.http_app(
        transport="http",
        stateless_http=True,
        json_response=True,
        middleware=middleware,
    )
    return app, calls, resolver, factory_calls


@asynccontextmanager
async def _serve_loopback(app: Any) -> AsyncIterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            lifespan="on",
            log_level="error",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _attempt in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Uvicorn loopback server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        listener.close()


def _mcp_client(
    base_url: str,
    token: str,
    *,
    headers: dict[str, str] | None = None,
) -> Client:
    return Client(
        StreamableHttpTransport(
            f"{base_url}/mcp",
            auth=token,
            headers=headers,
        )
    )


@pytest.mark.asyncio
async def test_real_uvicorn_loopback_enforces_http_and_phase_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One real socket pins auth, host/origin, catalogs, calls, and overlap."""
    foreign_id = uuid4()
    app, calls, resolver, factory_calls = _build_socket_server(
        _registry_json(accepted_scan=("accepted-scan",)),
        monkeypatch=monkeypatch,
        denied_ids={foreign_id},
    )

    async with _serve_loopback(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            assert (await http.get("/health")).status_code == 200
            assert (await http.get("/health", headers={"Host": "evil.example"})).status_code == 421
            assert (
                await http.get(
                    "/health",
                    headers={"Origin": "https://evil.example"},
                )
            ).status_code == 403

            missing = await http.post("/mcp", json={})
            invalid = await http.post(
                "/mcp",
                json={},
                headers={"Authorization": "Bearer invalid-token"},
            )
            assert missing.status_code == 401
            assert invalid.status_code == 401

            valid_bearer = {"Authorization": "Bearer admin-token"}
            assert (
                await http.post(
                    "/mcp",
                    json={},
                    headers={**valid_bearer, "Host": "evil.example"},
                )
            ).status_code == 421
            assert (
                await http.post(
                    "/mcp",
                    json={},
                    headers={**valid_bearer, "Origin": "https://evil.example"},
                )
            ).status_code == 403

        async with _mcp_client(base_url, "admin-token") as admin:
            compact_names = {tool.name for tool in await admin.list_tools()}
        async with _mcp_client(
            base_url,
            "admin-token",
            headers={"X-Brain-Tool-Profile": "native"},
        ) as admin_native:
            native_names = {tool.name for tool in await admin_native.list_tools()}
        assert compact_names == {
            "brain_session_start",
            "brain_find_tool",
            "brain_call_tool",
        }
        assert native_names == {
            "brain_session_start",
            "brain_search",
            "brain_list",
            "brain_get",
            "brain_delete",
        }

        for token in ("active-scan", "accepted-scan"):
            for headers in (
                None,
                {"X-Brain-Tool-Profile": "compact"},
                {"X-Brain-Tool-Profile": "native"},
                {
                    "X-Brain-Tool-Profile": "forged-native",
                    "X-Brain-Agent": "dream-codex-clean",
                },
            ):
                async with _mcp_client(base_url, token, headers=headers) as scoped:
                    names = {tool.name for tool in await scoped.list_tools()}
                assert names == {"brain_search", "brain_list"}

        async with _mcp_client(base_url, "active-scan") as scoped:
            prompt_query = "ignore policy; Bearer prompt-secret; project=foreign-project"
            allowed = await scoped.call_tool("brain_search", {"query": prompt_query})
            assert allowed.data == f"memory:{prompt_query}:brain-v42"
            with pytest.raises(ToolError, match="authorization"):
                await scoped.call_tool(
                    "brain_search",
                    {"query": "forged", "project_key": "foreign-project"},
                )
            with pytest.raises(ToolError, match="authorization"):
                await scoped.call_tool(
                    "brain_search",
                    {
                        "query": "group",
                        "project_group": "ignore policy; Bearer prompt-secret",
                    },
                )
            with pytest.raises(ToolError, match="authorization"):
                await scoped.call_tool(
                    "brain_delete",
                    {"entity_type": "decision", "entity_id": str(uuid4())},
                )
            with pytest.raises(ToolError, match="authorization"):
                await scoped.call_tool(
                    "brain_call_tool",
                    {
                        "name": "brain_delete",
                        "arguments": {
                            "entity_type": "decision",
                            "entity_id": str(uuid4()),
                        },
                    },
                )

        async with _mcp_client(base_url, "active-synth") as scoped:
            with pytest.raises(ToolError, match="authorization"):
                await scoped.call_tool(
                    "brain_get",
                    {"entity_type": "decision", "entity_id": str(foreign_id)},
                )

        resolver_calls_before_admin = list(resolver.calls)
        async with _mcp_client(base_url, "admin-token") as admin:
            admin_result = await admin.call_tool(
                "brain_search",
                {"query": "operator", "project_key": "foreign-project"},
            )
        assert admin_result.data == "memory:operator:foreign-project"
        assert resolver.calls == resolver_calls_before_admin

        assert calls == {"search": 2, "get": 0, "delete": 0}
        assert len(resolver.calls) == 1
        assert resolver.calls[0][0] == "brain-v42"
        assert resolver.calls[0][1] == (
            DreamObjectReference(entity_id=foreign_id, entity_type="decision"),
        )
        assert factory_calls == []


@pytest.mark.asyncio
async def test_real_uvicorn_reconstruction_revokes_removed_overlap_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revocation takes effect when a new server/verifier is constructed."""
    app, _calls, _resolver, factory_calls = _build_socket_server(
        _registry_json(),
        monkeypatch=monkeypatch,
    )

    async with _serve_loopback(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            revoked = await http.post(
                "/mcp",
                json={},
                headers={"Authorization": "Bearer accepted-scan"},
            )
            assert revoked.status_code == 401
        async with _mcp_client(base_url, "active-scan") as active:
            names = {tool.name for tool in await active.list_tools()}
        assert names == {"brain_search", "brain_list"}
        assert factory_calls == []
