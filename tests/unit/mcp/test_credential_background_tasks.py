"""No-database lifecycle and LISTEN recovery tests for credential HTTP mode."""

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, Mock

import pytest

from brain_v42.mcp import server
from tests.unit.mcp.test_credentials_http_wiring import settings


@pytest.mark.parametrize("mode", ["credentials", "shared_token"])
async def test_lifespan_owns_all_credential_tasks(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    started: set[str] = set()
    cancelled: set[str] = set()
    tasks: list[asyncio.Task] = []

    async def run(name: str) -> None:
        tasks.append(asyncio.current_task())
        started.add(name)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(name)

    verifier = Mock(
        refresh=AsyncMock(return_value=True),
        run_refresh_loop=lambda: run("refresh"),
        notify=AsyncMock(),
        run_listener_reconnected=AsyncMock(),
    )
    drainer = Mock(run=lambda stop: run("audit"))
    drainer_factory = Mock(return_value=drainer)
    listener_factory = Mock(
        side_effect=lambda dsn, channel, **kwargs: Mock(run=lambda: run(channel))
    )
    monkeypatch.setattr(server, "CredentialListener", listener_factory, raising=False)
    monkeypatch.setattr(server, "AuditDrainer", drainer_factory, raising=False)
    monkeypatch.setattr(server, "get_session_factory", Mock())
    for name in ("dispose_engine", "close_neo4j_driver", "close_activity_reporter"):
        monkeypatch.setattr(server, name, AsyncMock())
    config = settings()
    config.brain_mcp_auth_mode = mode
    config.decay_enabled = False
    services = {"access_logger": None, "neo4j_driver": None, "credential_verifier": verifier}
    async with server.app_lifecycle(config, services, None):
        for _ in range(20):
            await asyncio.sleep(0)
        expected = {"refresh", "audit", "brain_client_credentials", "brain_credential_audit"}
        assert started == (expected if mode == "credentials" else set())
        if mode == "credentials":
            calls = {call.args[1]: call.kwargs for call in listener_factory.call_args_list}
            assert calls["brain_client_credentials"]["on_notification"] == verifier.notify
            assert (
                calls["brain_client_credentials"]["on_connect"] == verifier.run_listener_reconnected
            )
            assert calls["brain_credential_audit"]["on_notification"] == drainer.wake
    assert cancelled == started
    assert all(task.done() for task in tasks)
    if mode == "shared_token":
        listener_factory.assert_not_called()
        drainer_factory.assert_not_called()


class FakeConnection:
    def __init__(self) -> None:
        self.notifications: dict[str, Callable] = {}
        self.termination: Callable | None = None
        self.closed = False
        self.ready = asyncio.Event()

    async def add_listener(self, channel: str, callback: Callable) -> None:
        self.notifications[channel] = callback
        self.ready.set()

    def add_termination_listener(self, callback: Callable) -> None:
        self.termination = callback

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    def disconnect(self) -> None:
        self.closed = True
        assert self.termination is not None
        self.termination(self)

    def notify(self, channel: str) -> None:
        self.notifications[channel](self, 123, channel, "ignored payload")


async def wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def test_listener_refreshes_after_each_connect_and_notification() -> None:
    from brain_v42.credentials.listener import CredentialListener

    first, second = FakeConnection(), FakeConnection()
    connect = AsyncMock(side_effect=[first, second])
    refresh = AsyncMock()
    notify = AsyncMock()
    sleep = AsyncMock()
    listener = CredentialListener(
        "postgresql+asyncpg://localhost/unused",
        "brain_client_credentials",
        on_notification=notify,
        on_connect=refresh,
        connect=connect,
        sleep=sleep,
    )
    task = asyncio.create_task(listener.run())
    try:
        await wait_until(lambda: refresh.await_count == 1)
        first.notify("brain_client_credentials")
        await wait_until(lambda: notify.await_count == 1)
        first.disconnect()
        await wait_until(lambda: refresh.await_count == 2)
        second.notify("brain_client_credentials")
        await wait_until(lambda: notify.await_count == 2)
        assert sleep.await_args.args[0] > 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert first.closed and second.closed


async def test_audit_notification_wakes_drainer_before_poll_deadline() -> None:
    from brain_v42.credentials.audit import AuditDrainer
    from brain_v42.credentials.listener import CredentialListener
    from tests.unit.mcp.test_credentials_http import NOW

    connection = FakeConnection()
    drainer = AuditDrainer(Mock(), clock=lambda: NOW, poll_interval=3600)
    drainer.drain_once = AsyncMock(return_value=0)
    stop = asyncio.Event()
    drain_task = asyncio.create_task(drainer.run(stop))
    listener = CredentialListener(
        "postgresql+asyncpg://localhost/unused",
        "brain_credential_audit",
        on_notification=drainer.wake,
        connect=AsyncMock(return_value=connection),
    )
    listen_task = asyncio.create_task(listener.run())
    try:
        await connection.ready.wait()
        await wait_until(lambda: drainer.drain_once.await_count == 1)
        connection.notify("brain_credential_audit")
        await wait_until(lambda: drainer.drain_once.await_count == 2)
    finally:
        stop.set()
        listen_task.cancel()
        await asyncio.gather(listen_task, drain_task, return_exceptions=True)
    assert connection.closed


async def test_listener_retries_with_bounded_backoff_and_can_be_cancelled() -> None:
    from brain_v42.credentials.listener import CredentialListener

    delays: list[float] = []
    saturated = asyncio.Event()

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 10:
            saturated.set()
            await asyncio.Event().wait()

    listener = CredentialListener(
        "postgresql+asyncpg://localhost/unused",
        "brain_client_credentials",
        on_notification=AsyncMock(),
        connect=AsyncMock(side_effect=OSError("unavailable")),
        sleep=sleep,
    )
    task = asyncio.create_task(listener.run())
    try:
        await asyncio.wait_for(saturated.wait(), 1)
        assert delays[:3] == [1.0, 2.0, 4.0]
        assert all(0 < delay <= 30 for delay in delays)
        assert delays[-1] == 30
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
