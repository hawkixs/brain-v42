"""Claim read orchestration keeps one clock and masks storage faults."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from brain_v42.models.claim_read import ClaimRead
from brain_v42.services import claim_read_service as module
from brain_v42.services.claim_read_service import ClaimReadError, ClaimReadService

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def _claim(entry_id: object, seq: int = 1) -> ClaimRead:
    return ClaimRead(
        id=uuid4(),
        seq=seq,
        entry_id=entry_id,
        entity_type="learning",
        project_key="project-a",
        claim_key="a" * 64,
        statement="Lag is bounded",
        fact_name="lag",
        definition_version=1,
        target="production",
        expected={"path": "/lag", "op": "lte", "value": 5},
        expected_resolved={"path": "/lag", "op": "lte", "value": 5},
        validity_seconds=60,
        provenance="declared",
        declared_by="tester",
        declared_at=NOW,
        recorded_at=NOW,
        retired_at=None,
        replaces_id=None,
        latest=None,
        conclusive=None,
    )


@asynccontextmanager
async def _session_factory():
    yield object()


@pytest.mark.asyncio
async def test_batch_groups_entries_at_one_injected_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_a, entry_b = uuid4(), uuid4()
    fetch = AsyncMock(return_value=[_claim(entry_b, 2), _claim(entry_a, 1)])
    monkeypatch.setattr(module.pg_claim_reads, "fetch_active_for_entries", fetch)
    ticks = iter([NOW, datetime(2026, 9, 27, tzinfo=UTC)])
    service = ClaimReadService(_session_factory, clock=lambda: next(ticks))
    result = await service.batch_summaries(
        [("learning", entry_a), ("learning", entry_b)], trusted_project_key="project-a"
    )
    assert result[("learning", entry_a)][0].as_of == NOW
    assert result[("learning", entry_b)][0].as_of == NOW
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_invalid_bounds_are_refused_before_sql() -> None:
    service = ClaimReadService(_session_factory)
    with pytest.raises(ClaimReadError) as error:
        await service.list_claims(after_seq=-1)
    assert error.value.code == "invalid_argument"
    with pytest.raises(ClaimReadError) as error:
        await service.list_claims(limit=True)
    assert error.value.code == "invalid_argument"
    with pytest.raises(ClaimReadError) as error:
        await service.history(uuid4(), limit=101)
    assert error.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_missing_or_out_of_scope_history_has_one_safe_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module.pg_claim_reads, "fetch_claim", AsyncMock(return_value=None))
    fetch_verdicts = AsyncMock()
    monkeypatch.setattr(module.pg_claim_reads, "fetch_verdict_page", fetch_verdicts)
    with pytest.raises(ClaimReadError) as error:
        await ClaimReadService(_session_factory).history(uuid4(), trusted_project_key="project-a")
    assert error.value.code == "claim_not_found"
    fetch_verdicts.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_storage_fault_is_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        module.pg_claim_reads,
        "fetch_active_for_entries",
        AsyncMock(side_effect=RuntimeError("secret")),
    )
    with pytest.raises(ClaimReadError) as error:
        await ClaimReadService(_session_factory).batch_summaries([("learning", uuid4())])
    assert error.value.code == "read_unavailable"
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_unexpected_evaluation_fault_is_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    entry_id = uuid4()
    monkeypatch.setattr(
        module.pg_claim_reads,
        "fetch_active_for_entries",
        AsyncMock(return_value=[_claim(entry_id)]),
    )

    def broken_evaluation(_claim: object, _now: datetime) -> object:
        raise RuntimeError("private row contents")

    monkeypatch.setattr(module, "evaluate_claim", broken_evaluation)
    with pytest.raises(ClaimReadError) as error:
        await ClaimReadService(_session_factory).batch_summaries([("learning", entry_id)])
    assert error.value.code == "read_unavailable"
    assert "private row contents" not in str(error.value)
