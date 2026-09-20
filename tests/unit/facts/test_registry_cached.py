"""Contracts for the read-only fact cache accessor."""

from __future__ import annotations

import pytest

from brain_v42.facts.registry import UnknownFactError

from .test_registry import Clock, FakeProbe, registry


async def test_cached_returns_the_stored_measurement_without_starting_a_probe() -> None:
    """Catalogue listing can show the last observation without consuming a probe budget."""
    clock = Clock()
    probe = FakeProbe("cache_visible")
    fact_registry = registry(probe, clock=clock)

    assert fact_registry.cached("cache_visible") is None
    assert probe.runs == 0

    measured = await fact_registry.measure("cache_visible")

    assert fact_registry.cached("cache_visible") is measured
    assert probe.runs == 1


def test_cached_refuses_a_name_outside_the_catalogue() -> None:
    """The read-only accessor keeps the registry's closed-name boundary intact."""
    fact_registry = registry(FakeProbe("known_fact"))

    with pytest.raises(UnknownFactError):
        fact_registry.cached("unknown_fact")
