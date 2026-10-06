"""Credential mode must reject configurations that bypass connection attribution."""

import secrets

import pytest
from pydantic import ValidationError

from brain_v42.config import Settings


@pytest.fixture(autouse=True)
def _isolate_auth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BRAIN_MCP_AUTH_MODE",
        "BRAIN_DREAM_CAPABILITY_ENFORCEMENT",
        "MCP_HTTP_ALLOW_UNAUTHENTICATED",
        "BRAIN_MCP_HTTP_ALLOW_UNAUTHENTICATED",
        "MCP_HTTP_TOKEN",
        "BRAIN_MCP_HTTP_TOKEN",
        "MCP_HTTP_STATELESS",
        "BRAIN_MCP_HTTP_STATELESS",
    ):
        monkeypatch.delenv(name, raising=False)


def _settings(**kwargs: object) -> Settings:
    return Settings(
        postgres_url="postgresql+asyncpg://localhost/unused",
        _env_file=None,
        **kwargs,
    )


def test_shared_token_mode_is_the_default() -> None:
    assert _settings().brain_mcp_auth_mode == "shared_token"


def test_credentials_mode_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_MCP_AUTH_MODE", "credentials")
    assert _settings().brain_mcp_auth_mode == "credentials"


@pytest.mark.parametrize(
    "conflict",
    [
        {"brain_dream_capability_enforcement": True},
        {"mcp_http_allow_unauthenticated": True},
        {"mcp_http_stateless": True},
    ],
    ids=["dream-capabilities", "unauthenticated", "stateless"],
)
def test_credentials_mode_refuses_an_incompatible_setting(conflict: dict[str, bool]) -> None:
    with pytest.raises(ValidationError):
        _settings(brain_mcp_auth_mode="credentials", **conflict)


def test_credentials_mode_refuses_a_shared_token_without_disclosing_it() -> None:
    token = secrets.token_urlsafe(32)
    with pytest.raises(ValidationError) as caught:
        _settings(brain_mcp_auth_mode="credentials", mcp_http_token=token)
    assert token not in str(caught.value)


def test_unknown_auth_mode_is_refused() -> None:
    with pytest.raises(ValidationError):
        _settings(brain_mcp_auth_mode="unsupported")
