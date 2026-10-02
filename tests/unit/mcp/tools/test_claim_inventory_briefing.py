"""The claim inventory service caches its one read; the briefing renders it loudly."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.repositories.pg_claim_inventory import ClaimInventory
from brain_v42.services import claim_inventory_service
from brain_v42.services.claim_inventory_service import ClaimInventoryService


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_claim_inventory_cache_expires_after_60_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = AsyncMock(
        return_value=ClaimInventory(extracted=1, declared=2, measured=3, created_7d=4)
    )
    monkeypatch.setattr(claim_inventory_service, "read_claim_inventory", reads)
    clock = _Clock()
    service = ClaimInventoryService(MagicMock(), clock=clock)

    first = await service.get()
    clock.now += 59.9
    second = await service.get()

    assert reads.await_count == 1
    assert second.inventory == first.inventory

    clock.now += 0.2
    await service.get()

    assert reads.await_count == 2
