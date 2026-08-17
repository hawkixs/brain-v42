"""Integration tests for the /health liveness route (Task 5.1).

Tests the bounded SELECT 1 health check endpoint registered on the FastMCP
server via @mcp.custom_route("/health", methods=["GET"]).

Uses httpx ASGITransport against mcp.http_app(...) (in-process, no network).
Requires a live PostgreSQL on localhost:5433 (skipped automatically when
unreachable via the session-scoped check in conftest.py).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from brain_v42.config import get_settings

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def health_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    """AsyncClient using httpx ASGITransport against the FastMCP http app.

    Sets POSTGRES_URL so get_engine() can build the Settings singleton, and
    clears the engine singleton before/after so each test starts clean.
    The /health route is registered at module-level on the `mcp` singleton via
    @mcp.custom_route, so importing server.py is sufficient -- no lifespan needed.
    """
    from tests.integration.conftest import INTEGRATION_DB_URL

    monkeypatch.setenv("POSTGRES_URL", INTEGRATION_DB_URL)
    monkeypatch.setenv("GRAPH_ENABLED", "false")
    get_settings.cache_clear()

    import brain_v42.db.engine as engine_module

    # Reset engine singleton so get_engine() picks up the fresh settings
    original_engine = engine_module._engine
    original_factory = engine_module._session_factory
    engine_module._engine = None
    engine_module._session_factory = None

    from brain_v42.mcp.server import mcp

    app = mcp.http_app(stateless_http=True, json_response=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    # Teardown: dispose engine created by the health check, restore prior state
    if engine_module._engine is not None:
        await engine_module._engine.dispose()
    engine_module._engine = original_engine
    engine_module._session_factory = original_factory
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_health_ok(health_client: httpx.AsyncClient) -> None:
    """GET /health -> 200, status=="ok", pool stats present (live PG)."""
    resp = await health_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "pool" in body
    assert "size" in body["pool"]
    assert "checked_out" in body["pool"]


@pytest.mark.asyncio
async def test_health_degraded(health_client: httpx.AsyncClient) -> None:
    """GET /health -> 503, status=="degraded" when DB connect raises.

    Simulates a wedged or unreachable pool by making engine.connect().__aenter__
    raise immediately, which asyncio.timeout(2) catches and the handler returns 503.
    """

    class _FailConnect:
        """Async context manager that raises on enter, simulating a dead pool."""

        async def __aenter__(self) -> None:
            raise OSError("connection refused")

        async def __aexit__(self, *args: object) -> bool:
            return False

    mock_engine = MagicMock()
    mock_engine.connect.return_value = _FailConnect()

    with patch("brain_v42.db.engine.get_engine", return_value=mock_engine):
        resp = await health_client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
