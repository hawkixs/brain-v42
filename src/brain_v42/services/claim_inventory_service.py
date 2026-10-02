"""Cached claim inventory plus the in-process extraction counters.

The briefing can be requested often and from several clients; the database
read behind it is cached for a minute so it costs one aggregate per minute no
matter how many briefings are built. The extraction counters are in-process and
cheap, so they are copied fresh on every call rather than cached.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.repositories.pg_claim_inventory import ClaimInventory, read_claim_inventory
from brain_v42.services import claim_extraction_counters
from brain_v42.services.claim_extraction_counters import ClaimExtractionSnapshot

CACHE_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ClaimInventoryReport:
    inventory: ClaimInventory
    extraction: ClaimExtractionSnapshot


class ClaimInventoryService:
    """Read the claim inventory at most once per cache window.

    A failed read is never cached: the next call retries, so a transient
    database error is not frozen into a minute of "unavailable".
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._lock = asyncio.Lock()
        self._cached: tuple[float, ClaimInventory] | None = None

    async def get(self) -> ClaimInventoryReport:
        """Return the (possibly cached) inventory; raises when the read fails."""
        async with self._lock:
            now = self._clock()
            if self._cached is None or now - self._cached[0] >= self._cache_seconds:
                async with self._session_factory() as session:
                    inventory = await read_claim_inventory(session)
                self._cached = (now, inventory)
            return ClaimInventoryReport(
                inventory=self._cached[1], extraction=claim_extraction_counters.snapshot()
            )
