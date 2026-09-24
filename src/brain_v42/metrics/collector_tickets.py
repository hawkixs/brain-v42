"""Per-project ticket counters for the sidecar's `tickets` section (ticket 0fb857ef).

Consumed by red-monitor's tickets tab, which replaces the abandoned roadmap
tab (contract agreed with red-monitor's monitor-5 session, 2026-09-23; their
spec: docs/specs/2026-09-23-tickets-tab-design.md). Categorisation is NOT
re-derived here: ``count_grouped_by_project`` (repositories/pg_ticket.py)
reuses the exact ``_ACTIONABLE``/``_CONFIRMABLE`` predicates already proven
by ``list_grouped``'s own tests -- brain owns the semantics in one place,
red-monitor stays a pure consumer with no status logic of its own.

Routed through the sidecar's ``SlowBlockCache`` by server.py
(``cache.get("tickets", collector.collect_ticket_counts)``): this method
deliberately does NOT catch its own exceptions. A raised exception lets the
cache's own ``except`` clause apply the short ``error_ttl_seconds`` retry
window instead of memoizing a degraded payload for the full ``ttl_seconds``
(slow_block_cache.py names this exact upcoming block in its own docstring).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from brain_v42.repositories.pg_ticket import count_grouped_by_project

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Column order and French labels are BRAIN's decision (contract, ticket
#: 0fb857ef): red-monitor renders whatever it receives, in this order, with
#: no category-specific code of its own -- a new category needs no
#: red-monitor change.
_CATEGORIES: tuple[dict[str, str], ...] = (
    {"key": "todo", "label": "à traiter"},
    {"key": "to_confirm", "label": "à confirmer"},
    {"key": "waiting", "label": "en attente"},
)


class _TicketCollectorsMixin:
    """Ticket-counter collector mixed into MetricsCollector."""

    # Provided by MetricsCollector.__init__ (declared for type-checkers only).
    if TYPE_CHECKING:
        _session_factory: async_sessionmaker[AsyncSession]

    async def collect_ticket_counts(self) -> dict[str, Any]:
        """The `tickets` block, pre-cache (`SlowBlockCache` adds `generated_at`).

        `projects` is sorted most-urgent-first by
        `count_grouped_by_project`'s own `ORDER BY`; a project with nothing
        pending is simply absent from the list rather than a zero-counts row.
        An empty `projects` list means "nothing pending anywhere" -- distinct
        from the whole `tickets` key being absent, which the cache applies on
        failure ("not measured").
        """
        async with self._session_factory() as session:
            rows = await count_grouped_by_project(session)
        return {
            "categories": [dict(category) for category in _CATEGORIES],
            "projects": [
                {
                    "project": row["project"],
                    "counts": {
                        "todo": row["todo"],
                        "to_confirm": row["to_confirm"],
                        "waiting": row["waiting"],
                    },
                }
                for row in rows
            ],
        }
