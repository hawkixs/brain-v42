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

import asyncio

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
    - Only one retry case: a busy shim. The shim computes ONE rerank at a
      time and answers a concurrent request with 503 + ``Retry-After``
      instead of queueing it; the slot frees within seconds (measured
      2026-09-23: 11 % of brain_search reranks fell back to RRF on that 503
      alone). Such a 503 is retried a bounded number of times, sleeping the
      advertised delay capped at ``busy_retry_cap_seconds``. Every other
      failure -- including a 503 WITHOUT ``Retry-After``, which is an outage
      rather than a busy slot -- still raises at once: reranking stays
      best-effort and callers fall back to RRF ordering.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8003",
        timeout: float = 10.0,
        *,
        wire: RerankWire | None = None,
        api_key: str = "",
        busy_retries: int = 3,
        busy_retry_cap_seconds: float = 2.0,
    ) -> None:
        """Initialize RerankerClient without creating the HTTP client.

        Args:
            base_url: URL of the reranker service.
            timeout: HTTP request timeout in seconds.
            wire: Request/response shape. Defaults to the private shim contract.
            api_key: Sent as ``Authorization: Bearer`` when non-empty.
            busy_retries: Extra attempts after a busy 503 (``Retry-After`` set).
            busy_retry_cap_seconds: Upper bound on one advertised retry delay.
        """
        self._base_url = base_url
        self._timeout = timeout
        self._wire: RerankWire = wire if wire is not None else ShimRerankWire()
        self._api_key = api_key
        self._busy_retries = busy_retries
        self._busy_retry_cap_seconds = busy_retry_cap_seconds
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
        for attempt in range(self._busy_retries + 1):
            response = await client.post(path, json=body)
            delay = self._busy_delay(response)
            if delay is None or attempt == self._busy_retries:
                break
            logger.info(
                "reranker_client.busy_retry",
                attempt=attempt + 1,
                max_retries=self._busy_retries,
                delay_seconds=delay,
                n_candidates=len(candidates),
            )
            await asyncio.sleep(delay)
        response.raise_for_status()
        return self._wire.parse(response.json(), expected=len(candidates))

    def _busy_delay(self, response: httpx.Response) -> float | None:
        """Seconds to wait before retrying, or None when the answer is not "busy"."""
        if response.status_code != 503:
            return None
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return None
        try:
            advertised = float(retry_after)
        except ValueError:
            advertised = self._busy_retry_cap_seconds
        return min(max(advertised, 0.0), self._busy_retry_cap_seconds)

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
