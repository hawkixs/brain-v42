"""Dedicated, redacted configuration for observable delivery workflows."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_REPOSITORY_REGISTRY: dict[str, dict[int, str]] = {
    "brain-v42": {1337360966: "hawkixs/brain-v42"},
}


class DeliverySettings(BaseSettings):
    """Observer configuration kept separate from interactive MCP credentials."""

    model_config = SettingsConfigDict(
        env_prefix="BRAIN_DELIVERY_",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    enabled: bool = False
    repository_registry: dict[str, dict[int, str]] = Field(
        default_factory=lambda: {
            key: dict(value) for key, value in DEFAULT_REPOSITORY_REGISTRY.items()
        }
    )
    poll_seconds: int = Field(default=60, ge=1)
    freshness_seconds: int = Field(default=600, ge=1)
    request_budget_per_minute: int = Field(default=40, ge=1)
    max_concurrent_requests: int = Field(default=2, ge=1)
    request_timeout_seconds: int = Field(default=10, ge=1)
    github_token: SecretStr = Field(default=SecretStr(""), repr=False)
    github_app_id: int | None = Field(default=None, gt=0)
    github_installation_id: int | None = Field(default=None, gt=0)
    github_private_key_path: Path | None = Field(default=None, repr=False)
    observer_env_path: Path = Field(
        default=Path("~/.config/brain-v42/delivery-observer.env").expanduser(), repr=False
    )

    def repositories_for(self, project_key: str) -> dict[int, str]:
        """Return a copy of numeric-authoritative repositories for one executor project."""
        return dict(self.repository_registry.get(project_key, {}))
