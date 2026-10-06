"""An off-loopback MCP bind must name both its auth and Host boundaries."""

import os

import pytest
from pydantic import ValidationError

from brain_v42.config import Settings


@pytest.fixture(autouse=True)
def isolate_mcp_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.upper().startswith(("MCP_HTTP_", "BRAIN_MCP_", "BRAIN_DREAM_")):
            monkeypatch.delenv(key)


def load_settings() -> Settings:
    return Settings(postgres_url="postgresql+asyncpg://localhost/unused", _env_file=None)


def test_default_http_boundary_stays_loopback_shared_token() -> None:
    config = load_settings()
    assert config.mcp_http_host == "127.0.0.1"
    assert config.brain_mcp_auth_mode == "shared_token"
    assert config.mcp_http_allow_non_loopback is False
    assert not config.mcp_http_allowed_hosts


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.4", "mcp.example.test"])
def test_non_loopback_requires_named_opt_in(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HTTP_HOST", host)
    with pytest.raises(ValidationError, match="MCP_HTTP_ALLOW_NON_LOOPBACK"):
        load_settings()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "0.0.0.0"])
def test_opt_in_requires_credentials_mode(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HTTP_HOST", host)
    monkeypatch.setenv("MCP_HTTP_ALLOW_NON_LOOPBACK", "true")
    monkeypatch.setenv("MCP_HTTP_ALLOWED_HOSTS", "mcp.example.test:8765")
    with pytest.raises(ValidationError, match="BRAIN_MCP_AUTH_MODE.*credentials"):
        load_settings()


@pytest.mark.parametrize("hosts", ["", " , , "])
def test_opt_in_requires_nonempty_host_list(hosts: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_HTTP_ALLOW_NON_LOOPBACK", "true")
    monkeypatch.setenv("BRAIN_MCP_AUTH_MODE", "credentials")
    monkeypatch.setenv("MCP_HTTP_ALLOWED_HOSTS", hosts)
    with pytest.raises(ValidationError, match="MCP_HTTP_ALLOWED_HOSTS"):
        load_settings()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.4", "mcp.example.test"])
def test_opted_in_credentials_bind_parses_comma_list(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HTTP_HOST", host)
    monkeypatch.setenv("MCP_HTTP_ALLOW_NON_LOOPBACK", "true")
    monkeypatch.setenv("BRAIN_MCP_AUTH_MODE", "credentials")
    monkeypatch.setenv(
        "MCP_HTTP_ALLOWED_HOSTS", "192.0.2.4:8765, mcp.example.test, [2001:db8::4]:8765"
    )
    config = load_settings()
    assert config.mcp_http_host == host
    assert config.mcp_http_allow_non_loopback is True
    assert set(config.mcp_http_allowed_hosts) == {
        "192.0.2.4:8765",
        "mcp.example.test",
        "[2001:db8::4]:8765",
    }
