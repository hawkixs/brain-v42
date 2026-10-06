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
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from brain_v42.services.rerank_wire import CohereRerankWire, ShimRerankWire


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_hosted_retry_delay_honours_uncapped_seconds(status: int) -> None:
    response = httpx.Response(status, headers={"Retry-After": "120"})
    assert CohereRerankWire("m").retry_delay(response, 0) == 120.0


@pytest.mark.parametrize("status", [200, 400, 401, 501])
def test_hosted_other_statuses_are_not_retryable(status: int) -> None:
    assert CohereRerankWire("m").retry_delay(httpx.Response(status), 0) is None


@pytest.mark.parametrize("offset,expected", [(120, 120.0), (-120, 0.0)])
def test_hosted_retry_delay_honours_http_dates(monkeypatch, offset, expected) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr("time.time", lambda: now)
    date = format_datetime(datetime.fromtimestamp(now + offset, UTC), usegmt=True)
    response = httpx.Response(429, headers={"Retry-After": date})
    assert CohereRerankWire("m").retry_delay(response, 0) == expected


@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_hosted_retry_delay_uses_full_exponential_jitter(monkeypatch, attempt) -> None:
    calls = []

    def jitter(low, high):
        calls.append((low, high))
        return high / 2

    monkeypatch.setattr("random.uniform", jitter)
    delay = CohereRerankWire("m").retry_delay(httpx.Response(503), attempt)
    assert calls == [(0, 0.1 * 2**attempt)]
    assert 0 <= delay <= 0.1 * 2**attempt


@pytest.mark.parametrize(
    "status,header,expected",
    [
        (503, None, None),
        (500, "1", None),
        (503, "1", 1.0),
        (503, "120", 2.0),
        (503, "invalid", 2.0),
        (503, "-1", 0.0),
    ],
)
def test_shim_retry_delay_preserves_busy_policy(status, header, expected) -> None:
    headers = {} if header is None else {"Retry-After": header}
    assert ShimRerankWire().retry_delay(httpx.Response(status, headers=headers), 0) == expected


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


class TestCohereRerankWireRouting:
    """Hosted aggregators pick the upstream provider per request."""

    def test_routing_is_sent_as_the_provider_object(self) -> None:
        routing = {"only": ["voyageai"], "allow_fallbacks": False, "data_collection": "deny"}
        _, body = CohereRerankWire(model="voyageai/rerank-3-lite", routing=routing).request(
            "q", ["a"]
        )
        assert body["provider"] == routing

    def test_no_provider_key_without_routing(self) -> None:
        _, body = CohereRerankWire(model="m").request("q", ["a"])
        assert "provider" not in body

    def test_the_body_does_not_alias_the_callers_routing(self) -> None:
        routing = {"only": ["voyageai"]}
        _, body = CohereRerankWire(model="m", routing=routing).request("q", ["a"])
        body["provider"]["only"] = ["other"]
        assert routing == {"only": ["voyageai"]}

    def test_health_path_is_honoured(self) -> None:
        assert CohereRerankWire(model="m", health_path="/v1/key").health_path == "/v1/key"


class TestWireIdentity:
    def test_shim_identity(self) -> None:
        assert ShimRerankWire().identity == "shim"

    def test_cohere_identity_names_the_model(self) -> None:
        assert CohereRerankWire(model="voyageai/rerank-3-lite").identity == (
            "cohere:voyageai/rerank-3-lite"
        )
