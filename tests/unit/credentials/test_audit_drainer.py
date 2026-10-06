"""Exercise delivery failures and wake/poll scheduling without PostgreSQL or sleeps."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from brain_v42.repositories.pg_client_credentials import AuditRow
from tests.unit.credentials.test_audit_event_shape import NOW, elevation_row


class FakeRepo:
    """Stage emission marks until commit so retries can expose transaction mistakes."""

    def __init__(self, rows: list[AuditRow]) -> None:
        self.rows = rows
        self.history: list[str] = []
        self.session = cast(AsyncSession, object())
        self.marked: set[int] = set()
        self.fail_mark = False
        self.expire_failures = 0
        self.claim_limits: list[int] = []
        self.times: list[datetime] = []
        self.committed = asyncio.Event()
        self.on_expire: Callable[[], None] | None = None

    @asynccontextmanager
    async def transaction(self, session: AsyncSession | None = None) -> AsyncIterator[AsyncSession]:
        before = self.marked.copy()
        self.history.append("begin")
        try:
            yield self.session
        except BaseException:
            self.marked = before
            self.history.append("rollback")
            raise
        else:
            self.history.append("commit")
            self.committed.set()

    async def audit_expired_elevations(
        self, now: datetime, *, session: AsyncSession | None = None
    ) -> int:
        assert session is self.session
        self.history.append("expire")
        self.times.append(now)
        if self.on_expire is not None:
            self.on_expire()
        if self.expire_failures:
            self.expire_failures -= 1
            raise RuntimeError("test expiry failed")
        return 0

    async def claim_unemitted_audit(self, limit: int, *, session: AsyncSession) -> list[AuditRow]:
        assert session is self.session
        self.history.append("claim")
        self.claim_limits.append(limit)
        return [row for row in self.rows if row.id not in self.marked][:limit]

    async def mark_audit_emitted(
        self, ids: Sequence[int], now: datetime, *, session: AsyncSession | None = None
    ) -> int:
        assert session is self.session
        self.history.append("mark")
        self.times.append(now)
        self.marked.update(ids)
        if self.fail_mark:
            raise RuntimeError("test mark failed")
        return len(ids)


async def test_one_transaction_orders_expire_claim_log_mark_then_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.credentials import audit

    row = elevation_row()
    repo = FakeRepo([row])
    warning = audit.logger.warning

    def record_warning(event: str, **fields: Any) -> None:
        repo.history.append("log")
        warning(event, **fields)

    monkeypatch.setattr(audit.logger, "warning", record_warning)
    with capture_logs() as logs:
        emitted = await audit.AuditDrainer(repo, clock=lambda: NOW, batch_size=7).drain_once()
    assert emitted == 1
    assert repo.history == ["begin", "expire", "claim", "log", "mark", "commit"]
    assert repo.marked == {row.id}
    assert repo.claim_limits == [7]
    assert repo.times == [NOW, NOW]
    assert logs[0]["elevation_id"] == str(row.elevation_id)


def _fail_second_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    from brain_v42.credentials import audit

    original = audit.logger.warning
    calls = 0

    def warning(event: str, **fields: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("test log sink failed")
        original(event, **fields)

    monkeypatch.setattr(audit.logger, "warning", warning)


def _two_rows() -> list[AuditRow]:
    first = elevation_row()
    second = elevation_row("credentials.unelevated")
    return [first, AuditRow(2, second.event, second.elevation_id, second.payload, NOW, None)]


async def test_logging_failure_after_the_first_row_rolls_back_without_marking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.credentials.audit import AuditDrainer

    repo = FakeRepo(_two_rows())
    _fail_second_warning(monkeypatch)
    with capture_logs() as logs, pytest.raises(RuntimeError, match="test log sink failed"):
        await AuditDrainer(repo, clock=lambda: NOW).drain_once()
    assert len(logs) == 1
    assert repo.history == ["begin", "expire", "claim", "rollback"]
    assert repo.marked == set()


async def test_redrain_reemits_the_same_elevation_after_a_logging_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.credentials.audit import AuditDrainer

    repo = FakeRepo(_two_rows())
    _fail_second_warning(monkeypatch)
    drainer = AuditDrainer(repo, clock=lambda: NOW)
    with capture_logs() as logs:
        with pytest.raises(RuntimeError, match="test log sink failed"):
            await drainer.drain_once()
        assert await drainer.drain_once() == 2
    assert logs[0]["elevation_id"] == logs[1]["elevation_id"] == str(repo.rows[0].elevation_id)
    assert logs[0]["event"] == logs[1]["event"]
    assert await drainer.drain_once() == 0


async def test_marking_failure_rolls_back_and_reemits_on_retry() -> None:
    from brain_v42.credentials.audit import AuditDrainer

    repo = FakeRepo([elevation_row()])
    repo.fail_mark = True
    drainer = AuditDrainer(repo, clock=lambda: NOW)
    with capture_logs() as logs:
        with pytest.raises(RuntimeError, match="test mark failed"):
            await drainer.drain_once()
        assert repo.marked == set()
        assert repo.history[-1] == "rollback"
        repo.fail_mark = False
        assert await drainer.drain_once() == 1
    assert logs[0] == logs[1]


async def test_transaction_factory_can_be_injected() -> None:
    from brain_v42.credentials.audit import AuditDrainer

    repo = FakeRepo([])
    transactions: list[str] = []

    @asynccontextmanager
    async def transaction() -> AsyncIterator[AsyncSession]:
        transactions.append("begin")
        yield repo.session
        transactions.append("commit")

    assert await AuditDrainer(repo, transaction=transaction, clock=lambda: NOW).drain_once() == 0
    assert transactions == ["begin", "commit"]
    assert "begin" not in repo.history


async def test_wake_drains_before_the_five_second_poll_interval() -> None:
    from brain_v42.credentials.audit import AuditDrainer

    repo = FakeRepo([])
    stop = asyncio.Event()
    drainer = AuditDrainer(repo, clock=lambda: NOW)
    task = asyncio.create_task(drainer.run(stop))
    try:
        await asyncio.wait_for(repo.committed.wait(), timeout=0.1)
        repo.committed.clear()
        drainer.wake()
        await asyncio.wait_for(repo.committed.wait(), timeout=0.1)
        assert repo.history.count("commit") == 2
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=0.1)


async def test_backlog_without_notify_drains_all_full_batches_before_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.credentials.audit import AuditDrainer

    template = elevation_row()
    rows = [
        AuditRow(index, template.event, template.elevation_id, template.payload, NOW, None)
        for index in range(1, 6)
    ]
    repo = FakeRepo(rows)
    stop = asyncio.Event()
    drainer = AuditDrainer(repo, clock=lambda: NOW, batch_size=2)

    async def wait_until_next_poll(stop: asyncio.Event) -> None:
        assert repo.marked == {row.id for row in rows}
        stop.set()

    monkeypatch.setattr(drainer, "_wait_for_wake_or_poll", wait_until_next_poll)
    with capture_logs() as logs:
        await drainer.run(stop)
    assert repo.claim_limits == [2, 2, 2]
    assert repo.history.count("commit") == 3
    assert len(logs) == 5


async def test_stop_during_a_full_batch_prevents_another_drain() -> None:
    from brain_v42.credentials.audit import AuditDrainer

    repo = FakeRepo(_two_rows())
    stop = asyncio.Event()
    repo.on_expire = stop.set
    with capture_logs():
        await asyncio.wait_for(
            AuditDrainer(repo, clock=lambda: NOW, batch_size=1).run(stop), timeout=0.1
        )
    assert repo.marked == {1}
    assert repo.history.count("commit") == 1


@pytest.mark.parametrize(
    ("event", "error"),
    [("credentials.unknown", "ValueError"), ("credentials.revoked", "KeyError")],
)
async def test_poison_row_warns_with_only_the_error_type(event: str, error: str) -> None:
    from brain_v42.credentials.audit import AuditDrainer

    row = AuditRow(41, event, None, {"token": "private-payload-sentinel"}, NOW, None)
    repo = FakeRepo([row])
    stop = asyncio.Event()
    repo.on_expire = stop.set
    with capture_logs() as logs:
        await AuditDrainer(repo, clock=lambda: NOW).run(stop)
    assert repo.history[-1] == "rollback"
    assert repo.marked == set()
    assert logs == [
        {
            "event": "credentials.audit_drain_failed",
            "log_level": "warning",
            "error": error,
            # The id locates the row that blocks the outbox head; it carries no payload.
            "row_id": 41,
        }
    ]


async def test_a_failing_drain_warns_without_payload_and_the_loop_continues() -> None:
    from brain_v42.credentials.audit import AuditDrainer

    repo = FakeRepo([])
    repo.expire_failures = 1
    stop = asyncio.Event()
    drainer = AuditDrainer(repo, clock=lambda: NOW)
    repo.on_expire = drainer.wake
    with capture_logs() as logs:
        task = asyncio.create_task(drainer.run(stop))
        try:
            await asyncio.wait_for(repo.committed.wait(), timeout=0.1)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=0.1)
    assert repo.history[:3] == ["begin", "expire", "rollback"]
    assert repo.history.count("commit") == 1
    assert len(logs) == 1
    assert logs[0]["log_level"] == "warning"
    assert logs[0].keys() == {"event", "log_level", "error"}
    assert logs[0]["error"] == "RuntimeError"
    assert "test expiry failed" not in str(logs)


async def test_a_wake_during_a_drain_is_not_lost() -> None:
    from brain_v42.credentials.audit import AuditDrainer

    repo = FakeRepo([])
    stop = asyncio.Event()
    drainer = AuditDrainer(repo, clock=lambda: NOW)

    def wake_then_stop() -> None:
        if repo.history.count("expire") == 1:
            drainer.wake()
        else:
            stop.set()

    repo.on_expire = wake_then_stop
    await asyncio.wait_for(drainer.run(stop), timeout=0.1)
    assert repo.history.count("commit") == 2


async def test_stop_already_set_does_not_drain() -> None:
    from brain_v42.credentials.audit import AuditDrainer

    repo = FakeRepo([])
    stop = asyncio.Event()
    stop.set()
    await AuditDrainer(repo, clock=lambda: NOW).run(stop)
    assert repo.history == []


async def test_cancelling_the_loop_releases_its_waiters() -> None:
    from brain_v42.credentials.audit import AuditDrainer

    repo = FakeRepo([])
    prior = asyncio.all_tasks()
    task = asyncio.create_task(AuditDrainer(repo, clock=lambda: NOW).run(asyncio.Event()))
    await asyncio.wait_for(repo.committed.wait(), timeout=0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert asyncio.all_tasks() == prior


async def test_a_broken_warning_sink_does_not_stop_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.credentials import audit

    repo = FakeRepo([elevation_row()])
    stop = asyncio.Event()
    drainer = audit.AuditDrainer(repo, clock=lambda: NOW)

    def broken_warning(event: str, **fields: Any) -> None:
        raise RuntimeError("test warning sink failed")

    def wake_then_stop() -> None:
        if repo.history.count("expire") == 1:
            drainer.wake()
        else:
            stop.set()

    monkeypatch.setattr(audit.logger, "warning", broken_warning)
    repo.on_expire = wake_then_stop
    await asyncio.wait_for(drainer.run(stop), timeout=0.1)
    assert repo.history.count("rollback") == 2
    assert repo.marked == set()
