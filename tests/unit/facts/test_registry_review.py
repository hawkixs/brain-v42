"""Findings of the independent review of the registry (2026-09-20), each pinned red first.

The first implementer's tests were mutation-tested by the reviewer: five
mutations survived. These tests are the ones that kill them, plus the
behaviours the review found unpinned or wrong.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from structlog.testing import capture_logs

from brain_v42.facts import FactTarget, Measured, SourceIdentity, Unreadable
from brain_v42.facts.registry import FactRegistry

from .test_registry import Clock, FakeProbe, FakeSource, identity, registry


@pytest.mark.asyncio
async def test_a_probe_returning_a_non_object_is_unreadable_for_every_reader() -> None:
    """F1: no third state — a `None` value must not escape as a TypeError."""
    probe = FakeProbe("shape")
    probe.value = None  # type: ignore[assignment]
    fact_registry = registry(probe)
    results = await asyncio.gather(*(fact_registry.measure("shape") for _ in range(20)))
    assert probe.runs == 1
    for result in results:
        assert isinstance(result, Unreadable)
        assert result.error_code == "value_not_canonical"


@pytest.mark.parametrize(
    "field,other",
    [
        ("system_identifier", "1"),
        ("database", "brain_test"),
        ("server_addr", "10.0.0.9"),
        ("server_port", 5433),
    ],
)
async def test_an_identity_differing_on_any_single_field_is_a_target_mismatch(
    field: str, other: object
) -> None:
    """F2: all four fields compare; a port-only equality would pass three of these."""
    measured = SourceIdentity(**{**identity().as_dict(), field: other})
    probe = FakeProbe("who")
    fact_registry = registry(probe, source=FakeSource(measured))
    with capture_logs() as records:
        result = await fact_registry.measure("who")
    assert isinstance(result, Unreadable)
    assert result.error_code == "target_mismatch"
    warning = [r for r in records if r.get("error_code") == "target_mismatch"]
    assert len(warning) == 1
    assert warning[0][field] == other


@pytest.mark.asyncio
async def test_a_hanging_probe_is_a_timeout_and_frees_its_slot_and_its_source() -> None:
    """F3: the deadline exists; the source is closed; the semaphore is released."""
    clock = Clock()
    hold = asyncio.Event()
    probe = FakeProbe("slow", timeout=3, hold=hold)
    exits: list[str] = []

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeSource]:
        try:
            yield FakeSource(identity())
        finally:
            exits.append("closed")

    fact_registry = FactRegistry(
        sources={FactTarget.PRODUCTION: factory},
        expected={FactTarget.PRODUCTION: identity()},
        monotonic=clock.monotonic,
        wall=clock.now,
        concurrency=1,
    )
    fact_registry.register(probe)
    other = FakeProbe("quick", timeout=2)  # a different deadline: the fake lets it run
    fact_registry.register(other)

    async def deterministic_wait_for(awaitable, seconds):  # type: ignore[no-untyped-def]
        if seconds == 3:
            # The deadline strikes after the source was opened and the probe is
            # holding: exactly what a real wait_for does to a hanging run.
            task = asyncio.ensure_future(awaitable)
            for _ in range(5):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            raise TimeoutError
        return await awaitable

    fact_registry._wait_for = deterministic_wait_for
    result = await fact_registry.measure("slow")
    assert isinstance(result, Unreadable)
    assert result.error_code == "timeout"
    assert result.where is None
    assert exits == ["closed"]
    quick = await fact_registry.measure("quick")
    assert isinstance(quick, Measured)


@pytest.mark.asyncio
async def test_a_failure_opening_the_source_is_named_as_such_not_as_the_probe() -> None:
    """F4: the probe never ran, so `where` must not claim it did."""
    probe = FakeProbe("closed_door")

    @asynccontextmanager
    async def refusing() -> AsyncIterator[FakeSource]:
        raise ConnectionRefusedError(111, "Connection refused")
        yield FakeSource(identity())  # pragma: no cover

    fact_registry = FactRegistry(
        sources={FactTarget.PRODUCTION: refusing},
        expected={FactTarget.PRODUCTION: identity()},
    )
    fact_registry.register(probe)
    result = await fact_registry.measure("closed_door")
    assert isinstance(result, Unreadable)
    assert result.error_code == "probe_error"
    assert result.where == "ConnectionRefusedError in source_open"
    assert probe.runs == 0


@pytest.mark.asyncio
async def test_cancelling_a_batch_caller_leaves_no_orphan_reader_task() -> None:
    """F5: the shielded run survives; the batch's own reader tasks do not linger."""
    hold = asyncio.Event()
    probe = FakeProbe("batched", hold=hold)
    fact_registry = registry(probe)
    batch = asyncio.create_task(fact_registry.measure_many(["batched"]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    batch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await batch
    await asyncio.sleep(0)
    lingering = {t.get_name() for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    assert lingering == {"fact:batched"}
    hold.set()
    result = await fact_registry.measure("batched")
    assert isinstance(result, Measured)


@pytest.mark.asyncio
async def test_a_capacity_refusal_is_not_cached_and_refunds_the_forced_refresh() -> None:
    """F6: a refusal of the registry is not a verdict on the target; it is not cached."""
    clock = Clock()
    hold = asyncio.Event()
    busy = FakeProbe("busy", hold=hold)
    other = FakeProbe("other")
    fact_registry = registry(busy, clock=clock, concurrency=1, queue_timeout=timedelta(seconds=1))
    fact_registry.register(other)

    async def deterministic_wait_for(awaitable, seconds):  # type: ignore[no-untyped-def]
        if seconds == 1 and fact_registry._semaphore.locked():
            # The queue wait elapses only while the single slot is held.
            task = asyncio.ensure_future(awaitable)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            raise TimeoutError
        return await awaitable

    fact_registry._wait_for = deterministic_wait_for
    first = asyncio.create_task(fact_registry.measure("busy"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    refused = await fact_registry.measure("other", max_age=timedelta(0))
    assert isinstance(refused, Unreadable)
    assert refused.error_code == "capacity_timeout"
    hold.set()
    await first
    # Not cached: the next read runs the probe now that a slot is free.
    served = await fact_registry.measure("other")
    assert isinstance(served, Measured)
    assert other.runs == 1
    # Refunded: ten forced refreshes still fit in the per-fact budget afterwards.
    for _ in range(10):
        outcome = await fact_registry.measure("other", max_age=timedelta(0))
        assert isinstance(outcome, Measured), outcome


@pytest.mark.asyncio
async def test_a_positive_max_age_that_misses_the_cache_is_charged() -> None:
    """F13: the refresh budget is charged by every forced run, not only by zero."""
    clock = Clock()
    probe = FakeProbe("charged", ttl=60)
    fact_registry = registry(probe, clock=clock)
    for _ in range(10):
        clock.mono += 0.2  # the cache entry is 0.2 s old, older than max_age: a forced run
        assert isinstance(
            await fact_registry.measure("charged", max_age=timedelta(milliseconds=100)), Measured
        )
    clock.mono += 0.2  # ten tokens spent in 2.2 s; the bucket refilled about a third of one
    eleventh = await fact_registry.measure("charged", max_age=timedelta(milliseconds=100))
    assert isinstance(eleventh, Unreadable)
    assert eleventh.error_code == "refresh_budget"


@pytest.mark.asyncio
async def test_a_cached_copy_keeps_the_original_instant_and_names_class_and_function() -> None:
    """F13: `measured_at` is the reading's instant, not the serving's; `where` is precise."""
    clock = Clock()
    probe = FakeProbe("stamped")
    fact_registry = registry(probe, clock=clock)
    first = await fact_registry.measure("stamped")
    clock.wall = clock.wall + timedelta(hours=1)
    clock.mono += 5
    cached = await fact_registry.measure("stamped")
    assert isinstance(first, Measured) and isinstance(cached, Measured)
    assert cached.measured_at == first.measured_at
    assert cached.source_kind == "cache"

    failing = FakeProbe("keyed", error=KeyError("secret_value_do_not_log"))
    other_registry = registry(failing)
    outcome = await other_registry.measure("keyed")
    assert isinstance(outcome, Unreadable)
    assert outcome.where == "KeyError in FakeProbe.measure"


@pytest.mark.asyncio
async def test_the_global_bound_holds_across_direct_and_batch_callers() -> None:
    """F13: never more than `concurrency` probes at once, whatever the mix of callers."""
    running = 0
    peak = 0
    release = asyncio.Event()

    class CountingProbe(FakeProbe):
        async def measure(self, source: FakeSource) -> Mapping[str, object]:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await release.wait()
            running -= 1
            return self.value

    probes = [CountingProbe(f"fact_{i}") for i in range(6)]
    fact_registry = registry(probes[0], concurrency=2, queue_timeout=timedelta(seconds=60))
    for probe in probes[1:]:
        fact_registry.register(probe)
    direct = [asyncio.create_task(fact_registry.measure(p.name)) for p in probes[:2]]
    batch = asyncio.create_task(fact_registry.measure_many([p.name for p in probes[2:]]))
    for _ in range(10):
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*direct, batch)
    assert peak == 2


def test_ipv6_spellings_of_one_address_are_one_identity() -> None:
    """F7: the declaration is JSON so IPv6 can be declared — and two spellings are equal."""
    a = SourceIdentity("7612696091383607335", "brain", "::1", 5432)
    b = SourceIdentity("7612696091383607335", "brain", "0:0:0:0:0:0:0:1", 5432)
    c = SourceIdentity(
        "7612696091383607335", "brain", "0000:0000:0000:0000:0000:0000:0000:0001/128", 5432
    )
    assert a == b == c
    with pytest.raises(ValueError, match="server_addr"):
        SourceIdentity("7612696091383607335", "brain", "10.0.0.0/24", 5432)
    with pytest.raises(ValueError, match="system_identifier"):
        SourceIdentity("007", "brain", "::1", 5432)


def test_zero_bounds_are_refused_at_registration() -> None:
    """F10: a zero timeout or queue timeout would make every measurement fail."""
    with pytest.raises(ValueError, match="timeout"):
        registry(FakeProbe("zero", timeout=0))
    with pytest.raises(ValueError, match="queue_timeout"):
        registry(FakeProbe("zero_queue"), queue_timeout=timedelta(0))
