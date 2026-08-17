"""Bounded-queue access logger — fire-and-forget, batch consumer."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import access_log
from brain_v42.provenance import get_current_actor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 50
_FLUSH_INTERVAL = 5.0  # seconds


class AccessLogger:
    """Enqueues access events and flushes them in batches."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        max_queue_size: int = 1000,
    ) -> None:
        self._session_factory = session_factory
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue_size)
        self._task: asyncio.Task[None] | None = None

    def log_access(self, entity_type: str, entity_id: UUID, access_type: str) -> None:
        """Enqueue an access event. Logs warning and drops if queue is full.

        L'acteur est lu ICI, dans le contexte de la requête. `_flush_batch`
        tourne dans une tâche de fond où le ContextVar vaudrait `unknown`.
        """
        try:
            self._queue.put_nowait(
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "access_type": access_type,
                    "actor": get_current_actor(),
                }
            )
        except asyncio.QueueFull:
            logger.warning(
                "access_logger.queue_full",
                entity_type=entity_type,
                entity_id=str(entity_id),
            )

    async def start(self) -> None:
        """Start the background consumer loop."""
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the consumer, flush remaining events."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Flush remaining
        if not self._queue.empty():
            await self._flush_batch()

    async def _run_loop(self) -> None:
        """Consumer loop: flush every N seconds or when batch is full."""
        while True:
            try:
                await asyncio.sleep(_FLUSH_INTERVAL)
                if not self._queue.empty():
                    await self._flush_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("access_logger.run_loop_error")

    async def _flush_batch(self) -> None:
        """Drain queue and batch-insert into access_log."""
        events: list[dict[str, Any]] = []
        while not self._queue.empty() and len(events) < _BATCH_SIZE:
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not events:
            return

        try:
            async with self._session_factory() as session:
                await session.execute(sa.insert(access_log), events)
                await session.commit()
            logger.debug("access_logger.flushed", count=len(events))
        except Exception:
            logger.warning("access_logger.flush_failed", count=len(events), exc_info=True)
