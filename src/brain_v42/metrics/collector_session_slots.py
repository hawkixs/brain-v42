"""Read-only `session_slots` metrics block (ADR #34 D12)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from brain_v42.repositories.pg_focus_slot import session_slots_block

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _SessionSlotCollectorsMixin:
    """Focus-slot collector mixed into MetricsCollector."""

    if TYPE_CHECKING:
        _session_factory: async_sessionmaker[AsyncSession]

    async def collect_session_slots(self) -> dict[str, Any]:
        """Read the block; exceptions propagate on purpose.

        This collector does not catch its own failures: the slow-block cache
        applies its short error TTL, so a broken read neither hides behind a
        stale value nor is retried on every scrape.
        """
        async with self._session_factory() as session:
            return await session_slots_block(session)
