"""GPU-based text embedding service using httpx async client.

Connects to a GPU embedding server (Qodo-Embed-1-1.5B, 1536 dims)
via HTTP. Provides high-quality embeddings with GPU acceleration.

The GPU service is expected to expose:
    POST /embed/query  {"text": "..."}  -> [float, ...]       (raw array)
    POST /embed        {"texts": [...]} -> [[float, ...], ...]  (raw array of arrays)
    GET  /healthz                       -> 200 OK

Usage:
    service = GPUEmbeddingService(base_url="http://localhost:8003")
    vec = await service.embed("hello world")           # list[float], len=1536
    vecs = await service.embed_texts(["foo", "bar"])   # list[list[float]]
    sim = service.similarity(vec_a, vec_b)             # float (dot product)
    ok = await service.healthcheck()                   # bool
    await service.close()                              # cleanup
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

logger = structlog.get_logger(__name__)


class EmbeddingUnavailable(Exception):
    """Raised when the embedding service is unreachable OR refuses service
    (supervisor 503 gpu_busy) after all retries. Callers should degrade
    gracefully — brain_search falls back to FTS, brain_learn still persists
    with NULL embedding, etc.

    The ``kind`` attribute distinguishes expected GPU contention from real
    outages so observers (red-monitor, the metrics sidecar) can alert
    differently:
        - "gpu_busy"     — supervisor returned 503 gpu_busy after retries
        - "unreachable"  — TCP layer never produced a response
        - "other"        — non-503 5xx that exhausted retries
    """

    def __init__(self, message: str, *, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


# Long backoff schedule used when the supervisor signals gpu_busy — the GPU
# may be busy for a sustained period (user gaming, training job), so short
# backoffs just waste retry budget.
_GPU_BUSY_BACKOFF_SEC = (5.0, 10.0, 20.0)


class GPUEmbeddingService:
    """Text embedding service backed by a GPU HTTP server.

    Design decisions:
    - Lazy client: httpx.AsyncClient is NOT created at __init__ to allow
      sync construction and avoid event loop issues.
    - Retry with backoff: transient errors (ConnectionError, TimeoutException,
      HTTP 5xx) retried up to max_retries times with exponential backoff
      (0.5s, 1s, 2s). HTTP 4xx errors propagate immediately.
    - L2 normalization done server-side: no client-side normalization needed.
    - Duck-typed interface: embed(), embed_texts(), similarity() match the
      contract expected by all domain services.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8003",
        timeout: float = 30.0,
        max_retries: int = 3,
        *,
        query_prefix: str = "",
        document_prefix: str = "",
    ) -> None:
        """Initialize GPUEmbeddingService without creating the HTTP client.

        Args:
            base_url: URL of the GPU embedding server.
            timeout: HTTP request timeout in seconds.
            max_retries: Number of retries on connection errors.
            query_prefix: Instruction prefix prepended by ``embed_query()``.
            document_prefix: Instruction prefix prepended by ``embed()`` and
                ``embed_texts()``.

        Both prefixes default to empty, which makes prefixing a no-op and keeps
        the outgoing request bodies byte-identical to the unprefixed contract.
        """
        self._base_url = base_url
        self._timeout = timeout
        self._max_retries = max_retries
        self._query_prefix = query_prefix
        self._document_prefix = document_prefix
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or lazily create the httpx.AsyncClient.

        Returns:
            The shared httpx.AsyncClient instance.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
            logger.info(
                "gpu_embedding_service.client_created",
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> httpx.Response:
        """Execute an HTTP request with retry logic on connection errors.

        Args:
            method: HTTP method ("GET" or "POST").
            url: URL path (relative to base_url).
            **kwargs: Additional arguments passed to httpx request.

        Returns:
            httpx.Response on success.

        Raises:
            httpx.ConnectError: After exhausting all retries on connection errors.
            httpx.TimeoutException: After exhausting all retries on timeouts.
            httpx.HTTPStatusError: After exhausting retries on 5xx, or immediately on 4xx.
        """
        client = self._get_client()
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                if method == "GET":
                    response = await client.get(url, **kwargs)  # type: ignore[arg-type]
                else:
                    response = await client.post(url, **kwargs)  # type: ignore[arg-type]
                response.raise_for_status()
                return response
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt < self._max_retries:
                    backoff = 0.5 * (2**attempt)
                    logger.warning(
                        "gpu_embedding_service.retry",
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        backoff_seconds=backoff,
                        error=str(exc),
                    )
                    await asyncio.sleep(backoff)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                code = exc.response.status_code
                is_gpu_busy = self._is_gpu_busy_response(exc.response)
                if code >= 500 and attempt < self._max_retries:
                    # Long backoff for 503 gpu_busy — GPU likely held for a
                    # sustained period (game, training job). Short backoff
                    # for other 5xx (transient server glitch).
                    if is_gpu_busy:
                        backoff = _GPU_BUSY_BACKOFF_SEC[
                            min(attempt, len(_GPU_BUSY_BACKOFF_SEC) - 1)
                        ]
                    else:
                        backoff = 0.5 * (2**attempt)
                    logger.warning(
                        "gpu_embedding_service.retry",
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        backoff_seconds=backoff,
                        error=str(exc),
                        status_code=code,
                        gpu_busy=is_gpu_busy,
                    )
                    await asyncio.sleep(backoff)
                elif code >= 500:
                    # Exhausted retries on 5xx (incl gpu_busy) — surface as
                    # EmbeddingUnavailable so callers degrade gracefully.
                    if is_gpu_busy:
                        raise EmbeddingUnavailable(
                            f"gpu_busy after {self._max_retries} retries",
                            kind="gpu_busy",
                        ) from exc
                    raise EmbeddingUnavailable(
                        f"HTTP {code} after {self._max_retries} retries",
                        kind="other",
                    ) from exc
                else:
                    raise  # 4xx propagates as-is

        # Connection/timeout errors exhausted — treat as service unavailable.
        raise EmbeddingUnavailable(
            f"embedding service unreachable after {self._max_retries} retries",
            kind="unreachable",
        ) from last_error

    @staticmethod
    def _is_gpu_busy_response(resp: httpx.Response) -> bool:
        if resp.status_code != 503:
            return False
        try:
            return bool(resp.json().get("error") == "gpu_busy")
        except Exception:  # noqa: BLE001
            return False

    async def embed(self, text: str) -> list[float]:
        """Embed a single DOCUMENT into a vector.

        This is the document path. ``/embed/query`` names the single-text
        route, not the query intent — most callers here are writes (learnings,
        decisions, ADRs, snippets, runbooks, features, dedup comparisons).
        Queries call :meth:`embed_query` instead.

        Body form is used instead of the legacy ?text= query param so long
        inputs (10KB+ brain_learn insights, SYNTH outputs) don't hit the ~8KB
        URL-length ceiling at the supervisor's aiohttp inbound.

        Args:
            text: Document text to embed.

        Returns:
            list[float], one vector of the configured embedding dimension.
        """
        response = await self._request_with_retry(
            "POST",
            "/embed/query",
            json={"text": self._document_prefix + text},
        )
        return response.json()  # type: ignore[no-any-return]

    async def embed_query(self, text: str) -> list[float]:
        """Embed a SEARCH QUERY into a vector.

        Same route as :meth:`embed`; the difference is which instruction prefix
        is applied. Asymmetric models (Qodo, E5, BGE) need the query side
        marked differently from the document side, and only the call site knows
        which it is.

        Args:
            text: Query text to embed.

        Returns:
            list[float], one vector of the configured embedding dimension.
        """
        response = await self._request_with_retry(
            "POST",
            "/embed/query",
            json={"text": self._query_prefix + text},
        )
        return response.json()  # type: ignore[no-any-return]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of DOCUMENTS into vectors.

        Calls POST /embed with JSON body {"texts": [...]}.

        Args:
            texts: Document texts to embed. Empty list returns [] without HTTP call.

        Returns:
            list[list[float]], one vector per input text.
        """
        if not texts:
            return []

        response = await self._request_with_retry(
            "POST",
            "/embed",
            json={"texts": [self._document_prefix + t for t in texts]},
        )
        return response.json()  # type: ignore[no-any-return]

    def similarity(self, vec_a: list[float], vec_b: list[float]) -> float:
        """Compute dot product similarity between two vectors.

        With L2-normalized vectors (as returned by the GPU service),
        dot product equals cosine similarity.

        Args:
            vec_a: First embedding vector.
            vec_b: Second embedding vector.

        Returns:
            float representing similarity (dot product).
        """
        return sum(a * b for a, b in zip(vec_a, vec_b, strict=True))

    async def healthcheck(self) -> bool:
        """Check if the GPU embedding service is healthy.

        Calls GET /healthz on the GPU service.

        Returns:
            True if the service responds with 200, False otherwise.
        """
        try:
            client = self._get_client()
            response = await client.get("/healthz")
            return response.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError):
            return False

    async def close(self) -> None:
        """Close the underlying httpx.AsyncClient.

        Safe to call even if the client was never created.
        """
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("gpu_embedding_service.client_closed")
