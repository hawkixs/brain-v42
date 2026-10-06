"""Bound admission and memory independently for each credential client."""

import math
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True, slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucket:
    def __init__(
        self,
        rate_per_second: float,
        burst: int,
        *,
        monotonic: Callable[[], float],
        max_keys: int = 1024,
    ) -> None:
        if not math.isfinite(rate_per_second) or rate_per_second <= 0:
            raise ValueError("rate must be finite and positive")
        if burst < 1 or max_keys < 1:
            raise ValueError("burst and max_keys must be positive")
        self._rate = rate_per_second
        self._burst = burst
        self._monotonic = monotonic
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        """Touch refused keys too: LRU measures use, rather than successful admission."""
        with self._lock:
            now = self._monotonic()
            previous = self._buckets.pop(key, _Bucket(float(self._burst), now))
            tokens = min(
                float(self._burst),
                previous.tokens + max(0.0, now - previous.updated_at) * self._rate,
            )
            allowed = tokens >= 1.0
            self._buckets[key] = _Bucket(tokens - 1.0 if allowed else tokens, now)
            if len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
            return allowed
