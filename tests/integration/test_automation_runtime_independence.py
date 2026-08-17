"""Real PostgreSQL/socket lifecycle tests for metrics and automation runtimes."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from aiohttp import ClientConnectorError, ClientSession
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brain_v42.automation.ownership import (
    AutomationOwnershipLease,
    OwnedGitLabIngestor,
    OwnedProjectKeyResolver,
    OwnershipLostError,
)
from brain_v42.automation.runtime import AutomationResources, AutomationRuntime
from brain_v42.automation.server import AutomationServer
from brain_v42.automation.webhook import GitLabWebhookEndpoint
from brain_v42.config import Settings
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.runtime import MetricsResources, MetricsRuntime
from brain_v42.metrics.server import MetricsServer

from .conftest import INTEGRATION_DB_URL

pytestmark = pytest.mark.integration


class CountingCloser:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1

    async def healthcheck(self) -> bool:
        return True


class CountingDisposer:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1
        await self._engine.dispose()


class CountingLease(AutomationOwnershipLease):
    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(engine, heartbeat_interval=0.02, heartbeat_timeout=0.2)
        self.release_calls = 0

    async def release(self) -> None:
        self.release_calls += 1
        await super().release()


class CountingDedupJob:
    def __init__(self) -> None:
        self.passes = 0
        self.merges = 0

    async def find_candidates(self, _project_key: str) -> list[tuple[object, object, float]]:
        self.passes += 1
        return []

    async def merge_features(
        self,
        _session: AsyncSession,
        _target: object,
        _source: object,
    ) -> bool:
        self.merges += 1
        return True


class RecordingIngestor:
    def __init__(self) -> None:
        self.calls = 0

    async def process_event(
        self,
        _payload: dict[str, object],
        _event_uuid: str,
        _project_key: str,
    ) -> dict[str, object]:
        self.calls += 1
        return {"status": "processed"}


class BlockingEmbedding:
    """Embedding boundary that holds one real webhook in flight."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def embed(self, _text: str) -> list[float]:
        self.entered.set()
        await self.resume.wait()
        return [0.01] * 1536


class RejectAfterGitLabInsert:
    """Allow pre-DML checks and reject the first check after real INSERT."""

    def __init__(self) -> None:
        self.dml_executed = False
        self.checks = 0

    def ensure_owned(self) -> None:
        self.checks += 1
        if self.dml_executed:
            raise OwnershipLostError("ownership lost before event commit")


class MinimalCollector:
    """Behavioral double limited to the public calls used by GET /metrics."""

    def get_metrics(self) -> dict[str, object]:
        return {"embedding_service": {}}

    async def collect_db_stats(self) -> dict[str, object]:
        return {}

    async def collect_search_quality(self) -> dict[str, object]:
        return {}

    async def collect_process_metrics(self) -> dict[str, object]:
        return {"active_processes": 0, "tools": {}, "embedding": {}}

    async def collect_dream_metrics(self) -> dict[str, object]:
        return {}

    async def collect_nightly_ops(self) -> dict[str, object]:
        return {}


class CountingAutomationServer(AutomationServer):
    def __init__(self, endpoint: GitLabWebhookEndpoint) -> None:
        super().__init__(endpoint, host="127.0.0.1", port=0)
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1
        await super().stop()


class CountingMetricsServer(MetricsServer):
    def __init__(
        self,
        collector: MetricsCollector,
        embedding_svc: CountingCloser,
        *,
        ingestor: OwnedGitLabIngestor | None = None,
        resolver: OwnedProjectKeyResolver | None = None,
    ) -> None:
        super().__init__(
            collector,
            embedding_svc,
            host="127.0.0.1",
            port=0,
            gitlab_ingestor=ingestor,
            project_key_resolver=resolver,
            webhook_secret="secret",
        )
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1
        await super().stop()


@dataclass(slots=True)
class AutomationBundle:
    runtime: AutomationRuntime
    resources: AutomationResources
    raw_engine: AsyncEngine
    disposer: CountingDisposer
    lease: CountingLease
    embedding: CountingCloser
    reranker: CountingCloser
    job: CountingDedupJob
    server: CountingAutomationServer
    ingestor: RecordingIngestor


@dataclass(slots=True)
class MetricsBundle:
    runtime: MetricsRuntime
    resources: MetricsResources
    raw_engine: AsyncEngine
    disposer: CountingDisposer
    lease: CountingLease | None
    embedding: CountingCloser
    reranker: CountingCloser | None
    job: CountingDedupJob | None
    server: CountingMetricsServer
    ingestor: RecordingIngestor | None


def _engine() -> AsyncEngine:
    return create_async_engine(
        INTEGRATION_DB_URL,
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
    )


def _automation_bundle(engine: AsyncEngine) -> AutomationBundle:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    lease = CountingLease(engine)
    embedding = CountingCloser()
    reranker = CountingCloser()
    job = CountingDedupJob()
    ingestor = RecordingIngestor()

    async def resolve(_path: str) -> str | None:
        return "brain-v42"

    endpoint = GitLabWebhookEndpoint(
        OwnedGitLabIngestor(ingestor, lease),
        OwnedProjectKeyResolver(resolve, lease),
        "secret",
    )
    server = CountingAutomationServer(endpoint)
    disposer = CountingDisposer(engine)
    resources = AutomationResources(
        engine=disposer,
        session_factory=factory,
        lease=lease,
        embedding_svc=embedding,
        reranker=reranker,
        dedup_job=job,
        server=server,
    )
    return AutomationBundle(
        runtime=AutomationRuntime(resources, dedup_interval=0.02),
        resources=resources,
        raw_engine=engine,
        disposer=disposer,
        lease=lease,
        embedding=embedding,
        reranker=reranker,
        job=job,
        server=server,
        ingestor=ingestor,
    )


def _metrics_bundle(engine: AsyncEngine, *, legacy: bool) -> MetricsBundle:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    embedding = CountingCloser()
    disposer = CountingDisposer(engine)
    lease = CountingLease(engine) if legacy else None
    reranker = CountingCloser() if legacy else None
    job = CountingDedupJob() if legacy else None
    ingestor = RecordingIngestor() if legacy else None
    guarded_ingestor = None
    guarded_resolver = None
    if lease is not None and ingestor is not None:

        async def resolve(_path: str) -> str | None:
            return "brain-v42"

        guarded_ingestor = OwnedGitLabIngestor(ingestor, lease)
        guarded_resolver = OwnedProjectKeyResolver(resolve, lease)

    collector = cast(MetricsCollector, MinimalCollector())
    server = CountingMetricsServer(
        collector,
        embedding,
        ingestor=guarded_ingestor,
        resolver=guarded_resolver,
    )
    resources = MetricsResources(
        engine=disposer,
        session_factory=factory,
        collector=collector,
        embedding_svc=embedding,
        server=server,
        lease=lease,
        reranker=reranker,
        dedup_job=job,
    )
    return MetricsBundle(
        runtime=MetricsRuntime(resources, dedup_interval=0.02),
        resources=resources,
        raw_engine=engine,
        disposer=disposer,
        lease=lease,
        embedding=embedding,
        reranker=reranker,
        job=job,
        server=server,
        ingestor=ingestor,
    )


def _bound_port(server: AutomationServer | MetricsServer) -> int | None:
    runner = server._runner
    if runner is None or not runner.sites:
        return None
    site = next(iter(runner.sites))
    site_server = site._server
    if site_server is None or not site_server.sockets:
        return None
    return int(site_server.sockets[0].getsockname()[1])


async def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _assert_test_database(*engines: AsyncEngine) -> None:
    for engine in engines:
        async with engine.connect() as connection:
            database = await connection.scalar(sa.text("SELECT current_database()"))
        assert database == "brain_test", f"refusing lifecycle test against {database!r}"


async def _terminate_backend(admin_engine: AsyncEngine, pid: int) -> bool:
    async with admin_engine.connect() as connection:
        terminated = await connection.scalar(
            sa.text("SELECT pg_terminate_backend(:pid)"),
            {"pid": pid},
        )
        await connection.commit()
    return bool(terminated)


async def _webhook_mutation_counts(
    engine: AsyncEngine,
    *,
    project_key: str,
    event_uuid: str,
) -> tuple[int, int, int]:
    async with engine.connect() as connection:
        feature_count = await connection.scalar(
            sa.text("SELECT count(*) FROM features WHERE project_key = :project_key"),
            {"project_key": project_key},
        )
        event_count = await connection.scalar(
            sa.text("SELECT count(*) FROM gitlab_events WHERE gitlab_event_id = :event_uuid"),
            {"event_uuid": event_uuid},
        )
        artifact_count = await connection.scalar(
            sa.text(
                "SELECT count(*) FROM feature_artifacts AS fa "
                "JOIN gitlab_events AS ge ON ge.id = fa.artifact_id "
                "WHERE fa.artifact_type = 'gitlab_event' "
                "AND ge.gitlab_event_id = :event_uuid"
            ),
            {"event_uuid": event_uuid},
        )
    return int(feature_count or 0), int(event_count or 0), int(artifact_count or 0)


async def _delete_webhook_probe(
    engine: AsyncEngine,
    *,
    project_key: str,
    event_uuid: str,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "DELETE FROM feature_artifacts WHERE artifact_type = 'gitlab_event' "
                "AND artifact_id IN ("
                "SELECT id FROM gitlab_events WHERE gitlab_event_id = :event_uuid"
                ")"
            ),
            {"event_uuid": event_uuid},
        )
        await connection.execute(
            sa.text("DELETE FROM gitlab_events WHERE gitlab_event_id = :event_uuid"),
            {"event_uuid": event_uuid},
        )
        await connection.execute(
            sa.text("DELETE FROM features WHERE project_key = :project_key"),
            {"project_key": project_key},
        )


async def _teardown_task(
    task: asyncio.Task[int],
    stop: asyncio.Event,
    *,
    server: AutomationServer | MetricsServer,
    lease: CountingLease | None,
    disposer: CountingDisposer,
) -> None:
    stop.set()
    finished = await _wait_until(task.done, timeout=2.0)
    if not finished:
        task.cancel()
    results = await asyncio.gather(task, return_exceptions=True)
    if results and isinstance(results[0], BaseException):
        await server.stop()
        if lease is not None:
            try:
                await lease.release()
            except NotImplementedError:
                # Surface-only RED: no lease behavior exists yet, but the
                # PostgreSQL engines and sockets still need deterministic teardown.
                pass
        await disposer.dispose()


async def test_metrics_and_automation_have_independent_real_lifecycles() -> None:
    automation = _automation_bundle(_engine())
    metrics = _metrics_bundle(_engine(), legacy=False)
    await _assert_test_database(automation.raw_engine, metrics.raw_engine)
    automation_stop = asyncio.Event()
    metrics_stop = asyncio.Event()
    automation_task = asyncio.create_task(automation.runtime.run(automation_stop))
    metrics_task = asyncio.create_task(metrics.runtime.run(metrics_stop))

    try:
        both_bound = await _wait_until(
            lambda: (
                _bound_port(automation.server) is not None
                and _bound_port(metrics.server) is not None
            )
        )
        assert both_bound, "both real runtime servers must bind ephemeral TCP sockets"
        automation_port = _bound_port(automation.server)
        assert automation_port is not None

        metrics_stop.set()
        metrics_stopped = await _wait_until(metrics_task.done)
        assert metrics_stopped, "metrics stop_event must complete only its lifecycle"
        assert await metrics_task == 0

        async with ClientSession() as client:
            response = await client.get(f"http://127.0.0.1:{automation_port}/health")
            assert response.status == 200

        restarted = _metrics_bundle(_engine(), legacy=False)
        await _assert_test_database(restarted.raw_engine)
        restarted_stop = asyncio.Event()
        restarted_task = asyncio.create_task(restarted.runtime.run(restarted_stop))
        try:
            rebound = await _wait_until(lambda: _bound_port(restarted.server) is not None)
            assert rebound, "metrics runtime must restart independently"
            restarted_port = _bound_port(restarted.server)
            assert restarted_port is not None

            automation_stop.set()
            automation_stopped = await _wait_until(automation_task.done)
            assert automation_stopped, "automation stop_event must complete only its lifecycle"
            assert await automation_task == 0

            async with ClientSession() as client:
                response = await client.get(f"http://127.0.0.1:{restarted_port}/metrics")
                assert response.status == 200
        finally:
            await _teardown_task(
                restarted_task,
                restarted_stop,
                server=restarted.server,
                lease=restarted.lease,
                disposer=restarted.disposer,
            )

        assert metrics.lease is None and metrics.job is None and metrics.reranker is None
        assert automation.server.stop_calls == 1
        assert automation.lease.release_calls == 1
        assert automation.embedding.close_calls == 1
        assert automation.reranker.close_calls == 1
        assert automation.disposer.dispose_calls == 1
        assert metrics.server.stop_calls == 1
        assert metrics.embedding.close_calls == 1
        assert metrics.disposer.dispose_calls == 1
    finally:
        await _teardown_task(
            metrics_task,
            metrics_stop,
            server=metrics.server,
            lease=metrics.lease,
            disposer=metrics.disposer,
        )
        await _teardown_task(
            automation_task,
            automation_stop,
            server=automation.server,
            lease=automation.lease,
            disposer=automation.disposer,
        )


async def test_runtime_conflict_refuses_second_scheduler_then_allows_handover() -> None:
    owner = _automation_bundle(_engine())
    contender = _automation_bundle(_engine())
    admin_engine = _engine()
    await _assert_test_database(owner.raw_engine, contender.raw_engine, admin_engine)
    project_key = f"integ-arc1-runtime-{uuid.uuid4().hex[:12]}"
    async with admin_engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO project_contexts (project_key, name, description) "
                "VALUES (:project_key, :name, :description)"
            ),
            {
                "project_key": project_key,
                "name": "ARC1 runtime integration",
                "description": "temporary scheduler probe",
            },
        )
    owner_stop = asyncio.Event()
    contender_stop = asyncio.Event()
    owner_task = asyncio.create_task(owner.runtime.run(owner_stop))
    contender_task: asyncio.Task[int] | None = None

    try:
        owner_bound = await _wait_until(lambda: _bound_port(owner.server) is not None)
        assert owner_bound, "first runtime must own the lease and bind"
        contender_task = asyncio.create_task(contender.runtime.run(contender_stop))
        contender_finished = await _wait_until(contender_task.done)
        assert contender_finished, "lease conflict must fail the second runtime promptly"
        assert await contender_task != 0
        assert _bound_port(contender.server) is None
        assert contender.job.passes == 0

        owner_stop.set()
        assert await _wait_until(owner_task.done), "owner must release lease on stop"
        assert await owner_task == 0

        successor = _automation_bundle(_engine())
        await _assert_test_database(successor.raw_engine)
        successor_stop = asyncio.Event()
        successor_task = asyncio.create_task(successor.runtime.run(successor_stop))
        try:
            successor_bound = await _wait_until(lambda: _bound_port(successor.server) is not None)
            assert successor_bound, "fresh runtime must acquire the handed-over lease"
            scheduler_ran = await _wait_until(lambda: successor.job.passes > 0)
            assert scheduler_ran, "only the successor scheduler may run after handover"
        finally:
            await _teardown_task(
                successor_task,
                successor_stop,
                server=successor.server,
                lease=successor.lease,
                disposer=successor.disposer,
            )
    finally:
        try:
            if contender_task is not None:
                await _teardown_task(
                    contender_task,
                    contender_stop,
                    server=contender.server,
                    lease=contender.lease,
                    disposer=contender.disposer,
                )
            else:
                await contender.raw_engine.dispose()
            await _teardown_task(
                owner_task,
                owner_stop,
                server=owner.server,
                lease=owner.lease,
                disposer=owner.disposer,
            )
        finally:
            try:
                async with admin_engine.begin() as connection:
                    await connection.execute(
                        sa.text("DELETE FROM project_contexts WHERE project_key = :project_key"),
                        {"project_key": project_key},
                    )
            finally:
                await admin_engine.dispose()


async def test_automation_backend_loss_stops_server_and_returns_nonzero() -> None:
    automation = _automation_bundle(_engine())
    admin_engine = _engine()
    await _assert_test_database(automation.raw_engine, admin_engine)
    stop = asyncio.Event()
    task = asyncio.create_task(automation.runtime.run(stop))

    try:
        bound = await _wait_until(lambda: _bound_port(automation.server) is not None)
        assert bound, "automation runtime must bind before backend-loss injection"
        port = _bound_port(automation.server)
        pid = automation.lease.backend_pid
        assert port is not None and pid is not None
        assert await _terminate_backend(admin_engine, pid)

        loss_detected = await _wait_until(automation.lease.ownership_lost.is_set)
        assert loss_detected, "dedicated lease watcher must publish ownership_lost"
        stopped = await _wait_until(task.done)
        assert stopped, "automation runtime must stop after lease loss"
        assert await task != 0
        assert automation.job.merges == 0

        async with ClientSession() as client:
            with pytest.raises(ClientConnectorError):
                await client.get(f"http://127.0.0.1:{port}/health")

        assert automation.server.stop_calls == 1
        assert automation.lease.release_calls == 1
        assert automation.embedding.close_calls == 1
        assert automation.reranker.close_calls == 1
        assert automation.disposer.dispose_calls == 1
    finally:
        await _teardown_task(
            task,
            stop,
            server=automation.server,
            lease=automation.lease,
            disposer=automation.disposer,
        )
        await admin_engine.dispose()


async def test_metrics_backend_loss_keeps_metrics_up_and_webhook_fail_closed() -> None:
    metrics = _metrics_bundle(_engine(), legacy=True)
    admin_engine = _engine()
    await _assert_test_database(metrics.raw_engine, admin_engine)
    stop = asyncio.Event()
    task = asyncio.create_task(metrics.runtime.run(stop))

    try:
        bound = await _wait_until(lambda: _bound_port(metrics.server) is not None)
        assert bound, "metrics runtime must bind before backend-loss injection"
        port = _bound_port(metrics.server)
        assert port is not None and metrics.lease is not None
        pid = metrics.lease.backend_pid
        assert pid is not None
        assert await _terminate_backend(admin_engine, pid)

        loss_detected = await _wait_until(metrics.lease.ownership_lost.is_set)
        assert loss_detected, "legacy lease watcher must publish ownership_lost"
        await asyncio.sleep(0.05)
        assert not task.done(), "metrics lifecycle must remain alive after legacy ownership loss"

        async with ClientSession() as client:
            metrics_response = await client.get(f"http://127.0.0.1:{port}/metrics")
            assert metrics_response.status == 200
            webhook_response = await client.post(
                f"http://127.0.0.1:{port}/gitlab/webhook",
                json={"project": {"path_with_namespace": "hawkixs/brain_v42"}},
                headers={
                    "X-Gitlab-Token": "secret",
                    "X-Gitlab-Event-UUID": "after-loss",
                },
            )
            assert webhook_response.status == 503
            assert await webhook_response.json() == {"status": "ownership_lost"}

        assert metrics.ingestor is not None and metrics.ingestor.calls == 0
        assert metrics.job is not None and metrics.job.merges == 0
        stop.set()
        assert await _wait_until(task.done), "metrics stop_event must finish after lease loss"
        assert await task == 0
        assert metrics.server.stop_calls == 1
        assert metrics.lease.release_calls == 1
        assert metrics.embedding.close_calls == 1
        assert metrics.reranker is not None and metrics.reranker.close_calls == 1
        assert metrics.disposer.dispose_calls == 1
    finally:
        await _teardown_task(
            task,
            stop,
            server=metrics.server,
            lease=metrics.lease,
            disposer=metrics.disposer,
        )
        await admin_engine.dispose()


async def test_inflight_webhook_cannot_commit_after_real_lease_handover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successor owner fences the predecessor at the next mutation boundary."""
    import brain_v42.automation.runtime as runtime_module

    owner_engine = _engine()
    successor_engine = _engine()
    admin_engine = _engine()
    embedding = BlockingEmbedding()
    monkeypatch.setattr(runtime_module, "GPUEmbeddingService", MagicMock(return_value=embedding))
    monkeypatch.setattr(
        runtime_module,
        "AutomationOwnershipLease",
        lambda engine: AutomationOwnershipLease(
            engine,
            heartbeat_interval=0.02,
            heartbeat_timeout=0.2,
        ),
    )
    settings = Settings(
        postgres_url=INTEGRATION_DB_URL,
        gitlab_webhook_secret="integration-secret",
        _env_file=None,  # type: ignore[call-arg]
    )
    runtime = runtime_module.build_automation_runtime(settings=settings, engine=owner_engine)
    owner = runtime._resources.lease
    successor = AutomationOwnershipLease(
        successor_engine,
        heartbeat_interval=0.02,
        heartbeat_timeout=0.2,
    )
    ingestor = runtime._resources.server._webhook_endpoint._gitlab_ingestor
    project_key = f"integ-arc1-fence-{uuid.uuid4().hex[:12]}"
    event_uuid = f"arc1-fence-{uuid.uuid4()}"
    event_task: asyncio.Task[dict[str, object]] | None = None

    await _assert_test_database(owner_engine, successor_engine, admin_engine)
    try:
        assert await owner.acquire(), "the predecessor must own the automation lease"
        event_task = asyncio.create_task(
            ingestor.process_event(
                {
                    "object_kind": "merge_request",
                    "object_attributes": {
                        "action": "open",
                        "title": "feat: mutation fence probe",
                        "description": "lease handover while embedding",
                        "source_branch": "feat/mutation-fence-probe",
                    },
                },
                event_uuid,
                project_key,
            )
        )
        assert await _wait_until(embedding.entered.is_set), "webhook must block inside embedding"
        pid = owner.backend_pid
        assert pid is not None
        assert await _terminate_backend(admin_engine, pid)
        assert await _wait_until(owner.ownership_lost.is_set), (
            "predecessor must detect backend loss"
        )
        assert await successor.acquire(), "successor must acquire before predecessor resumes"

        embedding.resume.set()
        outcome = (await asyncio.gather(event_task, return_exceptions=True))[0]
        counts = await _webhook_mutation_counts(
            admin_engine,
            project_key=project_key,
            event_uuid=event_uuid,
        )

        assert isinstance(outcome, OwnershipLostError), (
            "predecessor resumed after handover instead of failing closed: "
            f"outcome={outcome!r}, mutations(feature,event,artifact)={counts}"
        )
        assert counts == (0, 0, 0)
    finally:
        embedding.resume.set()
        if event_task is not None and not event_task.done():
            event_task.cancel()
        if event_task is not None:
            await asyncio.gather(event_task, return_exceptions=True)
        await _delete_webhook_probe(
            admin_engine,
            project_key=project_key,
            event_uuid=event_uuid,
        )
        await successor.release()
        await owner.release()
        await admin_engine.dispose()
        await successor_engine.dispose()
        await owner_engine.dispose()


async def test_real_event_insert_rolls_back_when_guard_rejects_before_commit() -> None:
    """A real post-DML ownership loss must leave no committed GitLab event."""
    from brain_v42.services.gitlab_ingestor import GitLabIngestor

    engine = _engine()
    await _assert_test_database(engine)
    project_key = f"integ-arc1-precommit-{uuid.uuid4().hex[:12]}"
    event_uuid = f"arc1-precommit-{uuid.uuid4()}"
    gate = RejectAfterGitLabInsert()
    feature_id: object | None = None

    def mark_gitlab_insert(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO GITLAB_EVENTS"):
            gate.dml_executed = True

    sa.event.listen(engine.sync_engine, "after_cursor_execute", mark_gitlab_insert)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                sa.text(
                    "INSERT INTO features (project_key, name, description) "
                    "VALUES (:project_key, :name, :description) RETURNING id"
                ),
                {
                    "project_key": project_key,
                    "name": "ARC1 precommit rollback probe",
                    "description": "temporary integration feature",
                },
            )
            feature_id = result.scalar_one()

        session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        ingestor = GitLabIngestor(
            session_factory,
            MagicMock(name="embedding"),
            MagicMock(name="cluster_guard"),
        )
        ingestor._mutation_guard = gate.ensure_owned  # type: ignore[attr-defined]
        try:
            outcome: object = await ingestor._store_event(
                gitlab_event_id=event_uuid,
                event_type="merge_request",
                project_key=project_key,
                ref="feat/precommit-probe",
                title="precommit rollback probe",
                embedding=[0.01] * 1536,
                feature_id=feature_id,
            )
        except BaseException as exc:
            outcome = exc

        async with engine.connect() as connection:
            persisted = await connection.scalar(
                sa.text("SELECT count(*) FROM gitlab_events WHERE gitlab_event_id = :event_uuid"),
                {"event_uuid": event_uuid},
            )

        assert gate.dml_executed, "the test must reach a real INSERT before rejection"
        assert isinstance(outcome, OwnershipLostError), (
            "_store_event committed after its real INSERT instead of checking ownership: "
            f"outcome={outcome!r}, checks={gate.checks}, persisted={persisted}"
        )
        assert int(persisted or 0) == 0
    finally:
        sa.event.remove(engine.sync_engine, "after_cursor_execute", mark_gitlab_insert)
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM gitlab_events WHERE gitlab_event_id = :event_uuid"),
                {"event_uuid": event_uuid},
            )
            if feature_id is not None:
                await connection.execute(
                    sa.text("DELETE FROM features WHERE id = :feature_id"),
                    {"feature_id": feature_id},
                )
        await engine.dispose()
