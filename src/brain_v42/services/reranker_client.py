"""HTTP client for the reranker service.

Connects to a cross-encoder reranker service via HTTP to score
query-candidate relevance. Used by ClusterGuard for feature deduplication.

The reranker service is expected to expose:
    POST /rerank  {"query": str, "candidates": [str, ...]}  -> {"scores": [float, ...]}
    GET  /health  -> 200 OK

Usage:
    client = RerankerClient(base_url="http://localhost:8003")
    scores = await client.rerank("decay system", ["Memory Decay", "Hybrid Search"])
    ok = await client.is_available()
    await client.close()
"""

from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger(__name__)


class RerankerClient:
    """Async HTTP client for a reranker service.

    Design decisions:
    - Lazy client: httpx.AsyncClient is NOT created at __init__ to allow
      sync construction and avoid event loop issues.
    - Same pattern as GPUEmbeddingService for consistency.
    - No retry logic: reranking is best-effort; callers fall back to
      embedding-only ranking if the service is unavailable.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8003",
        timeout: float = 10.0,
    ) -> None:
        """Initialize RerankerClient without creating the HTTP client.

        Args:
            base_url: URL of the reranker service.
            timeout: HTTP request timeout in seconds.
        """
        self._base_url = base_url
        self._timeout = timeout
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
                "reranker_client.client_created",
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        """Score query-candidate relevance via the reranker service.

        Calls POST /rerank with JSON body {"query": str, "candidates": [str, ...]}.

        Args:
            query: The query string to compare against.
            candidates: List of candidate strings to score.

        Returns:
            list[float] of relevance scores, one per candidate.
            Empty list if candidates is empty (no HTTP call made).
        """
        if not candidates:
            return []

        client = self._get_client()
        response = await client.post(
            "/rerank",
            json={"query": query, "candidates": candidates},
        )
        response.raise_for_status()
        return response.json()["scores"]  # type: ignore[no-any-return]

    async def is_available(self) -> bool:
        """Check if the reranker service is healthy.

        Calls GET /health on the reranker service.

        Returns:
            True if the service responds with 200, False otherwise.
        """
        try:
            client = self._get_client()
            response = await client.get("/health")
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
            logger.info("reranker_client.client_closed")
