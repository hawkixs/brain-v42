"""Cohere-style /v1/rerank wire (TDD Red phase).

Two constraints come from downstream code, not from taste:

1. ORDER. ``BatchingRerankerClient`` coalesces candidates from up to six
   concurrent callers into one list and slices the returned scores back by
   offset. Cohere-style endpoints answer SORTED BY SCORE, and their length
   check would not notice — a correctly-sized but reordered list silently
   hands every participant another participant's scores.

2. SCORE SPACE. ``HybridReranker`` applies ``1/(1+exp(-s))`` to each score
   because the reference cross-encoder returns raw logits. Cohere returns a
   relevance score already in [0, 1]; passing it through unchanged squashes
   the whole corpus into [0.5, 0.73] and quietly breaks min_score. Converting
   back to logit space makes the downstream sigmoid idempotent.
"""

from __future__ import annotations

import math

import pytest

from brain_v42.services.rerank_wire import CohereRerankWire, ShimRerankWire


class TestShimRerankWireIsTodaysContract:
    def test_request_shape_is_unchanged(self) -> None:
        path, body = ShimRerankWire().request("q", ["a", "b"])
        assert path == "/rerank"
        assert body == {"query": "q", "candidates": ["a", "b"]}

    def test_parse_reads_the_scores_key(self) -> None:
        assert ShimRerankWire().parse({"scores": [1.5, -2.0]}, expected=2) == [1.5, -2.0]


class TestCohereRerankWireRequest:
    def test_posts_the_cohere_shape(self) -> None:
        path, body = CohereRerankWire(model="rerank-english-v3.0").request("q", ["a", "b"])
        assert path == "/v1/rerank"
        assert body["model"] == "rerank-english-v3.0"
        assert body["query"] == "q"
        assert body["documents"] == ["a", "b"]

    def test_top_n_covers_every_candidate(self) -> None:
        """A default top_n would truncate and starve the offset slicing."""
        _, body = CohereRerankWire(model="m").request("q", ["a", "b", "c"])
        assert body["top_n"] == 3


class TestCohereRerankWireParsingRestoresInputOrder:
    def test_score_sorted_results_are_remapped_by_index(self) -> None:
        payload = {
            "results": [
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.1},
            ]
        }
        scores = CohereRerankWire(model="m").parse(payload, expected=3)

        # Back in input order, and in logit space so the downstream sigmoid
        # reproduces the provider's own relevance scores.
        assert [round(1 / (1 + math.exp(-s)), 6) for s in scores] == [0.5, 0.1, 0.9]

    def test_a_short_result_set_fails_closed(self) -> None:
        payload = {"results": [{"index": 0, "relevance_score": 0.5}]}
        with pytest.raises(ValueError, match="expected 3"):
            CohereRerankWire(model="m").parse(payload, expected=3)

    def test_a_missing_index_fails_closed_rather_than_padding(self) -> None:
        payload = {
            "results": [
                {"index": 0, "relevance_score": 0.5},
                {"index": 0, "relevance_score": 0.9},
            ]
        }
        with pytest.raises(ValueError, match="indices"):
            CohereRerankWire(model="m").parse(payload, expected=2)

    @pytest.mark.parametrize("score", [0.0, 1.0])
    def test_saturated_scores_stay_finite(self, score: float) -> None:
        """logit(0) and logit(1) are infinite; pgvector-free but they would
        poison sorting and min_score comparisons downstream."""
        payload = {"results": [{"index": 0, "relevance_score": score}]}
        (parsed,) = CohereRerankWire(model="m").parse(payload, expected=1)
        assert math.isfinite(parsed)
