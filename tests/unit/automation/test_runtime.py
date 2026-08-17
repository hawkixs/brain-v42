"""Unit tests for automation ownership and lifecycle composition."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


def test_runtime_surfaces_are_importable() -> None:
    from brain_v42.automation.ownership import (
        AutomationOwnershipLease,
        OwnershipLostError,
        OwnershipState,
    )
    from brain_v42.automation.runtime import (
        AutomationCleanupError,
        AutomationResources,
        AutomationRuntime,
    )
    from brain_v42.metrics.runtime import MetricsResources, MetricsRuntime

    assert all(
        surface is not None
        for surface in (
            AutomationOwnershipLease,
            OwnershipLostError,
            OwnershipState,
            AutomationCleanupError,
            AutomationResources,
            AutomationRuntime,
            MetricsResources,
            MetricsRuntime,
        )
    )


class FakeLeaseConnection:
    def __init__(
        self,
        *,
        pid_results: list[int] | None = None,
        lock_result: bool = True,
        unlock_result: bool = True,
    ) -> None:
        self.pid_results = list(pid_results or [4101])
        self.lock_result = lock_result
        self.unlock_result = unlock_result
        self.statements: list[tuple[str, object | None]] = []
        self.execution_options_calls: list[dict[str, object]] = []
        self.invalidate_calls = 0
        self.close_calls = 0
        self.unlock_started: asyncio.Event | None = None
        self.block_unlock: asyncio.Event | None = None
        self.heartbeat_started: asyncio.Event | None = None
        self.block_heartbeat: asyncio.Event | None = None
        self.pid_calls = 0

    async def execution_options(self, **options: object) -> FakeLeaseConnection:
        self.execution_options_calls.append(options)
        return self

    async def scalar(self, statement: object, parameters: object | None = None) -> object:
        sql = str(statement)
        self.statements.append((sql, parameters))
        if "pg_try_advisory_lock" in sql:
            return self.lock_result
        if "pg_advisory_unlock" in sql:
            if self.unlock_started is not None:
                self.unlock_started.set()
            if self.block_unlock is not None:
                await self.block_unlock.wait()
            return self.unlock_result
        if "pg_backend_pid" in sql:
            self.pid_calls += 1
            if self.pid_calls > 1 and self.heartbeat_started is not None:
                self.heartbeat_started.set()
                if self.block_heartbeat is not None:
                    await self.block_heartbeat.wait()
            if len(self.pid_results) > 1:
                return self.pid_results.pop(0)
            return self.pid_results[0]
        raise AssertionError(f"unexpected lease SQL: {sql}")

    async def invalidate(self) -> None:
        self.invalidate_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


class FakeLeaseEngine:
    def __init__(self, connection: FakeLeaseConnection) -> None:
        self.connection = connection
        self.connect_calls = 0

    async def connect(self) -> AsyncConnection:
        self.connect_calls += 1
        return cast(AsyncConnection, self.connection)


async def _event_within(event: asyncio.Event, timeout: float = 0.3) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return True


async def _predicate_within(predicate: object, timeout: float = 0.3) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        await asyncio.sleep(0.001)
    return bool(predicate())  # type: ignore[operator]


async def _acquire_or_none(lease: object) -> bool | None:
    try:
        return await lease.acquire()  # type: ignore[attr-defined]
    except NotImplementedError:
        return None


async def test_lease_uses_one_autocommit_session_and_explicit_unlock() -> None:
    from brain_v42.automation.ownership import AutomationOwnershipLease, OwnershipState

    connection = FakeLeaseConnection()
    engine = FakeLeaseEngine(connection)
    lease = AutomationOwnershipLease(
        cast(AsyncEngine, engine),
        heartbeat_interval=3600,
        heartbeat_timeout=1,
    )

    assert await _acquire_or_none(lease) is True, "lease must acquire its advisory lock"
    assert lease.state is OwnershipState.OWNED
    assert lease.backend_pid == 4101
    lease.ensure_owned()
    await lease.release()

    assert engine.connect_calls == 1
    assert connection.execution_options_calls == [{"isolation_level": "AUTOCOMMIT"}]
    assert sum("pg_try_advisory_lock" in sql for sql, _ in connection.statements) == 1
    assert sum("pg_advisory_unlock" in sql for sql, _ in connection.statements) == 1
    unlock_parameters = next(
        parameters for sql, parameters in connection.statements if "pg_advisory_unlock" in sql
    )
    assert unlock_parameters == {"lock_key": AutomationOwnershipLease.LOCK_KEY}
    assert connection.invalidate_calls == 0
    assert connection.close_calls == 1
    assert lease.state is OwnershipState.RELEASED


async def test_lease_conflict_closes_connection_without_starting_watcher() -> None:
    from brain_v42.automation.ownership import AutomationOwnershipLease, OwnershipState

    connection = FakeLeaseConnection(lock_result=False)
    engine = FakeLeaseEngine(connection)
    lease = AutomationOwnershipLease(cast(AsyncEngine, engine))

    assert await _acquire_or_none(lease) is False, "contended lease must return false"

    assert connection.close_calls == 1
    assert lease.backend_pid is None
    assert lease.state is OwnershipState.UNOWNED
    assert lease._watcher is None


async def test_pid_change_marks_lost_invalidates_and_never_reacquires() -> None:
    from brain_v42.automation.ownership import (
        AutomationOwnershipLease,
        OwnershipLostError,
        OwnershipState,
    )

    connection = FakeLeaseConnection(pid_results=[4101, 9999])
    engine = FakeLeaseEngine(connection)
    lease = AutomationOwnershipLease(
        cast(AsyncEngine, engine),
        heartbeat_interval=0.001,
        heartbeat_timeout=0.1,
    )
    assert await _acquire_or_none(lease) is True, "lease must start its PID watcher"

    assert await _event_within(lease.ownership_lost), "watcher must detect backend PID change"
    with pytest.raises(OwnershipLostError):
        lease.ensure_owned()

    assert lease.state is OwnershipState.LOST
    assert sum("pg_try_advisory_lock" in sql for sql, _ in connection.statements) == 1
    assert await _predicate_within(lambda: connection.invalidate_calls == 1), (
        "watcher must sanitize the lost physical connection"
    )
    assert connection.invalidate_calls == 1
    assert connection.close_calls == 1
    await lease.release()


async def test_release_waits_for_watcher_sanitization_even_when_connection_is_already_none() -> (
    None
):
    from brain_v42.automation.ownership import AutomationOwnershipLease, OwnershipState

    connection = FakeLeaseConnection()
    engine = FakeLeaseEngine(connection)
    lease = AutomationOwnershipLease(cast(AsyncEngine, engine))
    sanitizer_may_finish = asyncio.Event()
    watcher_cancelled = asyncio.Event()

    async def slow_sanitizer() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            watcher_cancelled.set()
            await sanitizer_may_finish.wait()

    lease._state = OwnershipState.LOST
    lease._connection = None
    lease._watcher = asyncio.create_task(slow_sanitizer())
    release_task = asyncio.create_task(lease.release())
    try:
        assert await _event_within(watcher_cancelled)
        await asyncio.sleep(0)
        assert not release_task.done(), "release must await the in-flight watcher sanitizer"
        sanitizer_may_finish.set()
        await release_task
        assert lease.state is OwnershipState.RELEASED
    finally:
        sanitizer_may_finish.set()
        if not release_task.done():
            release_task.cancel()
        await asyncio.gather(release_task, return_exceptions=True)
        watcher = lease._watcher
        if watcher is not None and not watcher.done():
            watcher.cancel()
        if watcher is not None:
            await asyncio.gather(watcher, return_exceptions=True)


async def test_cancellation_during_unlock_invalidates_and_closes_before_propagation() -> None:
    from brain_v42.automation.ownership import AutomationOwnershipLease

    connection = FakeLeaseConnection()
    connection.unlock_started = asyncio.Event()
    connection.block_unlock = asyncio.Event()
    engine = FakeLeaseEngine(connection)
    lease = AutomationOwnershipLease(
        cast(AsyncEngine, engine),
        heartbeat_interval=3600,
    )
    assert await _acquire_or_none(lease) is True, "lease must acquire before unlock"
    release_task = asyncio.create_task(lease.release())
    assert await _event_within(connection.unlock_started)

    release_task.cancel()
    result = (await asyncio.gather(release_task, return_exceptions=True))[0]

    assert isinstance(result, asyncio.CancelledError)
    assert connection.invalidate_calls == 1
    assert connection.close_calls == 1


async def test_release_is_idempotent_after_success() -> None:
    from brain_v42.automation.ownership import AutomationOwnershipLease

    connection = FakeLeaseConnection()
    lease = AutomationOwnershipLease(
        cast(AsyncEngine, FakeLeaseEngine(connection)),
        heartbeat_interval=3600,
    )
    assert await _acquire_or_none(lease) is True, "lease must acquire before idempotent release"

    await lease.release()
    await lease.release()

    assert sum("pg_advisory_unlock" in sql for sql, _ in connection.statements) == 1
    assert connection.close_calls == 1


async def test_release_during_inflight_heartbeat_sanitizes_instead_of_unlocking() -> None:
    from brain_v42.automation.ownership import AutomationOwnershipLease, OwnershipState

    connection = FakeLeaseConnection()
    connection.heartbeat_started = asyncio.Event()
    connection.block_heartbeat = asyncio.Event()
    lease = AutomationOwnershipLease(
        cast(AsyncEngine, FakeLeaseEngine(connection)),
        heartbeat_interval=0.0,
        heartbeat_timeout=1,
    )
    assert await lease.acquire() is True
    assert await _event_within(connection.heartbeat_started)

    await lease.release()

    assert sum("pg_advisory_unlock" in sql for sql, _ in connection.statements) == 0
    assert connection.invalidate_calls == 1
    assert connection.close_calls == 1
    assert lease.state is OwnershipState.RELEASED


async def test_owned_resolver_rechecks_lease_after_database_read() -> None:
    from brain_v42.automation.ownership import OwnedProjectKeyResolver, OwnershipLostError

    ownership = MagicMock()
    ownership.ensure_owned.side_effect = [None, OwnershipLostError("lost during read")]
    inner = AsyncMock(return_value="brain-v42")
    resolver = OwnedProjectKeyResolver(inner, ownership)

    result = (await asyncio.gather(resolver("hawkixs/brain_v42"), return_exceptions=True))[0]

    assert isinstance(result, OwnershipLostError)
    assert "lost during read" in str(result)
    inner.assert_awaited_once_with("hawkixs/brain_v42")
    assert ownership.ensure_owned.call_count == 2


async def test_owned_ingestor_checks_lease_immediately_before_mutation() -> None:
    from brain_v42.automation.ownership import OwnedGitLabIngestor, OwnershipLostError

    ownership = MagicMock()
    ownership.ensure_owned.side_effect = OwnershipLostError("lost before mutation")
    inner = AsyncMock()
    ingestor = OwnedGitLabIngestor(inner, ownership)

    result = (
        await asyncio.gather(
            ingestor.process_event({"object_kind": "push"}, "event-1", "brain-v42"),
            return_exceptions=True,
        )
    )[0]

    assert isinstance(result, OwnershipLostError)
    assert "lost before mutation" in str(result)
    inner.process_event.assert_not_awaited()


class OrderedCloser:
    def __init__(self, name: str, order: list[str], *, error: Exception | None = None) -> None:
        self._name = name
        self._order = order
        self._error = error
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self._order.append(self._name)
        if self._error is not None:
            raise self._error


class OrderedDisposer:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1
        self._order.append("engine")


def _runtime_resources(
    order: list[str],
    *,
    acquired: bool = True,
    bind_error: OSError | None = None,
    embedding_error: Exception | None = None,
):
    from brain_v42.automation.ownership import AutomationOwnershipLease
    from brain_v42.automation.runtime import AutomationResources
    from brain_v42.automation.server import AutomationServer

    lease = MagicMock(spec=AutomationOwnershipLease)
    lease.ownership_lost = asyncio.Event()

    async def acquire() -> bool:
        order.append("lease-acquire")
        return acquired

    async def release() -> None:
        order.append("lease")

    lease.acquire = AsyncMock(side_effect=acquire)
    lease.release = AsyncMock(side_effect=release)
    server = MagicMock(spec=AutomationServer)
    server._runner = None

    async def start() -> None:
        order.append("bind")
        if bind_error is not None:
            raise bind_error
        server._runner = object()

    async def stop() -> None:
        order.append("server")
        server._runner = None

    server.start = AsyncMock(side_effect=start)
    server.stop = AsyncMock(side_effect=stop)
    embedding = OrderedCloser("embedding", order, error=embedding_error)
    reranker = OrderedCloser("reranker", order)
    engine = OrderedDisposer(order)
    resources = AutomationResources(
        engine=engine,
        session_factory=cast(object, MagicMock()),  # type: ignore[arg-type]
        lease=lease,
        embedding_svc=embedding,
        reranker=reranker,
        dedup_job=cast(object, MagicMock()),  # type: ignore[arg-type]
        server=server,
    )
    return resources, lease, server, embedding, reranker, engine


async def test_automation_runtime_binds_before_scheduler_and_cleans_up_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.automation.runtime as runtime_module

    order: list[str] = []
    resources, lease, server, embedding, reranker, engine = _runtime_resources(order)
    scheduler_started = asyncio.Event()

    async def scheduler(*_args: object, **_kwargs: object) -> None:
        order.append("dedup-start")
        scheduler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            order.append("dedup")
            raise

    monkeypatch.setattr(runtime_module, "run_dedup_loop", scheduler, raising=False)
    stop = asyncio.Event()
    runtime = runtime_module.AutomationRuntime(resources, dedup_interval=17)
    task = asyncio.create_task(runtime.run(stop))
    assert await _event_within(scheduler_started), "scheduler must start after successful bind"
    stop.set()
    result = (await asyncio.gather(task, return_exceptions=True))[0]

    assert result == 0
    assert order == [
        "lease-acquire",
        "bind",
        "dedup-start",
        "dedup",
        "server",
        "embedding",
        "reranker",
        "lease",
        "engine",
    ]
    server.stop.assert_awaited_once()
    lease.release.assert_awaited_once()
    assert embedding.close_calls == reranker.close_calls == engine.dispose_calls == 1


async def test_automation_bind_failure_is_nonzero_and_cleans_every_resource_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.automation.runtime as runtime_module

    order: list[str] = []
    resources, lease, server, embedding, reranker, engine = _runtime_resources(
        order,
        bind_error=OSError("address in use"),
    )
    scheduler = AsyncMock()
    monkeypatch.setattr(runtime_module, "run_dedup_loop", scheduler, raising=False)

    result = await runtime_module.AutomationRuntime(resources, dedup_interval=17).run(
        asyncio.Event()
    )

    assert result != 0
    scheduler.assert_not_awaited()
    assert order == [
        "lease-acquire",
        "bind",
        "server",
        "embedding",
        "reranker",
        "lease",
        "engine",
    ]
    server.stop.assert_awaited_once()
    lease.release.assert_awaited_once()
    assert embedding.close_calls == reranker.close_calls == engine.dispose_calls == 1


async def test_automation_conflict_is_nonzero_without_bind_or_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.automation.runtime as runtime_module

    order: list[str] = []
    resources, lease, server, embedding, reranker, engine = _runtime_resources(
        order,
        acquired=False,
    )
    scheduler = AsyncMock()
    monkeypatch.setattr(runtime_module, "run_dedup_loop", scheduler, raising=False)

    result = await runtime_module.AutomationRuntime(resources, dedup_interval=17).run(
        asyncio.Event()
    )

    assert result != 0
    server.start.assert_not_awaited()
    scheduler.assert_not_awaited()
    lease.release.assert_awaited_once()
    assert embedding.close_calls == reranker.close_calls == engine.dispose_calls == 1


async def test_automation_lease_loss_cancels_scheduler_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.automation.runtime as runtime_module

    order: list[str] = []
    resources, lease, _server, _embedding, _reranker, _engine = _runtime_resources(order)
    scheduler_started = asyncio.Event()

    async def scheduler(*_args: object, **_kwargs: object) -> None:
        scheduler_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime_module, "run_dedup_loop", scheduler, raising=False)
    runtime = runtime_module.AutomationRuntime(resources, dedup_interval=17)
    task = asyncio.create_task(runtime.run(asyncio.Event()))
    assert await _event_within(scheduler_started)
    lease.ownership_lost.set()

    result = (await asyncio.gather(task, return_exceptions=True))[0]

    assert result != 0
    lease.release.assert_awaited_once()


async def test_normal_shutdown_surfaces_cleanup_error_after_attempting_later_closers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.automation.runtime as runtime_module

    order: list[str] = []
    resources, lease, server, embedding, reranker, engine = _runtime_resources(
        order,
        embedding_error=RuntimeError("embedding close failed"),
    )

    async def scheduler(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime_module, "run_dedup_loop", scheduler, raising=False)
    stop = asyncio.Event()
    stop.set()

    result = (
        await asyncio.gather(
            runtime_module.AutomationRuntime(resources, dedup_interval=17).run(stop),
            return_exceptions=True,
        )
    )[0]

    assert isinstance(result, runtime_module.AutomationCleanupError)
    assert "embedding close failed" in str(result)
    server.stop.assert_awaited_once()
    lease.release.assert_awaited_once()
    assert embedding.close_calls == reranker.close_calls == engine.dispose_calls == 1
    assert order[-4:] == ["embedding", "reranker", "lease", "engine"]
