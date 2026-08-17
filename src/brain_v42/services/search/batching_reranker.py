"""Batching transport layer for the reranker HTTP client.

Problem:
    brain_search fans out across up to 6 knowledge-type shards concurrently.
    Each shard calls HybridSearcher → HybridReranker → RerankerClient, producing
    up to 6 parallel POST /rerank HTTP requests with the SAME query but different
    candidate lists.  The GPU reranker processes these serially despite the async
    parallelism, wasting round-trip overhead.

Solution: BatchingRerankerClient
    Wraps any duck-typed inner client that exposes:
        rerank(query: str, candidates: list[str]) -> list[float]
        is_available() -> bool

    When rerank() is called, the request registers in a batch keyed by (query).
    A short asyncio timer (window_seconds, default 20 ms) waits for concurrent
    callers sharing the same query to arrive.  At expiry, ONE inner rerank() call
    is made with all candidates concatenated (stable registration order), then
    the scores are sliced back and each waiting Future receives its offset slice.

Guarantees:
    - Latency bounded: the window fires ONCE per batch.  A call arriving after
      flush creates a new batch immediately — no cascading windows.
    - No deadlock/leak: every Future is resolved (result or exception) before
      _flush() exits.  A broad ``try/BaseException/finally`` in ``_flush`` ensures
      that even unexpected errors (TypeError from a malformed None payload,
      CancelledError if the timer task is cancelled) resolve all pending futures.
      Score-length mismatches are validated before slicing and propagated to all
      participants as a ValueError.
    - Error coherence: a single inner exception is forwarded to ALL participants
      so that all shards fall back to rrf_fallback together (uniform degraded
      state, not partial).
    - Transparent to callers above (HybridReranker, InstrumentedReranker):
      same duck-typed interface, same exception semantics.

Threading model: asyncio-only; no threads.

Usage in build_services (server.py):
    # Wrap only the HybridReranker path — ClusterGuard/FeatureDedupJob
    # use solo calls (no benefit from batching, just added latency):
    batching_reranker = BatchingRerankerClient(reranker_client, window_seconds=0.02)
    hybrid_reranker = HybridReranker(client=batching_reranker)

Metrics note:
    InstrumentedReranker sits ABOVE HybridReranker, which sits ABOVE this class.
    The latency it measures therefore includes the coalescing window.  This is
    intentional and documented: the window (≤ 20 ms) is a one-time overhead per
    search, not per shard, so the total wall-clock time decreases despite the
    slightly higher per-call metric.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def _sanitized_batch_exception(exc: Exception) -> Exception:
    """Preserve safe builtin error categories without retaining their content."""
    exception_type = type(exc)
    message = f"batching_reranker: inner rerank failed ({exception_type.__name__})"
    if exception_type.__module__ == "builtins":
        try:
            return exception_type(message)
        except Exception:
            pass
    return RuntimeError(message)


class _PendingBatch:
    """One in-flight coalescing batch for a single query string.

    Lifecycle:
        1. Created by the first caller for a given query.
        2. A timer task is created that calls _flush() after window_seconds.
        3. Additional callers register themselves via _add_participant().
        4. When the timer fires, _flush() runs:
           - Removes the batch from the shared map (so late callers start fresh).
           - Calls the inner client once with all concatenated candidates.
           - Validates score-length matches total candidates before slicing.
           - Resolves each participant's Future with its offset score slice.
           - On any error (inner client exception, score-length mismatch,
             CancelledError from timer cancellation, or any other BaseException):
             the ``finally`` block ensures ALL participant futures are resolved
             (with the propagating exception) so no caller hangs forever.
    """

    def __init__(
        self,
        query: str,
        inner: Any,
        window_seconds: float,
        batch_map: dict[str, Any],
    ) -> None:
        self._query = query
        self._inner = inner
        self._batch_map = batch_map

        # Ordered list of (candidates, future) for stable offset slicing
        self._participants: list[tuple[list[str], asyncio.Future[list[float]]]] = []
        self._flushed = False

        # Use get_running_loop() — idiomatic for coroutines running inside an
        # event loop (get_event_loop() is deprecated for this use case in 3.10+
        # and raises DeprecationWarning when called outside a running loop).
        loop = asyncio.get_running_loop()
        self._timer_task: asyncio.Task[None] = loop.create_task(
            self._wait_and_flush(window_seconds),
            name="batching_reranker.flush",
        )

    def _add_participant(
        self,
        candidates: list[str],
        future: asyncio.Future[list[float]],
    ) -> None:
        """Register a new caller in this batch.

        Must only be called while self._flushed is False (enforced by the
        caller, which checks this under a consistent view of _batch_map).
        """
        self._participants.append((candidates, future))

    async def _wait_and_flush(self, window_seconds: float) -> None:
        """Timer coroutine: sleep the window duration, then flush."""
        await asyncio.sleep(window_seconds)
        await self._flush()

    async def _flush(self) -> None:
        """Execute the single coalesced inner call and distribute results.

        Thread-safety: asyncio is single-threaded; no lock needed.
        The batch is removed from batch_map before the inner call so that
        any new callers arriving concurrently during the inner HTTP round-trip
        will create a fresh batch (bounded latency: no cascading windows).

        Hang/leak guarantee:
            The entire flush logic is wrapped in ``try/BaseException/finally``.
            The ``finally`` block calls _resolve_all_pending(sentinel_exc) which
            sets an exception on any future not yet resolved.  This covers:
              - TypeError from slicing a None payload (inner returns None).
              - CancelledError if this coroutine or the timer task is cancelled.
              - Any other unexpected BaseException escaping from the scoring loop.
            For CancelledError specifically, the exception is re-raised after
            resolving futures so asyncio's task machinery is not confused.
        """
        # Remove ourselves from the shared map FIRST — any call arriving
        # after this point will create a new batch (bounded window guarantee).
        self._batch_map.pop(self._query, None)
        self._flushed = True

        if not self._participants:
            # Degenerate case: timer fired before any participant registered.
            return

        # Concatenate all candidates in registration order (stable)
        all_candidates: list[str] = []
        offsets: list[int] = []
        for candidates, _ in self._participants:
            offsets.append(len(all_candidates))
            all_candidates.extend(candidates)

        logger.debug(
            "batching_reranker.flush",
            query_length=len(self._query),
            n_participants=len(self._participants),
            n_candidates_total=len(all_candidates),
        )

        try:
            # --- inner HTTP call ---
            try:
                all_scores: list[float] = await self._inner.rerank(self._query, all_candidates)
            except Exception as exc:
                # Error coherence: all participants fall back together.
                logger.warning(
                    "batching_reranker.inner_error",
                    n_participants=len(self._participants),
                    exception_type=type(exc).__name__,
                )
                self._resolve_all_pending(_sanitized_batch_exception(exc))
                return

            # --- score-length validation ---
            # Must happen BEFORE slicing so a mismatch raises a clean exception
            # that the outer try/BaseException can catch and propagate to all futures.
            if len(all_scores) != len(all_candidates):
                mismatch_exc = ValueError(
                    f"batching_reranker: inner rerank returned {len(all_scores)} scores "
                    f"for {len(all_candidates)} candidates"
                )
                logger.warning(
                    "batching_reranker.score_length_mismatch",
                    query_length=len(self._query),
                    expected=len(all_candidates),
                    got=len(all_scores),
                )
                self._resolve_all_pending(mismatch_exc)
                return

            # --- offset slicing: distribute scores to each participant ---
            for i, (candidates, future) in enumerate(self._participants):
                start = offsets[i]
                end = start + len(candidates)
                if not future.done():
                    future.set_result(all_scores[start:end])

        except BaseException as exc:
            # Catches anything that escaped the inner try/except above —
            # primarily TypeError (e.g. all_scores is None and subscripting fails)
            # and CancelledError (BaseException since Python 3.8).
            # Resolve all still-pending futures so callers are not left hanging.
            self._resolve_all_pending(
                RuntimeError(f"batching_reranker: flush aborted ({type(exc).__name__})")
            )
            if isinstance(exc, asyncio.CancelledError):
                raise  # Must propagate CancelledError for asyncio task machinery.
        finally:
            # Last-resort net: in case any future was left unresolved by a
            # code path we did not anticipate.  No-op if all futures are done.
            self._resolve_all_pending(
                RuntimeError("batching_reranker: flush exited without resolving future")
            )

    def _resolve_all_pending(self, exc: BaseException) -> None:
        """Set ``exc`` on every participant future that has not yet been resolved."""
        for _, future in self._participants:
            if not future.done():
                future.set_exception(exc)


class BatchingRerankerClient:
    """Coalescing transport wrapper around a duck-typed reranker client.

    Reduces N concurrent HTTP /rerank calls (same query, different candidates)
    to 1 coalesced call per window, transparently slicing scores back to each
    caller.

    Args:
        inner: Any object exposing rerank(query, candidates) -> list[float]
               and is_available() -> bool.  Typically a RerankerClient.
        window_seconds: Coalescing window duration in seconds (default 0.02 = 20 ms).
                        Too small: no coalescing benefit; too large: added latency.
                        20 ms is a safe default for local-network GPU service.
    """

    def __init__(
        self,
        inner: Any,
        window_seconds: float = 0.02,
    ) -> None:
        self._inner = inner
        self._window_seconds = window_seconds
        # Active batches keyed by query string.
        # A batch is removed before its inner HTTP call fires so that
        # late-joining callers create a fresh batch (bounded window).
        self._batches: dict[str, _PendingBatch] = {}

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        """Score candidates via the coalesced reranker.

        Registers the call in the current batch for this query (or creates a new
        batch if none is active).  Waits for the batch's coalescing window to
        fire and returns this caller's score slice.

        Empty candidates list: returns [] immediately without joining any batch.

        Args:
            query: The query string (batching key).
            candidates: Candidate texts to score.

        Returns:
            list[float] of scores, one per candidate, in input order.

        Raises:
            Any exception raised by the inner client is propagated to all
            callers in the same batch (error coherence).
        """
        if not candidates:
            return []

        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[float]] = loop.create_future()

        # Check if there is an active (not-yet-flushed) batch for this query.
        batch = self._batches.get(query)
        if batch is None or batch._flushed:
            # Create a new batch for this query.
            batch = _PendingBatch(
                query=query,
                inner=self._inner,
                window_seconds=self._window_seconds,
                batch_map=self._batches,
            )
            self._batches[query] = batch

        batch._add_participant(candidates, future)

        # Await the future; handle cancellation without corrupting the batch.
        try:
            return await future
        except asyncio.CancelledError:
            # Cancel this participant's future if not already resolved.
            if not future.done():
                future.cancel()
            raise

    async def is_available(self) -> bool:
        """Check if the underlying reranker service is healthy.

        Delegates directly to the inner client — no batching needed for health checks.

        Returns:
            True if the inner service is available, False otherwise.
        """
        return await self._inner.is_available()  # type: ignore[no-any-return]
