"""The declared production identity: independent of the DSN, four validated keys."""

from __future__ import annotations

import json

import pytest

from brain_v42.config import Settings
from brain_v42.facts.model import SourceIdentity

_IDENTITY = {
    "system_identifier": "7612696091383607335",
    "database": "brain",
    "server_addr": "172.31.0.4",
    "server_port": 5432,
}


def _settings(monkeypatch: pytest.MonkeyPatch, raw: str | None) -> Settings:
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain:x@localhost:5433/brain")
    if raw is None:
        monkeypatch.delenv("BRAIN_FACTS_PRODUCTION_IDENTITY", raising=False)
    else:
        monkeypatch.setenv("BRAIN_FACTS_PRODUCTION_IDENTITY", raw)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _settings_for_identity(
    monkeypatch: pytest.MonkeyPatch, environment_name: str, raw: str | None
) -> Settings:
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain:x@localhost:5433/brain")
    monkeypatch.delenv("BRAIN_FACTS_PRODUCTION_IDENTITY", raising=False)
    monkeypatch.delenv("BRAIN_FACTS_LIVE_RELEASE_IDENTITY", raising=False)
    monkeypatch.delenv("BRAIN_FACTS_HOST_IDENTITY", raising=False)
    if raw is not None:
        monkeypatch.setenv(environment_name, raw)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_production_identity_is_absent_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent means no production probe can register — never a guessed identity."""
    assert _settings(monkeypatch, None).facts_production_identity() is None


def test_production_identity_is_parsed_into_the_four_declared_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declared = _settings(monkeypatch, json.dumps(_IDENTITY)).facts_production_identity()
    assert declared == _IDENTITY
    # The composition root builds the model from the mapping; the round trip holds.
    assert SourceIdentity.from_mapping(declared) == SourceIdentity(**_IDENTITY)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({**_IDENTITY, "server_port": "5432"}),
        json.dumps({k: v for k, v in _IDENTITY.items() if k != "server_addr"}),
        json.dumps({**_IDENTITY, "extra": 1}),
        json.dumps({**_IDENTITY, "server_port": True}),
        json.dumps([1, 2, 3]),
    ],
)
def test_a_malformed_production_identity_loads_but_is_refused_when_read(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """§5.3 rule 4: malformed is treated like absent at composition, said in the journal —
    the service starts without production facts, it does not go down on a typo."""
    settings = _settings(monkeypatch, raw)
    with pytest.raises(ValueError, match="BRAIN_FACTS_PRODUCTION_IDENTITY"):
        settings.facts_production_identity()


def test_deep_validation_is_the_models_job_not_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings check the shape; a value the model refuses is refused at composition."""
    declared = _settings(
        monkeypatch, json.dumps({**_IDENTITY, "system_identifier": "abc"})
    ).facts_production_identity()
    assert declared is not None
    with pytest.raises(ValueError, match="system_identifier"):
        SourceIdentity.from_mapping(declared)


@pytest.mark.parametrize(
    ("environment_name", "method_name", "identity"),
    [
        (
            "BRAIN_FACTS_LIVE_RELEASE_IDENTITY",
            "facts_live_release_identity",
            {"release_sha": "a" * 40, "package_version": "0.6.0"},
        ),
        ("BRAIN_FACTS_HOST_IDENTITY", "facts_host_identity", {"hostname": "host-a"}),
    ],
)
def test_new_identities_are_absent_or_parse_only_their_declared_shape(
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    method_name: str,
    identity: dict[str, str],
) -> None:
    absent = _settings_for_identity(monkeypatch, environment_name, None)
    assert getattr(absent, method_name)() is None
    settings = _settings_for_identity(monkeypatch, environment_name, json.dumps(identity))
    assert getattr(settings, method_name)() == identity


@pytest.mark.parametrize(
    ("environment_name", "method_name", "identity"),
    [
        (
            "BRAIN_FACTS_LIVE_RELEASE_IDENTITY",
            "facts_live_release_identity",
            {"release_sha": "a" * 40, "package_version": "0.6.0"},
        ),
        ("BRAIN_FACTS_HOST_IDENTITY", "facts_host_identity", {"hostname": "host-a"}),
    ],
)
@pytest.mark.parametrize("kind", ["not-json", "array", "missing", "extra", "wrong-type"])
def test_new_malformed_declarations_load_but_are_refused_when_read(
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    method_name: str,
    identity: dict[str, str],
    kind: str,
) -> None:
    raw = {
        "not-json": "not json",
        "array": "[]",
        "missing": json.dumps({}),
        "extra": json.dumps({**identity, "extra": "x"}),
        "wrong-type": json.dumps({**identity, next(iter(identity)): 1}),
    }[kind]
    settings = _settings_for_identity(monkeypatch, environment_name, raw)
    with pytest.raises(ValueError, match=environment_name):
        getattr(settings, method_name)()


def test_settings_leave_release_deep_validation_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declared = _settings_for_identity(
        monkeypatch,
        "BRAIN_FACTS_LIVE_RELEASE_IDENTITY",
        json.dumps({"release_sha": "A" * 40, "package_version": "dev"}),
    ).facts_live_release_identity()
    assert declared == {"release_sha": "A" * 40, "package_version": "dev"}
    from brain_v42.facts.model import ReleaseIdentity

    with pytest.raises(ValueError):
        ReleaseIdentity.from_mapping(declared)
