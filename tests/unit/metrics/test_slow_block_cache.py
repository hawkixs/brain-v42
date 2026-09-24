"""Tests for SlowBlockCache — TTL memo + single-flight for slow /metrics collectors.

Decision 1669d429 item 2: the dream, nightly-ops and graph-inventory collectors
together run on the order of fifteen PostgreSQL queries plus a Neo4j round trip,
uncached, on every ~5s red-monitor poll. This memo turns N concurrent pollers
into one in-flight computation per TTL window, without stampeding the database
on refresh and without caching a raised exception as a success for the full TTL.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from brain_v42.metrics.slow_block_cache import SlowBlockCache


def _clock_from(box: list[float]):
    """A monotonic-shaped clock whose value is whatever the test put in ``box``."""

    def _clock() -> float:
        return box[0]

    return _clock


def _fixed_wall_clock(value: datetime):
    def _wall_clock() -> datetime:
        return value

    return _wall_clock


async def test_cache_hit_within_ttl_does_not_recompute() -> None:
    calls = 0

    async def compute() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"n": calls}

    cache = SlowBlockCache(
        ttl_seconds=30.0,
        error_ttl_seconds=5.0,
        clock=_clock_from([0.0]),
        wall_clock=_fixed_wall_clock(datetime(2026, 1, 1, tzinfo=UTC)),
    )

    first = await cache.get("dream", compute)
    second = await cache.get("dream", compute)

    assert calls == 1
    assert first == second == {"n": 1, "generated_at": "2026-01-01T00:00:00+00:00"}


async def test_refresh_after_ttl_recomputes() -> None:
    calls = 0
    time_box = [0.0]

    async def compute() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"n": calls}

    cache = SlowBlockCache(
        ttl_seconds=30.0,
        error_ttl_seconds=5.0,
        clock=_clock_from(time_box),
        wall_clock=_fixed_wall_clock(datetime(2026, 1, 1, tzinfo=UTC)),
    )

    await cache.get("dream", compute)
    time_box[0] = 30.1  # past the 30s TTL
    result = await cache.get("dream", compute)

    assert calls == 2
    assert result == {"n": 2, "generated_at": "2026-01-01T00:00:00+00:00"}


async def test_single_flight_coalesces_concurrent_callers() -> None:
    """N concurrent callers during a refresh await the SAME computation, never stampede."""
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def compute() -> dict[str, int]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"n": calls}

    cache = SlowBlockCache(
        ttl_seconds=30.0,
        error_ttl_seconds=5.0,
        clock=_clock_from([0.0]),
        wall_clock=_fixed_wall_clock(datetime(2026, 1, 1, tzinfo=UTC)),
    )

    async def caller() -> dict[str, int] | None:
        return await cache.get("dream", compute)

    tasks = [asyncio.create_task(caller()) for _ in range(5)]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks)

    assert calls == 1
    assert all(r == {"n": 1, "generated_at": "2026-01-01T00:00:00+00:00"} for r in results)


async def test_a_raised_exception_degrades_to_none_and_never_crashes() -> None:
    async def compute() -> dict[str, int]:
        raise RuntimeError("boom")

    cache = SlowBlockCache(
        ttl_seconds=30.0,
        error_ttl_seconds=5.0,
        clock=_clock_from([0.0]),
        wall_clock=_fixed_wall_clock(datetime(2026, 1, 1, tzinfo=UTC)),
    )

    result = await cache.get("dream", compute)

    assert result is None


async def test_an_exception_is_cached_only_for_the_short_error_ttl_not_the_full_ttl() -> None:
    calls = 0
    time_box = [0.0]

    async def compute() -> dict[str, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return {"n": calls}

    cache = SlowBlockCache(
        ttl_seconds=30.0,
        error_ttl_seconds=5.0,
        clock=_clock_from(time_box),
        wall_clock=_fixed_wall_clock(datetime(2026, 1, 1, tzinfo=UTC)),
    )

    first = await cache.get("dream", compute)
    assert first is None
    assert calls == 1

    # Still inside the error TTL (5s): the failure stays memoized, no retry yet.
    time_box[0] = 4.9
    still_cached_failure = await cache.get("dream", compute)
    assert still_cached_failure is None
    assert calls == 1

    # Past the error TTL: the next call recomputes rather than waiting the full
    # success TTL (30s) out — a raised exception is never cached as a success.
    time_box[0] = 5.1
    recovered = await cache.get("dream", compute)
    assert recovered == {"n": 2, "generated_at": "2026-01-01T00:00:00+00:00"}
    assert calls == 2


async def test_generated_at_is_stable_across_hits_within_ttl() -> None:
    wall_box = [datetime(2026, 1, 1, tzinfo=UTC)]

    async def compute() -> dict[str, int]:
        return {"n": 1}

    def wall_clock() -> datetime:
        return wall_box[0]

    cache = SlowBlockCache(
        ttl_seconds=30.0, error_ttl_seconds=5.0, clock=_clock_from([0.0]), wall_clock=wall_clock
    )

    first = await cache.get("dream", compute)
    wall_box[0] = datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)  # wall clock moved, cache did not
    second = await cache.get("dream", compute)

    assert first is not None and second is not None
    assert first["generated_at"] == second["generated_at"] == "2026-01-01T00:00:00+00:00"


async def test_an_empty_falsy_result_is_a_legitimate_value_not_an_error() -> None:
    """{} from a collector means "nothing to report", not a failure — no generated_at added."""
    calls = 0

    async def compute() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {}

    cache = SlowBlockCache(
        ttl_seconds=30.0,
        error_ttl_seconds=5.0,
        clock=_clock_from([0.0]),
        wall_clock=_fixed_wall_clock(datetime(2026, 1, 1, tzinfo=UTC)),
    )

    result = await cache.get("dream", compute)
    second = await cache.get("dream", compute)

    assert result == {}
    assert second == {}
    assert calls == 1  # cached at the normal (success) TTL, not the error one


async def test_distinct_keys_are_memoized_independently() -> None:
    async def compute_dream() -> dict[str, str]:
        return {"who": "dream"}

    async def compute_nightly() -> dict[str, str]:
        return {"who": "nightly"}

    cache = SlowBlockCache(
        ttl_seconds=30.0,
        error_ttl_seconds=5.0,
        clock=_clock_from([0.0]),
        wall_clock=_fixed_wall_clock(datetime(2026, 1, 1, tzinfo=UTC)),
    )

    dream = await cache.get("dream", compute_dream)
    nightly = await cache.get("nightly", compute_nightly)

    assert dream is not None and dream["who"] == "dream"
    assert nightly is not None and nightly["who"] == "nightly"


async def test_default_clocks_need_no_injection_for_a_smoke_call() -> None:
    """Defaults exist (real monotonic/wall clocks) even though every other test injects one."""

    async def compute() -> dict[str, int]:
        return {"n": 1}

    cache = SlowBlockCache(ttl_seconds=30.0, error_ttl_seconds=5.0)

    result = await cache.get("dream", compute)

    assert result is not None
    assert result["n"] == 1
    assert isinstance(result["generated_at"], str)


if __name__ == "__main__":
    pytest.main([__file__])
