"""Unit tests for GPUEmbeddingService — httpx async client to GPU embedding server.

Tests mock httpx.AsyncClient to avoid requiring a running GPU service.
All tests verify behavior without real HTTP calls.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from brain_v42.services.embedding_wire import OpenAIWire, ShimWire
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable, GPUEmbeddingService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _http_response(status: int, *, retry_after: str | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        request=httpx.Request("POST", "http://embed.test/embed/query"),
        headers={"Retry-After": retry_after} if retry_after is not None else {},
        json=[0.01] * 1536,
    )


class TestRateLimits:
    @pytest.mark.parametrize("retry_after", ["3", "Thu, 01 Jan 1970 00:00:03 GMT"])
    @pytest.mark.asyncio
    async def test_retry_after_controls_the_delay(self, retry_after: str) -> None:
        service = GPUEmbeddingService()
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.side_effect = [
            _http_response(429, retry_after=retry_after),
            _http_response(200),
        ]
        service._client = client
        with (
            patch("asyncio.sleep", new=AsyncMock()) as sleep,
            patch("time.time", return_value=0.0),
        ):
            assert await service.embed("document") == [0.01] * 1536
        sleep.assert_awaited_once_with(3.0)
        assert client.post.await_count == 2

    @pytest.mark.parametrize("retry_after", [None, "malformed"])
    @pytest.mark.asyncio
    async def test_missing_or_malformed_retry_after_uses_backoff(
        self, retry_after: str | None
    ) -> None:
        service = GPUEmbeddingService()
        client = AsyncMock(spec=httpx.AsyncClient)
        limited = _http_response(429, retry_after=retry_after)
        client.post.side_effect = [limited, limited, _http_response(200)]
        service._client = client
        with patch("asyncio.sleep", new=AsyncMock()) as sleep:
            await service.embed("document")
        assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]

    @pytest.mark.asyncio
    async def test_non_finite_retry_after_uses_backoff(self) -> None:
        service = GPUEmbeddingService()
        client = AsyncMock(spec=httpx.AsyncClient)
        limited = _http_response(429, retry_after="nan")
        client.post.side_effect = [limited, limited, _http_response(200)]
        service._client = client
        with patch("asyncio.sleep", new=AsyncMock()) as sleep:
            await service.embed("document")
        assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]

    @pytest.mark.asyncio
    async def test_retry_after_cannot_be_shortened_to_fit_the_remaining_budget(self) -> None:
        service = GPUEmbeddingService(retry_budget_seconds=10.0)
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.return_value = _http_response(429, retry_after="3")
        service._client = client
        with (
            patch("asyncio.sleep", new=AsyncMock()) as sleep,
            patch("brain_v42.services.gpu_embedding_service.monotonic", side_effect=[0.0, 8.0]),
            pytest.raises(EmbeddingUnavailable) as caught,
        ):
            await service.embed("document")
        assert caught.value.kind == "rate_limited"
        sleep.assert_not_awaited()
        assert client.post.await_count == 1

    @pytest.mark.parametrize("retry_after", ["3600", "inf"])
    @pytest.mark.asyncio
    async def test_unbudgeted_client_refuses_an_unbounded_cooldown(self, retry_after: str) -> None:
        """Without a budget, a huge Retry-After must not park the caller indefinitely."""
        service = GPUEmbeddingService()
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.return_value = _http_response(429, retry_after=retry_after)
        service._client = client
        with (
            patch("asyncio.sleep", new=AsyncMock()) as sleep,
            pytest.raises(EmbeddingUnavailable) as caught,
        ):
            await service.embed("document")
        assert caught.value.kind == "rate_limited"
        sleep.assert_not_awaited()
        assert client.post.await_count == 1

    @pytest.mark.asyncio
    async def test_exhausted_rate_limits_keep_their_kind(self) -> None:
        service = GPUEmbeddingService(max_retries=2)
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.return_value = _http_response(429)
        service._client = client
        with patch("asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(EmbeddingUnavailable) as caught:
                await service.embed("document")
        assert caught.value.kind == "rate_limited"
        assert client.post.await_count == 3
        assert sleep.await_count == 2

    @pytest.mark.parametrize("status", [400, 401])
    @pytest.mark.asyncio
    async def test_other_client_errors_still_fail_without_retry(self, status: int) -> None:
        service = GPUEmbeddingService()
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.return_value = _http_response(status)
        service._client = client
        with patch("asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(EmbeddingUnavailable) as caught:
                await service.embed("document")
        assert caught.value.kind == "other"
        assert client.post.await_count == 1
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retry_log_names_status_and_delay_without_headers(self) -> None:
        from structlog.testing import capture_logs

        service = GPUEmbeddingService()
        response = _http_response(429, retry_after="3")
        response.headers["X-Private"] = "private-header-sentinel"
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.side_effect = [response, _http_response(200)]
        service._client = client
        with capture_logs() as records, patch("asyncio.sleep", new=AsyncMock()):
            await service.embed("document")
        retry = next(r for r in records if r["event"] == "gpu_embedding_service.retry")
        assert retry["status_code"] == 429
        assert retry["backoff_seconds"] == 3.0
        assert "private-header-sentinel" not in repr(records)


class TestElapsedBudgets:
    @pytest.mark.asyncio
    async def test_read_error_degrades_to_embedding_unavailable(self) -> None:
        service = GPUEmbeddingService(max_retries=0)
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.side_effect = httpx.ReadError(
            "connection dropped", request=httpx.Request("POST", "http://embed.test")
        )
        service._client = client
        with pytest.raises(EmbeddingUnavailable) as caught:
            await service.embed("document")
        assert caught.value.kind == "unreachable"

    @pytest.mark.parametrize("method", ["embed", "embed_query", "embed_texts"])
    @pytest.mark.asyncio
    async def test_hanging_attempt_degrades_on_deadline(self, method: str) -> None:
        service = GPUEmbeddingService(retry_budget_seconds=5.0)
        client = AsyncMock(spec=httpx.AsyncClient)

        async def hang(*args: object, **kwargs: object) -> httpx.Response:
            await asyncio.get_running_loop().create_future()
            raise AssertionError("the deadline must cancel the HTTP attempt")

        client.post.side_effect = hang
        service._client = client
        real_timeout = asyncio.timeout
        # Expire on the next loop turn instead of waiting for wall-clock seconds.
        with patch("asyncio.timeout", side_effect=lambda delay: real_timeout(0)) as timeout:
            with pytest.raises(EmbeddingUnavailable) as caught:
                # wait_for is the guard: should the deadline envelope regress, the
                # hanging attempt fails this test instead of stalling the job. It uses
                # asyncio.timeouts.timeout internally, which the patch above leaves alone.
                await asyncio.wait_for(
                    getattr(service, method)(["document"] if method == "embed_texts" else "text"),
                    timeout=2.0,
                )
        assert caught.value.kind == "deadline"
        timeout.assert_called_once_with(5.0)
        assert client.post.await_count == 1

    @pytest.mark.parametrize("failure", ["connect", "timeout", "server", "gpu_busy"])
    @pytest.mark.asyncio
    async def test_backoff_cannot_cross_the_remaining_budget(self, failure: str) -> None:
        service = GPUEmbeddingService(retry_budget_seconds=10.0)
        client = AsyncMock(spec=httpx.AsyncClient)
        if failure == "connect":
            client.post.side_effect = httpx.ConnectError("refused")
        elif failure == "timeout":
            client.post.side_effect = httpx.ReadTimeout("read timeout")
        else:
            response = _http_response(500)
            if failure == "gpu_busy":
                response = httpx.Response(503, request=response.request, json={"error": "gpu_busy"})
            client.post.return_value = response
        service._client = client
        with (
            patch("asyncio.sleep", new=AsyncMock()) as sleep,
            patch("brain_v42.services.gpu_embedding_service.monotonic", side_effect=[0.0, 9.8]),
            pytest.raises(EmbeddingUnavailable) as caught,
        ):
            await service.embed("document")
        assert caught.value.kind == "deadline"
        sleep.assert_not_awaited()
        assert client.post.await_count == 1

    @pytest.mark.parametrize("method", ["embed", "embed_query", "embed_texts"])
    @pytest.mark.asyncio
    async def test_query_deadline_leaves_writes_the_longer_budget(self, method: str) -> None:
        service = GPUEmbeddingService(retry_budget_seconds=60.0, query_deadline_seconds=8.0)
        client = AsyncMock(spec=httpx.AsyncClient)
        response = _http_response(200)
        if method == "embed_texts":
            response = httpx.Response(200, request=response.request, json=[[0.01] * 1536])
        client.post.side_effect = [httpx.ConnectError("refused"), response]
        service._client = client
        with (
            patch("asyncio.sleep", new=AsyncMock()) as sleep,
            patch("brain_v42.services.gpu_embedding_service.monotonic", side_effect=[0.0, 7.8]),
        ):
            if method == "embed_query":
                with pytest.raises(EmbeddingUnavailable) as caught:
                    await service.embed_query("query")
                assert caught.value.kind == "deadline"
                sleep.assert_not_awaited()
            else:
                await getattr(service, method)(["document"] if method == "embed_texts" else "text")
                sleep.assert_awaited_once_with(0.5)

    @pytest.mark.parametrize(
        ("write_budget", "query_deadline", "expected"),
        [(3.0, 8.0, 3.0), (None, 8.0, 8.0), (3.0, None, 3.0), (None, None, None)],
    )
    @pytest.mark.asyncio
    async def test_query_uses_the_shortest_configured_limit(
        self, write_budget: float | None, query_deadline: float | None, expected: float | None
    ) -> None:
        service = GPUEmbeddingService(
            retry_budget_seconds=write_budget, query_deadline_seconds=query_deadline
        )
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.return_value = _http_response(200)
        service._client = client
        with patch("asyncio.timeout", wraps=asyncio.timeout) as timeout:
            await service.embed_query("query")
        timeout.assert_called_once_with(expected)


class TestExpectedDimension:
    @pytest.mark.parametrize("backend", ["shim", "openai"])
    @pytest.mark.parametrize("method", ["embed", "embed_query", "embed_texts"])
    @pytest.mark.parametrize("expected_dimension", [1536, None])
    @pytest.mark.asyncio
    async def test_wrong_size_vectors_degrade_only_when_dimension_is_configured(
        self, backend: str, method: str, expected_dimension: int | None
    ) -> None:
        service = GPUEmbeddingService(
            wire=OpenAIWire(model="m") if backend == "openai" else ShimWire(),
            expected_dimension=expected_dimension,
        )
        wrong_vector = [1.0] + [0.0] * 1534
        vectors = (
            [[1.0] + [0.0] * 1535, wrong_vector] if method == "embed_texts" else [wrong_vector]
        )
        payload: object
        if backend == "openai":
            payload = {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
        else:
            payload = vectors if method == "embed_texts" else wrong_vector
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post.return_value = httpx.Response(
            200, request=httpx.Request("POST", "http://embed.test"), json=payload
        )
        service._client = client
        call = getattr(service, method)
        argument = ["first", "second"] if method == "embed_texts" else "text"
        if expected_dimension is not None:
            with pytest.raises(EmbeddingUnavailable, match="1535.*1536") as caught:
                await call(argument)
            assert caught.value.kind == "other"
        else:
            assert await call(argument) == (vectors if method == "embed_texts" else wrong_vector)
        body = client.post.call_args.kwargs["json"]
        if backend == "openai":
            assert body == {
                "model": "m",
                "input": argument if isinstance(argument, list) else [argument],
                "encoding_format": "float",
            }
        else:
            assert body == ({"texts": argument} if method == "embed_texts" else {"text": argument})


@pytest.fixture
def service() -> GPUEmbeddingService:
    """Create a GPUEmbeddingService with default settings."""
    return GPUEmbeddingService(base_url="http://localhost:8003")


@pytest.fixture
def fake_embedding_1536() -> list[float]:
    """A fake 1536-dim embedding vector."""
    return [0.01] * 1536


# ---------------------------------------------------------------------------
# embed()
# ---------------------------------------------------------------------------


def test_client_created_log_redacts_base_url() -> None:
    from structlog.testing import capture_logs

    service = GPUEmbeddingService(base_url="https://user:secret@embed.test/api?token=hidden")
    with capture_logs() as records:
        service._get_client()
    created = next(
        record for record in records if record["event"] == "gpu_embedding_service.client_created"
    )
    assert created["base_url"] == "https://embed.test/api"
    # Only the logged value is redacted: the client still calls the configured URL.
    assert service._client is not None
    assert service._client.base_url.userinfo == b"user:secret"
    assert service._client.base_url.host == "embed.test"


class TestEmbed:
    @pytest.mark.asyncio
    async def test_embed_returns_vector(
        self, service: GPUEmbeddingService, fake_embedding_1536: list[float]
    ) -> None:
        """embed() returns a list of floats from the GPU service."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = fake_embedding_1536
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)
        service._client = mock_client

        result = await service.embed("hello world")

        assert result == fake_embedding_1536
        assert len(result) == 1536
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "/embed/query" in call_args[0][0] or "/embed/query" in str(call_args)

    @pytest.mark.asyncio
    async def test_embed_passes_text_as_json_body(
        self, service: GPUEmbeddingService, fake_embedding_1536: list[float]
    ) -> None:
        """embed() sends text as a JSON body to POST /embed/query.

        Body form (not query param) is required to embed long inputs
        without tripping the ~8KB URL-length ceiling at the supervisor.
        """
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = fake_embedding_1536
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)
        service._client = mock_client

        await service.embed("test query")

        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs.get("json") == {"text": "test query"}
        assert call_kwargs.kwargs.get("params") is None


# ---------------------------------------------------------------------------
# embed_texts()
# ---------------------------------------------------------------------------


class TestEmbedTexts:
    @pytest.mark.asyncio
    async def test_embed_texts_returns_batch(self, service: GPUEmbeddingService) -> None:
        """embed_texts() returns one vector per input text."""
        fake_batch = [[0.01] * 1536, [0.02] * 1536, [0.03] * 1536]

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = fake_batch
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)
        service._client = mock_client

        results = await service.embed_texts(["a", "b", "c"])

        assert len(results) == 3
        assert all(len(v) == 1536 for v in results)

    @pytest.mark.asyncio
    async def test_embed_texts_sends_json_body(self, service: GPUEmbeddingService) -> None:
        """embed_texts() sends texts as JSON body to POST /embed."""
        fake_batch = [[0.01] * 1536]

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = fake_batch
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)
        service._client = mock_client

        await service.embed_texts(["hello"])

        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs.get("json") == {"texts": ["hello"]}

    @pytest.mark.asyncio
    async def test_embed_texts_empty_list(self, service: GPUEmbeddingService) -> None:
        """embed_texts([]) returns empty list without making HTTP call."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        service._client = mock_client

        results = await service.embed_texts([])

        assert results == []
        mock_client.post.assert_not_called()


# ---------------------------------------------------------------------------
# similarity()
# ---------------------------------------------------------------------------


class TestSimilarity:
    def test_similarity_identical_vectors(self, service: GPUEmbeddingService) -> None:
        """similarity() returns ~1.0 for identical unit vectors."""
        v = [1.0] + [0.0] * 1535
        assert service.similarity(v, v) == pytest.approx(1.0, abs=1e-5)

    def test_similarity_orthogonal_vectors(self, service: GPUEmbeddingService) -> None:
        """similarity() returns ~0.0 for orthogonal vectors."""
        v1 = [1.0] + [0.0] * 1535
        v2 = [0.0, 1.0] + [0.0] * 1534
        assert service.similarity(v1, v2) == pytest.approx(0.0, abs=1e-5)

    def test_similarity_opposite_vectors(self, service: GPUEmbeddingService) -> None:
        """similarity() returns ~-1.0 for opposite unit vectors."""
        v1 = [1.0] + [0.0] * 1535
        v2 = [-1.0] + [0.0] * 1535
        assert service.similarity(v1, v2) == pytest.approx(-1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# healthcheck()
# ---------------------------------------------------------------------------


class TestHealthcheck:
    @pytest.mark.asyncio
    async def test_healthcheck_returns_true_on_200(self, service: GPUEmbeddingService) -> None:
        """healthcheck() returns True when GPU service responds 200."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        service._client = mock_client

        result = await service.healthcheck()
        assert result is True

    @pytest.mark.asyncio
    async def test_healthcheck_returns_false_on_error(self, service: GPUEmbeddingService) -> None:
        """healthcheck() returns False when GPU service is down."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        service._client = mock_client

        result = await service.healthcheck()
        assert result is False


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


class TestRetry:
    @pytest.mark.asyncio
    async def test_embed_retries_on_connection_error(
        self, service: GPUEmbeddingService, fake_embedding_1536: list[float]
    ) -> None:
        """embed() retries on ConnectionError and succeeds on subsequent attempt."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = fake_embedding_1536
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        # Fail twice, succeed on third
        mock_client.post = AsyncMock(
            side_effect=[
                httpx.ConnectError("refused"),
                httpx.ConnectError("refused"),
                mock_response,
            ]
        )
        service._client = mock_client

        result = await service.embed("test")

        assert result == fake_embedding_1536
        assert mock_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_embed_raises_after_max_retries(self, service: GPUEmbeddingService) -> None:
        """embed() raises EmbeddingUnavailable after exhausting all retries.

        Updated contract (2026-04-15): all terminal failures (connect error,
        timeout, 5xx) now surface as EmbeddingUnavailable so callers only
        need one except clause to decide on graceful degradation.
        """
        from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        service._client = mock_client

        with pytest.raises(EmbeddingUnavailable):
            await service.embed("test")

        assert mock_client.post.call_count >= 3

    @pytest.mark.asyncio
    async def test_embed_texts_retries_on_connection_error(
        self, service: GPUEmbeddingService
    ) -> None:
        """embed_texts() retries on ConnectionError."""
        fake_batch = [[0.01] * 1536]
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = fake_batch
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            side_effect=[
                httpx.ConnectError("refused"),
                mock_response,
            ]
        )
        service._client = mock_client

        result = await service.embed_texts(["hello"])

        assert len(result) == 1
        assert mock_client.post.call_count == 2


# ---------------------------------------------------------------------------
# Retry on TimeoutException
# ---------------------------------------------------------------------------


class TestRetryOnTimeout:
    @pytest.mark.asyncio
    async def test_retries_on_timeout(
        self, service: GPUEmbeddingService, fake_embedding_1536: list[float]
    ) -> None:
        """embed() must retry on TimeoutException."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = fake_embedding_1536
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(
            side_effect=[
                httpx.TimeoutException("read timeout"),
                mock_response,
            ]
        )
        service._client = mock_client

        result = await service.embed("test")

        assert result == fake_embedding_1536
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_timeout_after_max_retries(self, service: GPUEmbeddingService) -> None:
        """embed() raises EmbeddingUnavailable after exhausting retries on timeout."""
        from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("read timeout"))
        service._client = mock_client

        with pytest.raises(EmbeddingUnavailable):
            await service.embed("test")

        assert mock_client.post.call_count == 4  # 1 initial + 3 retries


# ---------------------------------------------------------------------------
# Retry on HTTP 5xx
# ---------------------------------------------------------------------------


class TestRetryOn5xx:
    @pytest.mark.asyncio
    async def test_retries_on_server_error(
        self, service: GPUEmbeddingService, fake_embedding_1536: list[float]
    ) -> None:
        """embed() must retry on HTTP 500."""
        error_request = httpx.Request("POST", "http://localhost:8003/embed/query")
        error_response = httpx.Response(500, request=error_request)

        ok_response = MagicMock(spec=httpx.Response)
        ok_response.status_code = 200
        ok_response.json.return_value = fake_embedding_1536
        ok_response.raise_for_status = MagicMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        # First call returns 500 (raise_for_status raises), second succeeds
        fail_response = MagicMock(spec=httpx.Response)
        fail_response.status_code = 500
        fail_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error", request=error_request, response=error_response
            )
        )

        mock_client.post = AsyncMock(side_effect=[fail_response, ok_response])
        service._client = mock_client

        result = await service.embed("test")

        assert result == fake_embedding_1536
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_4xx(self, service: GPUEmbeddingService) -> None:
        """embed() must NOT retry on HTTP 400."""
        error_request = httpx.Request("POST", "http://localhost:8003/embed/query")
        error_response = httpx.Response(400, request=error_request)

        fail_response = MagicMock(spec=httpx.Response)
        fail_response.status_code = 400
        fail_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Bad Request", request=error_request, response=error_response
            )
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=fail_response)
        service._client = mock_client

        # 4xx is still not retried — that is what this test is about. It now
        # surfaces as EmbeddingUnavailable rather than a raw HTTPStatusError:
        # a 4xx from an embedding endpoint (rejected key, rate limit, unknown
        # model) still means "no embedding right now", and letting it escape
        # raw took down brain_search instead of degrading it to FTS. See
        # test_embedding_degradation_contract.py.
        with pytest.raises(EmbeddingUnavailable):
            await service.embed("test")

        # Should NOT retry — only 1 call
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_raises_5xx_after_max_retries(self, service: GPUEmbeddingService) -> None:
        """embed() raises EmbeddingUnavailable after exhausting retries on 5xx."""
        from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable

        error_request = httpx.Request("POST", "http://localhost:8003/embed/query")
        error_response = httpx.Response(502, request=error_request)

        fail_response = MagicMock(spec=httpx.Response)
        fail_response.status_code = 502
        fail_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Bad Gateway", request=error_request, response=error_response
            )
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=fail_response)
        service._client = mock_client

        with pytest.raises(EmbeddingUnavailable):
            await service.embed("test")

        assert mock_client.post.call_count == 4  # 1 initial + 3 retries


# ---------------------------------------------------------------------------
# Lazy client initialization
# ---------------------------------------------------------------------------


class TestClientInit:
    def test_client_not_created_at_init(self, service: GPUEmbeddingService) -> None:
        """httpx.AsyncClient is NOT created at __init__ (lazy)."""
        assert service._client is None

    def test_client_has_bounded_connection_limits(self, service: GPUEmbeddingService) -> None:
        """_get_client() creates an AsyncClient with explicit connection limits."""
        client = service._get_client()
        pool = client._transport._pool
        assert pool._max_connections == 20
        assert pool._max_keepalive_connections == 10

    @pytest.mark.asyncio
    async def test_client_created_on_first_embed(
        self, service: GPUEmbeddingService, fake_embedding_1536: list[float]
    ) -> None:
        """httpx.AsyncClient is created lazily on first embed() call."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = fake_embedding_1536
        mock_response.raise_for_status = MagicMock()

        # Capture real class before patching (Python 3.14 cannot spec a Mock)
        real_async_client = httpx.AsyncClient
        mock_instance = AsyncMock(spec=real_async_client)
        mock_instance.post = AsyncMock(return_value=mock_response)

        with patch("brain_v42.services.gpu_embedding_service.httpx.AsyncClient") as MockClient:
            MockClient.return_value = mock_instance

            await service.embed("test")

            MockClient.assert_called_once()
            assert service._client is mock_instance


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_close_closes_client(self, service: GPUEmbeddingService) -> None:
        """close() closes the underlying httpx.AsyncClient."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        service._client = mock_client

        await service.close()

        mock_client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_noop_when_no_client(self, service: GPUEmbeddingService) -> None:
        """close() is a no-op when client was never created."""
        await service.close()  # Should not raise


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_default_base_url(self) -> None:
        """Default base_url is http://localhost:8003."""
        svc = GPUEmbeddingService()
        assert svc._base_url == "http://localhost:8003"

    def test_custom_base_url(self) -> None:
        """Custom base_url is respected."""
        svc = GPUEmbeddingService(base_url="http://gpu-host:9000")
        assert svc._base_url == "http://gpu-host:9000"

    def test_default_timeout(self) -> None:
        """Default timeout is 30 seconds."""
        svc = GPUEmbeddingService()
        assert svc._timeout == 30.0

    def test_default_max_retries(self) -> None:
        """Default max_retries is 3."""
        svc = GPUEmbeddingService()
        assert svc._max_retries == 3


# ---------------------------------------------------------------------------
# 503 gpu_busy — lazy supervisor path
# ---------------------------------------------------------------------------


class TestGpuBusy503:
    """When the supervisor returns 503 gpu_busy, retry with longer backoff."""

    def _make_503_response(self, payload: dict | None = None) -> MagicMock:
        r = MagicMock(spec=httpx.Response)
        r.status_code = 503
        r.json.return_value = payload or {"error": "gpu_busy", "free_mib": 2000}
        r.text = str(payload or {"error": "gpu_busy"})
        r.request = MagicMock()
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            "503 Service Unavailable",
            request=r.request,
            response=r,
        )
        return r

    def _make_200_response(self, body: list) -> MagicMock:
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.json.return_value = body
        r.raise_for_status = MagicMock()
        return r

    @pytest.mark.asyncio
    async def test_embed_retries_on_gpu_busy_then_succeeds(
        self, fake_embedding_1536: list[float]
    ) -> None:
        """When GPU frees up between retries, the call succeeds."""
        svc = GPUEmbeddingService(base_url="http://localhost:8003", max_retries=3)
        ok = self._make_200_response(fake_embedding_1536)
        busy = self._make_503_response()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=[busy, busy, ok])
        svc._client = mock_client

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await svc.embed("hello")

        assert result == fake_embedding_1536
        assert mock_client.post.await_count == 3
        # Backoff for gpu_busy uses the long schedule (≥5s) not the short one.
        first_delay = mock_sleep.await_args_list[0].args[0]
        assert first_delay >= 5.0, f"gpu_busy backoff should be ≥5s, got {first_delay}"

    @pytest.mark.asyncio
    async def test_embed_raises_unavailable_after_exhausting_gpu_busy(self) -> None:
        """All retries 503 → raise EmbeddingUnavailable (degradation signal)."""
        from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable

        svc = GPUEmbeddingService(base_url="http://localhost:8003", max_retries=2)
        busy = self._make_503_response()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=busy)
        svc._client = mock_client

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(EmbeddingUnavailable):
                await svc.embed("hello")

        assert mock_client.post.await_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_connect_error_raises_unavailable_after_retries(self) -> None:
        """gpu-host offline → ConnectError after retries → EmbeddingUnavailable."""
        from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable

        svc = GPUEmbeddingService(base_url="http://localhost:8003", max_retries=2)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        svc._client = mock_client

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(EmbeddingUnavailable):
                await svc.embed("hello")
