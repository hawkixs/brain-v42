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

from brain_v42.services.rerank_wire import RerankWire, ShimRerankWire

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
        *,
        wire: RerankWire | None = None,
        api_key: str = "",
    ) -> None:
        """Initialize RerankerClient without creating the HTTP client.

        Args:
            base_url: URL of the reranker service.
            timeout: HTTP request timeout in seconds.
            wire: Request/response shape. Defaults to the private shim contract.
            api_key: Sent as ``Authorization: Bearer`` when non-empty.
        """
        self._base_url = base_url
        self._timeout = timeout
        self._wire: RerankWire = wire if wire is not None else ShimRerankWire()
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or lazily create the httpx.AsyncClient.

        Returns:
            The shared httpx.AsyncClient instance.
        """
        if self._client is None:
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=headers,
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
        path, body = self._wire.request(query, candidates)
        response = await client.post(path, json=body)
        response.raise_for_status()
        return self._wire.parse(response.json(), expected=len(candidates))

    async def is_available(self) -> bool:
        """Check if the reranker service is healthy.

        Calls GET /health on the reranker service.

        Returns:
            True if the service responds with 200, False otherwise.
        """
        try:
            client = self._get_client()
            response = await client.get(self._wire.health_path)
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
