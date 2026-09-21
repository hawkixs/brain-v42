"""Contracts for facts disabled after immutable definition drift."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from brain_v42.facts import FactRegistry, FactTarget, SourceIdentity, Unreadable
from brain_v42.facts.registry import UnknownFactError


class _Source:
    async def identity(self) -> SourceIdentity:
        return SourceIdentity("7612696091383607335", "brain", "127.0.0.1", 5432)


class _Probe:
    def __init__(self, name: str, *, raises: bool = False) -> None:
        self.name = name
        self.definition_version = 1
        self.target = FactTarget.PRODUCTION
        self.ttl = timedelta(seconds=15)
        self.timeout = timedelta(seconds=1)
        self.briefing = False
        self.policies = {"late_after_seconds": 300}
        self.value_schema = {"pending": "int"}
        self.raises = raises
        self.runs = 0

    async def measure(self, source: _Source) -> Mapping[str, object]:
        self.runs += 1
        if self.raises:
            raise AssertionError("disabled probe must not run")
        return {"pending": 0}


def _registry(*probes: _Probe, refusal: tuple[str, str] | None = None) -> FactRegistry:
    @asynccontextmanager
    async def source_factory() -> AsyncIterator[_Source]:
        yield _Source()

    identity = SourceIdentity("7612696091383607335", "brain", "127.0.0.1", 5432)
    registry = FactRegistry(
        sources={FactTarget.PRODUCTION: source_factory},
        expected={FactTarget.PRODUCTION: identity},
        wall=lambda: datetime(2026, 9, 21, tzinfo=UTC),
    )
    for probe in probes:
        registry.register(probe)
    if refusal is not None:
        registry.note_refusal(*refusal)
    registry.freeze()
    return registry


@pytest.mark.asyncio
async def test_disable_after_freeze_short_circuits_probe_and_fresh_cache() -> None:
    """A drift removes trust before cache, budget, or a source can answer."""
    probe = _Probe("drifting_fact", raises=True)
    registry = _registry(probe)

    registry.disable("drifting_fact", "definition_drift")

    result = await registry.measure("drifting_fact")

    assert isinstance(result, Unreadable)
    assert result.error_code == "definition_drift"
    assert result.where == "drifting_fact"
    assert probe.runs == 0
    assert registry.disabled() == {"drifting_fact": "definition_drift"}


def test_disable_rejects_unknown_name_after_freeze() -> None:
    """A typo must not create a disabled entry outside the registered catalogue."""
    registry = _registry(_Probe("known_fact"))

    with pytest.raises(UnknownFactError):
        registry.disable("unknown_fact", "definition_drift")


@pytest.mark.asyncio
async def test_disabled_fact_never_serves_a_fresh_cached_measurement() -> None:
    """The cache cannot preserve a reading whose immutable definition drifted."""
    probe = _Probe("cached_fact")
    registry = _registry(probe)
    cached = await registry.measure("cached_fact")
    assert not isinstance(cached, Unreadable)

    registry.disable("cached_fact", "definition_drift")
    result = await registry.measure("cached_fact")

    assert isinstance(result, Unreadable)
    assert result.error_code == "definition_drift"
    assert probe.runs == 1


@pytest.mark.asyncio
async def test_measure_many_preserves_other_facts_when_one_is_disabled() -> None:
    """Definition drift is fact-local rather than a failure of the whole batch."""
    drifting = _Probe("drifting_fact", raises=True)
    readable = _Probe("readable_fact")
    registry = _registry(drifting, readable)
    registry.disable("drifting_fact", "definition_drift")

    results = await registry.measure_many(("drifting_fact", "readable_fact"))

    assert isinstance(results["drifting_fact"], Unreadable)
    assert results["drifting_fact"].error_code == "definition_drift"
    assert not isinstance(results["readable_fact"], Unreadable)
    assert drifting.runs == 0
    assert readable.runs == 1


def test_disabled_and_refused_facts_are_separate_catalogue_states() -> None:
    """Composition refusal is not a later definition-drift disablement."""
    registry = _registry(_Probe("disabled_fact"), refusal=("refused_fact", "unverifiable_target"))
    registry.disable("disabled_fact", "definition_drift")

    assert registry.disabled() == {"disabled_fact": "definition_drift"}
    assert registry.refusals() == {"refused_fact": "unverifiable_target"}
