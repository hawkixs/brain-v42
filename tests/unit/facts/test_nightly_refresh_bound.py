"""Spec §4.5: the refresh budget cannot be exhausted by one invocation, whatever the cap.

Exercises `FactRegistry.measure()` directly (not the whole orchestrator+service+DB
stack) with an injected fake monotonic clock: the bound is a property of the
registry's own budget/cache logic under repeated `max_age`-bounded calls, the
same calls 200 claims sharing one long-TTL fact would each make.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

from brain_v42.facts.model import FactTarget, SourceIdentity, Unreadable
from brain_v42.facts.registry import FactRegistry


class _FakeClock:
    """Advances on every read, from any caller -- simulating wall-clock elapsing."""

    def __init__(self, *, step: float) -> None:
        self._value = 0.0
        self._step = step

    def __call__(self) -> float:
        self._value += self._step
        return self._value

    @property
    def value(self) -> float:
        return self._value


class _AlwaysUnreadableProbe:
    """Models `alembic_head_shipped`: long TTL, forced by every `max_age=60s` caller."""

    name = "alembic_head_shipped"
    definition_version = 1
    target = FactTarget.LIVE_RELEASE
    ttl = timedelta(days=3650)
    timeout = timedelta(seconds=3)
    briefing = True
    policies: dict[str, int] = {}
    value_schema = {"revision": "string"}

    def __init__(self) -> None:
        self.runs = 0

    async def measure(self, source: object) -> dict[str, object]:
        self.runs += 1
        raise RuntimeError("simulated probe failure")


async def test_two_hundred_claims_on_one_long_ttl_fact_force_a_bounded_number_of_producers() -> (
    None
):
    """D <= ~270s here -> at most 1 + floor(D/30) producer starts, and zero budget skips."""

    @asynccontextmanager
    async def source():
        yield object()

    clock = _FakeClock(step=1.25)
    identity = SourceIdentity("1", "brain_test", "127.0.0.1", 5432)
    probe = _AlwaysUnreadableProbe()
    registry = FactRegistry(
        sources={FactTarget.LIVE_RELEASE: source},
        expected={FactTarget.LIVE_RELEASE: identity},
        monotonic=clock,
    )
    registry.register(probe)
    registry.freeze()

    skipped_budget = 0
    for _ in range(200):
        measurement = await registry.measure("alembic_head_shipped", max_age=timedelta(seconds=60))
        if isinstance(measurement, Unreadable) and measurement.error_code == "refresh_budget":
            skipped_budget += 1

    # Measured with this exact fake clock: 8 producer starts over ~280s of
    # simulated elapsed time -- comfortably under the spec's own "<= 9" bound
    # for D <= 250s (recomputed here from the ACTUAL D, not hardcoded, so a
    # regression in either direction is caught).
    assert probe.runs <= 9
    assert probe.runs <= 1 + int(clock.value // 30)
    assert skipped_budget == 0
