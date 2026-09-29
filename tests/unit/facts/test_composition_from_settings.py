"""`build_fact_registry_from_settings` must be byte-identical to the inline server block it replaces."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from brain_v42.facts.composition import build_fact_registry, build_fact_registry_from_settings
from brain_v42.facts.registry import FactRegistry

_PRODUCTION = {
    "system_identifier": "7612696091383607335",
    "database": "brain",
    "server_addr": "172.31.0.4",
    "server_port": 5432,
}
_RELEASE = {"release_sha": "a" * 40, "package_version": "0.6.0"}
_HOST = {"hostname": "host-a"}


class _SettingsDouble:
    """The three methods `build_fact_registry_from_settings` actually calls."""

    def __init__(
        self,
        *,
        production: object = None,
        live_release: object = None,
        host: object = None,
        production_raises: Exception | None = None,
        live_release_raises: Exception | None = None,
        host_raises: Exception | None = None,
    ) -> None:
        self._production = production
        self._live_release = live_release
        self._host = host
        self._production_raises = production_raises
        self._live_release_raises = live_release_raises
        self._host_raises = host_raises

    def facts_production_identity(self) -> object:
        if self._production_raises is not None:
            raise self._production_raises
        return self._production

    def facts_live_release_identity(self) -> object:
        if self._live_release_raises is not None:
            raise self._live_release_raises
        return self._live_release

    def facts_host_identity(self) -> object:
        if self._host_raises is not None:
            raise self._host_raises
        return self._host


def _assert_equal_registries(a: FactRegistry, b: FactRegistry) -> None:
    assert a.names() == b.names()
    assert a.refusals() == b.refusals()
    for name in a.names():
        descriptor_a, descriptor_b = a.describe(name), b.describe(name)
        assert descriptor_a.target == descriptor_b.target
        assert descriptor_a.definition_version == descriptor_b.definition_version
        assert descriptor_a.ttl_seconds == descriptor_b.ttl_seconds


def test_matches_direct_construction_with_every_identity_declared() -> None:
    settings = _SettingsDouble(production=_PRODUCTION, live_release=_RELEASE, host=_HOST)

    from_settings = build_fact_registry_from_settings(settings, session_factory=MagicMock())
    direct = build_fact_registry(
        _PRODUCTION,
        session_factory=MagicMock(),
        declared_live_release_identity=_RELEASE,
        declared_host_identity=_HOST,
    )

    _assert_equal_registries(from_settings, direct)
    assert from_settings.names()[-1] == "claims_verification_last_night"
    assert from_settings.briefing_names()[-1] == "claims_verification_last_night"


def test_matches_direct_construction_with_no_identity_declared() -> None:
    settings = _SettingsDouble()

    from_settings = build_fact_registry_from_settings(settings, session_factory=MagicMock())
    direct = build_fact_registry(None, session_factory=MagicMock())

    _assert_equal_registries(from_settings, direct)


@pytest.mark.parametrize(
    ("raises_kwarg", "event"),
    [
        ("production_raises", "facts.production_identity_unreadable"),
        ("live_release_raises", "facts.live_release_identity_unreadable"),
        ("host_raises", "facts.host_identity_unreadable"),
    ],
)
def test_an_unreadable_identity_logs_the_same_event_as_the_inline_block_and_registers_like_undeclared(
    raises_kwarg: str, event: str
) -> None:
    settings = _SettingsDouble(
        production=_PRODUCTION,
        live_release=_RELEASE,
        host=_HOST,
        **{raises_kwarg: ValueError("settings double refuses to read this identity")},
    )

    with capture_logs() as logs:
        from_settings = build_fact_registry_from_settings(settings, session_factory=MagicMock())
    assert any(record["event"] == event for record in logs)

    direct_kwargs: dict[str, object] = {
        "declared_production_identity": _PRODUCTION,
        "declared_live_release_identity": _RELEASE,
        "declared_host_identity": _HOST,
    }
    field_by_kwarg = {
        "production_raises": "declared_production_identity",
        "live_release_raises": "declared_live_release_identity",
        "host_raises": "declared_host_identity",
    }
    direct_kwargs[field_by_kwarg[raises_kwarg]] = None
    declared_production = direct_kwargs.pop("declared_production_identity")
    direct = build_fact_registry(declared_production, session_factory=MagicMock(), **direct_kwargs)

    _assert_equal_registries(from_settings, direct)
