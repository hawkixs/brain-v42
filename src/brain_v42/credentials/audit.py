"""Emit the credential outbox at least once, with a closed watcher contract.

Logging cannot commit atomically with PostgreSQL. Consumers deduplicate elevations
on ``(event, elevation_id)`` and credential gestures on ``(event, credential_id)``.
"""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.credentials.redact import sanitize_label
from brain_v42.repositories.pg_client_credentials import AuditRow

logger = structlog.get_logger(__name__)

ELEVATION_EVENT_KEYS = frozenset(
    {
        "elevation_id",
        "session_id",
        "session_label",
        "expires_at",
        "ttl_seconds",
        "reason",
        "via",
        "client_id",
        "connection_count",
        "excluded_client_ids",
        "excluded_connection_count",
    }
)
_ELEVATION_EVENTS = frozenset(
    {"credentials.elevated", "credentials.unelevated", "credentials.elevation_expired"}
)
_ISSUED_KEYS = frozenset({"credential_id", "client_id", "families", "author"})
_REVOKED_KEYS = _ISSUED_KEYS | {"reason"}


def _sanitize_reason(value: str) -> str:
    """Bound reasons and replace control characters unsafe for watchers."""
    return "".join(" " if unicodedata.category(char) == "Cc" else char for char in value[:200])


def render_event(row: AuditRow) -> dict[str, Any]:
    """Allowlist fields so stored metadata cannot expand the public event contract."""
    if row.event in _ELEVATION_EVENTS:
        fields = {key: row.payload[key] for key in ELEVATION_EVENT_KEYS - {"excluded_client_ids"}}
        fields["session_label"] = sanitize_label(fields["session_label"])
        fields["reason"] = _sanitize_reason(fields["reason"])
        fields["excluded_client_ids"] = sorted(set(row.payload.get("excluded_client_ids", [])))
    elif row.event == "credentials.issued":
        fields = {key: row.payload[key] for key in _ISSUED_KEYS}
    elif row.event == "credentials.revoked":
        fields = {key: row.payload[key] for key in _REVOKED_KEYS}
        fields["reason"] = _sanitize_reason(fields["reason"])
    else:
        raise ValueError("unsupported credential audit event")
    return {"event": row.event, **fields}


class _AuditRepository(Protocol):
    """Keep the drainer independent of connection setup and fakeable without a DB."""

    def transaction(
        self, session: AsyncSession | None = None
    ) -> AbstractAsyncContextManager[AsyncSession]: ...

    async def audit_expired_elevations(
        self, now: datetime, *, session: AsyncSession | None = None
    ) -> int: ...

    async def claim_unemitted_audit(
        self, limit: int, *, session: AsyncSession
    ) -> list[AuditRow]: ...

    async def mark_audit_emitted(
        self, ids: Sequence[int], now: datetime, *, session: AsyncSession | None = None
    ) -> int: ...


class AuditDrainer:
    """Leave rows unmarked on any failure so a later drain can deliver them again.

    The repository's injected session factory normally owns the transaction. An
    explicit transaction callable also permits composition through its existing
    savepoint pattern. ``clock`` must return an aware UTC datetime.
    """

    def __init__(
        self,
        repository: _AuditRepository,
        *,
        clock: Callable[[], datetime],
        transaction: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None,
        poll_interval: float = 5.0,
        batch_size: int = 100,
    ) -> None:
        self._repository = repository
        self._transaction = transaction if transaction is not None else repository.transaction
        self._clock = clock
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._wake = asyncio.Event()

    async def drain_once(self) -> int:
        """Hold the claim locks until all log writes and emission marks commit."""
        async with self._transaction() as session:
            now = self._clock()
            await self._repository.audit_expired_elevations(now, session=session)
            rows = await self._repository.claim_unemitted_audit(self._batch_size, session=session)
            for row in rows:
                fields = render_event(row)
                event_name = fields.pop("event")
                logger.warning(event_name, **fields)
            await self._repository.mark_audit_emitted(
                [row.id for row in rows], now, session=session
            )
        return len(rows)

    def wake(self) -> None:
        """Coalesce NOTIFY bursts while retaining notifications received during a drain."""
        self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        """Poll even without NOTIFY, retrying failures without exposing their payload."""
        while not stop.is_set():
            self._wake.clear()
            try:
                if await self.drain_once() >= self._batch_size:
                    continue
            except Exception as exc:
                try:
                    logger.warning("credentials.audit_drain_failed", error=type(exc).__name__)
                except Exception:
                    # A broken sink must not turn a retryable drain into a dead loop.
                    pass
            if not stop.is_set():
                await self._wait_for_wake_or_poll(stop)

    async def _wait_for_wake_or_poll(self, stop: asyncio.Event) -> None:
        waiters = (asyncio.create_task(self._wake.wait()), asyncio.create_task(stop.wait()))
        try:
            await asyncio.wait(
                waiters,
                timeout=self._poll_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
