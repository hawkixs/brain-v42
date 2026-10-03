"""Independent, restartable GitHub observation; no execution agent is launched."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.github import GitHubClient, ReleaseTag
from brain_v42.delivery_observer.ownership import ObserverOwnership, ObserverOwnershipLost
from brain_v42.delivery_observer.transport import ProviderError
from brain_v42.facts.model import ReleaseIdentity
from brain_v42.models.delivery import (
    SAFE_OBSERVATION_ERROR_CODES,
    DeliveryError,
    PullRequestEvidence,
    RepositoryContextEvidence,
)
from brain_v42.models.ticket import release_key
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_delivery_queue import ObservationJob, PgDeliveryQueue
from brain_v42.repositories.pg_release_derivation import (
    BRAIN_V42_REPOSITORY_ID,
    PgReleaseDerivationRepo,
    ReleaseCandidate,
)


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


_MAX_DECODED_JOBS_PER_PASS = 100
_MAX_UNDECODABLE_ROWS_PER_PASS = 1000
_MAX_RELEASE_COMPARES_PER_PASS = 10


class _UndecodableScanLimitReached(Exception):
    pass


def _diagnose(
    subject: str, code: str, *, provider_code: str | None, diagnostic: str | None
) -> None:
    """One JSON line on stderr per failed observation.

    stdout is the process's result protocol and the journal held nothing per
    observation: on 2026-09-19 a binding failed every minute for an hour with
    only ``provider_invalid_response`` to read (ticket 731ab364). The line
    carries the subject and codes, never a provider payload.
    """
    line = {
        "event": "observation_error",
        "subject": subject,
        "error_code": code,
        "provider_code": provider_code,
        "diagnostic": diagnostic,
    }
    # A diagnostic must never break the observation it describes (the
    # client-activity emitter had that defect, ticket 1c40c36a): a closed or
    # broken stderr is the journal's problem, not the attempt's. The write is
    # synchronous, like the stdout protocol of this process; the caller runs
    # it only once the attempt is persisted.
    with suppress(OSError, ValueError):
        sys.stderr.write(json.dumps(line) + "\n")
        sys.stderr.flush()


class DeliveryObserverRuntime:
    def __init__(
        self,
        *,
        settings: DeliverySettings,
        owner: ObserverOwnership,
        client: GitHubClient,
        evidence_repository: PgDeliveryEvidenceRepo,
        queue: PgDeliveryQueue | None = None,
        releases: PgReleaseDerivationRepo | None = None,
        release_identity: Callable[[], ReleaseIdentity | None] = lambda: None,
    ) -> None:
        self.settings = DeliverySettings.model_validate(settings.model_dump())
        self.owner, self.client = owner, client
        self.evidence_repository = evidence_repository
        self.queue = queue or PgDeliveryQueue()
        self.releases = releases
        self.release_identity = release_identity
        self._commit_dates: dict[str, datetime] = {}
        self._not_contained: set[tuple[str, str]] = set()
        self._blocked_keys: set[tuple[UUID, str, str]] = set()
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
        undecodable_seen: set[str] = set()
        decoded = 0
        undecodable: list[tuple[str, str]] = []
        watcher = asyncio.create_task(self._watch_ownership())
        try:
            # Bound a one-shot pass under continuously arriving work. Older due
            # rows stay ahead of completed/rescheduled rows on the next pass.
            while decoded < _MAX_DECODED_JOBS_PER_PASS and not stop_event.is_set():
                undecodable.clear()

                def report_undecodable(identity: str, error_type: str) -> None:
                    if len(undecodable_seen) >= _MAX_UNDECODABLE_ROWS_PER_PASS:
                        raise _UndecodableScanLimitReached
                    undecodable.append((identity, error_type))

                async with self.owner.transaction() as session:
                    try:
                        jobs = await self.queue.due(
                            session,
                            project_key=project_key,
                            limit=min(2, _MAX_DECODED_JOBS_PER_PASS - decoded),
                            exclude=seen,
                            on_undecodable=report_undecodable,
                        )
                    except _UndecodableScanLimitReached:
                        jobs = ()
                for identity, error_type in undecodable:
                    # The row stays due: excluding it keeps this pass moving past it.
                    seen.add(identity)
                    undecodable_seen.add(identity)
                    failed += 1
                    _diagnose(
                        identity,
                        "observer_undecodable_row",
                        provider_code=None,
                        diagnostic=error_type,
                    )
                if len(undecodable_seen) >= _MAX_UNDECODABLE_ROWS_PER_PASS:
                    _diagnose(
                        "delivery_observer",
                        "observer_undecodable_scan_limit",
                        provider_code=None,
                        diagnostic=None,
                    )
                    break
                if not jobs:
                    if undecodable:
                        continue
                    break
                seen.update(job.identity for job in jobs)
                decoded += len(jobs)
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
            if not stop_event.is_set() and self.owner.owned and not self.owner.lost.is_set():
                compares = await self._release_pass()
                await self._deployed_pass(compares)
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

    async def _tag_date(self, repository_id: int, tag: ReleaseTag) -> datetime:
        return await self._tag_date_of_sha(repository_id, tag.sha)

    async def _tag_date_of_sha(self, repository_id: int, sha: str) -> datetime:
        if sha not in self._commit_dates:
            self._commit_dates[sha] = await self.client.commit_date(repository_id, sha)
        return self._commit_dates[sha]

    async def _derivation_candidates(
        self, *, repository_id: int | None = None, live_release_sha: str = ""
    ) -> AsyncIterator[ReleaseCandidate]:
        """Page past memoized candidates without letting a SQL limit starve later work."""
        if self.releases is None:
            return
        repositories = {
            rid for repos in self.settings.repository_registry.values() for rid in repos
        }
        seen: set[tuple[UUID, str]] = set()
        while not self.owner.lost.is_set():
            async with self.owner.transaction() as session:
                if repository_id is None:
                    candidates = await self.releases.unreleased(
                        session,
                        repository_ids=repositories,
                        limit=_MAX_RELEASE_COMPARES_PER_PASS,
                        exclude=seen,
                    )
                else:
                    candidates = await self.releases.undeployed(
                        session,
                        repository_id=repository_id,
                        live_release_sha=live_release_sha,
                        limit=_MAX_RELEASE_COMPARES_PER_PASS,
                        exclude=seen,
                    )
            for candidate in candidates:
                seen.add((candidate.ticket_id, candidate.deliverable_key))
                yield candidate
            if len(candidates) < _MAX_RELEASE_COMPARES_PER_PASS:
                return

    async def _release_pass(self) -> int:
        """Bound containment checks while sharing the transport's admission budget."""
        if self.releases is None:
            return 0
        compares = 0
        tags_by_repo: dict[int, list[tuple[ReleaseTag, datetime]]] = {}
        async for candidate in self._derivation_candidates():
            if self.owner.lost.is_set():
                break
            blocked_key: tuple[UUID, str, str] | None = None
            try:
                if candidate.repository_id not in tags_by_repo:
                    # A failed list/date fetch stays empty for this pass, then retries next cycle.
                    tags_by_repo[candidate.repository_id] = []
                    tags = await self.client.release_tags(candidate.repository_id)
                    dated = [
                        (tag, await self._tag_date(candidate.repository_id, tag)) for tag in tags
                    ]
                    tags_by_repo[candidate.repository_id] = sorted(
                        dated, key=lambda item: (item[1], release_key(item[0].version))
                    )
                dated = tags_by_repo[candidate.repository_id]
                if not dated:
                    continue
                merged_at = await self._tag_date_of_sha(
                    candidate.repository_id, candidate.integration_sha
                )
                for tag, tag_date in dated:
                    if self.owner.lost.is_set():
                        return compares
                    if tag_date < merged_at:
                        continue
                    key = f"released:{candidate.ticket_id}:{candidate.deliverable_key}:{tag.name}"
                    blocked_key = (candidate.ticket_id, candidate.deliverable_key, key)
                    pair = (candidate.integration_sha, tag.sha)
                    if blocked_key in self._blocked_keys:
                        # A conflict on the first containing tag must never claim a later release.
                        break
                    if pair in self._not_contained:
                        continue
                    if compares >= _MAX_RELEASE_COMPARES_PER_PASS:
                        return compares
                    compares += 1
                    if await self.client.contains(
                        candidate.repository_id, candidate.integration_sha, tag.sha
                    ):
                        async with self.owner.transaction() as session:
                            await self.releases.record_released(session, candidate, tag, tag_date)
                        break
                    self._not_contained.add(pair)
            except ProviderError as error:
                _diagnose(
                    f"release:{candidate.ticket_id}",
                    error.code,
                    provider_code=error.code,
                    diagnostic=error.diagnostic,
                )
            except DeliveryError as error:
                if error.code == "idempotency_key_reused" and blocked_key is not None:
                    self._blocked_keys.add(blocked_key)
                _diagnose(
                    f"release:{candidate.ticket_id}",
                    error.code,
                    provider_code=None,
                    diagnostic=type(error).__name__,
                )
        return compares

    async def _deployed_pass(self, compares: int) -> None:
        """Claim deployment only from this process's immutable brain-v42 release."""
        if (
            self.releases is None
            or compares >= _MAX_RELEASE_COMPARES_PER_PASS
            or self.owner.lost.is_set()
        ):
            return
        identity = self.release_identity()
        if identity is None:
            return
        repository_id = BRAIN_V42_REPOSITORY_ID
        if repository_id not in self.settings.repositories_for("brain-v42"):
            return
        async for candidate in self._derivation_candidates(
            repository_id=repository_id, live_release_sha=identity.release_sha
        ):
            if self.owner.lost.is_set():
                break
            key = (
                f"deployed:{candidate.ticket_id}:{candidate.deliverable_key}:{identity.release_sha}"
            )
            blocked_key = (candidate.ticket_id, candidate.deliverable_key, key)
            pair = (candidate.integration_sha, identity.release_sha)
            if blocked_key in self._blocked_keys or pair in self._not_contained:
                continue
            if compares >= _MAX_RELEASE_COMPARES_PER_PASS:
                return
            compares += 1
            try:
                if await self.client.contains(
                    candidate.repository_id, candidate.integration_sha, identity.release_sha
                ):
                    async with self.owner.transaction() as session:
                        await self.releases.record_deployed(session, candidate, identity)
                else:
                    self._not_contained.add(pair)
            except ProviderError as error:
                _diagnose(
                    f"deployed:{candidate.ticket_id}",
                    error.code,
                    provider_code=error.code,
                    diagnostic=error.diagnostic,
                )
            except DeliveryError as error:
                if error.code == "idempotency_key_reused":
                    self._blocked_keys.add(blocked_key)
                _diagnose(
                    f"deployed:{candidate.ticket_id}",
                    error.code,
                    provider_code=None,
                    diagnostic=type(error).__name__,
                )

    async def _observe(self, job: ObservationJob) -> _Outcome:
        started_at = datetime.now(UTC)
        evidence: PullRequestEvidence | RepositoryContextEvidence | None = None
        code: str | None = None
        retry = 0.0
        provider_code: str | None = None
        diagnostic: str | None = None
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
            provider_code, diagnostic = error.code, error.diagnostic
        except Exception as error:
            code, retry = "provider_unavailable", min(300.0, 5.0 * 2 ** min(job.failure_count, 6))
            diagnostic = type(error).__name__
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
            if code is not None:
                # After the attempt is persisted and rescheduled: stderr is
                # synchronous, and a journal that stalls may delay only what
                # comes next, never the record of this attempt.
                _diagnose(job.identity, code, provider_code=provider_code, diagnostic=diagnostic)
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
            # Any other domain error is confined to this one job: re-raising
            # would end the process, and the oldest due job being retried
            # first, every restart would die on the same one (ticket b78b5144).
            # The code is internal and never persisted; the message is dropped.
            _diagnose(
                job.identity,
                "observer_persist_error",
                provider_code=None,
                diagnostic=type(error).__name__,
            )
            return _Outcome("failed")
