"""Tests for bearer token config setting (TDD Red phase).

Verifies that mcp_http_token exists, defaults to empty string, and is
loaded from MCP_HTTP_TOKEN env var.
"""

from __future__ import annotations

import pytest


class TestBearerTokenConfig:
    """mcp_http_token setting: opt-in bearer auth (disabled when empty)."""

    def test_mcp_http_token_field_exists(self) -> None:
        """mcp_http_token field must exist in Settings."""
        from brain_v42.config import Settings

        assert "mcp_http_token" in Settings.model_fields

    def test_mcp_http_token_default_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """mcp_http_token default is empty string (auth disabled by default)."""
        monkeypatch.delenv("MCP_HTTP_TOKEN", raising=False)
        from brain_v42.config import Settings

        s = Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert s.mcp_http_token == ""

    def test_mcp_http_token_loaded_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MCP_HTTP_TOKEN env var sets the token."""
        monkeypatch.setenv("MCP_HTTP_TOKEN", "supersecrettoken")
        from brain_v42.config import Settings

        s = Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert s.mcp_http_token == "supersecrettoken"

    def test_mcp_http_token_empty_means_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicitly setting MCP_HTTP_TOKEN='' keeps auth disabled."""
        monkeypatch.setenv("MCP_HTTP_TOKEN", "")
        from brain_v42.config import Settings

        s = Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert s.mcp_http_token == ""
        # Empty token signals no auth — bool check used by server wiring
        assert not s.mcp_http_token


def test_mcp_http_token_is_excluded_from_settings_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings repr/str must not expose the legacy HTTP bearer."""
    token = "legacy-http-super-secret"
    monkeypatch.setenv("MCP_HTTP_TOKEN", token)
    from brain_v42.config import Settings

    settings = Settings(
        postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.mcp_http_token == token
    assert token not in repr(settings)
    assert token not in str(settings)
