"""Preserve calibrated reranking when a hosted provider fails or saturates."""

from __future__ import annotations

import asyncio
import math

import httpx

from brain_v42.services.rerank_calibration import RerankCalibration, is_calibrated
from brain_v42.services.rerank_wire import CohereRerankWire
from brain_v42.services.reranker_client import RerankBudgetExhausted, RerankerClient


class RerankChain:
    """Reserve time for a second link and keep one search floor for both scales."""

    def __init__(
        self,
        primary: RerankerClient,
        fallback: RerankerClient,
        *,
        overall_budget: float,
        primary_budget: float,
    ) -> None:
        if not isinstance(primary._wire, CohereRerankWire) or not isinstance(
            fallback._wire, CohereRerankWire
        ):
            raise ValueError("RerankChain requires CohereRerankWire for both links")
        if not is_calibrated(fallback.calibration.identity):
            raise ValueError("rerank_fallback_model requires a calibrated identity")
        if not 0 < primary_budget < overall_budget:
            raise ValueError("primary_budget must be positive and below overall_budget")
        self._primary = primary
        self._fallback = fallback
        self._overall_budget = overall_budget
        self._primary_budget = primary_budget
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
            return await self._primary.rerank_until(
                query, candidates, deadline=start + self._primary_budget, may_wait=False
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        try:
            scores = await self._fallback.rerank_until(
                query, candidates, deadline=start + self._overall_budget, may_wait=True
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise RerankBudgetExhausted("both rerank links failed") from exc
        # BrainService applies the primary floor after HybridReranker's sigmoid.
        return [score + self._shift for score in scores]

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
