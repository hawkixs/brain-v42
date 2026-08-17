"""TDD tests for BatchingRerankerClient.

Design under test (batching_reranker.py):
  - BatchingRerankerClient wraps a duck-typed inner client that exposes
    rerank(query, candidates) -> list[float] and is_available() -> bool.
  - When rerank() is called, the request joins a batch keyed by query string.
  - A short asyncio timer (window_seconds, default 0.02s) lets concurrent callers
    coalesce into one HTTP call. At expiry, a SINGLE inner rerank() call is made
    with the concatenation of all participants' candidates.
  - Scores are sliced back by offset and each participant's Future receives its
    correct offset slice (not the head slice).
  - An error from the inner client is propagated to ALL participants in the batch.
  - Queries that arrive after the flush create a new batch (latency bounded: window
    fires exactly once per batch).
  - Queries with different query strings → independent batches, independent calls.
  - is_available() delegates to the inner client without batching.
  - Solo call (only one participant when window fires) → passthrough (no extra alloc).
  - finally guarantee: all futures are resolved even on inner TypeError or
    CancelledError — no permanent hangs.

Concurrency: tests use asyncio.gather() to simulate parallel callers.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

# ---------------------------------------------------------------------------
# Shared score fixture — content-derived so offset correctness can be asserted
# ---------------------------------------------------------------------------

# Stable mapping: candidate text → expected score.
# Inner mock returns [SCORE[c] for c in candidates] so that we can assert
# exact values regardless of asyncio scheduling order.
SCORE: dict[str, float] = {
    "a1": 10.0,
    "a2": 20.0,
    "b1": 30.0,
    "c1": 40.0,
    "c2": 50.0,
    "c3": 60.0,
    # extras
    "A": 11.0,
    "B": 22.0,
    "C": 33.0,
}


def _make_content_inner() -> MagicMock:
    """Return a mock inner client whose scores are content-derived via SCORE."""
    inner = MagicMock()

    async def content_rerank(query: str, candidates: list[str]) -> list[float]:
        return [SCORE[c] for c in candidates]

    inner.rerank = content_rerank
    return inner


class TestBatchingRerankerClientCoalescing:
    """Core coalescing behaviour: N concurrent same-query calls → 1 inner call."""

    @pytest.mark.asyncio
    async def test_three_concurrent_same_query_one_inner_call(self) -> None:
        """3 concurrent rerank() for the same query → exactly 1 inner HTTP call."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        call_count = 0
        inner = MagicMock()

        async def counting_rerank(query: str, candidates: list[str]) -> list[float]:
            nonlocal call_count
            call_count += 1
            return [SCORE[c] for c in candidates]

        inner.rerank = counting_rerank

        client = BatchingRerankerClient(inner, window_seconds=0.01)

        scores_a, scores_b, scores_c = await asyncio.gather(
            client.rerank("what is brain_v42?", ["a1", "a2"]),
            client.rerank("what is brain_v42?", ["b1"]),
            client.rerank("what is brain_v42?", ["c1", "c2", "c3"]),
        )

        # Exactly ONE inner call
        assert call_count == 1

        # Correct scores by value (not just length)
        assert scores_a == [SCORE["a1"], SCORE["a2"]]
        assert scores_b == [SCORE["b1"]]
        assert scores_c == [SCORE["c1"], SCORE["c2"], SCORE["c3"]]

    @pytest.mark.asyncio
    async def test_concatenated_candidates_stable_order(self) -> None:
        """Inner call receives candidates in stable registration order."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        captured_candidates: list[list[str]] = []

        inner = MagicMock()

        async def capture_rerank(query: str, candidates: list[str]) -> list[float]:
            captured_candidates.append(list(candidates))
            # Use index-based scores so any ordering is distinguishable
            return [float(i + 1) * 5.0 for i in range(len(candidates))]

        inner.rerank = capture_rerank

        client = BatchingRerankerClient(inner, window_seconds=0.01)

        scores_a, scores_bc = await asyncio.gather(
            client.rerank("query", ["A"]),
            client.rerank("query", ["B", "C"]),
        )

        # Only one inner call
        assert len(captured_candidates) == 1
        total = captured_candidates[0]
        # Total must have 3 items: A + B + C (or B+C then A — registration order)
        assert len(total) == 3
        # Check set membership (scheduling determines first-registered)
        assert set(total) == {"A", "B", "C"}

        # Each caller must get exactly len(their candidates) scores
        assert len(scores_a) == 1
        assert len(scores_bc) == 2

        # Scores must be contiguous slices of the inner result — verify by
        # reconstructing offsets from the captured order:
        #   participant-0 starts at 0, participant-1 at len(participant-0's list)
        # We don't know scheduling order, so assert by reconstruction:
        full_scores = [float(i + 1) * 5.0 for i in range(3)]
        if total[0] == "A":
            # A was first: offset 0 (len=1), BC at offset 1 (len=2)
            assert scores_a == full_scores[:1]
            assert scores_bc == full_scores[1:3]
        else:
            # BC was first: offset 0 (len=2), A at offset 2 (len=1)
            assert scores_bc == full_scores[:2]
            assert scores_a == full_scores[2:3]

    @pytest.mark.asyncio
    async def test_scores_correctly_redistributed(self) -> None:
        """Each participant receives the CORRECT OFFSET SLICE of scores.

        Mutation guard: replacing all_scores[start:end] with all_scores[:len(candidates)]
        (head-slice bug) would make scores_b == [10.0] and scores_c == [10.0, 20.0, 30.0]
        instead of [30.0] and [40.0, 50.0, 60.0] — this test catches that mutation.
        """
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        inner = _make_content_inner()
        client = BatchingRerankerClient(inner, window_seconds=0.01)

        # Register in a predictable sequential order so offsets are deterministic.
        # asyncio.gather launches all coroutines in order; in a single-threaded
        # event loop the first await point (create_future) registers them in
        # gather order.
        cands_a = ["a1", "a2"]  # offset 0 → scores [10.0, 20.0]
        cands_b = ["b1"]  # offset 2 → scores [30.0]
        cands_c = ["c1", "c2", "c3"]  # offset 3 → scores [40.0, 50.0, 60.0]

        scores_a, scores_b, scores_c = await asyncio.gather(
            client.rerank("q", cands_a),
            client.rerank("q", cands_b),
            client.rerank("q", cands_c),
        )

        # Exact value assertions — offset correctness, not just length
        assert scores_a == [10.0, 20.0], f"Expected [10.0, 20.0] got {scores_a}"
        assert scores_b == [30.0], f"Expected [30.0] got {scores_b}"
        assert scores_c == [40.0, 50.0, 60.0], f"Expected [40.0, 50.0, 60.0] got {scores_c}"


class TestBatchingRerankerClientIsolation:
    """Different queries → separate batches; separate inner calls."""

    @pytest.mark.asyncio
    async def test_different_queries_get_separate_inner_calls(self) -> None:
        """Concurrent calls with different queries produce independent inner calls."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        inner = AsyncMock()
        inner.rerank = AsyncMock(side_effect=lambda q, c: [0.5] * len(c))

        client = BatchingRerankerClient(inner, window_seconds=0.01)

        scores_x, scores_y = await asyncio.gather(
            client.rerank("query X", ["X1", "X2"]),
            client.rerank("query Y", ["Y1"]),
        )

        # Two distinct queries → two inner calls
        assert inner.rerank.await_count == 2
        assert len(scores_x) == 2
        assert len(scores_y) == 1

    @pytest.mark.asyncio
    async def test_different_query_calls_with_correct_texts(self) -> None:
        """Each separate-query batch is called with its own candidates only."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        captured: dict[str, list[str]] = {}

        async def capture_rerank(query: str, candidates: list[str]) -> list[float]:
            captured[query] = candidates
            return [0.0] * len(candidates)

        inner = MagicMock()
        inner.rerank = capture_rerank

        client = BatchingRerankerClient(inner, window_seconds=0.01)

        await asyncio.gather(
            client.rerank("alpha", ["alpha-cand"]),
            client.rerank("beta", ["beta-cand-1", "beta-cand-2"]),
        )

        assert set(captured.keys()) == {"alpha", "beta"}
        assert captured["alpha"] == ["alpha-cand"]
        assert captured["beta"] == ["beta-cand-1", "beta-cand-2"]


class TestBatchingRerankerClientErrorPropagation:
    """Errors from inner client are propagated to ALL participants in the batch."""

    @pytest.mark.asyncio
    async def test_inner_error_propagated_to_all_batch_participants(self) -> None:
        """When inner.rerank raises, all callers in the same batch see the exception."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        inner = AsyncMock()
        inner.rerank = AsyncMock(side_effect=RuntimeError("GPU offline"))

        client = BatchingRerankerClient(inner, window_seconds=0.01)

        results = await asyncio.gather(
            client.rerank("query", ["cand A"]),
            client.rerank("query", ["cand B"]),
            client.rerank("query", ["cand C"]),
            return_exceptions=True,
        )

        # All three must have received the exception (not just one)
        for r in results:
            assert isinstance(r, RuntimeError), (
                f"Expected RuntimeError but got {type(r).__name__}: {r}"
            )

        # Only one inner call was made despite 3 concurrent callers
        inner.rerank.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_inner_error_preserves_builtin_category_without_leaking_content(self) -> None:
        """Fallback stays category-compatible but strips query and exception text."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        secret_marker = "Bearer SYNTHETIC_RERANKER_SECRET"
        inner = AsyncMock()
        inner.rerank = AsyncMock(side_effect=ValueError(secret_marker))
        client = BatchingRerankerClient(inner, window_seconds=0.01)

        with structlog.testing.capture_logs() as logs:
            result = await asyncio.gather(
                client.rerank(secret_marker, ["candidate"]),
                return_exceptions=True,
            )

        assert isinstance(result[0], ValueError)
        assert secret_marker not in str(result[0])
        assert secret_marker not in repr(logs)

    @pytest.mark.asyncio
    async def test_error_from_one_batch_does_not_affect_other_query(self) -> None:
        """Error in batch for query A does not affect batch for query B."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        async def selective_rerank(query: str, candidates: list[str]) -> list[float]:
            if query == "bad":
                raise ValueError("bad query refused")
            return [1.0] * len(candidates)

        inner = MagicMock()
        inner.rerank = selective_rerank

        client = BatchingRerankerClient(inner, window_seconds=0.01)

        results = await asyncio.gather(
            client.rerank("bad", ["x"]),
            client.rerank("good", ["y"]),
            return_exceptions=True,
        )

        bad_result, good_result = results
        assert isinstance(bad_result, ValueError)
        assert good_result == [1.0]

    @pytest.mark.asyncio
    async def test_inner_type_error_does_not_leave_futures_hanging(self) -> None:
        """TypeError from malformed inner return (e.g. None) resolves all futures.

        Regression guard for Blocker 1: without a finally block, a TypeError raised
        during score slicing (e.g. inner returns None → TypeError on all_scores[0:1])
        propagates up the timer task, leaving participant futures permanently unresolved.
        Callers would hang forever.

        With the finally block: any unresolved future gets set_exception before the
        flush exits, so callers receive an exception instead of hanging.
        """
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        inner = MagicMock()

        async def bad_inner(query: str, candidates: list[str]) -> list[float]:
            # Simulate reranker responding with None (malformed payload):
            # inner.rerank returns None; slicing None raises TypeError.
            return None  # type: ignore[return-value]

        inner.rerank = bad_inner

        client = BatchingRerankerClient(inner, window_seconds=0.01)

        # Directly await without wrapping in wait_for: if the futures hang, the
        # test itself hangs (caught by pytest-asyncio's per-test timeout) and
        # fails with a timeout.  With a proper finally block, the callers receive
        # an exception and this gather resolves quickly.
        results = await asyncio.gather(
            client.rerank("q", ["x"]),
            client.rerank("q", ["y"]),
            return_exceptions=True,
        )

        # Both should see an exception, not a successful result
        for r in results:
            assert isinstance(r, Exception), (
                f"Expected exception (futures resolved via finally), got {type(r)}: {r!r}"
            )
        # Must NOT be TimeoutError — the futures must have been resolved by the
        # finally block, not by a timeout wrapper.
        for r in results:
            assert not isinstance(r, TimeoutError), (
                f"Got TimeoutError — futures are HANGING (finally block missing!): {r}"
            )

    @pytest.mark.asyncio
    async def test_inner_score_length_mismatch_resolved_not_hung(self) -> None:
        """Inner returning wrong-length scores resolves all futures with an exception.

        Without length validation + finally, slicing into a short list silently
        produces empty slices for later participants or raises IndexError mid-loop,
        leaving some futures unresolved.
        """
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        inner = MagicMock()

        async def mismatch_inner(query: str, candidates: list[str]) -> list[float]:
            # Return only 1 score for N candidates (deliberate mismatch)
            return [99.0]

        inner.rerank = mismatch_inner

        client = BatchingRerankerClient(inner, window_seconds=0.01)

        results = await asyncio.gather(
            asyncio.wait_for(client.rerank("q", ["a1", "a2"]), timeout=2.0),
            asyncio.wait_for(client.rerank("q", ["b1"]), timeout=2.0),
            return_exceptions=True,
        )

        # Both participants must be resolved (exception, not hang)
        for r in results:
            assert isinstance(r, Exception), f"Expected exception, got {type(r)}: {r!r}"


class TestBatchingRerankerClientSolo:
    """Solo call (single participant when window fires) → passthrough."""

    @pytest.mark.asyncio
    async def test_solo_call_still_works(self) -> None:
        """A single caller with no concurrent siblings gets results normally."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        inner = AsyncMock()
        inner.rerank = AsyncMock(return_value=[0.9, 0.1])

        client = BatchingRerankerClient(inner, window_seconds=0.01)

        scores = await client.rerank("single query", ["doc1", "doc2"])

        inner.rerank.assert_awaited_once_with("single query", ["doc1", "doc2"])
        assert scores == [0.9, 0.1]

    @pytest.mark.asyncio
    async def test_solo_empty_candidates(self) -> None:
        """Empty candidate list → immediate return, no inner call."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        inner = AsyncMock()
        inner.rerank = AsyncMock(return_value=[])

        client = BatchingRerankerClient(inner, window_seconds=0.01)

        scores = await client.rerank("query", [])

        # Empty: inner may be called with empty list or not at all —
        # either is acceptable; key property is empty list returned.
        assert scores == []


class TestBatchingRerankerClientIsAvailable:
    """is_available() delegates to inner client."""

    @pytest.mark.asyncio
    async def test_is_available_delegates_true(self) -> None:
        """is_available() returns True when inner is available."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        inner = AsyncMock()
        inner.is_available = AsyncMock(return_value=True)

        client = BatchingRerankerClient(inner, window_seconds=0.01)
        assert await client.is_available() is True
        inner.is_available.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_is_available_delegates_false(self) -> None:
        """is_available() returns False when inner is unavailable."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        inner = AsyncMock()
        inner.is_available = AsyncMock(return_value=False)

        client = BatchingRerankerClient(inner, window_seconds=0.01)
        assert await client.is_available() is False


class TestBatchingRerankerClientWindowBounding:
    """Latency is bounded: window fires once, calls arriving after flush create new batch."""

    @pytest.mark.asyncio
    async def test_late_arriving_call_creates_new_batch(self) -> None:
        """A call arriving after the window fires is NOT coalesced into the previous batch."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        call_log: list[tuple[str, list[str]]] = []

        async def recording_rerank(query: str, candidates: list[str]) -> list[float]:
            call_log.append((query, candidates))
            return [0.0] * len(candidates)

        inner = MagicMock()
        inner.rerank = recording_rerank

        client = BatchingRerankerClient(inner, window_seconds=0.02)

        # First call — starts the batch and window
        scores_first = await client.rerank("q", ["early"])

        # Wait long enough for the window to have fired
        await asyncio.sleep(0.05)

        # Second call — must start a fresh batch
        scores_second = await client.rerank("q", ["late"])

        # Two separate inner calls
        assert len(call_log) == 2
        assert call_log[0][1] == ["early"]
        assert call_log[1][1] == ["late"]
        assert scores_first == [0.0]
        assert scores_second == [0.0]

    @pytest.mark.asyncio
    async def test_window_does_not_cascade(self) -> None:
        """Window fires once per batch; no cascading/extended windows.

        Three sequential batches each see their own bounded window, not an ever-growing one.
        """
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        inner_call_count = 0

        async def count_calls(query: str, candidates: list[str]) -> list[float]:
            nonlocal inner_call_count
            inner_call_count += 1
            return [0.0] * len(candidates)

        inner = MagicMock()
        inner.rerank = count_calls

        client = BatchingRerankerClient(inner, window_seconds=0.01)

        for _ in range(3):
            await client.rerank("q", ["x"])
            await asyncio.sleep(0.03)  # ensure each call is a distinct batch

        assert inner_call_count == 3

    @pytest.mark.asyncio
    async def test_window_fires_once_with_rapid_joiners(self) -> None:
        """Window fires exactly once even when new callers arrive before expiry.

        Spec exigence (c): the first caller's window must fire no matter how many
        late joiners arrive during the window period — no cascading reset.

        A cascading implementation would reset the timer on each join, extending
        the window indefinitely.  This test probes that: we send 4 joiners at
        window/4 intervals (0.25×, 0.5×, 0.75×, 1.0×) and assert the first
        caller resolves within ~2× the window (not 4× which cascading would need).
        """
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        WINDOW = 0.04  # 40 ms — big enough to be accurate, small enough to be fast

        inner = MagicMock()

        async def instant_rerank(query: str, candidates: list[str]) -> list[float]:
            return [1.0] * len(candidates)

        inner.rerank = instant_rerank

        client = BatchingRerankerClient(inner, window_seconds=WINDOW)

        async def late_joiner(delay: float) -> list[float]:
            await asyncio.sleep(delay)
            return await client.rerank("q", ["x"])

        # All four tasks run in the same batch (all delays < WINDOW)
        first_task = asyncio.create_task(client.rerank("q", ["first"]))

        joiners = [
            asyncio.create_task(late_joiner(WINDOW * 0.25)),
            asyncio.create_task(late_joiner(WINDOW * 0.50)),
            asyncio.create_task(late_joiner(WINDOW * 0.75)),
        ]

        # First caller must complete within 2× the window, not 4× (cascading would need ~4×)
        result = await asyncio.wait_for(first_task, timeout=WINDOW * 2.5)
        assert result == [1.0]

        # All joiners also resolve
        for j in joiners:
            scores = await asyncio.wait_for(j, timeout=WINDOW * 2.5)
            assert scores == [1.0]


class TestBatchingRerankerClientCancellation:
    """Cancellation of one participant does not kill the batch for others."""

    @pytest.mark.asyncio
    async def test_cancelled_participant_others_still_receive_scores(self) -> None:
        """If one awaiter is cancelled, the remaining participants still get scores."""
        from brain_v42.services.search.batching_reranker import BatchingRerankerClient

        inner = MagicMock()

        async def content_rerank(query: str, candidates: list[str]) -> list[float]:
            return [SCORE[c] for c in candidates]

        inner.rerank = content_rerank

        client = BatchingRerankerClient(inner, window_seconds=0.05)

        # Start one call
        task_a = asyncio.create_task(client.rerank("q", ["A"]))
        # Give it a moment to register in the batch
        await asyncio.sleep(0)

        # Start another
        task_b = asyncio.create_task(client.rerank("q", ["B"]))
        await asyncio.sleep(0)

        # Cancel task_a before the window fires
        task_a.cancel()

        try:
            await task_a
        except asyncio.CancelledError:
            pass

        # task_b should still complete with the correct score
        score_b = await task_b
        assert score_b == [SCORE["B"]], f"Expected [{SCORE['B']}] got {score_b}"
