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
