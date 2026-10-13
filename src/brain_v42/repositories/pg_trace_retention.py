"""Bounded retention for unreferenced agent traces and delivered graph events.

Trace deletion excludes ledger donors because a ``closed_inactive`` trace can still
provide derived capture and the artifact foreign key cascades. Checkpoints,
``focus_slot_history`` and relay children use RESTRICT keys, so their exclusions
are required; slot history and relay checks are also defence in depth because
``brain_sessions_slot_operator_only`` forces those references NULL on agent rows.
Deleting a trace intentionally cascades its ``brain_session_connections``. Agent
sessions cannot own ``brain_admin_elevations``. Supported writers do not add ledger,
checkpoint or history rows to closed traces: deposits target open sessions, absorption
only moves rows away from donors, and closed sessions are never reopened.

Deleting delivered outbox rows is safe: aggregate revisions live in
``brain_entities`` and ``entity_relations``, the projector reads only rows with
``delivered_at IS NULL``, and full projection re-inserts with ON CONFLICT.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class TraceRetentionReport:
    """Counts eligible rows and deletions for one bounded purge invocation."""

    cutoff: datetime
    candidates: int
    deleted: int
    dry_run: bool


_TRACE_WHERE = """
FROM brain_sessions AS s
WHERE s.nature = 'agent'
  AND s.status IN ('closed_inactive', 'abandoned')
  AND s.ended_at < :cutoff
  AND NOT EXISTS (SELECT 1 FROM brain_session_artifacts AS a WHERE a.session_id = s.id)
  AND NOT EXISTS (SELECT 1 FROM brain_session_checkpoints AS c WHERE c.session_id = s.id)
  AND NOT EXISTS (SELECT 1 FROM focus_slot_history AS h WHERE h.session_id = s.id)
  AND NOT EXISTS (SELECT 1 FROM brain_sessions AS child WHERE child.relayed_from_session_id = s.id)
"""
_TRACE_COUNT = sa.text("SELECT count(*) " + _TRACE_WHERE)
_TRACE_SELECT = sa.text(
    "SELECT s.id "
    + _TRACE_WHERE
    + " ORDER BY s.ended_at, s.id LIMIT :batch FOR UPDATE OF s SKIP LOCKED"
)
_TRACE_DELETE = sa.text("""
DELETE FROM brain_sessions
WHERE id = ANY(:ids)
  AND NOT EXISTS (
      SELECT 1 FROM brain_session_artifacts AS a WHERE a.session_id = brain_sessions.id
  )
RETURNING id
""").bindparams(sa.bindparam("ids", type_=ARRAY(UUID(as_uuid=True))))
_OUTBOX_WHERE = (
    "FROM graph_outbox AS o WHERE o.delivered_at IS NOT NULL AND o.delivered_at < :cutoff"
)
_OUTBOX_COUNT = sa.text("SELECT count(*) " + _OUTBOX_WHERE)
_OUTBOX_SELECT = sa.text(
    "SELECT o.id "
    + _OUTBOX_WHERE
    + " ORDER BY o.delivered_at, o.id LIMIT :batch FOR UPDATE OF o SKIP LOCKED"
)
_OUTBOX_DELETE = sa.text("DELETE FROM graph_outbox WHERE id = ANY(:ids) RETURNING id").bindparams(
    sa.bindparam("ids", type_=ARRAY(sa.BigInteger()))
)


class PgTraceRetention:
    """Purge eligible rows in independent, bounded transactions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Bind retention operations to the caller's async database factory."""
        self._session_factory = session_factory

    @staticmethod
    def _validate(
        older_than: timedelta, batch_size: int, max_batches: int, dry_run: bool, floor: int
    ) -> None:
        """Reject unsafe retention windows and unbounded or malformed batches."""
        if not isinstance(older_than, timedelta) or older_than < timedelta(days=floor):
            raise ValueError(f"retention window must be at least {floor} days")
        if type(batch_size) is not int or not 1 <= batch_size <= 50_000:
            raise ValueError("batch_size must be between 1 and 50000")
        if type(max_batches) is not int or max_batches < 1:
            raise ValueError("max_batches must be positive")
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")

    async def _purge(
        self,
        *,
        older_than: timedelta,
        batch_size: int,
        max_batches: int,
        dry_run: bool,
        floor: int,
        count: sa.TextClause,
        select: sa.TextClause,
        delete: sa.TextClause,
    ) -> TraceRetentionReport:
        """Count candidates or delete them in independent bounded batches."""
        self._validate(older_than, batch_size, max_batches, dry_run, floor)
        cutoff = datetime.now(UTC) - older_than
        if dry_run:
            async with self._session_factory.begin() as session:
                candidates = int((await session.scalar(count, {"cutoff": cutoff})) or 0)
            return TraceRetentionReport(cutoff, candidates, 0, True)
        candidates = deleted = 0
        for _ in range(max_batches):
            async with self._session_factory.begin() as session:
                ids = list(await session.scalars(select, {"cutoff": cutoff, "batch": batch_size}))
                if ids:
                    removed = list(await session.scalars(delete, {"ids": ids}))
                    candidates += len(ids)
                    deleted += len(removed)
            if len(ids) < batch_size:
                break
        return TraceRetentionReport(cutoff, candidates, deleted, False)

    async def purge_agent_traces(
        self, *, older_than: timedelta, batch_size: int, max_batches: int, dry_run: bool
    ) -> TraceRetentionReport:
        """Remove unreferenced closed agent traces older than the requested window."""
        return await self._purge(
            older_than=older_than,
            batch_size=batch_size,
            max_batches=max_batches,
            dry_run=dry_run,
            floor=30,
            count=_TRACE_COUNT,
            select=_TRACE_SELECT,
            delete=_TRACE_DELETE,
        )

    async def purge_delivered_outbox(
        self, *, older_than: timedelta, batch_size: int, max_batches: int, dry_run: bool
    ) -> TraceRetentionReport:
        """Remove delivered graph outbox rows older than the requested window."""
        return await self._purge(
            older_than=older_than,
            batch_size=batch_size,
            max_batches=max_batches,
            dry_run=dry_run,
            floor=7,
            count=_OUTBOX_COUNT,
            select=_OUTBOX_SELECT,
            delete=_OUTBOX_DELETE,
        )
