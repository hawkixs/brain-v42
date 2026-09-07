"""Single observer ownership with serialized transactions on the owning backend."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from brain_v42.repositories.pg_delivery import DELIVERY_OBSERVER_LOCK


class ObserverOwnershipLost(RuntimeError):
    """Stable, redacted fatal error: this process must not publish again."""

    def __init__(self) -> None:
        super().__init__("delivery_observer_ownership_lost")


class ObserverOwnership:
    """One non-autocommit connection; every SQL operation shares its mutex.

    A session advisory lock survives the short acquisition transaction. Network
    collection happens outside transaction(), leaving the backend idle. A lost
    backend is permanently fenced; only a new process/owner may acquire again.
    """

    LOCK_KEY = DELIVERY_OBSERVER_LOCK

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._connection: AsyncConnection | None = None
        self._backend_pid: int | None = None
        self._state: Literal["new", "owned", "lost", "released"] = "new"
        self._mutex = asyncio.Lock()
        self.lost = asyncio.Event()

    @property
    def backend_pid(self) -> int | None:
        return self._backend_pid

    @property
    def owned(self) -> bool:
        return self._state == "owned"

    def _lose(self) -> None:
        self._state = "lost"
        self.lost.set()

    @staticmethod
    async def _dispose(connection: AsyncConnection) -> None:
        # Closing the physical dedicated connection also drops any session lock
        # if cancellation or an error prevented a normal unlock transaction.
        with suppress(SQLAlchemyError):
            await connection.invalidate()
        with suppress(SQLAlchemyError):
            await connection.close()

    async def acquire(self) -> bool:
        async with self._mutex:
            if self._state != "new":
                raise ObserverOwnershipLost()
            connection: AsyncConnection | None = None
            try:
                connection = await self._engine.connect()
                connection = await connection.execution_options(isolation_level="READ COMMITTED")
                async with connection.begin():
                    backend_pid = await connection.scalar(sa.text("SELECT pg_backend_pid()"))
                    acquired = await connection.scalar(
                        sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": self.LOCK_KEY}
                    )
                if not acquired:
                    self._state = "released"
                    await self._dispose(connection)
                    return False
                if not isinstance(backend_pid, int):
                    raise ObserverOwnershipLost()
                self._connection, self._backend_pid = connection, backend_pid
                self._state = "owned"
                return True
            except BaseException as error:
                self._lose()
                if connection is not None:
                    await self._dispose(connection)
                if isinstance(error, SQLAlchemyError):
                    raise ObserverOwnershipLost() from None
                raise

    async def _verify(self, connection: AsyncConnection) -> None:
        # Check invalidation before executing: SQLAlchemy could otherwise open a
        # fresh backend transparently and reuse this process's stale authority.
        if not self.owned or connection.invalidated or connection.closed:
            self._lose()
            raise ObserverOwnershipLost()
        try:
            async with asyncio.timeout(5):
                row = (
                    await connection.execute(
                        sa.text(
                            "SELECT pg_backend_pid() AS backend_pid, EXISTS ("
                            "SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted "
                            "AND pid=pg_backend_pid() AND objsubid=1 "
                            "AND classid=((CAST(:key AS bigint) >> 32) & 4294967295)::oid "
                            "AND objid=(CAST(:key AS bigint) & 4294967295)::oid) AS locked"
                        ),
                        {"key": self.LOCK_KEY},
                    )
                ).one()
            if row.backend_pid != self._backend_pid or not row.locked:
                raise ObserverOwnershipLost()
        except (SQLAlchemyError, TimeoutError, ObserverOwnershipLost):
            self._lose()
            raise ObserverOwnershipLost() from None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Yield the sole publisher session; commit only with retained ownership."""
        async with self._mutex:
            if not self.owned or self._connection is None:
                raise ObserverOwnershipLost()
            connection = self._connection
            try:
                if connection.invalidated or connection.closed:
                    self._lose()
                    raise ObserverOwnershipLost()
                async with connection.begin():
                    await self._verify(connection)
                    async with AsyncSession(
                        bind=connection,
                        expire_on_commit=False,
                        join_transaction_mode="rollback_only",
                    ) as session:
                        async with session.begin():
                            yield session
                        await self._verify(connection)
            except SQLAlchemyError:
                self._lose()
                raise ObserverOwnershipLost() from None
            except BaseException:
                if connection.invalidated or connection.closed:
                    self._lose()
                raise

    async def heartbeat(self) -> None:
        """Verify ownership without retaining a transaction during provider waits."""
        async with self.transaction():
            pass

    async def release(self) -> None:
        """Idempotently close the dedicated backend; released owners cannot restart."""
        async with self._mutex:
            connection, self._connection = self._connection, None
            try:
                if connection is not None and self.owned:
                    with suppress(SQLAlchemyError, ObserverOwnershipLost, TimeoutError):
                        async with connection.begin():
                            await self._verify(connection)
                            unlocked = await connection.scalar(
                                sa.text("SELECT pg_advisory_unlock(:key)"), {"key": self.LOCK_KEY}
                            )
                            if not unlocked:
                                self._lose()
            finally:
                if not self.lost.is_set():
                    self._state = "released"
                if connection is not None:
                    cleanup = asyncio.create_task(self._dispose(connection))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
