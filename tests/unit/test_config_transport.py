"""Tests for MCP transport configuration fields (TDD — Task 1.1)."""

import pytest
from pydantic import ValidationError

_PG_URL = "postgresql+asyncpg://u:p@localhost:5433/db"


def test_transport_defaults_to_stdio_and_loopback(monkeypatch):
    """With only POSTGRES_URL set, transport defaults to stdio and loopback host."""
    monkeypatch.delenv("BRAIN_MCP_TRANSPORT", raising=False)
    monkeypatch.delenv("MCP_HTTP_HOST", raising=False)
    monkeypatch.delenv("MCP_HTTP_PORT", raising=False)
    from brain_v42.config import Settings

    s = Settings(postgres_url=_PG_URL)
    assert s.brain_mcp_transport == "stdio"
    assert s.mcp_http_host == "127.0.0.1"
    assert isinstance(s.mcp_http_port, int)


def test_http_host_rejects_0_0_0_0(monkeypatch):
    """mcp_http_host must reject 0.0.0.0 (bind-all forbidden)."""
    monkeypatch.setenv("MCP_HTTP_HOST", "0.0.0.0")
    from brain_v42.config import Settings

    with pytest.raises(ValidationError):
        Settings(postgres_url=_PG_URL)


def test_transport_http_via_env(monkeypatch):
    """BRAIN_MCP_TRANSPORT=http sets brain_mcp_transport to 'http'."""
    monkeypatch.setenv("BRAIN_MCP_TRANSPORT", "http")
    from brain_v42.config import Settings

    s = Settings(postgres_url=_PG_URL)
    assert s.brain_mcp_transport == "http"
