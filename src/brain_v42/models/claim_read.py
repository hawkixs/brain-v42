"""Immutable claim read records and clock-dependent validity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

ClaimStatus = Literal["unverified", "unreadable", "holds", "falsified", "stale"]


@dataclass(frozen=True, slots=True)
class VerdictRead:
    """The audit fields needed to explain a latest or conclusive observation."""

    id: UUID
    seq: int
    verdict: str
    reason: str | None
    emitted_at: datetime
    recorded_at: datetime
    observation_id: UUID
    measurement: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ClaimRead:
    """One occurrence anchored to the entry's authoritative source UUID."""

    id: UUID
    seq: int
    entry_id: UUID
    entity_type: str
    project_key: str
    claim_key: str
    statement: str
    fact_name: str
    definition_version: int
    target: str
    expected: Mapping[str, object]
    expected_resolved: Mapping[str, object]
    validity_seconds: int
    provenance: str
    declared_by: str
    declared_at: datetime
    recorded_at: datetime
    retired_at: datetime | None
    replaces_id: UUID | None
    latest: VerdictRead | None
    conclusive: VerdictRead | None


@dataclass(frozen=True, slots=True)
class ClaimState:
    """A current state tied to one supplied UTC instant, without ledger writes."""

    claim: ClaimRead
    status: ClaimStatus
    as_of: datetime
    valid_until: datetime | None
    age_seconds: float | None
    latest: VerdictRead | None
    conclusive: VerdictRead | None
    previous_conclusive_kind: str | None
    retired_at: datetime | None


def evaluate_claim(claim: ClaimRead, now: datetime) -> ClaimState:
    """Use observation time for freshness, while preserving a newer failed attempt."""
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("now must be timezone-aware UTC")
    now = now.astimezone(UTC)
    conclusive = claim.conclusive
    valid_until = (
        conclusive.emitted_at + timedelta(seconds=claim.validity_seconds)
        if conclusive is not None
        else None
    )
    if conclusive is None:
        status: ClaimStatus = "unreadable" if claim.latest is not None else "unverified"
        age = None
    else:
        status = (
            "stale"
            if valid_until is not None and now >= valid_until
            else ("holds" if conclusive.verdict == "holds" else "falsified")
        )
        age = max(0.0, (now - conclusive.emitted_at).total_seconds())
    return ClaimState(
        claim=claim,
        status=status,
        as_of=now,
        valid_until=valid_until,
        age_seconds=age,
        latest=claim.latest,
        conclusive=conclusive,
        previous_conclusive_kind=conclusive.verdict if conclusive is not None else None,
        retired_at=claim.retired_at,
    )
