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
