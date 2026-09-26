"""Periodic at-least-once projector from PostgreSQL graph facts to Neo4j."""

from __future__ import annotations

import asyncio
from math import ceil
from typing import Any
from uuid import uuid4

import structlog

from brain_v42.services.neo4j_graph_projection_writer import ProjectionOutcome

logger = structlog.get_logger(__name__)


class GraphOutboxProjector:
    """Lease pending relation events and replay them idempotently."""

    def __init__(
        self,
        repo: Any,
        graph: Any,
        *,
        interval_seconds: float = 5.0,
        batch_size: int = 100,
        max_attempts: int = 10,
        lease_seconds: int = 30,
    ) -> None:
        self._repo = repo
        self._graph = graph
        self._interval = interval_seconds
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_seconds = max(1, lease_seconds, ceil(interval_seconds * 2))
        self._worker_id = f"graph-projector-{uuid4()}"
        self._task: asyncio.Task[None] | None = None
        self._leadership: Any | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info("graph_outbox_projector.started")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        leadership = self._leadership
        self._leadership = None
        if leadership is not None:
            await self._repo.release_leadership(leadership)
        logger.info("graph_outbox_projector.stopped")

    async def _run_loop(self) -> None:
        while True:
            try:
                await self._project_batch()
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("graph_outbox_projector.batch_failed")
                await asyncio.sleep(self._interval)

    async def _project_batch(self) -> None:
        leadership = await self._repo.acquire_leadership(
            self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if leadership is None:
            self._leadership = None
            return
        self._leadership = leadership
        release_after_batch = False
        try:
            activation = await self._graph.activate_generation(leadership)
            if not activation.accepted:
                # 'crash between Neo4j activation and PG arm' hole (decision
                # 3d3d72e4 / ticket 416266ec): a predecessor activated this exact
                # generation in Neo4j and then died before confirming the arm in
                # PostgreSQL. We only ever hold `leadership` because PostgreSQL's
                # own row-level CAS already proved the predecessor's PG lease had
                # expired, so once Neo4j's own rejection response independently
                # confirms it is durably at exactly this generation (neither
                # ahead nor behind), a single bounded advance is no riskier than
                # the ordinary armed handover. If PostgreSQL was already armed
                # here, a conflicting live owner cannot be ruled out this way:
                # refuse and fall through to recovery, unchanged.
                #
                # Equal generations are not, by themselves, proof of that exact
                # story (independent review of PR #230): a PostgreSQL restore
                # can resurrect this same unarmed shape at a generation Neo4j
                # actually armed and used for real deliveries before this
                # process ever started (the runbook's own admitted residual --
                # "same generation does not mean same content"). A standalone,
                # unlocked ``has_cursor_evidence`` read cannot close that hole
                # by itself (second independent review of PR #230): a
                # predecessor's in-flight write can commit between that read
                # and the advance below, landing under a fence this process
                # believes it just claimed unopposed. Use it here only as a
                # cheap early exit -- skip the PostgreSQL CAS entirely when
                # refusal is already certain -- and let the real authority be
                # ``require_no_prior_cursor=True`` on the retry activation
                # below, which re-checks the exact same evidence inside the
                # SAME locked Neo4j transaction that performs the advance, so
                # an in-flight predecessor write and this advance always
                # serialize on the fence's own write lock.
                if (
                    not leadership.armed
                    and activation.current_generation == leadership.generation
                    and not await self._graph.has_cursor_evidence(leadership.generation)
                ):
                    advanced = await self._repo.advance_confirmed_generation(
                        leadership,
                        lease_seconds=self._lease_seconds,
                    )
                    if advanced is not None:
                        leadership = advanced
                        self._leadership = leadership
                        activation = await self._graph.activate_generation(
                            leadership,
                            require_no_prior_cursor=True,
                        )
            if not activation.accepted:
                release_after_batch = True
                reason = (
                    "neo4j_fence_ahead"
                    if activation.current_generation > leadership.generation
                    else "owner_conflict_or_incomplete_arm"
                )
                logger.error(
                    "graph_outbox_projector.fence_rejected",
                    reason=reason,
                    postgres_generation=leadership.generation,
                    neo4j_generation=activation.current_generation,
                )
                return
            if not leadership.armed and not await self._repo.arm_leadership(leadership):
                release_after_batch = True
                return
            claims = await self._repo.claim_pending(
                leadership,
                limit=self._batch_size,
                lease_seconds=self._lease_seconds,
                max_attempts=self._max_attempts,
            )
            for claim in claims:
                renewed = await self._repo.renew_claim(
                    claim,
                    lease_seconds=self._lease_seconds,
                )
                if renewed is None:
                    release_after_batch = True
                    return
                try:
                    outcome = await self._graph.apply(renewed)
                except Exception:
                    outcome = ProjectionOutcome.ERROR

                if outcome is ProjectionOutcome.STALE_GENERATION:
                    release_after_batch = True
                    return
                if outcome is ProjectionOutcome.CONFLICT:
                    logger.error(
                        "graph_outbox_projector.history_conflict",
                        aggregate_revision=renewed.event.aggregate_revision,
                    )
                    return
                if outcome in {
                    ProjectionOutcome.APPLIED,
                    ProjectionOutcome.ALREADY_CURRENT,
                    ProjectionOutcome.SUPERSEDED,
                }:
                    if not await self._repo.mark_delivered(renewed):
                        release_after_batch = True
                        return
                    continue
                error_code = self._error_code(outcome)
                if not await self._repo.mark_failed(
                    renewed,
                    error_code,
                    max_attempts=self._max_attempts,
                ):
                    release_after_batch = True
                    return
        finally:
            if release_after_batch:
                await self._repo.release_leadership(leadership)
                if self._leadership is leadership:
                    self._leadership = None

    @staticmethod
    def _error_code(outcome: ProjectionOutcome) -> str:
        if outcome is ProjectionOutcome.MISSING_NODE:
            return "missing_node"
        if outcome in {ProjectionOutcome.CONFLICT, ProjectionOutcome.INVALID_EVENT}:
            return "invalid_event"
        return "projection_failed"
