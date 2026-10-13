"""Direct Voyage preserves candidate order and the calibrated probability scale."""

import math
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from brain_v42.services.rerank_wire import VoyageRerankWire


def voyage_wire() -> VoyageRerankWire:
    return VoyageRerankWire("rerank-3-lite")


def test_request_and_identity() -> None:
    wire = voyage_wire()
    assert wire.identity == "voyage:rerank-3-lite"
    assert wire.request("q", ["a", "b"]) == (
        "/v1/rerank",
        {"query": "q", "documents": ["a", "b"], "model": "rerank-3-lite", "truncation": True},
    )


def test_probe_uses_one_document_without_a_get_health_path() -> None:
    wire = voyage_wire()
    assert wire.health_path is None
    assert wire.probe_request() == (
        "POST",
        "/v1/rerank",
        {"query": "ping", "documents": ["ping"], "model": "rerank-3-lite", "truncation": True},
    )


def test_parse_remaps_scores_to_input_order() -> None:
    scores = voyage_wire().parse(
        {
            "data": [
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.1},
            ]
        },
        expected=3,
    )
    assert [1 / (1 + math.exp(-s)) for s in scores] == pytest.approx([0.5, 0.1, 0.9])


@pytest.mark.parametrize("indexes", [[0, 0], [-1, 1], [0, 2]])
def test_parse_refuses_duplicate_or_out_of_range_indexes(indexes: list[int]) -> None:
    with pytest.raises(ValueError, match="indices"):
        voyage_wire().parse(
            {"data": [{"index": i, "relevance_score": 0.5} for i in indexes]}, expected=2
        )


def test_parse_refuses_wrong_count() -> None:
    with pytest.raises(ValueError, match="expected 2"):
        voyage_wire().parse({"data": [{"index": 0, "relevance_score": 0.5}]}, expected=2)


@pytest.mark.parametrize("score", [math.nan, math.inf, -math.inf, -0.1, 1.1])
def test_parse_refuses_invalid_scores(score: float) -> None:
    with pytest.raises(ValueError):
        voyage_wire().parse({"data": [{"index": 0, "relevance_score": score}]}, expected=1)


@pytest.mark.parametrize("score", [0.0, 1.0])
def test_saturated_scores_stay_finite(score: float) -> None:
    assert math.isfinite(
        voyage_wire().parse({"data": [{"index": 0, "relevance_score": score}]}, expected=1)[0]
    )


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 200, 400, 401, 501])
def test_retry_policy_matches_cohere(status: int) -> None:
    from brain_v42.services.rerank_wire import CohereRerankWire

    response = httpx.Response(status, headers={"Retry-After": "120"})
    assert voyage_wire().retry_delay(response, 0) == CohereRerankWire("m").retry_delay(response, 0)


def test_retry_honours_http_date_and_uses_same_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.time", lambda: 1_800_000_000.0)
    date = format_datetime(datetime.fromtimestamp(1_800_000_120, UTC), usegmt=True)
    assert voyage_wire().retry_delay(httpx.Response(429, headers={"Retry-After": date}), 0) == 120
    monkeypatch.setattr("random.uniform", lambda low, high: high / 2)
    assert voyage_wire().retry_delay(httpx.Response(503), 2) == 0.2
