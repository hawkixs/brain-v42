"""The claim inventory service caches its one read; the briefing renders it loudly."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.mcp.tools.session_tools import make_session_briefing_loader
from brain_v42.repositories.pg_claim_inventory import ClaimInventory
from brain_v42.services import claim_inventory_service
from brain_v42.services.claim_extraction_counters import ClaimExtractionSnapshot
from brain_v42.services.claim_inventory_service import (
    ClaimInventoryReport,
    ClaimInventoryService,
)
from brain_v42.services.dream_run_service import KillswitchState


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


# ---------------------------------------------------------------------------
# Briefing line: loud at zero, explicit when the read fails.
# ---------------------------------------------------------------------------


def _killswitches() -> KillswitchState:
    return KillswitchState(
        last_run_date=None,
        promote_enabled=False,
        promote_dry=False,
        reorg_enabled=False,
        reorg_dry=False,
        promote_clean_dry_nights=0,
        reorg_clean_dry_nights=0,
    )


def _loader_services() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock, MagicMock, MagicMock]:
    """Only the independent reads a briefing composes (mirrors test_session_briefing_claim_suffix)."""
    context = MagicMock()
    context.get_by_key = AsyncMock(
        return_value=SimpleNamespace(
            project_key="brain-v42", current_focus="ship it", blockers=[], focus_updated_at=None
        )
    )
    decisions = MagicMock()
    decisions.list_all = AsyncMock(return_value=[])
    learnings = MagicMock()
    learnings.list_all = AsyncMock(return_value=[])
    dreams = MagicMock()
    dreams.killswitch_state = AsyncMock(return_value=_killswitches())
    dreams.last_failure = AsyncMock(return_value=None)
    features = MagicMock()
    features.roadmap_alive = AsyncMock(return_value=[])
    features.stale_pinned = AsyncMock(return_value=[])
    sessions = MagicMock()
    sessions.recent_checkpoints = AsyncMock(return_value=[])
    return context, decisions, learnings, dreams, features, sessions


def _report(
    inventory: ClaimInventory,
    *,
    skipped: int = 0,
    failed: int = 0,
) -> ClaimInventoryReport:
    return ClaimInventoryReport(
        inventory=inventory,
        extraction=ClaimExtractionSnapshot(
            skipped={("learning", "quota_full"): skipped},
            failed={("decision", "persistence_error"): failed},
        ),
    )


def _technical_state(briefing: str) -> str:
    return briefing.split("### État technique (mesuré)\n", 1)[1].split("\n\n", 1)[0]


async def _briefing(service: Any, *, enabled: bool) -> str:
    loader = make_session_briefing_loader(
        *_loader_services(), claim_inventory_svc=service, claim_extraction_enabled=enabled
    )
    return await loader("brain-v42", uuid4())


async def test_claim_inventory_briefing_loud_at_zero() -> None:
    service = AsyncMock()
    service.get.return_value = _report(ClaimInventory(0, 0, 0, 0))

    section = _technical_state(await _briefing(service, enabled=True))

    assert "- CLAIMS: 0 active; extraction on; 0 created in 7d" in section.splitlines()


async def test_claim_inventory_briefing_shows_split_state_and_non_zero_counters() -> None:
    service = AsyncMock()
    service.get.return_value = _report(ClaimInventory(3, 7, 2, 5), skipped=4, failed=1)

    section = _technical_state(await _briefing(service, enabled=False))

    assert (
        "- CLAIMS: 12 active (3 extracted, 7 declared, 2 measured); extraction off; "
        "5 created in 7d; 4 skipped, 1 failed since restart"
    ) in section.splitlines()


async def test_claim_inventory_unavailable_is_visible() -> None:
    service = AsyncMock()
    service.get.side_effect = RuntimeError("connection refused")

    section = _technical_state(await _briefing(service, enabled=True))

    line = next(line for line in section.splitlines() if "CLAIMS" in line)
    assert line == "- CLAIMS: unavailable (inventory read failed); extraction on"
    assert "0 active" not in section


async def test_briefing_without_inventory_service_is_unchanged() -> None:
    loader = make_session_briefing_loader(*_loader_services())

    assert "CLAIMS" not in await loader("brain-v42", uuid4())
