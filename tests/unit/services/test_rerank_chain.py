"""A second calibrated provider preserves reranking under primary saturation."""

from __future__ import annotations

import asyncio
import json
import math
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from brain_v42.services.rerank_wire import CohereRerankWire
from brain_v42.services.reranker_client import RerankBudgetExhausted, RerankerClient
from brain_v42.services.search.batching_reranker import BatchingRerankerClient
from brain_v42.services.search.hybrid import RankedCandidate
from brain_v42.services.search.reranker import HybridReranker

PRIMARY = "voyageai/rerank-3-lite"
FALLBACK = "cohere/rerank-4-fast"


@pytest.mark.parametrize("outcome", ["primary", "fallback", "failed_all"])
async def test_chain_reports_outcome_and_handover(outcome: str) -> None:
    observer = MagicMock()
    chain = chain_over(
        lambda request: success(0.8) if outcome == "primary" else httpx.Response(400),
        lambda request: httpx.Response(400) if outcome == "failed_all" else success(0.3),
        observer=observer,
    )
    try:
        if outcome == "failed_all":
            with pytest.raises(RerankBudgetExhausted):
                await chain.rerank("q", ["a"])
        else:
            await chain.rerank("q", ["a"])
        identity = (
            None
            if outcome == "failed_all"
            else f"cohere:{PRIMARY if outcome == 'primary' else FALLBACK}"
        )
        observer.on_chain.assert_called_once_with(outcome, identity)
        assert observer.on_handover.call_count == (0 if outcome == "primary" else 1)
    finally:
        await chain.close()


@pytest.mark.parametrize("raising", [False, True])
async def test_chain_telemetry_is_optional_and_never_changes_results(raising: bool) -> None:
    observer = MagicMock(spec=["on_chain", "on_handover"] if raising else [])
    if raising:
        observer.on_chain.side_effect = RuntimeError("telemetry failed")
        observer.on_handover.side_effect = RuntimeError("telemetry failed")
    chain = chain_over(
        lambda request: httpx.Response(400), lambda request: success(0.3), observer=observer
    )
    try:
        assert await chain.rerank("q", ["a"]) == pytest.approx([0.0])
    finally:
        await chain.close()


def success(*probabilities: float) -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": [{"index": i, "relevance_score": p} for i, p in enumerate(probabilities)]},
    )


def link(model, handler, observer=None):
    client = RerankerClient(wire=CohereRerankWire(model), observer=observer)
    client._client = httpx.AsyncClient(
        base_url="http://mock", transport=httpx.MockTransport(handler)
    )
    return client


def chain_over(
    primary_handler, fallback_handler, *, observer=None, primary_budget=0.02, overall_budget=0.07
):
    from brain_v42.services.rerank_chain import RerankChain

    return RerankChain(
        link(PRIMARY, primary_handler, observer),
        link(FALLBACK, fallback_handler, observer),
        overall_budget=overall_budget,
        primary_budget=primary_budget,
    )


@pytest.mark.asyncio
async def test_primary_success_returns_unchanged_logits() -> None:
    fallback = MagicMock(return_value=success(0.3))
    chain = chain_over(lambda request: success(0.8), fallback)
    try:
        assert await chain.rerank("q", ["a"]) == pytest.approx([math.log(4)])
        fallback.assert_not_called()
    finally:
        await chain.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [429, 500, 502, 503, 504, 400, "connect", "parse", "key", "type"]
)
async def test_primary_failure_serves_aligned_fallback(failure) -> None:
    calls = []
    observer = MagicMock()
    probabilities = [0.299, 0.30, 0.301]

    def primary(request):
        calls.append(request)
        if failure == "connect":
            raise httpx.ConnectError("unavailable")
        if failure == "parse":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(
            failure if isinstance(failure, int) else 200, headers={"Retry-After": "30"}
        )

    fallback = MagicMock(side_effect=lambda request: success(*probabilities))
    chain = chain_over(primary, fallback, observer=observer)
    if failure in {"key", "type"}:
        chain._primary._wire.parse = MagicMock(
            side_effect=KeyError() if failure == "key" else TypeError()
        )
    try:
        with patch(
            "brain_v42.services.reranker_client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            scores = await chain.rerank("q", ["a", "b", "c"])
            sleep.assert_not_called()
        assert len(calls) == 1
        assert fallback.call_count == 1
        shift = -math.log(0.3 / 0.7)
        assert scores == pytest.approx([math.log(p / (1 - p)) + shift for p in probabilities])
        assert [(1 / (1 + math.exp(-s))) >= 0.5 for s in scores] == [
            p >= 0.3 for p in probabilities
        ]
        observer.on_retry.assert_not_called()
        assert [call.args[0] for call in observer.on_operation.call_args_list] == [
            f"cohere:{PRIMARY}",
            f"cohere:{FALLBACK}",
        ]
    finally:
        await chain.close()


@pytest.mark.asyncio
async def test_stalled_primary_leaves_time_for_fallback() -> None:
    cancelled = asyncio.Event()

    async def stalled(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    chain = chain_over(stalled, lambda request: success(0.3))
    started = asyncio.get_running_loop().time()
    try:
        assert await chain.rerank("q", ["a"]) == pytest.approx([0.0])
        assert asyncio.get_running_loop().time() - started <= 0.09
        assert cancelled.is_set()
    finally:
        await chain.close()


@pytest.mark.asyncio
async def test_fallback_retries_short_cooldown() -> None:
    calls = []
    observer = MagicMock()

    def fallback(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "0.001"})
            if len(calls) == 1
            else success(0.3)
        )

    chain = chain_over(lambda request: httpx.Response(429), fallback, observer=observer)
    try:
        assert await chain.rerank("q", ["a"]) == pytest.approx([0.0])
        assert len(calls) == 2
        observer.on_retry.assert_called_once_with(f"cohere:{FALLBACK}", "http_429")
    finally:
        await chain.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_failure", ["http", "parse", "timeout"])
async def test_both_fail_raises_http_budget_error_and_search_uses_rrf(fallback_failure) -> None:
    async def fallback(request):
        if fallback_failure == "timeout":
            await asyncio.Event().wait()
        return httpx.Response(400) if fallback_failure == "http" else httpx.Response(200, json={})

    chain = chain_over(
        lambda request: httpx.Response(400), fallback, primary_budget=0.005, overall_budget=0.01
    )
    try:
        with pytest.raises(RerankBudgetExhausted) as excinfo:
            await chain.rerank("q", ["a"])
        assert excinfo.value.__cause__ is not None
        candidate = RankedCandidate(
            id=uuid4(), entity=MagicMock(), entity_type="learning", score=0.01, text="a"
        )
        mode, result = await HybridReranker(chain).rerank_with_mode("q", [candidate])
        assert mode == "rrf_fallback"
        assert result[0].score == 1.0
    finally:
        await chain.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_link", ["primary", "fallback"])
async def test_cancellation_propagates(cancel_link) -> None:
    def cancelled(request):
        raise asyncio.CancelledError

    fallback = MagicMock(
        side_effect=cancelled if cancel_link == "fallback" else lambda request: success(0.3)
    )
    chain = chain_over(
        cancelled if cancel_link == "primary" else lambda request: httpx.Response(400), fallback
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await chain.rerank("q", ["a"])
        if cancel_link == "primary":
            fallback.assert_not_called()
    finally:
        await chain.close()


@pytest.mark.asyncio
async def test_empty_candidates_make_no_call() -> None:
    primary, fallback = MagicMock(), MagicMock()
    chain = chain_over(primary, fallback)
    try:
        assert await chain.rerank("q", []) == []
        primary.assert_not_called()
        fallback.assert_not_called()
    finally:
        await chain.close()


@pytest.mark.asyncio
async def test_observer_exception_preserves_fallback_result() -> None:
    observer = MagicMock()
    for method in (observer.on_attempt, observer.on_retry, observer.on_operation):
        method.side_effect = RuntimeError("observer unavailable")
    chain = chain_over(
        lambda request: httpx.Response(429), lambda request: success(0.3), observer=observer
    )
    try:
        assert await chain.rerank("q", ["a"]) == pytest.approx([0.0])
    finally:
        await chain.close()


@pytest.mark.asyncio
async def test_batching_slices_aligned_fallback_scores() -> None:
    calls = []

    def fallback(request):
        calls.append(json.loads(request.content))
        return success(0.2, 0.3, 0.4)

    primary = MagicMock(return_value=httpx.Response(429))
    chain = chain_over(primary, fallback)
    batched = BatchingRerankerClient(chain, window_seconds=0.001)
    try:
        first, second = await asyncio.gather(
            batched.rerank("q", ["a"]), batched.rerank("q", ["b", "c"])
        )
        shift = -math.log(0.3 / 0.7)
        assert first == pytest.approx([math.log(0.2 / 0.8) + shift])
        assert second == pytest.approx([0.0, math.log(0.4 / 0.6) + shift])
        assert calls[0]["documents"] == ["a", "b", "c"]
        assert primary.call_count == len(calls) == 1
    finally:
        await chain.close()


def test_uncalibrated_fallback_is_refused_at_construction() -> None:
    from brain_v42.services.rerank_chain import RerankChain

    with pytest.raises(ValueError, match="rerank_fallback_model.*calibrat"):
        RerankChain(
            RerankerClient(wire=CohereRerankWire(PRIMARY)),
            RerankerClient(wire=CohereRerankWire("unknown/model")),
            overall_budget=1.5,
            primary_budget=1.0,
        )


@pytest.mark.asyncio
async def test_probe_calibration_and_close_delegate_to_links() -> None:
    chain = chain_over(lambda request: httpx.Response(200), lambda request: httpx.Response(401))
    primary_http, fallback_http = chain._primary._client, chain._fallback._client
    assert chain.calibration is chain._primary.calibration
    assert chain.calibration.identity == f"cohere:{PRIMARY}"
    assert chain.last_probe_ok is None
    assert await chain.is_available()
    assert chain.last_probe_ok is True
    assert chain.last_probe_reason == "ok"
    assert chain.last_probe_monotonic == chain._primary.last_probe_monotonic
    await chain.close()
    assert primary_http.is_closed and fallback_http.is_closed


@pytest.mark.asyncio
async def test_voyage_429_serves_cohere_on_the_voyage_scale() -> None:
    from brain_v42.services.rerank_chain import RerankChain
    from brain_v42.services.rerank_wire import VoyageRerankWire

    primary = RerankerClient(wire=VoyageRerankWire("rerank-3-lite"))
    requests = []

    def limited(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    primary._client = httpx.AsyncClient(
        base_url="https://api.voyageai.com", transport=httpx.MockTransport(limited)
    )
    fallback = MagicMock(side_effect=lambda request: success(0.2, 0.3, 0.4))
    chain = RerankChain(primary, link(FALLBACK, fallback), overall_budget=0.1, primary_budget=0.05)
    try:
        scores = await chain.rerank("q", ["a", "b", "c"])
        shift = math.log(0.5 / 0.5) - math.log(0.3 / 0.7)
        assert scores == pytest.approx([math.log(p / (1 - p)) + shift for p in [0.2, 0.3, 0.4]])
        assert chain.calibration.identity == "voyage:rerank-3-lite"
        assert chain.calibration.search_min_score == 0.50
        assert chain.calibration.dedup_signal is None
        assert len(requests) == fallback.call_count == 1
    finally:
        await chain.close()
