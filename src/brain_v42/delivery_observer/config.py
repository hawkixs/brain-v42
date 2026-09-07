"""Explicit private process configuration, independent of the MCP application."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.auth import load_private_environment, read_private_file


def load_observer_settings(path: Path) -> DeliverySettings:
    """The selected file wins over ambient settings; credentials stay file-backed."""
    values = load_private_environment(path)
    data: dict[str, Any] = {
        name: values[key]
        for name in DeliverySettings.model_fields
        if (key := "BRAIN_DELIVERY_" + name.upper()) in values
    }
    data["observer_env_path"] = path
    if "repository_registry" in data:
        data["repository_registry"] = json.loads(data["repository_registry"])
    if "postgres_url" not in data and not os.environ.get("BRAIN_DELIVERY_POSTGRES_URL"):
        legacy = values.get("POSTGRES_URL") or os.environ.get("POSTGRES_URL")
        if legacy:
            data["postgres_url"] = legacy
    settings = DeliverySettings(**data)
    if not settings.enabled or settings.postgres_url is None:
        raise ValueError("observer configuration is incomplete")
    app = (
        settings.github_app_id,
        settings.github_installation_id,
        settings.github_private_key_path,
    )
    if any(item is not None for item in app):
        if any(item is None for item in app):
            raise ValueError("observer application credentials are incomplete")
        assert settings.github_private_key_path is not None
        read_private_file(settings.github_private_key_path)
    else:
        token = values.get("BRAIN_DELIVERY_GITHUB_TOKEN", "")
        if not 1 <= len(token) <= 8192 or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("observer credentials are incomplete")
    return settings
