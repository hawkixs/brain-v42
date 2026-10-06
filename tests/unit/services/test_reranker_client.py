"""Tests for RerankerClient."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.services.reranker_client import RerankerClient


@pytest.fixture
def client():
    return RerankerClient(base_url="http://localhost:8004")


@pytest.mark.asyncio
async def test_rerank_returns_scores(client):
    mock_response = MagicMock()  # httpx.Response.json() is sync
    mock_response.json.return_value = {"scores": [0.92, 0.15, 0.08]}
    mock_response.raise_for_status = MagicMock()

    with patch.object(client, "_get_client") as mock_get:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_get.return_value = mock_http

        scores = await client.rerank(
            query="Memory decay system",
            candidates=["Memory Decay", "Hybrid Search", "Knowledge Graph"],
        )
        assert scores == [0.92, 0.15, 0.08]


@pytest.mark.asyncio
async def test_rerank_returns_empty_on_no_candidates(client):
    scores = await client.rerank(query="test", candidates=[])
    assert scores == []


@pytest.mark.asyncio
async def test_is_available_returns_false_on_connection_error(client):
    with patch.object(client, "_get_client") as mock_get:
        mock_http = AsyncMock()
        import httpx

        mock_http.get.side_effect = httpx.ConnectError("refused")
        mock_get.return_value = mock_http

        result = await client.is_available()
        assert result is False


@pytest.mark.asyncio
async def test_is_available_returns_true_on_200(client):
    with patch.object(client, "_get_client") as mock_get:
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_http.get.return_value = mock_response
        mock_get.return_value = mock_http

        result = await client.is_available()
        assert result is True


@pytest.mark.asyncio
async def test_is_available_returns_false_on_timeout(client):
    with patch.object(client, "_get_client") as mock_get:
        mock_http = AsyncMock()
        import httpx

        mock_http.get.side_effect = httpx.TimeoutException("timeout")
        mock_get.return_value = mock_http

        result = await client.is_available()
        assert result is False


@pytest.mark.asyncio
async def test_rerank_calls_post_with_correct_payload(client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"scores": [0.5]}
    mock_response.raise_for_status = MagicMock()

    with patch.object(client, "_get_client") as mock_get:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_get.return_value = mock_http

        await client.rerank(query="test query", candidates=["candidate 1"])
        mock_http.post.assert_called_once_with(
            "/rerank",
            json={"query": "test query", "candidates": ["candidate 1"]},
        )


@pytest.mark.asyncio
async def test_close_cleans_up_client(client):
    mock_http = AsyncMock()
    client._client = mock_http

    await client.close()

    mock_http.aclose.assert_called_once()
    assert client._client is None


@pytest.mark.asyncio
async def test_close_is_noop_when_no_client(client):
    assert client._client is None
    await client.close()  # Should not raise
    assert client._client is None


@pytest.mark.asyncio
async def test_get_client_creates_lazy_client(client):
    assert client._client is None
    http_client = client._get_client()
    assert http_client is not None
    assert client._client is http_client
    # Calling again returns the same instance
    assert client._get_client() is http_client
    await client.close()


@pytest.mark.asyncio
async def test_client_has_bounded_connection_limits(client):
    """_get_client() creates an AsyncClient with explicit connection limits."""
    http_client = client._get_client()
    pool = http_client._transport._pool
    assert pool._max_connections == 20
    assert pool._max_keepalive_connections == 10
    await client.close()


# ── Busy shim: bounded retry honouring Retry-After ─────────────────────────
# The shim holds ONE rerank computation at a time and answers any concurrent
# request with 503 + Retry-After instead of queueing it. Measured 2026-09-23:
# 70 of 623 brain_search reranks (11 %) hit that 503 and fell back to RRF,
# although the slot frees within seconds.


def _busy_client() -> RerankerClient:
    return RerankerClient(base_url="http://shim", busy_retries=2, busy_retry_cap_seconds=1.0)


def _response(status: int, *, scores=None, retry_after: str | None = None):
    import httpx

    request = httpx.Request("POST", "http://shim/rerank")
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(status, json={"scores": scores or []}, headers=headers, request=request)


@pytest.fixture
def recorded_sleeps(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("brain_v42.services.reranker_client.asyncio.sleep", fake_sleep)
    return sleeps


def _client_answering(client: RerankerClient, *responses):
    mock_http = AsyncMock()
    mock_http.post.side_effect = list(responses)
    return patch.object(client, "_get_client", return_value=mock_http), mock_http


@pytest.mark.asyncio
async def test_rerank_retries_a_busy_503_after_retry_after(recorded_sleeps):
    client = _busy_client()
    patcher, mock_http = _client_answering(
        client, _response(503, retry_after="1"), _response(200, scores=[0.5, 0.1])
    )
    with patcher:
        scores = await client.rerank("q", ["a", "b"])

    assert scores == [0.5, 0.1]
    assert mock_http.post.await_count == 2
    assert recorded_sleeps == [1.0]


@pytest.mark.asyncio
async def test_rerank_busy_retries_are_bounded(recorded_sleeps):
    import httpx

    client = _busy_client()
    patcher, mock_http = _client_answering(
        client, *[_response(503, retry_after="1") for _ in range(5)]
    )
    with patcher, pytest.raises(httpx.HTTPStatusError):
        await client.rerank("q", ["a"])

    # one attempt + busy_retries=2, never more
    assert mock_http.post.await_count == 3


@pytest.mark.asyncio
async def test_rerank_retry_after_is_capped(recorded_sleeps):
    client = _busy_client()
    patcher, _ = _client_answering(
        client, _response(503, retry_after="120"), _response(200, scores=[0.3])
    )
    with patcher:
        await client.rerank("q", ["a"])

    assert recorded_sleeps == [1.0]


@pytest.mark.asyncio
async def test_rerank_does_not_retry_other_errors(recorded_sleeps):
    import httpx

    client = _busy_client()
    patcher, mock_http = _client_answering(client, _response(500), _response(200, scores=[0.3]))
    with patcher, pytest.raises(httpx.HTTPStatusError):
        await client.rerank("q", ["a"])

    assert mock_http.post.await_count == 1


@pytest.mark.asyncio
async def test_rerank_does_not_retry_a_503_without_retry_after(recorded_sleeps):
    """A 503 without Retry-After is an outage, not a busy slot: fail fast."""
    import httpx

    client = _busy_client()
    patcher, mock_http = _client_answering(client, _response(503), _response(200, scores=[0.3]))
    with patcher, pytest.raises(httpx.HTTPStatusError):
        await client.rerank("q", ["a"])

    assert mock_http.post.await_count == 1


# ---------------------------------------------------------------------------
# Hosted reranker: URL composition and probe state
# ---------------------------------------------------------------------------


def _client_over(handler, *, base_url: str, wire):
    """A real RerankerClient whose httpx.AsyncClient talks to ``handler``.

    Patching the constructor rather than assigning ``_client`` keeps the
    client's own base_url handling under test.
    """
    import functools

    import httpx

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    client = RerankerClient(base_url=base_url, wire=wire)
    patcher = patch(
        "brain_v42.services.reranker_client.httpx.AsyncClient",
        functools.partial(real_async_client, transport=transport),
    )
    patcher.start()
    return client, patcher


@pytest.mark.asyncio
async def test_hosted_urls_compose_under_the_base_path() -> None:
    import httpx

    from brain_v42.services.rerank_wire import CohereRerankWire

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.method == "POST":
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]})
        return httpx.Response(200)

    client, patcher = _client_over(
        handler,
        base_url="https://openrouter.ai/api",
        wire=CohereRerankWire(model="voyageai/rerank-3-lite", health_path="/v1/key"),
    )
    try:
        await client.rerank("q", ["a"])
        assert await client.is_available() is True
    finally:
        await client.close()
        patcher.stop()

    assert seen == [
        ("POST", "https://openrouter.ai/api/v1/rerank"),
        ("GET", "https://openrouter.ai/api/v1/key"),
    ]


class TestProbeState:
    @staticmethod
    def _probe_client(handler):
        from brain_v42.services.rerank_wire import CohereRerankWire

        return _client_over(
            handler,
            base_url="https://openrouter.ai/api",
            wire=CohereRerankWire(model="m", health_path="/v1/key"),
        )

    @pytest.mark.asyncio
    async def test_200_is_available_and_recorded(self) -> None:
        import httpx

        client, patcher = self._probe_client(lambda request: httpx.Response(200))
        try:
            assert client.last_probe_ok is None
            assert await client.is_available() is True
        finally:
            await client.close()
            patcher.stop()
        assert client.last_probe_ok is True
        assert client.last_probe_reason == "ok"
        assert client.last_probe_monotonic is not None

    @pytest.mark.asyncio
    async def test_401_is_unavailable_with_the_status_as_reason(self) -> None:
        import httpx

        client, patcher = self._probe_client(lambda request: httpx.Response(401))
        try:
            assert await client.is_available() is False
        finally:
            await client.close()
            patcher.stop()
        assert client.last_probe_ok is False
        assert client.last_probe_reason == "http_401"

    @pytest.mark.asyncio
    async def test_any_transport_error_is_unavailable_not_raised(self) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client, patcher = self._probe_client(handler)
        try:
            assert await client.is_available() is False
        finally:
            await client.close()
            patcher.stop()
        assert client.last_probe_reason == "transport_ConnectError"

    @pytest.mark.asyncio
    async def test_an_http_error_outside_connect_and_timeout_is_also_caught(self) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.RemoteProtocolError("peer closed")

        client, patcher = self._probe_client(handler)
        try:
            assert await client.is_available() is False
        finally:
            await client.close()
            patcher.stop()
        assert client.last_probe_reason == "transport_RemoteProtocolError"

    @pytest.mark.asyncio
    async def test_logs_only_on_state_change(self) -> None:
        import httpx
        from structlog.testing import capture_logs

        status = {"code": 401}
        client, patcher = self._probe_client(lambda request: httpx.Response(status["code"]))
        try:
            with capture_logs() as records:
                await client.is_available()  # first probe, fails: counts
                await client.is_available()  # still failing: silent
                status["code"] = 200
                await client.is_available()  # recovery
                await client.is_available()  # still fine: silent
        finally:
            await client.close()
            patcher.stop()

        events = [
            (r["event"], r["log_level"])
            for r in records
            if r["event"] in ("reranker_client.unavailable", "reranker_client.available_again")
        ]
        assert events == [
            ("reranker_client.unavailable", "warning"),
            ("reranker_client.available_again", "info"),
        ]
        unavailable = next(r for r in records if r["event"] == "reranker_client.unavailable")
        assert unavailable["reason"] == "http_401"
        assert unavailable["health_path"] == "/v1/key"

    @pytest.mark.asyncio
    async def test_a_healthy_first_probe_is_silent(self) -> None:
        import httpx
        from structlog.testing import capture_logs

        client, patcher = self._probe_client(lambda request: httpx.Response(200))
        try:
            with capture_logs() as records:
                await client.is_available()
        finally:
            await client.close()
            patcher.stop()
        assert [r["event"] for r in records if r["event"].startswith("reranker_client.")] == [
            "reranker_client.client_created"
        ]


class TestProbeLoop:
    @pytest.mark.asyncio
    async def test_probes_at_start_then_every_interval_and_survives_a_raise(self) -> None:
        import asyncio

        from brain_v42.services.reranker_client import run_rerank_probe_loop

        calls = 0
        second_call = asyncio.Event()

        class FakeClient:
            async def is_available(self) -> bool:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("probe blew up")
                second_call.set()
                return True

        task = asyncio.create_task(run_rerank_probe_loop(FakeClient(), interval_seconds=0.01))  # type: ignore[arg-type]
        await asyncio.wait_for(second_call.wait(), timeout=2)
        assert calls >= 2
        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_the_first_probe_does_not_wait_for_the_interval(self) -> None:
        import asyncio

        from brain_v42.services.reranker_client import run_rerank_probe_loop

        first = asyncio.Event()

        class FakeClient:
            async def is_available(self) -> bool:
                first.set()
                return True

        task = asyncio.create_task(run_rerank_probe_loop(FakeClient(), interval_seconds=3600))  # type: ignore[arg-type]
        try:
            await asyncio.wait_for(first.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestAHealthPathNeverLeavesTheBaseUrl:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["https://evil.example/x", "//evil.example/x", "v1/key"])
    async def test_a_non_relative_health_path_sends_nothing(self, path: str) -> None:
        import httpx

        from brain_v42.services.rerank_wire import CohereRerankWire

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        client, patcher = _client_over(
            handler,
            base_url="https://openrouter.ai/api",
            wire=CohereRerankWire(model="m", health_path=path),
        )
        try:
            assert await client.is_available() is False
        finally:
            await client.close()
            patcher.stop()
        assert seen == []
        assert client.last_probe_reason == "invalid_health_path"


class TestAnUnexpectedProbeErrorIsStateNotNoise:
    @pytest.mark.asyncio
    async def test_it_is_recorded_and_logged_once_per_entry(self) -> None:
        from structlog.testing import capture_logs

        from brain_v42.services.rerank_wire import CohereRerankWire

        client = RerankerClient(
            base_url="https://openrouter.ai/api",
            wire=CohereRerankWire(model="m", health_path="/v1/key"),
        )
        with (
            patch.object(client, "_get_client", side_effect=RuntimeError("boom")),
            capture_logs() as records,
        ):
            assert await client.is_available() is False
            assert await client.is_available() is False

        assert client.last_probe_ok is False
        assert client.last_probe_reason == "error_RuntimeError"
        assert client.last_probe_monotonic is not None
        warnings = [r for r in records if r["event"] == "reranker_client.unavailable"]
        assert len(warnings) == 1
        assert warnings[0]["reason"] == "error_RuntimeError"
        assert not [r for r in records if r["event"] == "reranker_client.probe_failed"]
