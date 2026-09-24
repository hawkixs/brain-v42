"""TTL memo with single-flight coalescing for slow `/metrics` collector blocks.

Decision 1669d429 item 2: `/metrics` is polled by red-monitor roughly every 5s,
and the dream, nightly-ops and graph-inventory collectors it assembles together
run on the order of fifteen PostgreSQL queries plus a Neo4j round trip on EVERY
poll, uncached. `database` and the embedding healthcheck are deliberately kept
out of this cache -- they stay live on every poll (operator decision).

Two rules the memo enforces so a new block (the `tickets` block) can opt in
with one line and inherit both for free:

- **Single-flight**: concurrent callers who arrive while a key is being
  recomputed await the SAME in-flight computation rather than each starting
  their own query set. A `/metrics` stampede (N red-monitor pollers landing at
  once right after a TTL expiry) must cost one recomputation, not N.
- **A raised exception is never cached as a success for the full TTL.** Two
  shapes exist among the collectors this cache wraps:
  - ``collector_tickets.py`` does not catch its own exceptions at all: a SQL
    failure reaches this cache as a plain exception, degrades to ``None``
    ("not measured", same contract as everywhere else in this payload), and
    is memoized only for the short ``error_ttl_seconds``.
  - ``collector_dream.py``, ``collector_nightly.py`` and
    ``collector_db.py::collect_graph_inventory`` catch their own SQL/Neo4j
    failures internally and already had a degraded value ready to return
    (``{}``, or a partial dict) -- returning it plainly used to be
    indistinguishable from a legitimate success and was memoized for the
    FULL ``ttl_seconds`` (the bug this module now closes). Those collectors
    instead **raise** :class:`CollectorDegraded`, carrying that exact
    already-computed value as ``.payload``. This cache recognises it
    specially: the payload is published UNCHANGED (red-monitor sees the same
    shape as before -- ``{}``, or the partial dict, still gains
    ``generated_at`` when truthy, exactly like a success) but memoized only
    for ``error_ttl_seconds``, so a transient failure is retried on roughly
    the next poll instead of blacking out the panel for a full refresh
    window.

  A plain (non-:class:`CollectorDegraded`) exception still degrades to
  ``None`` under the short TTL, unchanged from before -- `CollectorDegraded`
  is additive, not a replacement for that fallback.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def _default_wall_clock() -> datetime:
    return datetime.now(UTC)


class _Missing:
    """Sentinel distinguishing "no fresh entry" from a fresh entry whose value is `None`.

    A cached failure legitimately memoizes ``None`` (decision: a raised
    exception degrades like every other collector failure — "not measured").
    Using bare ``None`` as the "cache miss" marker would make that memoized
    failure indistinguishable from a miss and defeat the whole point of the
    short error TTL: every caller would recompute anyway.
    """


_MISSING = _Missing()


class CollectorDegraded(Exception):
    """Raised by a cached collector to signal a failed or partial result explicitly.

    A collector that already builds its own degraded value (``{}``, or a
    partial dict) instead of letting an exception escape raises this with
    that exact value as ``payload`` -- see the module docstring. SlowBlockCache
    stores and returns ``payload`` unchanged (so the published shape is
    whatever the collector always returned here), but under the short
    ``error_ttl_seconds`` instead of the normal ``ttl_seconds``.
    """

    def __init__(self, payload: dict[str, Any] | None) -> None:
        super().__init__("collector result is degraded; see .payload")
        self.payload = payload


class SlowBlockCache:
    """Per-key TTL memo + single-flight for the slow `/metrics` collector blocks.

    ``clock`` and ``wall_clock`` are injectable so tests can advance time
    without sleeping: ``clock`` (monotonic-shaped, seconds) drives TTL expiry,
    ``wall_clock`` (a ``datetime`` factory) stamps ``generated_at``. Real
    defaults (`time.monotonic`, `datetime.now(UTC)`) are used outside tests.

    No unbounded growth: the key set is closed by construction -- callers pass
    the fixed block names they wired at the call site (``"dream"``,
    ``"nightly"``, ``"graph_inventory"``, ...), never request-derived data.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        error_ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _default_wall_clock,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._error_ttl_seconds = error_ttl_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._entries: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def get(
        self,
        key: str,
        compute: Callable[[], Awaitable[dict[str, Any] | None]],
    ) -> dict[str, Any] | None:
        """Return the memoized block for ``key``, recomputing it once if stale.

        ``compute`` must return the block already fully assembled (a caller
        combining several collector calls into one dict does that combining
        INSIDE ``compute``, not around this call) -- ``generated_at`` is added
        to the result here, once, at computation time.
        """
        fresh = self._fresh_entry(key)
        if not isinstance(fresh, _Missing):
            return fresh

        async with self._lock_for(key):
            # Re-check: another caller may have refreshed this key while we
            # were waiting for the lock -- that is the single-flight coalescing.
            fresh = self._fresh_entry(key)
            if not isinstance(fresh, _Missing):
                return fresh

            try:
                value = await compute()
            except CollectorDegraded as degraded:
                logger.warning("metrics.slow_block_cache.compute_degraded", key=key, exc_info=True)
                return self._store(key, degraded.payload, ttl=self._error_ttl_seconds)
            except Exception:
                logger.warning("metrics.slow_block_cache.compute_failed", key=key, exc_info=True)
                self._entries[key] = (self._clock() + self._error_ttl_seconds, None)
                return None

            return self._store(key, value, ttl=self._ttl_seconds)

    def _store(
        self, key: str, value: dict[str, Any] | None, *, ttl: float
    ) -> dict[str, Any] | None:
        """Stamp a truthy value with `generated_at` and memoize it for `ttl`.

        Shared by the success path and the `CollectorDegraded` path so a
        degraded-but-truthy payload (e.g. nightly's killswitches-only partial
        dict) is stamped exactly like a success would be -- only the TTL
        differs, never the shape.
        """
        if value:
            value = {**value, "generated_at": self._wall_clock().isoformat()}
        self._entries[key] = (self._clock() + ttl, value)
        return value

    def _fresh_entry(self, key: str) -> dict[str, Any] | None | _Missing:
        entry = self._entries.get(key)
        if entry is None:
            return _MISSING
        expires_at, value = entry
        if self._clock() >= expires_at:
            return _MISSING
        return value
