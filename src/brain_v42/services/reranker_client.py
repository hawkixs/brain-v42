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
import random
import time
from typing import Any, Protocol

import httpx
import structlog

from brain_v42.config import is_relative_request_path
from brain_v42.services.rerank_calibration import RerankCalibration, calibration_for_identity
from brain_v42.services.rerank_wire import CohereRerankWire, RerankWire, ShimRerankWire

logger = structlog.get_logger(__name__)


class RerankBudgetExhausted(httpx.HTTPError):
    """Keep budget exhaustion on the existing best-effort HTTP fallback path."""


class RerankObserver(Protocol):
    """Synchronous telemetry; status outcomes on attempts count every HTTP failure.

    Operations use ok, budget_exhausted, http_error, transport_error or parse_error.
    Attempts additionally use http_<status> so the final failure is counted even
    when no retry is sent. Retry reasons use http_<status> or transport_<type>.
    """

    def on_attempt(self, identity: str, outcome: str, latency_ms: float) -> None: ...
    def on_retry(self, identity: str, reason: str) -> None: ...
    def on_operation(self, identity: str, outcome: str, total_ms: float) -> None: ...
    def on_probe(self, identity: str, ok: bool, reason: str) -> None: ...


class RerankerClient:
    """Async HTTP client for a reranker service.

    Design decisions:
    - Lazy client: httpx.AsyncClient is NOT created at __init__ to allow
      sync construction and avoid event loop issues.
    - Same pattern as GPUEmbeddingService for consistency.
    - A busy shim keeps its historical retry policy. It computes ONE rerank at a
      time and answers a concurrent request with 503 + ``Retry-After``
      instead of queueing it; the slot frees within seconds (measured
      2026-09-23: 11 % of brain_search reranks fell back to RRF on that 503
      alone). Such a 503 is retried a bounded number of times, sleeping the
      advertised delay capped at ``busy_retry_cap_seconds``. Every other
      failure -- including a 503 WITHOUT ``Retry-After``, which is an outage
      rather than a busy slot -- still raises at once: reranking stays
      best-effort and callers fall back to RRF ordering.
    - Hosted Cohere requests share one elapsed-time budget across all attempts
      and sleeps, so provider saturation cannot hold up a search indefinitely.
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
        budget_seconds: float = 1.5,
        max_retries: int = 2,
        observer: RerankObserver | None = None,
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
        self._calibration = calibration_for_identity(self._wire.identity)
        self._api_key = api_key
        self._busy_retries = busy_retries
        self._busy_retry_cap_seconds = busy_retry_cap_seconds
        self._budget_seconds = budget_seconds
        self._max_retries = max_retries
        self._observer = observer
        self._observer_warning_logged = False
        self._client: httpx.AsyncClient | None = None
        self._last_probe_ok: bool | None = None
        self._last_probe_reason: str | None = None
        self._last_probe_monotonic: float | None = None

    @property
    def calibration(self) -> RerankCalibration:
        """Expose only the wire's calibration so callers cannot inject another scale."""
        return self._calibration

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

        started = time.monotonic()
        outcome = "ok"
        try:
            if isinstance(self._wire, CohereRerankWire):
                deadline = asyncio.get_running_loop().time() + self._budget_seconds
                try:
                    async with asyncio.timeout_at(deadline):
                        scores = await self._rerank(query, candidates, deadline=deadline)
                        # Synchronous JSON parsing cannot yield to the timeout callback.
                        if asyncio.get_running_loop().time() >= deadline:
                            raise RerankBudgetExhausted("rerank latency budget exhausted")
                        return scores
                except TimeoutError as exc:
                    raise RerankBudgetExhausted("rerank latency budget exhausted") from exc
            return await self._rerank(query, candidates)
        except RerankBudgetExhausted:
            outcome = "budget_exhausted"
            raise
        except httpx.HTTPStatusError:
            outcome = "http_error"
            raise
        except httpx.HTTPError:
            outcome = "transport_error"
            raise
        except asyncio.CancelledError:
            outcome = "transport_error"
            raise
        except Exception:
            outcome = "parse_error"
            raise
        finally:
            self._observe("on_operation", outcome, (time.monotonic() - started) * 1000)

    async def _rerank(
        self, query: str, candidates: list[str], *, deadline: float | None = None
    ) -> list[float]:
        client = self._get_client()
        path, body = self._wire.request(query, candidates)
        retries = self._max_retries if deadline is not None else self._busy_retries
        for attempt in range(retries + 1):
            delay: float | None
            try:
                response = await self._request(client, path, body, deadline=deadline)
            except httpx.ConnectError as exc:
                if deadline is None:
                    raise
                delay = random.uniform(0, 0.1 * 2**attempt)
                reason = "transport_ConnectError"
                if attempt == retries:
                    raise RerankBudgetExhausted("rerank retries exhausted") from exc
            else:
                if isinstance(self._wire, ShimRerankWire):
                    delay = self._wire.retry_delay(
                        response, attempt, cap=self._busy_retry_cap_seconds
                    )
                else:
                    delay = self._wire.retry_delay(response, attempt)
                if delay is None or (deadline is None and attempt == retries):
                    response.raise_for_status()
                    return self._wire.parse(response.json(), expected=len(candidates))
                if attempt == retries:
                    raise RerankBudgetExhausted("rerank retries exhausted")
                reason = f"http_{response.status_code}"
            if deadline is not None and delay >= deadline - asyncio.get_running_loop().time():
                raise RerankBudgetExhausted("rerank cooldown exceeds remaining latency budget")
            self._observe("on_retry", reason)
            logger.info(
                "reranker_client.busy_retry" if deadline is None else "reranker_client.retry",
                attempt=attempt + 1,
                max_retries=retries,
                delay_seconds=delay,
                n_candidates=len(candidates),
            )
            await asyncio.sleep(delay)
        raise AssertionError("unreachable rerank attempt")

    async def _request(
        self,
        client: httpx.AsyncClient,
        path: str,
        body: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> httpx.Response:
        started = time.monotonic()
        outcome = "transport_error"
        try:
            response = await client.post(path, json=body)
            outcome = "ok" if response.is_success else f"http_{response.status_code}"
            return response
        except asyncio.CancelledError:
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                outcome = "budget_exhausted"
            raise
        finally:
            self._observe("on_attempt", outcome, (time.monotonic() - started) * 1000)

    def _observe(self, method: str, *args: Any) -> None:
        """Telemetry failures must never change search results or flood the logs."""
        if self._observer is None:
            return
        try:
            getattr(self._observer, method)(self._wire.identity, *args)
        except Exception:
            if not self._observer_warning_logged:
                self._observer_warning_logged = True
                logger.warning("reranker_client.observer_failed", identity=self._wire.identity)

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
        if not is_relative_request_path(self._wire.health_path):
            # Defence in depth behind the settings validator: httpx would send the
            # bearer to the host an absolute URL names, ignoring base_url.
            return self._record_probe(False, "invalid_health_path")
        try:
            client = self._get_client()
            response = await client.get(self._wire.health_path)
        except httpx.HTTPError as exc:
            return self._record_probe(False, f"transport_{type(exc).__name__}")
        except Exception as exc:
            # A probe must never raise, and an unexpected failure is still a state:
            # recorded and logged on entry like any other, not a traceback per tick.
            return self._record_probe(False, f"error_{type(exc).__name__}")
        if response.status_code == 200:
            return self._record_probe(True, "ok")
        return self._record_probe(False, f"http_{response.status_code}")

    def _record_probe(self, ok: bool, reason: str) -> bool:
        previous = self._last_probe_ok
        self._last_probe_ok = ok
        self._last_probe_reason = reason
        self._last_probe_monotonic = time.monotonic()
        self._observe("on_probe", ok, reason)
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
