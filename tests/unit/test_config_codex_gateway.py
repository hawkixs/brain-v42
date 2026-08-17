"""Configuration contract for the Codex management gateway."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from brain_v42.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
        _env_file=None,  # type: ignore[call-arg]
        **overrides,
    )


def test_codex_gateway_defaults_to_loopback_port_9211_and_secret_token() -> None:
    settings = _settings()

    assert settings.brain_codex_gateway_host == "127.0.0.1"
    assert settings.brain_codex_gateway_port == 9211
    assert isinstance(settings.brain_codex_gateway_token, SecretStr)
    assert settings.brain_codex_gateway_token.get_secret_value() == ""


def test_codex_gateway_values_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_CODEX_GATEWAY_HOST", "::1")
    monkeypatch.setenv("BRAIN_CODEX_GATEWAY_PORT", "9311")
    monkeypatch.setenv("BRAIN_CODEX_GATEWAY_TOKEN", "private-token")

    settings = _settings()

    assert settings.brain_codex_gateway_host == "::1"
    assert settings.brain_codex_gateway_port == 9311
    assert settings.brain_codex_gateway_token.get_secret_value() == "private-token"
    assert "private-token" not in repr(settings)


def test_codex_gateway_all_interfaces_bind_requires_explicit_container_opt_in() -> None:
    with pytest.raises(ValidationError, match="allow_all_interfaces"):
        _settings(brain_codex_gateway_host="0.0.0.0")

    settings = _settings(
        brain_codex_gateway_host="0.0.0.0",
        brain_codex_gateway_allow_all_interfaces=True,
    )

    assert settings.brain_codex_gateway_host == "0.0.0.0"


def test_codex_gateway_env_bind_all_interfaces_is_refused_without_the_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant portant le `# nosec B104` de `_codex_gateway_private_bind_only`.

    Le littéral "0.0.0.0" de config.py est le motif REFUSÉ par le validateur, pas une
    adresse de bind. Ce test échoue si quelqu'un retire le garde-fou, ouvre son défaut,
    ou laisse la variable d'environnement — seule entrée du champ — atteindre le bind.
    """
    monkeypatch.setenv("BRAIN_CODEX_GATEWAY_HOST", "0.0.0.0")

    with pytest.raises(ValidationError, match="allow_all_interfaces"):
        _settings()

    monkeypatch.delenv("BRAIN_CODEX_GATEWAY_HOST")
    settings = _settings()

    assert settings.brain_codex_gateway_allow_all_interfaces is False
    assert settings.brain_codex_gateway_host != "0.0.0.0"


@pytest.mark.parametrize("host", ["192.168.1.12", "gateway.example.com"])
def test_codex_gateway_rejects_unapproved_bind_hosts(host: str) -> None:
    with pytest.raises(ValidationError, match="approved private bind"):
        _settings(brain_codex_gateway_host=host)


def test_codex_gateway_killswitch_path_loads_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "BRAIN_CODEX_GATEWAY_KILLSWITCHES_PATH",
        "/run/brain-v42/killswitches.conf",
    )

    settings = _settings()

    assert str(settings.brain_codex_gateway_killswitches_path) == (
        "/run/brain-v42/killswitches.conf"
    )


@pytest.mark.parametrize("port", [0, 65536])
def test_codex_gateway_port_is_bounded(port: int) -> None:
    with pytest.raises(ValidationError):
        _settings(brain_codex_gateway_port=port)
