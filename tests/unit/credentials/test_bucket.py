"""Exercise admission with virtual time instead of sleeping."""

from brain_v42.credentials.bucket import TokenBucket


def test_burst_starts_full_then_refuses() -> None:
    bucket = TokenBucket(1.0, 3, monotonic=lambda: 0.0)
    assert [bucket.allow("client") for _ in range(4)] == [True, True, True, False]


def test_refill_is_continuous() -> None:
    now = [0.0]
    bucket = TokenBucket(2.0, 1, monotonic=lambda: now[0])
    assert bucket.allow("client")
    now[0] = 0.49
    assert not bucket.allow("client")
    now[0] = 0.5
    assert bucket.allow("client")


def test_keys_have_independent_allowances() -> None:
    bucket = TokenBucket(1.0, 1, monotonic=lambda: 0.0)
    assert bucket.allow("a")
    assert not bucket.allow("a")
    assert bucket.allow("b")


def test_eviction_is_bounded_and_least_recently_used() -> None:
    bucket = TokenBucket(1.0, 1, monotonic=lambda: 0.0, max_keys=2)
    assert bucket.allow("a")
    assert bucket.allow("b")
    assert not bucket.allow("a")
    assert bucket.allow("c")
    assert not bucket.allow("a")
    assert bucket.allow("b")
    assert len(bucket._buckets) == 2


def test_elevation_profile_allows_one_request_per_ten_seconds() -> None:
    now = [0.0]
    bucket = TokenBucket(0.1, 3, monotonic=lambda: now[0])
    assert all(bucket.allow("client") for _ in range(3))
    assert not bucket.allow("client")
    now[0] = 9.0
    assert not bucket.allow("client")
    now[0] = 10.0
    assert bucket.allow("client")


def test_idle_refill_never_exceeds_burst() -> None:
    now = [0.0]
    bucket = TokenBucket(1.0, 1, monotonic=lambda: now[0])
    assert bucket.allow("client")
    now[0] = 100.0
    assert bucket.allow("client")
    assert not bucket.allow("client")
