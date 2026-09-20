"""Unit contracts for the closed, cached and cancellation-safe fact registry."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import structlog

from brain_v42.facts import FactTarget, Measured, SourceIdentity, Unreadable
from brain_v42.facts.registry import (
    DuplicateFactError,
    FactRegistry,
    RefreshBudget,
    RegistryClosedError,
    RegistryFrozenError,
    UnknownFactError,
    UnverifiableTargetError,
)


class Clock:
    """Provide clocks that tests advance independently without waiting in real time."""

    def __init__(self) -> None:
        self.mono = 0.0
        self.wall = datetime(2026, 9, 20, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        return self.wall


class FakeSource:
    """Expose a configurable identity in the same context manager as a fake read."""

    def __init__(
        self, identity: SourceIdentity, *, identity_error: Exception | None = None
    ) -> None:
        self._identity = identity
        self._identity_error = identity_error

    async def identity(self) -> SourceIdentity:
        if self._identity_error is not None:
            raise self._identity_error
        return self._identity


class FakeProbe:
    """Act as one real probe contract with controllable completion and outcomes."""

    def __init__(
        self,
        name: str,
        *,
        target: FactTarget = FactTarget.PRODUCTION,
        ttl: int = 15,
        timeout: int = 3,
        briefing: bool = False,
        value: Mapping[str, object] | None = None,
        error: Exception | None = None,
        hold: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.definition_version = 1
        self.target = target
        self.ttl = timedelta(seconds=ttl)
        self.timeout = timedelta(seconds=timeout)
        self.briefing = briefing
        self.policies = {"late_after_seconds": 300}
        self.value_schema = {"pending": "int"}
        self.value = {"pending": 0} if value is None else value
        self.error = error
        self.hold = hold
        self.runs = 0

    async def measure(self, source: FakeSource) -> Mapping[str, object]:
        self.runs += 1
        if self.hold is not None:
            await self.hold.wait()
        if self.error is not None:
            raise self.error
        return self.value


def identity(port: int = 5432) -> SourceIdentity:
    """Keep test identity literals complete, as production comparison requires."""
    return SourceIdentity("7612696091383607335", "brain", "127.0.0.1", port)


def registry(
    probe: FakeProbe,
    *,
    clock: Clock | None = None,
    source: FakeSource | None = None,
    **kwargs: object,
) -> FactRegistry:
    """Build the actual registry around a source context, never a registry mock."""
    active_clock = Clock() if clock is None else clock
    active_source = FakeSource(identity()) if source is None else source

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeSource]:
        yield active_source

    result = FactRegistry(
        sources={probe.target: factory},
        expected={probe.target: identity()},
        monotonic=active_clock.monotonic,
        wall=active_clock.now,
        **kwargs,
    )
    result.register(probe)
    return result


def test_registration_refuses_closed_or_unverifiable_catalogue_entries() -> None:
    """Composition cannot publish duplicate, frozen or unverified fact declarations."""
    probe = FakeProbe("valid_fact")
    fact_registry = registry(probe)
    with pytest.raises(DuplicateFactError):
        fact_registry.register(probe)
    fact_registry.freeze()
    with pytest.raises(RegistryFrozenError):
        fact_registry.register(FakeProbe("later_fact"))
    with pytest.raises(UnverifiableTargetError):
        FactRegistry(sources={}, expected={}).register(FakeProbe("untrusted_fact"))


@pytest.mark.parametrize("name", ["Bad", "with-dash"])
def test_registration_refuses_invalid_fact_grammar(name: str) -> None:
    """A fact name must be a stable closed-catalogue identifier before registration."""
    with pytest.raises(ValueError):
        registry(FakeProbe(name))


def test_registration_requires_expected_identity_and_cheap_briefing_probe() -> None:
    """Both target trust and briefing latency are admission checks, not runtime surprises."""
    probe = FakeProbe("identity_missing")
    with pytest.raises(UnverifiableTargetError):
        FactRegistry(sources={FactTarget.PRODUCTION: lambda: None}, expected={}).register(probe)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        registry(FakeProbe("slow_briefing", timeout=4, briefing=True))


def test_registration_refuses_non_positive_definition_version() -> None:
    """A descriptor version starts at one so later definition drift is meaningful."""
    probe = FakeProbe("versioned")
    probe.definition_version = 0
    with pytest.raises(ValueError):
        registry(probe)


def test_catalogue_metadata_preserves_order_and_filters_briefing() -> None:
    """The tool-facing catalogue is deterministic and exposes immutable descriptor data."""
    first = FakeProbe("first", briefing=True)
    second = FakeProbe("second")
    fact_registry = registry(first)
    fact_registry.register(second)
    assert fact_registry.names() == ("first", "second")
    assert fact_registry.briefing_names() == ("first",)
    assert fact_registry.describe("first").deadline_seconds == 5
    with pytest.raises(UnknownFactError):
        fact_registry.describe("absent")


async def test_cache_uses_monotonic_ttl_and_preserves_observation_identity() -> None:
    """A backwards wall clock cannot make a measured value appear fresher than its TTL."""
    clock = Clock()
    probe = FakeProbe("cached")
    fact_registry = registry(probe, clock=clock)
    first = await fact_registry.measure("cached")
    assert isinstance(first, Measured)
    clock.wall -= timedelta(hours=1)
    clock.mono += 14
    cached = await fact_registry.measure("cached")
    assert cached.source_kind == "cache"
    assert cached.observation_id == first.observation_id
    clock.mono += 2
    second = await fact_registry.measure("cached")
    assert second.source_kind == "probe"
    assert second.observation_id != first.observation_id
    assert probe.runs == 2


async def test_max_age_zero_bypasses_cache_and_joins_the_owned_run() -> None:
    """Forced readers share one producer even when a usable cache entry already exists."""
    release = asyncio.Event()
    probe = FakeProbe("single_flight", hold=release)
    fact_registry = registry(probe)
    first_reader = asyncio.create_task(fact_registry.measure("single_flight", max_age=timedelta(0)))
    await asyncio.sleep(0)
    second_reader = asyncio.create_task(
        fact_registry.measure("single_flight", max_age=timedelta(0))
    )
    await asyncio.sleep(0)
    assert probe.runs == 1
    release.set()
    first, second = await asyncio.gather(first_reader, second_reader)
    assert first.source_kind == second.source_kind == "probe"
    assert first.observation_id == second.observation_id


async def test_short_positive_max_age_forces_a_refresh_before_ttl() -> None:
    """A caller may ask for stricter freshness without changing the descriptor TTL."""
    clock = Clock()
    probe = FakeProbe("short_age")
    fact_registry = registry(probe, clock=clock)
    first = await fact_registry.measure("short_age")
    clock.mono += 2
    second = await fact_registry.measure("short_age", max_age=timedelta(seconds=1))
    assert second.observation_id != first.observation_id
    assert probe.runs == 2


async def test_negative_max_age_refuses_before_a_probe_runs() -> None:
    """Invalid freshness input cannot consume capacity or execute user-controlled probe code."""
    probe = FakeProbe("negative_age")
    fact_registry = registry(probe)
    with pytest.raises(ValueError):
        await fact_registry.measure("negative_age", max_age=timedelta(seconds=-1))
    assert probe.runs == 0


async def test_unreadable_is_cached_briefly_but_zero_forces_a_retry() -> None:
    """Failure cache avoids stampedes without preventing an explicit fresh investigation."""
    clock = Clock()
    probe = FakeProbe("negative_cache", error=RuntimeError("safe"), ttl=60)
    fact_registry = registry(probe, clock=clock)
    first = await fact_registry.measure("negative_cache")
    assert isinstance(first, Unreadable)
    second = await fact_registry.measure("negative_cache")
    assert second.source_kind == "cache"
    assert probe.runs == 1
    clock.mono += 31
    await fact_registry.measure("negative_cache")
    assert probe.runs == 2
    await fact_registry.measure("negative_cache", max_age=timedelta(0))
    assert probe.runs == 3


async def test_refresh_budget_is_per_fact_and_does_not_replace_a_cache_entry() -> None:
    """An exhausted forced-refresh budget returns its own failure rather than stale data."""
    probe = FakeProbe("refresh")
    fact_registry = registry(probe)
    first = await fact_registry.measure("refresh")
    for _ in range(10):
        await fact_registry.measure("refresh", max_age=timedelta(0))
    limited = await fact_registry.measure("refresh", max_age=timedelta(0))
    assert isinstance(limited, Unreadable)
    assert limited.error_code == "refresh_budget"
    cached = await fact_registry.measure("refresh")
    assert cached.source_kind == "cache"
    assert cached.observation_id != first.observation_id
    assert probe.runs == 11


async def test_refresh_budget_refills_continuously_and_is_shared_by_target() -> None:
    """A target bucket limits multiple fact names while fake monotonic time refills tokens."""
    clock = Clock()
    first = FakeProbe("first_budget")
    second = FakeProbe("second_budget")
    fact_registry = registry(
        first,
        clock=clock,
        refresh_budget=RefreshBudget(per_fact_per_minute=10, per_target_per_minute=2),
    )
    fact_registry.register(second)
    await fact_registry.measure("first_budget", max_age=timedelta(0))
    await fact_registry.measure("first_budget", max_age=timedelta(0))
    saturated = await fact_registry.measure("second_budget", max_age=timedelta(0))
    assert isinstance(saturated, Unreadable)
    assert saturated.error_code == "refresh_budget"
    clock.mono += 30
    refilled = await fact_registry.measure("second_budget", max_age=timedelta(0))
    assert isinstance(refilled, Measured)


async def test_capacity_timeout_does_not_run_a_third_probe() -> None:
    """The global semaphore refuses a queued producer before its probe body starts."""
    release = asyncio.Event()
    first = FakeProbe("capacity_first", hold=release)
    second = FakeProbe("capacity_second", hold=release)
    third = FakeProbe("capacity_third", hold=release)
    fact_registry = registry(first, concurrency=2, queue_timeout=timedelta(seconds=1))
    fact_registry.register(second)
    fact_registry.register(third)
    running = [
        asyncio.create_task(fact_registry.measure(name))
        for name in ("capacity_first", "capacity_second")
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    async def deterministic_wait_for(awaitable: object, seconds: float) -> object:
        if seconds == 1 and fact_registry._semaphore.locked():
            awaitable.close()  # type: ignore[union-attr]
            raise TimeoutError
        return await asyncio.wait_for(awaitable, seconds)  # type: ignore[arg-type]

    fact_registry._wait_for = deterministic_wait_for
    refused = await fact_registry.measure("capacity_third")
    assert isinstance(refused, Unreadable)
    assert refused.error_code == "capacity_timeout"
    assert third.runs == 0
    release.set()
    await asyncio.gather(*running)


async def test_first_and_follower_cancellation_do_not_cancel_the_registry_run() -> None:
    """Shielding leaves the producer alive when either disconnected reader is cancelled."""
    release = asyncio.Event()
    probe = FakeProbe("cancelled_reader", hold=release)
    fact_registry = registry(probe)
    first = asyncio.create_task(fact_registry.measure("cancelled_reader"))
    await asyncio.sleep(0)
    follower = asyncio.create_task(fact_registry.measure("cancelled_reader"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower
    release.set()
    await asyncio.sleep(0)
    result = await fact_registry.measure("cancelled_reader")
    assert isinstance(result, Measured)
    assert probe.runs == 1


async def test_twenty_readers_share_one_observation_and_done_cleanup_is_identity_safe() -> None:
    """An old completion callback cannot evict a newer task registered for the same name."""
    release = asyncio.Event()
    probe = FakeProbe("twenty_readers", hold=release)
    fact_registry = registry(probe)
    readers = [asyncio.create_task(fact_registry.measure("twenty_readers")) for _ in range(20)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert probe.runs == 1
    release.set()
    results = await asyncio.gather(*readers)
    assert {result.observation_id for result in results}.__len__() == 1
    await asyncio.sleep(0)
    assert "twenty_readers" not in fact_registry._inflight
    old = asyncio.create_task(asyncio.sleep(0))
    newer = asyncio.create_task(asyncio.sleep(0))
    fact_registry._inflight["twenty_readers"] = newer  # type: ignore[assignment]
    fact_registry._cleanup_inflight("twenty_readers", old)  # type: ignore[arg-type]
    assert fact_registry._inflight["twenty_readers"] is newer
    await asyncio.gather(old, newer)


async def test_run_failures_are_redacted_and_logged_once_for_many_readers() -> None:
    """One failed producer emits one safe warning despite twenty concurrent consumers."""
    release = asyncio.Event()
    probe = FakeProbe("redacted", error=KeyError("secret-token"), hold=release)
    fact_registry = registry(probe)
    with structlog.testing.capture_logs() as logs:
        readers = [asyncio.create_task(fact_registry.measure("redacted")) for _ in range(20)]
        await asyncio.sleep(0)
        release.set()
        outcomes = await asyncio.gather(*readers)
    assert all(
        isinstance(outcome, Unreadable) and outcome.error_code == "probe_error"
        for outcome in outcomes
    )
    assert "secret-token" not in outcomes[0].where
    assert len([entry for entry in logs if entry["event"] == "fact_measurement_failed"]) == 1


@pytest.mark.parametrize(
    ("probe", "source", "code"),
    [
        (FakeProbe("mismatch"), FakeSource(identity(5433)), "target_mismatch"),
        (
            FakeProbe("identity_failure"),
            FakeSource(identity(), identity_error=OSError()),
            "identity_unreadable",
        ),
        (
            FakeProbe("schema_failure", value={"pending": 0, "extra": 1}),
            FakeSource(identity()),
            "value_not_canonical",
        ),
        (
            FakeProbe("float_failure", value={"pending": 1.5}),
            FakeSource(identity()),
            "value_not_canonical",
        ),
        (
            FakeProbe("large_failure", value={"pending": 0, "big": "x" * 4096}),
            FakeSource(identity()),
            "value_too_large",
        ),
    ],
)
async def test_untrusted_run_outcomes_are_returned_as_closed_codes(
    probe: FakeProbe, source: FakeSource, code: str
) -> None:
    """Identity and canonicality faults turn into data rather than leaking exceptions."""
    if probe.name == "large_failure":
        probe.value_schema = {"pending": "int", "big": "string"}
    result = await registry(probe, source=source).measure(probe.name)
    assert isinstance(result, Unreadable)
    assert result.error_code == code


async def test_measure_many_validates_before_starting_and_honours_zero_budget() -> None:
    """Bad batches run nothing, while elapsed briefing budget leaves facts explicitly unreadable."""
    probe = FakeProbe("batch")
    fact_registry = registry(probe)
    with pytest.raises(ValueError):
        await fact_registry.measure_many(["batch", "batch"])
    with pytest.raises(ValueError):
        await fact_registry.measure_many(["missing"])
    result = await fact_registry.measure_many(["batch"], budget=timedelta(0))
    assert result["batch"].error_code == "briefing_budget"  # type: ignore[union-attr]
    assert probe.runs == 0


async def test_measure_many_refuses_more_than_thirty_two_names_before_work() -> None:
    """The batch limit is checked before any named probe can enter the producer queue."""
    probe = FakeProbe("bounded_batch")
    fact_registry = registry(probe)
    with pytest.raises(ValueError):
        await fact_registry.measure_many(["bounded_batch"] * 33)
    assert probe.runs == 0


async def test_measure_many_finishes_started_work_after_budget_expires() -> None:
    """A budget closes admission, not the probe that was already admitted to the registry."""
    clock = Clock()
    release = asyncio.Event()
    first = FakeProbe("started_batch", hold=release)
    second = FakeProbe("deferred_batch")
    fact_registry = registry(first, clock=clock, concurrency=1)
    fact_registry.register(second)
    batch = asyncio.create_task(
        fact_registry.measure_many(["started_batch", "deferred_batch"], budget=timedelta(seconds=1))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    clock.mono = 1
    release.set()
    results = await batch
    assert isinstance(results["started_batch"], Measured)
    assert isinstance(results["deferred_batch"], Unreadable)
    assert results["deferred_batch"].error_code == "briefing_budget"
    assert second.runs == 0


async def test_aclose_cancels_a_held_run_and_refuses_new_work() -> None:
    """Shutdown resolves owned work before a caller can dispose its source factory."""
    release = asyncio.Event()
    probe = FakeProbe("shutdown", hold=release)
    fact_registry = registry(probe)
    reader = asyncio.create_task(fact_registry.measure("shutdown"))
    await asyncio.sleep(0)
    await fact_registry.aclose()
    result = await reader
    assert isinstance(result, Unreadable)
    assert (result.error_code, result.where) == ("timeout", "cancelled")
    with pytest.raises(RegistryClosedError):
        await fact_registry.measure("shutdown")
    await fact_registry.aclose()
