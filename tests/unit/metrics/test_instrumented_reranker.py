"""Unit tests for InstrumentedReranker — new rerank_with_mode API.

TDD Red phase: these tests are written against the NEW interface
(rerank_with_mode, not rerank) and will fail until instrument.py is updated.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.instrument import InstrumentedReranker
from brain_v42.services.search.hybrid import RankedCandidate


def _make_candidates(n: int) -> list[RankedCandidate]:
    return [
        RankedCandidate(
            id=uuid.uuid4(),
            entity=None,
            entity_type="learning",
            score=0.0,
            text=f"doc{i}",
        )
        for i in range(n)
    ]


@pytest.fixture()
def collector() -> MetricsCollector:
    return MetricsCollector(engine=MagicMock(), session_factory=MagicMock())


class TestInstrumentedRerankerRerankedMode:
    """rerank_with_mode delegates and records metrics correctly in 'reranked' mode."""

    async def test_delegates_to_inner_rerank_with_mode(self, collector: MetricsCollector) -> None:
        """InstrumentedReranker.rerank_with_mode delegates to inner.rerank_with_mode."""
        inner = MagicMock()
        candidates = _make_candidates(3)
        expected_result = list(reversed(candidates))

        async def mock_rerank_with_mode(query: str, cands: list) -> tuple:
            return ("reranked", list(reversed(cands)))

        inner.rerank_with_mode = mock_rerank_with_mode

        wrapped = InstrumentedReranker(inner, collector)
        mode, result = await wrapped.rerank_with_mode("test query", candidates)

        assert mode == "reranked"
        assert result == expected_result

    async def test_records_total_call(self, collector: MetricsCollector) -> None:
        """A successful call increments total_calls."""
        inner = MagicMock()
        candidates = _make_candidates(4)

        async def mock_rerank_with_mode(query: str, cands: list) -> tuple:
            return ("reranked", cands)

        inner.rerank_with_mode = mock_rerank_with_mode

        wrapped = InstrumentedReranker(inner, collector)
        await wrapped.rerank_with_mode("q", candidates)

        assert collector._reranker_stats["total_calls"] == 1

    async def test_records_candidate_count(self, collector: MetricsCollector) -> None:
        """Candidate count is recorded for the instrumented call."""
        inner = MagicMock()
        candidates = _make_candidates(5)

        async def mock_rerank_with_mode(query: str, cands: list) -> tuple:
            return ("reranked", cands)

        inner.rerank_with_mode = mock_rerank_with_mode

        wrapped = InstrumentedReranker(inner, collector)
        await wrapped.rerank_with_mode("q", candidates)

        assert collector._reranker_stats["total_candidates"] == 5

    async def test_records_latency(self, collector: MetricsCollector) -> None:
        """Latency is recorded (positive) even for a near-instant call."""
        inner = MagicMock()
        candidates = _make_candidates(2)

        async def mock_rerank_with_mode(query: str, cands: list) -> tuple:
            return ("reranked", cands)

        inner.rerank_with_mode = mock_rerank_with_mode

        wrapped = InstrumentedReranker(inner, collector)
        await wrapped.rerank_with_mode("q", candidates)

        assert collector._reranker_stats["total_latency"] >= 0
        assert collector._reranker_stats["total_calls"] == 1

    async def test_no_error_on_reranked_mode(self, collector: MetricsCollector) -> None:
        """'reranked' mode does NOT count as an error."""
        inner = MagicMock()
        candidates = _make_candidates(2)

        async def mock_rerank_with_mode(query: str, cands: list) -> tuple:
            return ("reranked", cands)

        inner.rerank_with_mode = mock_rerank_with_mode

        wrapped = InstrumentedReranker(inner, collector)
        await wrapped.rerank_with_mode("q", candidates)

        assert collector._reranker_stats["total_errors"] == 0
        assert len(collector._reranker_error_times) == 0


class TestInstrumentedRerankerRrfFallbackMode:
    """rrf_fallback mode is counted as an error (service degradation)."""

    async def test_rrf_fallback_counts_as_error(self, collector: MetricsCollector) -> None:
        """rrf_fallback mode increments total_errors — it signals reranker service failure."""
        inner = MagicMock()
        candidates = _make_candidates(3)

        async def mock_rerank_with_mode(query: str, cands: list) -> tuple:
            return ("rrf_fallback", cands)

        inner.rerank_with_mode = mock_rerank_with_mode

        wrapped = InstrumentedReranker(inner, collector)
        mode, result = await wrapped.rerank_with_mode("q", candidates)

        assert mode == "rrf_fallback"
        assert result == candidates
        # rrf_fallback == reranker service was down → error counter
        assert collector._reranker_stats["total_calls"] == 1
        assert collector._reranker_stats["total_errors"] == 1
        assert len(collector._reranker_error_times) == 1

    async def test_rrf_fallback_still_records_candidates_and_latency(
        self, collector: MetricsCollector
    ) -> None:
        """Even in fallback mode, candidate count and latency are still recorded."""
        inner = MagicMock()
        candidates = _make_candidates(6)

        async def mock_rerank_with_mode(query: str, cands: list) -> tuple:
            return ("rrf_fallback", cands)

        inner.rerank_with_mode = mock_rerank_with_mode

        wrapped = InstrumentedReranker(inner, collector)
        await wrapped.rerank_with_mode("q", candidates)

        assert collector._reranker_stats["total_candidates"] == 6
        assert collector._reranker_stats["total_latency"] >= 0


class TestInstrumentedRerankerException:
    """Exceptions from inner.rerank_with_mode are propagated and counted as errors."""

    async def test_exception_propagates(self, collector: MetricsCollector) -> None:
        """Exceptions bubble up to the caller."""
        inner = MagicMock()

        async def failing_rerank(query: str, cands: list) -> tuple:
            raise RuntimeError("service crash")

        inner.rerank_with_mode = failing_rerank

        wrapped = InstrumentedReranker(inner, collector)
        with pytest.raises(RuntimeError, match="service crash"):
            await wrapped.rerank_with_mode("q", _make_candidates(1))

    async def test_exception_counts_as_error(self, collector: MetricsCollector) -> None:
        """Exceptions from inner are counted as errors in the collector."""
        inner = MagicMock()

        async def failing_rerank(query: str, cands: list) -> tuple:
            raise RuntimeError("service crash")

        inner.rerank_with_mode = failing_rerank

        wrapped = InstrumentedReranker(inner, collector)
        with pytest.raises(RuntimeError):
            await wrapped.rerank_with_mode("q", _make_candidates(2))

        assert collector._reranker_stats["total_calls"] == 1
        assert collector._reranker_stats["total_errors"] == 1
        assert collector._reranker_stats["total_candidates"] == 2

    async def test_exception_records_latency_in_finally(self, collector: MetricsCollector) -> None:
        """Latency is still recorded even when an exception is raised (finally block)."""
        inner = MagicMock()

        async def failing_rerank(query: str, cands: list) -> tuple:
            raise RuntimeError("crash")

        inner.rerank_with_mode = failing_rerank

        wrapped = InstrumentedReranker(inner, collector)
        with pytest.raises(RuntimeError):
            await wrapped.rerank_with_mode("q", _make_candidates(1))

        assert collector._reranker_stats["total_latency"] >= 0


class TestInstrumentedRerankerIsAvailable:
    """is_available() passthrough is unchanged."""

    async def test_is_available_passthrough(self, collector: MetricsCollector) -> None:
        """is_available delegates to inner without side effects on metrics."""
        inner = MagicMock()

        async def mock_available() -> bool:
            return True

        inner.is_available = mock_available

        wrapped = InstrumentedReranker(inner, collector)
        assert await wrapped.is_available() is True
        # No metrics recorded for healthcheck
        assert collector._reranker_stats["total_calls"] == 0

    async def test_is_available_returns_false(self, collector: MetricsCollector) -> None:
        """is_available propagates False from inner."""
        inner = MagicMock()

        async def mock_not_available() -> bool:
            return False

        inner.is_available = mock_not_available

        wrapped = InstrumentedReranker(inner, collector)
        assert await wrapped.is_available() is False
