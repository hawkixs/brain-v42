"""Recover LISTEN subscriptions without trusting payloads or delivery history."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

import asyncpg
import structlog
from sqlalchemy.engine import make_url

logger = structlog.get_logger(__name__)


class _Connection(Protocol):
    async def add_listener(self, channel: str, callback: Callable[..., None]) -> None: ...

    def add_termination_listener(self, callback: Callable[..., None]) -> None: ...

    def is_closed(self) -> bool: ...

    async def close(self) -> None: ...


async def _connect(dsn: str) -> _Connection:
    return cast(_Connection, await asyncpg.connect(dsn, timeout=10))


class CredentialListener:
    """Coalesce notifications and fully reload after subscribing on every reconnect.

    A dedicated connection owns LISTEN, never a pooled transaction. Cancellation
    closes it; retries expose only the exception type, never a DSN or payload.
    """

    def __init__(
        self,
        dsn: str,
        channel: str,
        *,
        on_notification: Callable[[], Awaitable[Any] | None],
        on_connect: Callable[[], Awaitable[Any] | None] | None = None,
        connect: Callable[[str], Awaitable[_Connection]] = _connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if channel not in {"brain_client_credentials", "brain_credential_audit"}:
            raise ValueError("unsupported credential notification channel")
        self._dsn = make_url(dsn).set(drivername="postgresql").render_as_string(hide_password=False)
        self._channel = channel
        self._on_notification = on_notification
        self._on_connect = on_connect
        self._connect = connect
        self._sleep = sleep

    async def run(self) -> None:
        delay = 1.0
        while True:
            connection: _Connection | None = None
            wake = asyncio.Event()
            try:
                connection = await self._connect(self._dsn)
                connection.add_termination_listener(lambda *_, event=wake: event.set())
                await connection.add_listener(self._channel, lambda *_, event=wake: event.set())
                # Subscribe FIRST, then reload changes missed while disconnected.
                if self._on_connect is not None:
                    await self._invoke(self._on_connect)
                delay = 1.0
                while not connection.is_closed():
                    await wake.wait()
                    wake.clear()
                    if not connection.is_closed():
                        await self._invoke(self._on_notification)
            except Exception as exc:
                logger.warning(
                    "credentials.listener_failed", channel=self._channel, error=type(exc).__name__
                )
            finally:
                if connection is not None:
                    await connection.close()
            await self._sleep(delay)
            delay = min(delay * 2, 30.0)

    @staticmethod
    async def _invoke(callback: Callable[[], Awaitable[Any] | None]) -> None:
        result = callback()
        if inspect.isawaitable(result):
            await result
