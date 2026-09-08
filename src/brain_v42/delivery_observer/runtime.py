"""Independent, restartable GitHub observation; no execution agent is launched."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.github import GitHubClient
from brain_v42.delivery_observer.ownership import ObserverOwnership, ObserverOwnershipLost
from brain_v42.delivery_observer.transport import ProviderError
from brain_v42.models.delivery import (
    SAFE_OBSERVATION_ERROR_CODES,
    DeliveryError,
    PullRequestEvidence,
    RepositoryContextEvidence,
)
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_delivery_queue import ObservationJob, PgDeliveryQueue


class ObservationRunResult(BaseModel):
    """Counts describe jobs attempted in this bounded pass, never provider bodies."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    collected: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    deferred: int = Field(default=0, ge=0)
    last_success_at: datetime | None = None
    max_lag_seconds: float = Field(default=0, ge=0)
    exit_code: Literal[0, 1, 2] = 0  # success / provider failures / ownership conflict or loss


@dataclass(frozen=True, slots=True)
class _Outcome:
    status: Literal["collected", "failed", "deferred"]
    finished_at: datetime | None = None


class DeliveryObserverRuntime:
    def __init__(
        self,
        *,
        settings: DeliverySettings,
        owner: ObserverOwnership,
        client: GitHubClient,
        evidence_repository: PgDeliveryEvidenceRepo,
        queue: PgDeliveryQueue | None = None,
    ) -> None:
        self.settings = DeliverySettings.model_validate(settings.model_dump())
        self.owner, self.client = owner, client
        self.evidence_repository = evidence_repository
        self.queue = queue or PgDeliveryQueue()
        self._run_lock = asyncio.Lock()

    async def run_once(self, project_key: str | None = None) -> ObservationRunResult:
        """Acquire for one pass unless run() already owns the connection."""
        async with self._run_lock:
            if not self.settings.enabled:
                return ObservationRunResult()
            release = not self.owner.owned
            try:
                if release and not await self.owner.acquire():
                    return ObservationRunResult(exit_code=2)
                return await self._cycle(asyncio.Event(), project_key=project_key)
            except ObserverOwnershipLost:
                return ObservationRunResult(exit_code=2)
            finally:
                if release:
                    await self.owner.release()

    async def run(self, stop_event: asyncio.Event) -> int:
        """Poll persisted work until stopped; provider outages remain retryable."""
        if not self.settings.enabled or stop_event.is_set():
            return 0
        try:
            if not await self.owner.acquire():
                return 2
            while not stop_event.is_set():
                async with self._run_lock:
                    result = await self._cycle(stop_event)
                if result.exit_code == 2:
                    return 2
                deadline = asyncio.get_running_loop().time() + self.settings.poll_seconds
                while not stop_event.is_set() and asyncio.get_running_loop().time() < deadline:
                    delay = min(1.0, max(0.0, deadline - asyncio.get_running_loop().time()))
                    try:
                        await asyncio.wait_for(stop_event.wait(), delay)
                    except TimeoutError:
                        await self.owner.heartbeat()
            return 0
        except ObserverOwnershipLost:
            return 2
        finally:
            await self.owner.release()

    async def _watch_ownership(self) -> None:
        try:
            while True:
                await asyncio.sleep(1)
                await self.owner.heartbeat()
        except ObserverOwnershipLost:
            return  # The owner's lost event wakes the in-flight batch immediately.

    async def _batch(
        self, jobs: tuple[ObservationJob, ...], stop_event: asyncio.Event
    ) -> tuple[_Outcome, ...] | None:
        tasks = tuple(asyncio.create_task(self._observe(job)) for job in jobs)
        work = asyncio.gather(*tasks)
        stopping = asyncio.create_task(stop_event.wait())
        lost = asyncio.create_task(self.owner.lost.wait())
        try:
            done, _ = await asyncio.wait(
                (work, stopping, lost), return_when=asyncio.FIRST_COMPLETED
            )
            if work in done:
                return tuple(work.result())
            return None
        finally:
            for task in (stopping, lost):
                if not task.done():
                    task.cancel()
            for child in tasks:
                if not child.done():
                    child.cancel()
            if not work.done():
                work.cancel()
            await asyncio.gather(stopping, lost, work, *tasks, return_exceptions=True)

    async def _cycle(
        self, stop_event: asyncio.Event, *, project_key: str | None = None
    ) -> ObservationRunResult:
        collected = failed = deferred = 0
        latest: datetime | None = None
        lag = 0.0
        seen: set[str] = set()
        watcher = asyncio.create_task(self._watch_ownership())
        try:
            # Bound a one-shot pass under continuously arriving work. Older due
            # rows stay ahead of completed/rescheduled rows on the next pass.
            while len(seen) < 100 and not stop_event.is_set():
                async with self.owner.transaction() as session:
                    jobs = await self.queue.due(
                        session,
                        project_key=project_key,
                        limit=min(2, 100 - len(seen)),
                        exclude=seen,
                    )
                if not jobs:
                    break
                seen.update(job.identity for job in jobs)
                lag = max(
                    lag, *(max(0.0, (job.captured_at - job.due_at).total_seconds()) for job in jobs)
                )
                outcomes = await self._batch(jobs, stop_event)
                if outcomes is None:
                    deferred += len(jobs)
                    break
                for outcome in outcomes:
                    if outcome.status == "collected":
                        collected += 1
                        if outcome.finished_at is not None:
                            latest = (
                                max(latest, outcome.finished_at) if latest else outcome.finished_at
                            )
                    elif outcome.status == "failed":
                        failed += 1
                    else:
                        deferred += 1
        except ObserverOwnershipLost:
            return ObservationRunResult(
                collected=collected,
                failed=failed,
                deferred=deferred,
                last_success_at=latest,
                max_lag_seconds=lag,
                exit_code=2,
            )
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
        exit_code: Literal[0, 1, 2] = 2 if self.owner.lost.is_set() else 1 if failed else 0
        return ObservationRunResult(
            collected=collected,
            failed=failed,
            deferred=deferred,
            last_success_at=latest,
            max_lag_seconds=lag,
            exit_code=exit_code,
        )

    async def _observe(self, job: ObservationJob) -> _Outcome:
        started_at = datetime.now(UTC)
        evidence: PullRequestEvidence | RepositoryContextEvidence | None = None
        code: str | None = None
        retry = 0.0
        try:
            if job.binding is not None:
                evidence = await self.client.collect(
                    job.binding, job.contract, previous=job.previous
                )
            else:
                evidence = await self.client.collect_repository_context(job.contract)
                if not evidence.complete:
                    raise ProviderError("provider_not_found")
        except ProviderError as error:
            # Adapter-specific revision failures are deliberately mapped into the
            # shared persistence allowlist; every failed attempt remains visible.
            code = (
                error.code
                if error.code in SAFE_OBSERVATION_ERROR_CODES
                else "provider_invalid_response"
            )
            retry = max(error.retry_after_seconds, min(300.0, 5.0 * 2 ** min(job.failure_count, 6)))
        except Exception:
            code, retry = "provider_unavailable", min(300.0, 5.0 * 2 ** min(job.failure_count, 6))
        finished_at = max(started_at, datetime.now(UTC))
        try:
            async with self.owner.transaction() as session:
                repo = self.evidence_repository
                if job.binding is not None:
                    if code is None:
                        assert isinstance(evidence, PullRequestEvidence)
                        await repo.publish_observation(
                            session, job.binding.id, job.version, evidence, started_at, finished_at
                        )
                    else:
                        await repo.record_observation_error(
                            session, job.binding.id, job.version, code, started_at, finished_at
                        )
                elif code is None:
                    assert isinstance(evidence, RepositoryContextEvidence)
                    await repo.publish_repository_context(
                        session,
                        job.ticket_id,
                        job.contract.contract_revision,
                        job.attempt,
                        job.context_set_digest,
                        job.version,
                        evidence,
                        started_at,
                        finished_at,
                    )
                else:
                    await repo.record_repository_context_error(
                        session,
                        job.ticket_id,
                        job.contract.contract_revision,
                        job.attempt,
                        job.context_set_digest,
                        job.version,
                        code,
                        started_at,
                        finished_at,
                    )
                await self.queue.schedule_after(
                    session,
                    job,
                    expected_version=job.version + 1,
                    delay_seconds=min(86400, max(self.settings.poll_seconds, retry)),
                )
            return _Outcome(
                "collected" if code is None else "failed", finished_at if code is None else None
            )
        except DeliveryError as error:
            if error.code in {
                "binding_conflict",
                "binding_superseded",
                "repository_context_conflict",
                "repository_context_superseded",
            }:
                return _Outcome("deferred")
            raise
