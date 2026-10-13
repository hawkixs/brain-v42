"""Preserve calibrated reranking when a hosted provider fails or saturates."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import httpx
import structlog

from brain_v42.services.rerank_calibration import RerankCalibration, is_calibrated
from brain_v42.services.rerank_wire import is_hosted_wire
from brain_v42.services.reranker_client import (
    RerankBudgetExhausted,
    RerankChainObserver,
    RerankerClient,
)

logger = structlog.get_logger(__name__)


class RerankChain:
    """Reserve time for a second link and keep one search floor for both scales."""

    def __init__(
        self,
        primary: RerankerClient,
        fallback: RerankerClient,
        *,
        overall_budget: float,
        primary_budget: float,
        observer: RerankChainObserver | None = None,
    ) -> None:
        if not is_hosted_wire(primary._wire) or not is_hosted_wire(fallback._wire):
            raise ValueError("RerankChain requires hosted rerank wires for both links")
        if not is_calibrated(fallback.calibration.identity):
            raise ValueError("rerank_fallback_model requires a calibrated identity")
        if not 0 < primary_budget < overall_budget:
            raise ValueError("primary_budget must be positive and below overall_budget")
        self._primary = primary
        self._fallback = fallback
        self._overall_budget = overall_budget
        self._primary_budget = primary_budget
        # The factory already wires the MCP collector into the primary link.
        self._observer = observer if observer is not None else primary._observer
        self._observer_warning_logged = False
        primary_floor = primary.calibration.search_min_score
        fallback_floor = fallback.calibration.search_min_score
        self._shift = math.log(primary_floor / (1 - primary_floor)) - math.log(
            fallback_floor / (1 - fallback_floor)
        )

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        if not candidates:
            return []
        start = asyncio.get_running_loop().time()
        try:
            scores = await self._primary.rerank_until(
                query, candidates, deadline=start + self._primary_budget, may_wait=False
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        else:
            self._observe("on_chain", "primary", self._primary.calibration.identity)
            return scores
        self._observe("on_handover")
        try:
            scores = await self._fallback.rerank_until(
                query, candidates, deadline=start + self._overall_budget, may_wait=True
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            self._observe("on_chain", "failed_all", None)
            raise RerankBudgetExhausted("both rerank links failed") from exc
        self._observe("on_chain", "fallback", self._fallback.calibration.identity)
        # BrainService applies the primary floor after HybridReranker's sigmoid.
        return [score + self._shift for score in scores]

    def _observe(self, method: str, *args: Any) -> None:
        """Older or failing observers must never alter the calibrated result."""
        try:
            callback = getattr(self._observer, method, None)
            if callback is None:
                return
            callback(*args)
        except Exception:
            if not self._observer_warning_logged:
                self._observer_warning_logged = True
                logger.warning("rerank_chain.observer_failed")

    @property
    def calibration(self) -> RerankCalibration:
        return self._primary.calibration

    @property
    def last_probe_ok(self) -> bool | None:
        return self._primary.last_probe_ok

    @property
    def last_probe_reason(self) -> str | None:
        return self._primary.last_probe_reason

    @property
    def last_probe_monotonic(self) -> float | None:
        return self._primary.last_probe_monotonic

    async def is_available(self) -> bool:
        return await self._primary.is_available()

    async def close(self) -> None:
        try:
            await self._primary.close()
        finally:
            await self._fallback.close()
