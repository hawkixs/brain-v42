"""Dedicated, redacted configuration for observable delivery workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
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
        hide_input_in_errors=True,
    )

    enabled: bool = False
    repository_registry: dict[str, dict[int, str]] = Field(
        default_factory=lambda: {
            key: dict(value) for key, value in DEFAULT_REPOSITORY_REGISTRY.items()
        }
    )
    poll_seconds: int = Field(default=60, ge=1)
    freshness_seconds: int = Field(default=600, ge=1, le=86400)
    request_budget_per_minute: int = Field(default=40, ge=1, le=40)
    max_concurrent_requests: int = Field(default=2, ge=1, le=2)
    request_timeout_seconds: int = Field(default=10, ge=1, le=10)
    max_response_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=8 * 1024 * 1024)
    github_api_origin: str = "https://api.github.com"
    github_api_version: Literal["2026-03-10", "2022-11-28"] = "2026-03-10"
    github_token: SecretStr = Field(default=SecretStr(""), repr=False)
    github_app_id: int | None = Field(default=None, gt=0)
    github_installation_id: int | None = Field(default=None, gt=0)
    github_private_key_path: Path | None = Field(default=None, repr=False)
    observer_env_path: Path = Field(
        default=Path("~/.config/brain-v42/delivery-observer.env").expanduser(), repr=False
    )

    @field_validator("github_api_origin")
    @classmethod
    def _https_origin(cls, value: str) -> str:
        try:
            parts = urlsplit(value)
            if (
                value != value.strip()
                or "\\" in value
                or parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.path not in {"", "/"}
                or parts.query
                or parts.fragment
                or any(ord(char) < 33 or ord(char) == 127 for char in value)
            ):
                raise ValueError
            _ = parts.port
        except ValueError:
            raise ValueError(
                "GitHub API origin must be an HTTPS origin without credentials"
            ) from None
        return value.rstrip("/")

    def repositories_for(self, project_key: str) -> dict[int, str]:
        """Return a copy of numeric-authoritative repositories for one executor project."""
        return dict(self.repository_registry.get(project_key, {}))
