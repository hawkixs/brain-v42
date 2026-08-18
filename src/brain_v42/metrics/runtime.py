"""Pilotable lifecycle for the standalone metrics sidecar."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

import sqlalchemy as sa
import structlog
from neo4j import AsyncDriver
from sqlalchemy import text
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
    ProjectKeyResolver,
)
from brain_v42.config import Settings, get_settings
from brain_v42.db.neo4j import create_neo4j_driver
from brain_v42.db.tables import project_contexts
from brain_v42.metrics.brain_graph_server import BrainGraphMetricsServer
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.recent_log import RecentLogProcessor
from brain_v42.metrics.retention import PROCESS_METRICS_STALE_SQL
from brain_v42.metrics.server import MetricsServer
from brain_v42.services.brain_graph_projection import (
    BrainGraphProjectionService,
    Neo4jGraphSnapshotReader,
    PostgresGraphSnapshotReader,
)
from brain_v42.services.embedding_factory import (
    build_embedding_service,
    build_reranker_client,
)
from brain_v42.services.feature_dedup_job import FeatureDedupJob
from brain_v42.services.gpu_embedding_service import GPUEmbeddingService
from brain_v42.services.graph_service import GraphService
from brain_v42.services.status_engine import StatusEngine

logger = structlog.get_logger(__name__)

LEGACY_LEASE_ACQUIRE_TIMEOUT_SECONDS = 2.0


class AsyncCloser(Protocol):
    async def close(self) -> None: ...


class AsyncDisposer(Protocol):
    async def dispose(self) -> None: ...


@dataclass(slots=True)
class LegacyAutomationResources:
    """Business resources constructed only after the legacy lease is acquired."""

    reranker: AsyncCloser
    dedup_job: FeatureDedupJobProtocol
    gitlab_ingestor: GitLabEventProcessor | None
    project_key_resolver: ProjectKeyResolver | None


@dataclass(slots=True)
class MetricsResources:
    """Typed resources owned by one metrics sidecar lifecycle."""

    engine: AsyncDisposer
    session_factory: async_sessionmaker[AsyncSession]
    collector: MetricsCollector
    embedding_svc: AsyncCloser
    server: MetricsServer | None
    neo4j_driver: AsyncDriver | None = None
    lease: AutomationOwnershipLease | None = None
    reranker: AsyncCloser | None = None
    dedup_job: FeatureDedupJobProtocol | None = None
    legacy_factory: Callable[[AutomationOwnershipLease], LegacyAutomationResources] | None = None
    server_factory: (
        Callable[[GitLabEventProcessor | None, ProjectKeyResolver | None], MetricsServer] | None
    ) = None


class MetricsRuntime:
    """Keep metrics alive independently from optional legacy automation."""

    def __init__(self, resources: MetricsResources, *, dedup_interval: float) -> None:
        self._resources = resources
        self._dedup_interval = dedup_interval

    async def run(self, stop_event: asyncio.Event) -> int:
        cleanup_task: asyncio.Task[None] | None = None
        dedup_task: asyncio.Task[None] | None = None
        wait_tasks: list[asyncio.Task[bool]] = []
        legacy_owned = False
        exit_code = 0
        try:
            lease = self._resources.lease
            if lease is not None:
                try:
                    legacy_owned = await asyncio.wait_for(
                        lease.acquire(),
                        timeout=LEGACY_LEASE_ACQUIRE_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    logger.warning(
                        "metrics_runtime.legacy_lease_timeout",
                        timeout_seconds=LEGACY_LEASE_ACQUIRE_TIMEOUT_SECONDS,
                    )
                    legacy_owned = False
                except Exception:
                    logger.exception("metrics_runtime.legacy_lease_failed")
                    legacy_owned = False
                if not legacy_owned:
                    logger.warning("metrics_runtime.legacy_lease_conflict")

            gitlab_ingestor: GitLabEventProcessor | None = None
            project_key_resolver: ProjectKeyResolver | None = None
            if legacy_owned and lease is not None and self._resources.legacy_factory is not None:
                legacy = self._resources.legacy_factory(lease)
                self._resources.reranker = legacy.reranker
                self._resources.dedup_job = legacy.dedup_job
                gitlab_ingestor = legacy.gitlab_ingestor
                project_key_resolver = legacy.project_key_resolver

            if self._resources.server_factory is not None:
                self._resources.server = self._resources.server_factory(
                    gitlab_ingestor,
                    project_key_resolver,
                )

            server = self._resources.server
            if server is None:
                logger.error("metrics_runtime.server_not_built")
                return 3
            await server.start()
            if server._runner is None:
                return 0

            cleanup_task = asyncio.create_task(
                run_cleanup_loop(self._resources.session_factory),
                name="metrics-cleanup-loop",
            )
            if legacy_owned and lease is not None and self._resources.dedup_job is not None:
                dedup_task = asyncio.create_task(
                    run_dedup_loop(
                        self._resources.dedup_job,
                        self._resources.session_factory,
                        interval=self._dedup_interval,
                        ownership=lease,
                    ),
                    name="metrics-legacy-dedup-loop",
                )

            stop_wait = asyncio.create_task(stop_event.wait())
            wait_tasks.append(stop_wait)
            if legacy_owned and lease is not None:
                loss_wait = asyncio.create_task(lease.ownership_lost.wait())
                wait_tasks.append(loss_wait)
                done, _pending = await asyncio.wait(
                    {stop_wait, loss_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if loss_wait in done and lease.ownership_lost.is_set():
                    await self._stop_dedup_after_loss(dedup_task)
                    dedup_task = None
                    await stop_wait
            else:
                await stop_wait
            return exit_code
        finally:
            for task in wait_tasks:
                if not task.done():
                    task.cancel()
            if wait_tasks:
                await asyncio.gather(*wait_tasks, return_exceptions=True)
            errors = await self._cleanup(cleanup_task, dedup_task)
            if errors:
                logger.error(
                    "metrics_runtime.cleanup_failed",
                    errors=[str(error) for error in errors],
                )

    @staticmethod
    async def _stop_dedup_after_loss(dedup_task: asyncio.Task[None] | None) -> None:
        if dedup_task is None:
            return
        if not dedup_task.done():
            dedup_task.cancel()
        try:
            await dedup_task
        except (asyncio.CancelledError, OwnershipLostError):
            pass

    async def _cleanup(
        self,
        cleanup_task: asyncio.Task[None] | None,
        dedup_task: asyncio.Task[None] | None,
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        for task in (cleanup_task, dedup_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, OwnershipLostError):
                pass
            except BaseException as exc:
                errors.append(exc)

        if self._resources.server is not None:
            await self._attempt(self._resources.server.stop, errors)
        await self._attempt(self._resources.embedding_svc.close, errors)
        if self._resources.reranker is not None:
            await self._attempt(self._resources.reranker.close, errors)
        if self._resources.lease is not None:
            await self._attempt_protected(self._resources.lease.release, errors)
        if self._resources.neo4j_driver is not None:
            await self._attempt(self._resources.neo4j_driver.close, errors)
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


def build_sidecar_structlog_processors(collector: MetricsCollector) -> list[Any]:
    """Return the structlog chain used only by the metrics sidecar."""
    return [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        RecentLogProcessor(collector, min_level="info"),
        structlog.dev.ConsoleRenderer(),
    ]


async def run_cleanup_loop(
    session_factory: async_sessionmaker[AsyncSession],
    interval: float = 60.0,
) -> None:
    """Periodically remove stale process metrics and old search log rows."""
    last_search_log_cleanup = 0.0
    while True:
        try:
            await asyncio.sleep(interval)
            async with session_factory() as session:
                # Le seul fragment interpolé est PROCESS_METRICS_STALE_SQL, importé en
                # tête de module depuis brain_v42.metrics.retention. Il y est construit
                # à l'import à partir du littéral entier PROCESS_METRICS_RETENTION_SECONDS
                # = 3600 ; retention.py n'importe ni os, ni Settings, ni rien d'externe.
                # Ni `session_factory` ni `interval`, seuls paramètres de cette boucle,
                # ne touchent la chaîne SQL. L'invariant est épinglé par
                # tests/unit/metrics/test_runtime_stale_sql_is_a_literal_constant.py,
                # qui échoue si la constante devient dynamique.
                await session.execute(
                    text(f"DELETE FROM process_metrics WHERE {PROCESS_METRICS_STALE_SQL}")  # nosec B608 - fragment = constante d'import PROCESS_METRICS_STALE_SQL (metrics/retention.py), figée sur l'int littéral 3600, hors de portée de toute entrée ; exception revue le 2026-08-16, à réexaminer avant le 2026-09-30
                )
                now = time.time()
                if now - last_search_log_cleanup > 3600:
                    await session.execute(
                        text("DELETE FROM search_log WHERE created_at < NOW() - INTERVAL '30 days'")
                    )
                    last_search_log_cleanup = now
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("metrics_standalone.cleanup_error")


def _build_legacy_resources(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    embedding_svc: GPUEmbeddingService,
    lease: AutomationOwnershipLease,
) -> LegacyAutomationResources:
    """Build mandatory dedup first, then the optional webhook boundary."""
    reranker = build_reranker_client(settings)
    dedup_job = FeatureDedupJob(
        session_factory,
        reranker,
        embedding_svc,
        mutation_guard=lease.ensure_owned,
    )
    guarded_ingestor: GitLabEventProcessor | None = None
    guarded_resolver: ProjectKeyResolver | None = None
    try:
        from brain_v42.services.cluster_guard import ClusterGuard  # noqa: PLC0415
        from brain_v42.services.gitlab_ingestor import GitLabIngestor  # noqa: PLC0415

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
    except ImportError:
        logger.info("metrics_standalone.webhook_disabled", reason="GitLabIngestor not available")

    return LegacyAutomationResources(
        reranker=reranker,
        dedup_job=dedup_job,
        gitlab_ingestor=guarded_ingestor,
        project_key_resolver=guarded_resolver,
    )


def build_metrics_runtime(
    *,
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
) -> MetricsRuntime:
    """Build metrics infrastructure and defer legacy business until lease ownership."""
    effective_settings = settings or get_settings()
    if effective_settings.graph_projector_enabled:
        raise RuntimeError("projector role is restricted to the MCP runtime")
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
    collector = MetricsCollector(engine=runtime_engine, session_factory=session_factory)
    embedding_svc = build_embedding_service(effective_settings)
    neo4j_driver = create_neo4j_driver(
        url=effective_settings.neo4j_url,
        user=effective_settings.neo4j_user,
        password=effective_settings.neo4j_password,
        enabled=effective_settings.graph_enabled,
    )
    graph_svc = GraphService(neo4j_driver) if neo4j_driver is not None else None
    graph_projection_svc = BrainGraphProjectionService(
        postgres_source=PostgresGraphSnapshotReader(session_factory),
        neo4j_source=(Neo4jGraphSnapshotReader(neo4j_driver) if neo4j_driver is not None else None),
    )

    def server_factory(
        gitlab_ingestor: GitLabEventProcessor | None,
        project_key_resolver: ProjectKeyResolver | None,
    ) -> MetricsServer:
        return BrainGraphMetricsServer(
            collector=collector,
            embedding_svc=embedding_svc,
            port=effective_settings.metrics_port,
            host=effective_settings.metrics_host,
            gitlab_ingestor=gitlab_ingestor,
            project_key_resolver=project_key_resolver,
            webhook_secret=effective_settings.gitlab_webhook_secret,
            graph_svc=graph_svc,
            graph_projection_svc=graph_projection_svc,
        )

    lease = (
        AutomationOwnershipLease(runtime_engine)
        if effective_settings.metrics_legacy_automation_enabled
        else None
    )
    legacy_factory: Callable[[AutomationOwnershipLease], LegacyAutomationResources] | None = None
    if lease is not None:

        def build_legacy(owned_lease: AutomationOwnershipLease) -> LegacyAutomationResources:
            return _build_legacy_resources(
                settings=effective_settings,
                session_factory=session_factory,
                embedding_svc=embedding_svc,
                lease=owned_lease,
            )

        legacy_factory = build_legacy

    resources = MetricsResources(
        engine=runtime_engine,
        session_factory=session_factory,
        collector=collector,
        embedding_svc=embedding_svc,
        server=None,
        neo4j_driver=neo4j_driver,
        lease=lease,
        legacy_factory=legacy_factory,
        server_factory=server_factory,
    )
    return MetricsRuntime(
        resources,
        dedup_interval=effective_settings.automation_dedup_interval_seconds,
    )
