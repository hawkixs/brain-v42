"""Tests for the pilotable metrics lifecycle and legacy rollback facade."""

from __future__ import annotations

import asyncio
import builtins
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.server import MetricsServer


class CountingCloser:
    def __init__(self, name: str, order: list[str]) -> None:
        self._name = name
        self._order = order
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self._order.append(self._name)


class CountingDisposer:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1
        self._order.append("engine")


async def _event_within(event: asyncio.Event, timeout: float = 0.3) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return True


def _base_resources(
    order: list[str],
    *,
    lease: object | None,
    reranker: CountingCloser | None = None,
    dedup_job: object | None = None,
):
    from brain_v42.metrics.runtime import MetricsResources

    server = MagicMock(spec=MetricsServer)
    server._runner = object()

    async def start() -> None:
        order.append("bind")

    async def stop() -> None:
        order.append("server")

    server.start = AsyncMock(side_effect=start)
    server.stop = AsyncMock(side_effect=stop)
    embedding = CountingCloser("embedding", order)
    engine = CountingDisposer(order)
    resources = MetricsResources(
        engine=engine,
        session_factory=cast(async_sessionmaker[AsyncSession], MagicMock()),
        collector=cast(MetricsCollector, MagicMock()),
        embedding_svc=embedding,
        server=server,
        lease=lease,  # type: ignore[arg-type]
        reranker=reranker,
        dedup_job=dedup_job,  # type: ignore[arg-type]
    )
    return resources, server, embedding, engine


async def test_legacy_off_runs_metrics_without_lease_or_business_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.metrics.runtime as runtime_module

    order: list[str] = []
    resources, server, embedding, engine = _base_resources(order, lease=None)
    scheduler = AsyncMock()
    monkeypatch.setattr(runtime_module, "run_dedup_loop", scheduler, raising=False)
    stop = asyncio.Event()
    stop.set()

    result = (
        await asyncio.gather(
            runtime_module.MetricsRuntime(resources, dedup_interval=17).run(stop),
            return_exceptions=True,
        )
    )[0]

    assert result == 0
    scheduler.assert_not_awaited()
    server.start.assert_awaited_once()
    server.stop.assert_awaited_once()
    assert embedding.close_calls == engine.dispose_calls == 1


async def test_metrics_bind_red_preserves_normal_exit_and_full_cleanup() -> None:
    import brain_v42.metrics.runtime as runtime_module

    order: list[str] = []
    resources, server, embedding, engine = _base_resources(order, lease=None)

    async def fail_to_bind_without_raising() -> None:
        order.append("bind-red")
        server._runner = None

    server.start = AsyncMock(side_effect=fail_to_bind_without_raising)

    result = await runtime_module.MetricsRuntime(resources, dedup_interval=17).run(asyncio.Event())

    assert result == 0
    server.stop.assert_awaited_once()
    assert embedding.close_calls == 1
    assert engine.dispose_calls == 1


async def test_lease_conflict_keeps_metrics_only_and_never_starts_business(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.metrics.runtime as runtime_module

    order: list[str] = []
    lease = MagicMock()
    lease.acquire = AsyncMock(return_value=False)
    lease.release = AsyncMock(side_effect=lambda: order.append("lease"))
    lease.ownership_lost = asyncio.Event()
    reranker = CountingCloser("reranker", order)
    resources, server, _embedding, _engine = _base_resources(
        order,
        lease=lease,
        reranker=reranker,
        dedup_job=MagicMock(),
    )
    scheduler = AsyncMock()
    monkeypatch.setattr(runtime_module, "run_dedup_loop", scheduler, raising=False)
    stop = asyncio.Event()
    stop.set()

    result = (
        await asyncio.gather(
            runtime_module.MetricsRuntime(resources, dedup_interval=17).run(stop),
            return_exceptions=True,
        )
    )[0]

    assert result == 0
    server.start.assert_awaited_once()
    scheduler.assert_not_awaited()
    lease.release.assert_awaited_once()


async def test_blocked_legacy_lease_falls_back_to_metrics_only_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.metrics.runtime as runtime_module

    order: list[str] = []
    acquire_started = asyncio.Event()
    acquire_cancelled = asyncio.Event()
    lease = MagicMock()

    async def acquire() -> bool:
        acquire_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            acquire_cancelled.set()
            raise

    lease.acquire = AsyncMock(side_effect=acquire)
    lease.release = AsyncMock(side_effect=lambda: order.append("lease"))
    lease.ownership_lost = asyncio.Event()
    resources, server, _embedding, _engine = _base_resources(order, lease=lease)
    scheduler = AsyncMock()
    monkeypatch.setattr(runtime_module, "run_dedup_loop", scheduler, raising=False)
    monkeypatch.setattr(runtime_module, "run_cleanup_loop", AsyncMock(), raising=False)
    monkeypatch.setattr(
        runtime_module,
        "LEGACY_LEASE_ACQUIRE_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    stop = asyncio.Event()
    stop.set()

    result = await asyncio.wait_for(
        runtime_module.MetricsRuntime(resources, dedup_interval=17).run(stop),
        timeout=0.3,
    )

    assert result == 0
    assert acquire_started.is_set()
    assert acquire_cancelled.is_set()
    server.start.assert_awaited_once()
    scheduler.assert_not_awaited()
    lease.release.assert_awaited_once()


async def test_conflict_defers_business_and_builds_metrics_only_server_after_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.metrics.runtime as runtime_module

    order: list[str] = []
    lease = MagicMock()

    async def acquire() -> bool:
        order.append("acquire")
        return False

    lease.acquire = AsyncMock(side_effect=acquire)
    lease.release = AsyncMock()
    lease.ownership_lost = asyncio.Event()
    resources, initial_server, _embedding, _engine = _base_resources(order, lease=lease)
    legacy_factory = MagicMock(side_effect=AssertionError("business built on conflict"))
    metrics_only_server = MagicMock(spec=MetricsServer)
    metrics_only_server._runner = object()
    metrics_only_server.start = AsyncMock()
    metrics_only_server.stop = AsyncMock()

    def server_factory(ingestor: object | None, resolver: object | None) -> MetricsServer:
        order.append("server-factory")
        assert ingestor is None and resolver is None
        return metrics_only_server

    resources.legacy_factory = legacy_factory
    resources.server_factory = MagicMock(side_effect=server_factory)
    forbidden_constructor = MagicMock(side_effect=AssertionError("business constructed"))
    monkeypatch.setattr(runtime_module, "RerankerClient", forbidden_constructor)
    monkeypatch.setattr(runtime_module, "FeatureDedupJob", forbidden_constructor)
    monkeypatch.setattr(runtime_module, "GitLabIngestor", forbidden_constructor, raising=False)
    monkeypatch.setattr(runtime_module, "run_cleanup_loop", AsyncMock(), raising=False)
    stop = asyncio.Event()
    stop.set()

    result = await runtime_module.MetricsRuntime(resources, dedup_interval=17).run(stop)

    assert result == 0
    assert order[:2] == ["acquire", "server-factory"]
    resources.server_factory.assert_called_once_with(None, None)
    legacy_factory.assert_not_called()
    forbidden_constructor.assert_not_called()
    initial_server.start.assert_not_awaited()
    metrics_only_server.start.assert_awaited_once()


async def test_success_builds_business_once_then_server_with_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.metrics.runtime as runtime_module

    order: list[str] = []
    lease = MagicMock()

    async def acquire() -> bool:
        order.append("acquire")
        return True

    lease.acquire = AsyncMock(side_effect=acquire)
    lease.release = AsyncMock()
    lease.ownership_lost = asyncio.Event()
    resources, initial_server, _embedding, _engine = _base_resources(order, lease=lease)
    reranker = CountingCloser("reranker", order)
    dedup_job = MagicMock()
    ingestor = MagicMock(name="guarded-ingestor")
    resolver = MagicMock(name="guarded-resolver")
    legacy = runtime_module.LegacyAutomationResources(
        reranker=reranker,
        dedup_job=dedup_job,
        gitlab_ingestor=ingestor,
        project_key_resolver=resolver,
    )

    def build_legacy(_lease: object) -> object:
        order.append("legacy-factory")
        return legacy

    legacy_factory = MagicMock(side_effect=build_legacy)
    webhook_server = MagicMock(spec=MetricsServer)
    webhook_server._runner = object()
    webhook_server.start = AsyncMock()
    webhook_server.stop = AsyncMock()

    def server_factory(actual_ingestor: object, actual_resolver: object) -> MetricsServer:
        order.append("server-factory")
        assert actual_ingestor is ingestor
        assert actual_resolver is resolver
        return webhook_server

    resources.legacy_factory = legacy_factory
    resources.server_factory = MagicMock(side_effect=server_factory)
    monkeypatch.setattr(runtime_module, "run_dedup_loop", AsyncMock(), raising=False)
    monkeypatch.setattr(runtime_module, "run_cleanup_loop", AsyncMock(), raising=False)
    stop = asyncio.Event()
    stop.set()

    result = await runtime_module.MetricsRuntime(resources, dedup_interval=17).run(stop)

    assert result == 0
    assert order[:3] == ["acquire", "legacy-factory", "server-factory"]
    legacy_factory.assert_called_once_with(lease)
    resources.server_factory.assert_called_once_with(ingestor, resolver)
    initial_server.start.assert_not_awaited()
    webhook_server.start.assert_awaited_once()


async def test_legacy_ownership_loss_stops_dedup_but_keeps_metrics_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.metrics.runtime as runtime_module

    order: list[str] = []
    lease = MagicMock()
    lease.acquire = AsyncMock(return_value=True)
    lease.release = AsyncMock(side_effect=lambda: order.append("lease"))
    lease.ownership_lost = asyncio.Event()
    reranker = CountingCloser("reranker", order)
    resources, _server, _embedding, _engine = _base_resources(
        order,
        lease=lease,
        reranker=reranker,
        dedup_job=MagicMock(),
    )
    scheduler_started = asyncio.Event()
    scheduler_cancelled = asyncio.Event()

    async def scheduler(*_args: object, **_kwargs: object) -> None:
        scheduler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            scheduler_cancelled.set()
            raise

    monkeypatch.setattr(runtime_module, "run_dedup_loop", scheduler, raising=False)
    stop = asyncio.Event()
    task = asyncio.create_task(
        runtime_module.MetricsRuntime(resources, dedup_interval=17).run(stop)
    )
    try:
        assert await _event_within(scheduler_started), "legacy scheduler must start"
        lease.ownership_lost.set()
        assert await _event_within(scheduler_cancelled), "loss must cancel legacy dedup"
        await asyncio.sleep(0)
        assert not task.done(), "metrics server must remain alive after ownership loss"
        stop.set()
        assert await task == 0
    finally:
        stop.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_build_metrics_runtime_legacy_off_constructs_no_business(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.metrics.runtime as runtime_module
    from brain_v42.config import Settings

    builder = getattr(runtime_module, "build_metrics_runtime", None)
    assert builder is not None, "metrics runtime needs a production composition builder"
    forbidden = MagicMock(side_effect=AssertionError("legacy business constructed"))
    monkeypatch.setattr(runtime_module, "FeatureDedupJob", forbidden, raising=False)
    monkeypatch.setattr(runtime_module, "GitLabIngestor", forbidden, raising=False)
    settings = Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
        metrics_legacy_automation_enabled=False,
        graph_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )
    engine = MagicMock()

    runtime = builder(settings=settings, engine=engine)

    assert runtime._resources.lease is None
    assert runtime._resources.reranker is None
    assert runtime._resources.dedup_job is None
    forbidden.assert_not_called()
    assert runtime._resources.server is None
    assert runtime._resources.server_factory is not None
    metrics_only_server = runtime._resources.server_factory(None, None)
    routes = {
        route.resource.canonical for route in metrics_only_server._build_app().router.routes()
    }
    assert "/gitlab/webhook" not in routes


def test_import_error_disables_webhook_but_preserves_legacy_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.metrics.runtime as runtime_module
    from brain_v42.config import Settings

    legacy_builder = getattr(runtime_module, "_build_legacy_resources", None)
    assert legacy_builder is not None, "legacy composition needs a narrow fallback seam"
    reranker = CountingCloser("reranker", [])
    dedup_job = MagicMock()
    monkeypatch.setattr(runtime_module, "RerankerClient", MagicMock(return_value=reranker))
    monkeypatch.setattr(runtime_module, "FeatureDedupJob", MagicMock(return_value=dedup_job))
    real_import = builtins.__import__

    def fail_webhook_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "brain_v42.services.cluster_guard":
            raise ImportError("optional webhook unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_webhook_import)
    settings = Settings(
        postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
        metrics_legacy_automation_enabled=True,
        _env_file=None,  # type: ignore[call-arg]
    )

    legacy = legacy_builder(
        settings=settings,
        session_factory=cast(async_sessionmaker[AsyncSession], MagicMock()),
        embedding_svc=MagicMock(),
        lease=MagicMock(),
    )

    assert legacy.dedup_job is dedup_job
    assert legacy.reranker is reranker
    assert legacy.gitlab_ingestor is None
    assert legacy.project_key_resolver is None


def test_legacy_builder_wires_one_lease_guard_through_webhook_mutations() -> None:
    """Rollback mode must use its legacy lease at every webhook mutation layer."""
    from brain_v42.automation.ownership import AutomationOwnershipLease
    from brain_v42.config import Settings
    from brain_v42.metrics.runtime import _build_legacy_resources

    lease = AutomationOwnershipLease(MagicMock(name="engine"))
    legacy = _build_legacy_resources(
        settings=Settings(
            postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
            metrics_legacy_automation_enabled=True,
            _env_file=None,  # type: ignore[call-arg]
        ),
        session_factory=cast(async_sessionmaker[AsyncSession], MagicMock()),
        embedding_svc=MagicMock(),
        lease=lease,
    )
    assert legacy.gitlab_ingestor is not None
    ingestor = legacy.gitlab_ingestor._inner
    cluster_guard = ingestor._cluster_guard

    ingestor_guard = getattr(ingestor, "_mutation_guard", None)
    cluster_guard_callback = getattr(cluster_guard, "_mutation_guard", None)
    assert ingestor_guard == lease.ensure_owned
    assert cluster_guard_callback == lease.ensure_owned
    assert getattr(ingestor_guard, "__self__", None) is lease
    assert getattr(cluster_guard_callback, "__self__", None) is lease
