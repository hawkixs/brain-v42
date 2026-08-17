"""PostgreSQL-backed single-owner lease for automation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Protocol

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = structlog.get_logger(__name__)


class OwnershipLostError(RuntimeError):
    """Raised when automation work is attempted without a healthy lease."""


class OwnershipState(Enum):
    """Observable lifecycle of the automation lease."""

    UNOWNED = "unowned"
    OWNED = "owned"
    LOST = "lost"
    RELEASED = "released"


class OwnershipGate(Protocol):
    """Admission gate shared by webhook and scheduler work."""

    def ensure_owned(self) -> None: ...


class GitLabEventProcessor(Protocol):
    """Typed GitLab mutation boundary."""

    async def process_event(
        self,
        payload: dict[str, object],
        event_uuid: str,
        project_key: str,
    ) -> dict[str, object]: ...


ProjectKeyResolver = Callable[[str], Awaitable[str | None]]


class OwnedProjectKeyResolver:
    """Lease-aware project resolver."""

    def __init__(self, inner: ProjectKeyResolver, ownership: OwnershipGate) -> None:
        self._inner = inner
        self._ownership = ownership

    async def __call__(self, gitlab_path: str) -> str | None:
        self._ownership.ensure_owned()
        project_key = await self._inner(gitlab_path)
        self._ownership.ensure_owned()
        return project_key


class OwnedGitLabIngestor:
    """Lease-aware final admission gate before event mutation."""

    def __init__(self, inner: GitLabEventProcessor, ownership: OwnershipGate) -> None:
        self._inner = inner
        self._ownership = ownership

    async def process_event(
        self,
        payload: dict[str, object],
        event_uuid: str,
        project_key: str,
    ) -> dict[str, object]:
        self._ownership.ensure_owned()
        return await self._inner.process_event(payload, event_uuid, project_key)


class AutomationOwnershipLease:
    """Dedicated-session advisory lock with bounded loss detection."""

    LOCK_KEY = 4_151_019_227_643_017_711

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        heartbeat_interval: float = 1.0,
        heartbeat_timeout: float = 1.0,
    ) -> None:
        self._engine = engine
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout
        self._connection: AsyncConnection | None = None
        self._backend_pid: int | None = None
        self._state = OwnershipState.UNOWNED
        self._ownership_lost = asyncio.Event()
        self._watcher: asyncio.Task[None] | None = None
        self._release_lock = asyncio.Lock()

    @property
    def state(self) -> OwnershipState:
        return self._state

    @property
    def ownership_lost(self) -> asyncio.Event:
        return self._ownership_lost

    @property
    def backend_pid(self) -> int | None:
        return self._backend_pid

    def ensure_owned(self) -> None:
        if self._state is not OwnershipState.OWNED:
            raise OwnershipLostError(f"automation ownership is {self._state.value}")

    async def acquire(self) -> bool:
        if self._state is not OwnershipState.UNOWNED:
            raise OwnershipLostError("automation lease cannot be reacquired before restart")

        connection = await self._engine.connect()
        try:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            backend_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": self.LOCK_KEY},
            )
        except BaseException:
            await self._invalidate_and_close(connection)
            raise

        if not bool(acquired):
            await connection.close()
            return False

        self._connection = connection
        self._backend_pid = int(backend_pid)
        self._state = OwnershipState.OWNED
        self._watcher = asyncio.create_task(
            self._watch_connection(),
            name="automation-ownership-watcher",
        )
        return True

    async def release(self) -> None:
        async with self._release_lock:
            if self._state is OwnershipState.RELEASED:
                return

            await self._cancel_and_wait_watcher()
            connection = self._connection
            self._connection = None

            if connection is None:
                self._state = OwnershipState.RELEASED
                return

            if self._state is not OwnershipState.OWNED:
                await self._invalidate_and_close_protected(connection)
                self._state = OwnershipState.RELEASED
                return

            try:
                unlocked = await connection.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": self.LOCK_KEY},
                )
            except asyncio.CancelledError:
                await self._invalidate_and_close_protected(connection)
                self._state = OwnershipState.RELEASED
                raise
            except BaseException:
                await self._invalidate_and_close_protected(connection)
                self._state = OwnershipState.RELEASED
                raise

            if not bool(unlocked):
                await self._invalidate_and_close_protected(connection)
                self._state = OwnershipState.RELEASED
                raise OwnershipLostError("advisory unlock returned false")

            try:
                await connection.close()
            except asyncio.CancelledError:
                await self._invalidate_and_close_protected(connection)
                self._state = OwnershipState.RELEASED
                raise
            self._state = OwnershipState.RELEASED

    async def _watch_connection(self) -> None:
        try:
            while self._state is OwnershipState.OWNED:
                await asyncio.sleep(self._heartbeat_interval)
                connection = self._connection
                if connection is None or bool(getattr(connection, "invalidated", False)):
                    await self._mark_lost()
                    return
                try:
                    backend_pid = await asyncio.wait_for(
                        connection.scalar(text("SELECT pg_backend_pid()")),
                        timeout=self._heartbeat_timeout,
                    )
                except asyncio.CancelledError:
                    # Cancelling an asyncpg query can invalidate its physical
                    # connection. Treat an in-flight probe as uncertain rather
                    # than attempting unlock on a possibly broken session.
                    await self._mark_lost()
                    raise
                except BaseException:
                    await self._mark_lost()
                    return
                if int(backend_pid) != self._backend_pid:
                    await self._mark_lost()
                    return
        except asyncio.CancelledError:
            raise

    async def _mark_lost(self) -> None:
        if self._state is not OwnershipState.OWNED:
            return
        self._state = OwnershipState.LOST
        self._ownership_lost.set()
        connection = self._connection
        self._connection = None
        if connection is not None:
            await self._invalidate_and_close_protected(connection)

    async def _cancel_and_wait_watcher(self) -> None:
        watcher = self._watcher
        if watcher is None:
            return
        if watcher is asyncio.current_task():
            return
        if not watcher.done():
            watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        finally:
            self._watcher = None

    async def _invalidate_and_close_protected(self, connection: AsyncConnection) -> None:
        cleanup = asyncio.create_task(self._invalidate_and_close(connection))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    @staticmethod
    async def _invalidate_and_close(connection: AsyncConnection) -> None:
        try:
            await connection.invalidate()
        except BaseException:
            logger.exception("automation_ownership.invalidate_failed")
        try:
            await connection.close()
        except BaseException:
            logger.exception("automation_ownership.close_failed")
