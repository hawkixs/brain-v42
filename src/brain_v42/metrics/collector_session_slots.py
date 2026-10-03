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
        async with self._session_factory() as session:
            return await session_slots_block(session)
