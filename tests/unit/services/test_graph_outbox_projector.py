"""Contracts for fenced PostgreSQL-to-Neo4j projection orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from brain_v42.services.graph_outbox_projector import GraphOutboxProjector
from brain_v42.services.neo4j_graph_projection_writer import (
    ProjectionActivation,
    ProjectionOutcome,
)


@dataclass(frozen=True)
class _OutboxEvent:
    event_id: UUID
    operation: str = "upsert_relation"
    aggregate_revision: int = 1


@dataclass(frozen=True)
class _ProjectionLeadership:
    owner_id: str
    generation: int
    lease_until: float
    armed: bool = False


@dataclass(frozen=True)
class _ProjectionClaim:
    event: _OutboxEvent
    owner_id: str
    lease_generation: int
    claim_version: int
    leased_until: float


class _FencedOutboxRepo:
    def __init__(self, trace: list[tuple[Any, ...]]) -> None:
        self.trace = trace
        self.leadership: _ProjectionLeadership | None = None
        self.claims: list[_ProjectionClaim] = []
        self.renew_results: list[_ProjectionClaim | None] = []
        self.arm_result = True
        self.ack_result = True
        self.fail_result = True
        self.delivered: list[_ProjectionClaim] = []
        self.failed: list[tuple[_ProjectionClaim, str, int]] = []
        self.acquired = asyncio.Event()

    async def acquire_leadership(
        self,
        owner_id: str,
        *,
        lease_seconds: int,
    ) -> _ProjectionLeadership | None:
        self.trace.append(("repo.acquire_leadership", owner_id, lease_seconds))
        self.acquired.set()
        return self.leadership

    async def arm_leadership(self, leadership: _ProjectionLeadership) -> bool:
        self.trace.append(("repo.arm_leadership", leadership))
        return self.arm_result

    async def claim_pending(
        self,
        leadership: _ProjectionLeadership,
        *,
        limit: int,
        lease_seconds: int,
        max_attempts: int,
    ) -> list[_ProjectionClaim]:
        self.trace.append(("repo.claim_pending", leadership, limit, lease_seconds, max_attempts))
        return self.claims[:limit]

    async def renew_claim(
        self,
        claim: _ProjectionClaim,
        *,
        lease_seconds: int,
    ) -> _ProjectionClaim | None:
        self.trace.append(("repo.renew_claim", claim, lease_seconds))
        return self.renew_results.pop(0) if self.renew_results else None

    async def mark_delivered(self, claim: _ProjectionClaim) -> bool:
        self.trace.append(("repo.mark_delivered", claim))
        self.delivered.append(claim)
        return self.ack_result

    async def mark_failed(
        self,
        claim: _ProjectionClaim,
        error_code: str,
        *,
        max_attempts: int,
    ) -> bool:
        self.trace.append(("repo.mark_failed", claim, error_code, max_attempts))
        self.failed.append((claim, error_code, max_attempts))
        return self.fail_result

    async def release_leadership(self, leadership: _ProjectionLeadership) -> bool:
        self.trace.append(("repo.release_leadership", leadership))
        return True


class _FencedWriter:
    def __init__(self, trace: list[tuple[Any, ...]]) -> None:
        self.trace = trace
        self.activation_results: list[ProjectionActivation] = []
        self.apply_outcomes: list[ProjectionOutcome] = []

    async def activate_generation(
        self,
        leadership: _ProjectionLeadership,
    ) -> ProjectionActivation:
        self.trace.append(("writer.activate_generation", leadership))
        if self.activation_results:
            return self.activation_results.pop(0)
        return ProjectionActivation(True, leadership.generation)

    async def apply(self, claim: _ProjectionClaim) -> ProjectionOutcome:
        self.trace.append(("writer.apply", claim))
        if self.apply_outcomes:
            return self.apply_outcomes.pop(0)
        return ProjectionOutcome.APPLIED


@dataclass
class _Subject:
    projector: GraphOutboxProjector
    repo: _FencedOutboxRepo
    writer: _FencedWriter
    trace: list[tuple[Any, ...]]
    leadership: _ProjectionLeadership
    claims: list[_ProjectionClaim]
    renewed: list[_ProjectionClaim]


def _subject(*, event_count: int = 1) -> _Subject:
    trace: list[tuple[Any, ...]] = []
    repo = _FencedOutboxRepo(trace)
    writer = _FencedWriter(trace)
    projector = GraphOutboxProjector(
        repo,
        writer,
        batch_size=max(event_count, 2),
        max_attempts=3,
        lease_seconds=11,
    )
    leadership = _ProjectionLeadership(projector._worker_id, 41, 100.0)
    claims = [
        _ProjectionClaim(
            event=_OutboxEvent(uuid4()),
            owner_id=projector._worker_id,
            lease_generation=leadership.generation,
            claim_version=index * 2 + 1,
            leased_until=110.0,
        )
        for index in range(event_count)
    ]
    renewed = [
        _ProjectionClaim(
            event=claim.event,
            owner_id=claim.owner_id,
            lease_generation=claim.lease_generation,
            claim_version=claim.claim_version + 1,
            leased_until=120.0,
        )
        for claim in claims
    ]
    repo.leadership = leadership
    repo.claims = claims
    repo.renew_results = list(renewed)
    return _Subject(projector, repo, writer, trace, leadership, claims, renewed)


def _armed_batch_prefix(subject: _Subject) -> list[tuple[Any, ...]]:
    return [
        ("repo.acquire_leadership", subject.projector._worker_id, 11),
        ("writer.activate_generation", subject.leadership),
        ("repo.arm_leadership", subject.leadership),
        ("repo.claim_pending", subject.leadership, 2, 11, 3),
    ]


def test_leadership_lease_covers_two_poll_intervals() -> None:
    trace: list[tuple[Any, ...]] = []

    projector = GraphOutboxProjector(
        _FencedOutboxRepo(trace),
        _FencedWriter(trace),
        interval_seconds=60.0,
        lease_seconds=30,
    )

    assert projector._lease_seconds == 120


@pytest.mark.asyncio
async def test_fenced_batch_arms_generation_renews_claim_then_cas_acks() -> None:
    subject = _subject()

    await subject.projector._project_batch()

    assert subject.trace == [
        *_armed_batch_prefix(subject),
        ("repo.renew_claim", subject.claims[0], 11),
        ("writer.apply", subject.renewed[0]),
        ("repo.mark_delivered", subject.renewed[0]),
    ]
    assert subject.repo.delivered == [subject.renewed[0]]
    assert subject.repo.failed == []


@pytest.mark.asyncio
async def test_successful_batch_retains_generation_until_projector_stop() -> None:
    subject = _subject()

    await subject.projector._project_batch()

    assert ("repo.release_leadership", subject.leadership) not in subject.trace

    await subject.projector.stop()

    assert subject.trace[-1] == ("repo.release_leadership", subject.leadership)


@pytest.mark.asyncio
async def test_already_armed_generation_is_revalidated_without_rearming() -> None:
    subject = _subject(event_count=0)
    armed_leadership = _ProjectionLeadership(
        subject.projector._worker_id,
        subject.leadership.generation,
        subject.leadership.lease_until,
        armed=True,
    )
    subject.repo.leadership = armed_leadership

    await subject.projector._project_batch()

    assert subject.trace == [
        ("repo.acquire_leadership", subject.projector._worker_id, 11),
        ("writer.activate_generation", armed_leadership),
        ("repo.claim_pending", armed_leadership, 2, 11, 3),
    ]


@pytest.mark.parametrize(
    "outcome",
    [ProjectionOutcome.ALREADY_CURRENT, ProjectionOutcome.SUPERSEDED],
)
@pytest.mark.asyncio
async def test_fenced_idempotent_outcomes_are_cas_acked(
    outcome: ProjectionOutcome,
) -> None:
    subject = _subject()
    subject.writer.apply_outcomes = [outcome]

    await subject.projector._project_batch()

    assert subject.trace[-3:] == [
        ("repo.renew_claim", subject.claims[0], 11),
        ("writer.apply", subject.renewed[0]),
        ("repo.mark_delivered", subject.renewed[0]),
    ]
    assert subject.repo.delivered == [subject.renewed[0]]
    assert subject.repo.failed == []


@pytest.mark.asyncio
async def test_fenced_retryable_outcome_is_cas_failed() -> None:
    subject = _subject()
    subject.writer.apply_outcomes = [ProjectionOutcome.MISSING_NODE]

    await subject.projector._project_batch()

    assert subject.trace == [
        *_armed_batch_prefix(subject),
        ("repo.renew_claim", subject.claims[0], 11),
        ("writer.apply", subject.renewed[0]),
        ("repo.mark_failed", subject.renewed[0], "missing_node", 3),
    ]
    assert subject.repo.delivered == []
    assert subject.repo.failed == [(subject.renewed[0], "missing_node", 3)]


@pytest.mark.asyncio
async def test_fenced_batch_without_leadership_does_not_touch_neo_or_claim() -> None:
    subject = _subject()
    subject.repo.leadership = None

    await subject.projector._project_batch()

    assert subject.trace == [("repo.acquire_leadership", subject.projector._worker_id, 11)]


@pytest.mark.asyncio
async def test_fenced_batch_blocks_when_neo_generation_is_ahead() -> None:
    subject = _subject()
    subject.writer.activation_results = [
        ProjectionActivation(False, subject.leadership.generation + 4)
    ]

    await subject.projector._project_batch()

    assert subject.trace == [
        ("repo.acquire_leadership", subject.projector._worker_id, 11),
        ("writer.activate_generation", subject.leadership),
        ("repo.release_leadership", subject.leadership),
    ]


@pytest.mark.asyncio
async def test_fenced_batch_blocks_same_generation_owner_conflict() -> None:
    subject = _subject()
    subject.writer.activation_results = [
        ProjectionActivation(False, subject.leadership.generation),
    ]

    await subject.projector._project_batch()

    assert subject.trace == [
        ("repo.acquire_leadership", subject.projector._worker_id, 11),
        ("writer.activate_generation", subject.leadership),
        ("repo.release_leadership", subject.leadership),
    ]


@pytest.mark.asyncio
async def test_fenced_batch_stops_when_postgres_cannot_arm_generation() -> None:
    subject = _subject()
    subject.repo.arm_result = False

    await subject.projector._project_batch()

    assert subject.trace == [
        ("repo.acquire_leadership", subject.projector._worker_id, 11),
        ("writer.activate_generation", subject.leadership),
        ("repo.arm_leadership", subject.leadership),
        ("repo.release_leadership", subject.leadership),
    ]


@pytest.mark.asyncio
async def test_fenced_batch_stops_before_mutation_when_claim_renewal_is_stale() -> None:
    subject = _subject(event_count=2)
    subject.repo.renew_results = [None, subject.renewed[1]]

    await subject.projector._project_batch()

    assert subject.trace == [
        *_armed_batch_prefix(subject),
        ("repo.renew_claim", subject.claims[0], 11),
        ("repo.release_leadership", subject.leadership),
    ]
    assert subject.repo.delivered == []
    assert subject.repo.failed == []


@pytest.mark.asyncio
async def test_fenced_batch_stops_without_cas_on_stale_neo_generation() -> None:
    subject = _subject(event_count=2)
    subject.writer.apply_outcomes = [
        ProjectionOutcome.STALE_GENERATION,
        ProjectionOutcome.APPLIED,
    ]

    await subject.projector._project_batch()

    assert subject.trace == [
        *_armed_batch_prefix(subject),
        ("repo.renew_claim", subject.claims[0], 11),
        ("writer.apply", subject.renewed[0]),
        ("repo.release_leadership", subject.leadership),
    ]
    assert subject.repo.delivered == []
    assert subject.repo.failed == []


@pytest.mark.asyncio
async def test_fenced_batch_keeps_history_conflict_pending() -> None:
    subject = _subject(event_count=2)
    subject.writer.apply_outcomes = [
        ProjectionOutcome.CONFLICT,
        ProjectionOutcome.APPLIED,
    ]

    await subject.projector._project_batch()

    assert subject.trace == [
        *_armed_batch_prefix(subject),
        ("repo.renew_claim", subject.claims[0], 11),
        ("writer.apply", subject.renewed[0]),
    ]
    assert subject.repo.delivered == []
    assert subject.repo.failed == []


@pytest.mark.asyncio
async def test_fenced_batch_stops_when_delivery_cas_is_stale() -> None:
    subject = _subject(event_count=2)
    subject.repo.ack_result = False

    await subject.projector._project_batch()

    assert subject.trace == [
        *_armed_batch_prefix(subject),
        ("repo.renew_claim", subject.claims[0], 11),
        ("writer.apply", subject.renewed[0]),
        ("repo.mark_delivered", subject.renewed[0]),
        ("repo.release_leadership", subject.leadership),
    ]


@pytest.mark.asyncio
async def test_fenced_batch_stops_when_failure_cas_is_stale() -> None:
    subject = _subject(event_count=2)
    subject.repo.fail_result = False
    subject.writer.apply_outcomes = [ProjectionOutcome.MISSING_NODE]

    await subject.projector._project_batch()

    assert subject.trace == [
        *_armed_batch_prefix(subject),
        ("repo.renew_claim", subject.claims[0], 11),
        ("writer.apply", subject.renewed[0]),
        ("repo.mark_failed", subject.renewed[0], "missing_node", 3),
        ("repo.release_leadership", subject.leadership),
    ]


@pytest.mark.asyncio
async def test_stop_ends_periodic_leadership_attempts() -> None:
    trace: list[tuple[Any, ...]] = []
    repo = _FencedOutboxRepo(trace)
    projector = GraphOutboxProjector(
        repo,
        _FencedWriter(trace),
        interval_seconds=0.001,
    )

    await projector.start()
    await asyncio.wait_for(repo.acquired.wait(), timeout=1.0)
    await projector.stop()
    attempts_after_stop = len(trace)
    await asyncio.sleep(0.01)

    assert len(trace) == attempts_after_stop
