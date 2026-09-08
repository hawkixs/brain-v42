"""Public configuration contract for the delivery observer boundary."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_delivery_settings_are_disabled_and_pin_the_brain_registry() -> None:
    """A missing environment must not enable provider work or accept a name-only repo."""
    from brain_v42.delivery_config import DeliverySettings

    settings = DeliverySettings()

    assert settings.enabled is False
    assert settings.github_api_version == "2022-11-28"
    assert settings.repositories_for("brain-v42") == {1337360966: "hawkixs/brain-v42"}
    assert settings.repositories_for("unknown-project") == {}


def test_delivery_settings_refuse_the_removed_github_api_version() -> None:
    """A removed API version cannot reintroduce incomplete merge observations."""
    from brain_v42.delivery_config import DeliverySettings

    with pytest.raises(ValidationError):
        DeliverySettings(github_api_version="2026-03-10")
