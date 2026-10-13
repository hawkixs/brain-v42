"""The `programmes/v1` block for the sidecar's `GET /programmes` (contract programmes_v1).

Contract `docs/contracts/programmes_v1.md` (tickets 8a063256, 24c1f172). The shape,
the order and the publication window (live programmes, then the four most recent
done ones of the last seven days) are the repository's: one serializer for the MCP
read and the sidecar, so they cannot disagree.

Routed through `SlowBlockCache` by server.py (`cache.get("programmes", ...)`), which
stamps `generated_at`. Like the tickets collector, this method does NOT catch its own
exceptions: a failed read reaches the cache, which publishes nothing for the short
error TTL instead of memoising a degraded block as a success.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from brain_v42.repositories.pg_programme import PgProgrammeRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _ProgrammeCollectorsMixin:
    """Programme collector mixed into MetricsCollector."""

    # Provided by MetricsCollector.__init__ (declared for type-checkers only).
    if TYPE_CHECKING:
        _session_factory: async_sessionmaker[AsyncSession]

    async def collect_programmes(self) -> dict[str, Any]:
        """The block before the cache stamps `generated_at`; `programmes: []` is published."""
        return await PgProgrammeRepo(self._session_factory).published_block()
