"""The elevation allowlist is parsed strictly before any attribution write."""

import pytest
from pydantic import ValidationError

from brain_v42.config import Settings
from brain_v42.repositories.pg_client_credentials import DEFAULT_ELEVATABLE_CLIENT_IDS


def test_default_allowlist_matches_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAIN_ELEVATABLE_CLIENT_IDS", raising=False)
    settings = Settings(postgres_url="postgresql+asyncpg://localhost/test", _env_file=None)
    assert settings.elevatable_client_ids == DEFAULT_ELEVATABLE_CLIENT_IDS


def test_allowlist_reads_comma_separated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_ELEVATABLE_CLIENT_IDS", "workstation-claude,other.client-2")
    settings = Settings(postgres_url="postgresql+asyncpg://localhost/test", _env_file=None)
    assert settings.elevatable_client_ids == {"workstation-claude", "other.client-2"}


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        " workstation-claude",
        "workstation-claude ",
        "a, b",
        "a,",
        ",a",
        "UPPER",
        "a_b",
        ".a",
        "a/b",
        "a\nb",
        "a" * 65,
        '["workstation-claude"]',
    ],
)
def test_allowlist_rejects_malformed_environment(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("BRAIN_ELEVATABLE_CLIENT_IDS", value)
    with pytest.raises(ValidationError):
        Settings(postgres_url="postgresql+asyncpg://localhost/test", _env_file=None)


def test_explicit_empty_allowlist_disables_claims() -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://localhost/test",
        elevatable_client_ids=set(),
        _env_file=None,
    )
    assert settings.elevatable_client_ids == set()
