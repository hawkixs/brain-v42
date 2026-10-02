"""Q134 = c: a declared PostgreSQL identity may omit server_addr, which is then not compared.

Docker reassigned the Postgres container's address three times in twelve days
(ticket a7a026d8), and every move blinded the briefing as a target_mismatch.
system_identifier, database and server_port stay mandatory; a host that can pin
its address may keep declaring it for the stricter check.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from brain_v42.config import Settings
from brain_v42.facts import FactTarget, Measured, SourceIdentity, Unreadable
from brain_v42.facts.model import ReleaseIdentity, identity_matches
from brain_v42.facts.nightly import NightlyVerifier
from brain_v42.facts.registry import FactRegistry
from brain_v42.facts.verification import _comparison

from .test_nightly_dry_run import _claim, _Descriptor, _FakeRegistry, _FakeReleaseCheckDry
from .test_registry import FakeProbe, FakeSource

_DECLARED = {"system_identifier": "7612696091383607335", "database": "brain", "server_port": 5432}


def _observed(addr: str = "172.31.0.3", **changes: object) -> SourceIdentity:
    return SourceIdentity.from_mapping({**_DECLARED, "server_addr": addr, **changes})


def _declared_without_addr() -> SourceIdentity:
    return SourceIdentity.from_mapping(_DECLARED)


def test_a_declaration_without_server_addr_builds_an_identity_that_omits_it() -> None:
    declared = _declared_without_addr()
    assert declared.server_addr is None
    assert declared.as_dict() == _DECLARED


def test_the_three_other_keys_stay_mandatory() -> None:
    for key in _DECLARED:
        with pytest.raises(ValueError, match="missing keys"):
            SourceIdentity.from_mapping({k: v for k, v in _DECLARED.items() if k != key})


def test_an_undeclared_address_matches_any_observed_address() -> None:
    assert identity_matches(_observed("172.31.0.3"), _declared_without_addr())
    assert identity_matches(_observed("172.31.0.5"), _declared_without_addr())


def test_a_declared_address_is_still_compared() -> None:
    declared = _observed("172.31.0.5")
    assert identity_matches(_observed("172.31.0.5"), declared)
    assert not identity_matches(_observed("172.31.0.3"), declared)


@pytest.mark.parametrize(
    "field,other", [("system_identifier", "1"), ("database", "brain_test"), ("server_port", 5433)]
)
def test_an_undeclared_address_relaxes_nothing_else(field: str, other: object) -> None:
    assert not identity_matches(_observed(**{field: other}), _declared_without_addr())


def test_an_observation_without_an_address_never_satisfies_a_declared_one() -> None:
    declared = _observed("172.31.0.5")
    assert not identity_matches(SourceIdentity.from_mapping(_DECLARED), declared)


def test_identities_of_different_kinds_never_match() -> None:
    release = ReleaseIdentity("a" * 40, "0.6.1")
    assert not identity_matches(release, _declared_without_addr())
    assert not identity_matches(_observed(), release)


def _registry(observed: SourceIdentity, probe: FakeProbe) -> FactRegistry:
    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeSource]:
        yield FakeSource(observed)

    registry = FactRegistry(
        sources={probe.target: factory}, expected={probe.target: _declared_without_addr()}
    )
    registry.register(probe)
    return registry


@pytest.mark.asyncio
async def test_the_registry_measures_through_a_moved_address_when_none_is_declared() -> None:
    result = await _registry(_observed("172.31.0.9"), FakeProbe("who")).measure("who")
    assert isinstance(result, Measured)
    # The observation keeps the address it actually read.
    assert result.source.as_dict()["server_addr"] == "172.31.0.9"


@pytest.mark.asyncio
async def test_the_registry_still_refuses_another_cluster_when_no_address_is_declared() -> None:
    probe = FakeProbe("who")
    result = await _registry(_observed(system_identifier="1"), probe).measure("who")
    assert isinstance(result, Unreadable)
    assert result.error_code == "target_mismatch"
    assert result.where == "system_identifier"


def _measured(source: SourceIdentity) -> Measured:
    return Measured.from_value(
        fact="fact_a",
        definition_version=1,
        target=FactTarget.PRODUCTION,
        source=source,
        value={"lag": 1},
        observation_id=UUID(int=1),
        measured_at=datetime(2026, 10, 2, tzinfo=UTC),
        duration_ms=1,
        ttl_seconds=30,
    )


class _ExpectedOnly:
    def expected_identity(self, target: FactTarget) -> SourceIdentity:
        return _declared_without_addr()


def test_claim_verification_accepts_a_moved_address_when_none_is_declared() -> None:
    comparison = _comparison(
        fact_name="fact_a",
        definition_version=1,
        target="production",
        expected_resolved={"path": "/lag", "op": "lte", "value": 5},
        registry=_ExpectedOnly(),  # type: ignore[arg-type]
        measurement=_measured(_observed("172.31.0.9")),
    )
    assert comparison.verdict == "holds"


@pytest.mark.asyncio
async def test_the_nightly_dry_run_counts_a_moved_address_as_identity_ok() -> None:
    registry = _FakeRegistry(
        descriptors={"fact_a": _Descriptor(1, FactTarget.PRODUCTION)},
        measurements={"fact_a": _measured(_observed("172.31.0.9"))},
    )
    registry.expected_identity = _ExpectedOnly().expected_identity  # type: ignore[method-assign]
    verifier = NightlyVerifier(service=None, release_check=_FakeReleaseCheckDry("verify"))

    report = await verifier.run_dry(
        (_claim(0, fact="fact_a"),), registry=registry, run_id=1, run_date=date(2026, 10, 2)
    )

    assert report.dry_facts["fact_a"]["identity_ok"] is True


def test_settings_accept_a_production_identity_without_server_addr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain:x@localhost:5433/brain")
    monkeypatch.setenv("BRAIN_FACTS_PRODUCTION_IDENTITY", json.dumps(_DECLARED))
    declared = Settings(_env_file=None).facts_production_identity()  # type: ignore[call-arg]
    assert declared == _DECLARED


@pytest.mark.parametrize("bad", [None, 172, ""])
def test_settings_still_type_check_a_declared_server_addr(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain:x@localhost:5433/brain")
    raw = json.dumps({**_DECLARED, "server_addr": bad})
    monkeypatch.setenv("BRAIN_FACTS_PRODUCTION_IDENTITY", raw)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    if bad == "":
        # The shape is a string; the model refuses it as no IP literal.
        with pytest.raises(ValueError, match="server_addr"):
            SourceIdentity.from_mapping(settings.facts_production_identity() or {})
    else:
        with pytest.raises(ValueError, match="BRAIN_FACTS_PRODUCTION_IDENTITY"):
            settings.facts_production_identity()


def test_an_observation_without_an_address_never_matches_even_an_addressless_declaration() -> None:
    """Fail closed: an observed identity must say where it was read (review of #265)."""
    addressless = SourceIdentity("7612696091383607335", "brain", None, 5432)
    assert not identity_matches(addressless, _declared_without_addr())


def test_an_explicit_null_address_is_refused_not_read_as_an_omission() -> None:
    with pytest.raises(ValueError, match="server_addr"):
        SourceIdentity.from_mapping({**_DECLARED, "server_addr": None})


@pytest.mark.asyncio
async def test_the_registry_refuses_an_addressless_observation_and_names_the_field() -> None:
    addressless = SourceIdentity("7612696091383607335", "brain", None, 5432)
    result = await _registry(addressless, FakeProbe("who")).measure("who")
    assert isinstance(result, Unreadable)
    assert (result.error_code, result.where) == ("target_mismatch", "server_addr")


def test_a_measurement_never_records_an_addressless_source() -> None:
    """The `Measured` contract itself refuses it, whatever path built the identity."""
    addressless = SourceIdentity("7612696091383607335", "brain", None, 5432)
    with pytest.raises(ValueError, match="server_addr"):
        _measured(addressless)
