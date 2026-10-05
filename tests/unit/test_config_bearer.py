"""Tests for bearer token config setting (TDD Red phase).

Verifies that mcp_http_token exists, defaults to empty string, and is
loaded from MCP_HTTP_TOKEN env var.
"""

from __future__ import annotations

import pytest


def test_allow_unauthenticated_defaults_false() -> None:
    from brain_v42.config import Settings

    assert Settings.model_fields["mcp_http_allow_unauthenticated"].default is False


@pytest.mark.parametrize(
    "alias", ["MCP_HTTP_ALLOW_UNAUTHENTICATED", "BRAIN_MCP_HTTP_ALLOW_UNAUTHENTICATED"]
)
def test_allow_unauthenticated_reads_both_aliases(
    alias: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brain_v42.config import Settings

    monkeypatch.setenv(alias, "true")
    settings = Settings(
        postgres_url="postgresql+asyncpg://unused:unused@localhost/unused", _env_file=None
    )
    assert settings.mcp_http_allow_unauthenticated is True


class TestBearerTokenConfig:
    """Settings accepts an empty bearer; the HTTP boundary refuses it."""

    def test_mcp_http_token_field_exists(self) -> None:
        """mcp_http_token field must exist in Settings."""
        from brain_v42.config import Settings

        assert "mcp_http_token" in Settings.model_fields

    def test_mcp_http_token_default_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The config default remains empty; HTTP startup validates authentication."""
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

    def test_mcp_http_token_empty_remains_a_settings_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Settings is shared with stdio and accepts an explicitly empty token."""
        monkeypatch.setenv("MCP_HTTP_TOKEN", "")
        from brain_v42.config import Settings

        s = Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert s.mcp_http_token == ""
        # An empty Settings value is accepted; HTTP startup refuses it by default.
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
