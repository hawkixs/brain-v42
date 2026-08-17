"""Composition and lifecycle for the independently managed automation runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brain_v42.automation.dedup import FeatureDedupJobProtocol, run_dedup_loop
from brain_v42.automation.ownership import (
    AutomationOwnershipLease,
    GitLabEventProcessor,
    OwnedGitLabIngestor,
    OwnedProjectKeyResolver,
    OwnershipLostError,
)
from brain_v42.automation.server import AutomationServer
from brain_v42.automation.webhook import GitLabWebhookEndpoint
from brain_v42.config import Settings, get_settings
from brain_v42.db.tables import project_contexts
from brain_v42.services.cluster_guard import ClusterGuard
from brain_v42.services.feature_dedup_job import FeatureDedupJob
from brain_v42.services.gitlab_ingestor import GitLabIngestor
from brain_v42.services.gpu_embedding_service import GPUEmbeddingService
from brain_v42.services.reranker_client import RerankerClient
from brain_v42.services.status_engine import StatusEngine

logger = structlog.get_logger(__name__)


class AsyncCloser(Protocol):
    async def close(self) -> None: ...


class AsyncDisposer(Protocol):
    async def dispose(self) -> None: ...


class AutomationCleanupError(RuntimeError):
    """Aggregates shutdown failures after every closer has been attempted."""

    def __init__(self, errors: list[BaseException]) -> None:
        self.errors = tuple(errors)
        detail = "; ".join(str(error) for error in errors)
        super().__init__(f"automation cleanup failed: {detail}")


@dataclass(slots=True)
class AutomationResources:
    """Typed resources owned exclusively by one automation process."""

    engine: AsyncDisposer
    session_factory: async_sessionmaker[AsyncSession]
    lease: AutomationOwnershipLease
    embedding_svc: AsyncCloser
    reranker: AsyncCloser
    dedup_job: FeatureDedupJobProtocol
    server: AutomationServer


class AutomationRuntime:
    """Own startup, loss handling and deterministic automation shutdown."""

    def __init__(self, resources: AutomationResources, *, dedup_interval: float) -> None:
        self._resources = resources
        self._dedup_interval = dedup_interval

    async def run(self, stop_event: asyncio.Event) -> int:
        dedup_task: asyncio.Task[None] | None = None
        wait_tasks: list[asyncio.Task[bool | None]] = []
        exit_code = 0
        try:
            try:
                acquired = await self._resources.lease.acquire()
            except Exception:
                logger.exception("automation_runtime.lease_acquire_failed")
                return 2
            if not acquired:
                logger.error("automation_runtime.lease_conflict")
                return 2

            try:
                await self._resources.server.start()
            except OSError:
                logger.exception("automation_runtime.bind_failed")
                return 3

            dedup_task = asyncio.create_task(
                run_dedup_loop(
                    self._resources.dedup_job,
                    self._resources.session_factory,
                    interval=self._dedup_interval,
                    ownership=self._resources.lease,
                ),
                name="automation-dedup-loop",
            )
            stop_wait = asyncio.create_task(stop_event.wait())
            loss_wait = asyncio.create_task(self._resources.lease.ownership_lost.wait())
            wait_tasks = [stop_wait, loss_wait]
            done, _pending = await asyncio.wait(
                {stop_wait, loss_wait, dedup_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if loss_wait in done and self._resources.lease.ownership_lost.is_set():
                exit_code = 4
            elif dedup_task in done:
                try:
                    dedup_task.result()
                except OwnershipLostError:
                    exit_code = 4
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("automation_runtime.scheduler_failed")
                    exit_code = 5
                else:
                    exit_code = 5
            return exit_code
        finally:
            for task in wait_tasks:
                if not task.done():
                    task.cancel()
            if wait_tasks:
                await asyncio.gather(*wait_tasks, return_exceptions=True)
            errors = await self._cleanup(dedup_task)
            if errors:
                if exit_code == 0:
                    raise AutomationCleanupError(errors)
                logger.error(
                    "automation_runtime.cleanup_failed",
                    errors=[str(error) for error in errors],
                )

    async def _cleanup(self, dedup_task: asyncio.Task[None] | None) -> list[BaseException]:
        errors: list[BaseException] = []
        if dedup_task is not None:
            if not dedup_task.done():
                dedup_task.cancel()
            try:
                await dedup_task
            except (asyncio.CancelledError, OwnershipLostError):
                pass
            except BaseException as exc:
                errors.append(exc)

        await self._attempt(self._resources.server.stop, errors)
        await self._attempt(self._resources.embedding_svc.close, errors)
        await self._attempt(self._resources.reranker.close, errors)
        await self._attempt_protected(self._resources.lease.release, errors)
        await self._attempt(self._resources.engine.dispose, errors)
        return errors

    @staticmethod
    async def _attempt(
        action: Callable[[], Awaitable[None]],
        errors: list[BaseException],
    ) -> None:
        try:
            await action()
        except BaseException as exc:
            errors.append(exc)

    @staticmethod
    async def _attempt_protected(
        action: Callable[[], Awaitable[None]],
        errors: list[BaseException],
    ) -> None:
        cleanup: asyncio.Future[None] = asyncio.ensure_future(action())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            try:
                await cleanup
            except BaseException as exc:
                errors.append(exc)
            raise
        except BaseException as exc:
            errors.append(exc)


def build_automation_runtime(
    *,
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
) -> AutomationRuntime:
    """Build the production automation composition without starting it."""
    effective_settings = settings or get_settings()
    runtime_engine = engine or create_async_engine(
        effective_settings.postgres_url,
        pool_size=20,
        max_overflow=10,
        pool_timeout=10,
        pool_recycle=1800,
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(
        runtime_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    lease = AutomationOwnershipLease(runtime_engine)
    embedding_svc = GPUEmbeddingService(base_url=effective_settings.embedding_service_url)
    reranker = RerankerClient(
        base_url=effective_settings.reranker_url,
        timeout=effective_settings.reranker_timeout,
    )
    cluster_guard = ClusterGuard(
        session_factory,
        embedding_svc,
        reranker,
        StatusEngine(),
        mutation_guard=lease.ensure_owned,
    )
    ingestor = GitLabIngestor(
        session_factory,
        embedding_svc,
        cluster_guard,
        mutation_guard=lease.ensure_owned,
    )
    dedup_job = FeatureDedupJob(
        session_factory,
        reranker,
        embedding_svc,
        mutation_guard=lease.ensure_owned,
    )

    async def resolve_project_key(gitlab_path: str) -> str | None:
        async with session_factory() as session:
            result = await session.execute(
                sa.select(project_contexts.c.project_key).where(
                    project_contexts.c.gitlab_project_path == gitlab_path
                )
            )
            row = result.fetchone()
            return cast(str, row.project_key) if row else None

    guarded_ingestor = OwnedGitLabIngestor(cast(GitLabEventProcessor, ingestor), lease)
    guarded_resolver = OwnedProjectKeyResolver(resolve_project_key, lease)
    endpoint = GitLabWebhookEndpoint(
        guarded_ingestor,
        guarded_resolver,
        effective_settings.gitlab_webhook_secret,
    )
    server = AutomationServer(
        endpoint,
        host=effective_settings.automation_host,
        port=effective_settings.automation_port,
    )
    resources = AutomationResources(
        engine=runtime_engine,
        session_factory=session_factory,
        lease=lease,
        embedding_svc=embedding_svc,
        reranker=reranker,
        dedup_job=dedup_job,
        server=server,
    )
    return AutomationRuntime(
        resources,
        dedup_interval=effective_settings.automation_dedup_interval_seconds,
    )
