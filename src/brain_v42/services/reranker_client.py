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
import time

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
        self._last_probe_ok: bool | None = None
        self._last_probe_reason: str | None = None
        self._last_probe_monotonic: float | None = None

    @property
    def last_probe_ok(self) -> bool | None:
        """Outcome of the last ``is_available()`` call; ``None`` before the first."""
        return self._last_probe_ok

    @property
    def last_probe_reason(self) -> str | None:
        """``"ok"``, ``"http_<status>"`` or ``"transport_<ExceptionName>"``."""
        return self._last_probe_reason

    @property
    def last_probe_monotonic(self) -> float | None:
        """``time.monotonic()`` at the last probe, for staleness checks."""
        return self._last_probe_monotonic

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

        Calls GET on the wire's health path. The outcome and its reason are kept
        on the client, and a change of state is logged once: a hosted reranker
        whose key was revoked answers 401 forever, and without that line the only
        symptom is searches quietly falling back to RRF ordering.

        Returns:
            True if the service responds with 200, False otherwise. Never raises
            on a transport error.
        """
        try:
            client = self._get_client()
            response = await client.get(self._wire.health_path)
        except httpx.HTTPError as exc:
            return self._record_probe(False, f"transport_{type(exc).__name__}")
        if response.status_code == 200:
            return self._record_probe(True, "ok")
        return self._record_probe(False, f"http_{response.status_code}")

    def _record_probe(self, ok: bool, reason: str) -> bool:
        previous = self._last_probe_ok
        self._last_probe_ok = ok
        self._last_probe_reason = reason
        self._last_probe_monotonic = time.monotonic()
        if not ok and previous is not False:
            # Never the headers: they carry the bearer.
            logger.warning(
                "reranker_client.unavailable",
                reason=reason,
                health_path=self._wire.health_path,
            )
        elif ok and previous is False:
            logger.info("reranker_client.available_again", health_path=self._wire.health_path)
        return ok

    async def close(self) -> None:
        """Close the underlying httpx.AsyncClient.

        Safe to call even if the client was never created.
        """
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("reranker_client.client_closed")


async def run_rerank_probe_loop(client: RerankerClient, interval_seconds: float) -> None:
    """Probe ``client`` once now, then every ``interval_seconds``, until cancelled.

    Without it the health state only moves when something calls ``is_available()``,
    and a hosted reranker whose key was revoked would stay invisible until an
    operator wondered why search quality dropped. ``is_available()`` itself logs
    the transitions; this loop only has to keep it called, and must never die of
    a probe error, or the monitoring would stop silently.
    """
    while True:
        try:
            await client.is_available()
        except Exception:
            logger.warning("reranker_client.probe_failed", exc_info=True)
        await asyncio.sleep(interval_seconds)
