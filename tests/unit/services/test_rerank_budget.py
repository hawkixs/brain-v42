"""Bounded hosted retries must preserve search latency and shim behaviour."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import httpx
import pytest
from structlog.testing import capture_logs

from brain_v42.services import reranker_client as module
from brain_v42.services.rerank_wire import CohereRerankWire, ShimRerankWire


def client_over(handler, **kwargs):
    client = module.RerankerClient(wire=CohereRerankWire("m"), **kwargs)
    client._client = httpx.AsyncClient(
        base_url="https://rerank.test", transport=httpx.MockTransport(handler)
    )
    return client


def success() -> httpx.Response:
    return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]})


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [429, 500, 502, 503, 504, "connect"])
async def test_hosted_retry_then_success_is_observed(monkeypatch, first) -> None:
    observer = MagicMock()
    calls = 0
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    monkeypatch.setattr("random.uniform", lambda a, b: 0.01)

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            if first == "connect":
                raise httpx.ConnectError("refused")
            return httpx.Response(first, headers={"Retry-After": "0.01"})
        return success()

    client = client_over(handler, observer=observer)
    try:
        assert await client.rerank("q", ["a"]) == [0.0]
    finally:
        await client.close()
    assert calls == 2
    assert sleeps == [0.01]
    observer.on_retry.assert_called_once_with(
        "cohere:m", "transport_ConnectError" if first == "connect" else f"http_{first}"
    )
    assert observer.on_attempt.call_count == 2
    assert observer.on_operation.call_args.args[:2] == ("cohere:m", "ok")


@pytest.mark.asyncio
async def test_forever_busy_exhausts_bounded_retries(monkeypatch) -> None:
    calls = 0
    observer = MagicMock()

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "0"})

    client = client_over(handler, budget_seconds=0.02, max_retries=2, observer=observer)
    started = time.monotonic()
    try:
        with pytest.raises(module.RerankBudgetExhausted):
            await client.rerank("q", ["a"])
    finally:
        await client.close()
    assert time.monotonic() - started < 0.07
    assert calls == 3
    assert observer.on_retry.call_count == 2
    assert observer.on_operation.call_args.args[:2] == ("cohere:m", "budget_exhausted")


@pytest.mark.asyncio
async def test_retry_after_outside_budget_sends_no_more_requests(monkeypatch) -> None:
    calls = 0
    sleep = MagicMock(side_effect=AssertionError("must not sleep"))
    monkeypatch.setattr(module.asyncio, "sleep", sleep)

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    client = client_over(handler, budget_seconds=0.02)
    try:
        with pytest.raises(module.RerankBudgetExhausted):
            await client.rerank("q", ["a"])
    finally:
        await client.close()
    assert calls == 1
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_stalled_transport_is_cancelled_at_deadline() -> None:
    cancelled = False
    observer = MagicMock()

    async def handler(request):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return success()

    client = client_over(handler, budget_seconds=0.02, observer=observer)
    started = time.monotonic()
    try:
        with pytest.raises(module.RerankBudgetExhausted) as caught:
            await client.rerank("q", ["a"])
    finally:
        await client.close()
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert cancelled
    assert time.monotonic() - started < 0.07
    assert observer.on_operation.call_args.args[:2] == ("cohere:m", "budget_exhausted")
    assert observer.on_attempt.call_count == 1


def test_budget_exhausted_preserves_http_error_fallback() -> None:
    assert issubclass(module.RerankBudgetExhausted, httpx.HTTPError)


@pytest.mark.asyncio
async def test_caller_cancellation_is_never_counted_as_success() -> None:
    entered = asyncio.Event()
    observer = MagicMock()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()
        return success()

    client = client_over(handler, observer=observer)
    task = asyncio.create_task(client.rerank("q", ["a"]))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client.close()
    assert observer.on_operation.call_args.args[:2] == ("cohere:m", "transport_error")
    assert observer.on_attempt.call_args.args[:2] == ("cohere:m", "transport_error")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401])
async def test_non_retryable_status_fails_at_once(status) -> None:
    observer = MagicMock()
    client = client_over(lambda request: httpx.Response(status), observer=observer)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.rerank("q", ["a"])
    finally:
        await client.close()
    observer.on_retry.assert_not_called()
    assert observer.on_attempt.call_count == 1
    assert observer.on_operation.call_args.args[:2] == ("cohere:m", "http_error")


@pytest.mark.asyncio
async def test_total_latency_includes_backoff(monkeypatch) -> None:
    observer = MagicMock()
    clock = [0.0]
    calls = 0
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    async def sleep(delay):
        clock[0] += delay

    monkeypatch.setattr(module.asyncio, "sleep", sleep)

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "0.1"}) if calls == 1 else success()

    client = client_over(handler, observer=observer)
    try:
        await client.rerank("q", ["a"])
    finally:
        await client.close()
    assert observer.on_operation.call_args.args[2] == pytest.approx(100)
    assert all(call.args[2] == 0 for call in observer.on_attempt.call_args_list)


@pytest.mark.asyncio
async def test_observer_exceptions_are_logged_once_and_swallowed() -> None:
    observer = MagicMock()
    for method in (
        observer.on_attempt,
        observer.on_retry,
        observer.on_operation,
        observer.on_probe,
    ):
        method.side_effect = RuntimeError("metrics unavailable")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"Retry-After": "0"}) if calls == 1 else success()

    client = client_over(handler, observer=observer)
    try:
        with capture_logs() as records:
            assert await client.rerank("q", ["a"]) == [0.0]
            assert await client.is_available()
            assert await client.is_available()
    finally:
        await client.close()
    assert len([r for r in records if r["event"] == "reranker_client.observer_failed"]) == 1
    assert observer.on_probe.call_count == 2


@pytest.mark.asyncio
async def test_empty_candidates_do_not_count_as_operation() -> None:
    observer = MagicMock()
    client = module.RerankerClient(observer=observer)
    assert await client.rerank("q", []) == []
    observer.on_operation.assert_not_called()
    observer.on_attempt.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,outcome",
    [(httpx.ReadError("broken"), "transport_error"), (ValueError("bad payload"), "parse_error")],
)
async def test_other_failures_are_counted_without_retry(failure, outcome) -> None:
    observer = MagicMock()

    def handler(request):
        if isinstance(failure, httpx.HTTPError):
            raise failure
        return httpx.Response(200, json={"results": []})

    client = client_over(handler, observer=observer)
    try:
        with pytest.raises(type(failure)):
            await client.rerank("q", ["a"])
    finally:
        await client.close()
    observer.on_retry.assert_not_called()
    assert observer.on_operation.call_args.args[:2] == ("cohere:m", outcome)


@pytest.mark.asyncio
async def test_shim_ignores_elapsed_hosted_budget_and_retries_three_times(monkeypatch) -> None:
    clock = [0.0]
    sleeps = []
    calls = 0
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(module.asyncio, "sleep", sleep)

    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 4:
            return httpx.Response(503, headers={"Retry-After": "2"})
        clock[0] += 1.6
        return httpx.Response(200, json={"scores": [0.7]})

    client = module.RerankerClient(wire=ShimRerankWire(), budget_seconds=1.5)
    client._client = httpx.AsyncClient(
        base_url="http://shim", transport=httpx.MockTransport(handler), timeout=10
    )
    try:
        assert await client.rerank("q", ["a"]) == [0.7]
    finally:
        await client.close()
    assert calls == 4
    assert sleeps == [2.0, 2.0, 2.0]
    assert clock[0] > 1.5
