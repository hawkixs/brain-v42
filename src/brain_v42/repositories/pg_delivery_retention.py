"""Bounded retention of old, unreferenced successful delivery confirmations.

Receipt issuance holds ``lock_workflows``, freezing the pointers it names. The
observer cannot move a pointer back to an old confirmation, and receipts require
fresh evidence (ten minutes), far inside the minimum seven-day retention window.
Thus old candidates cannot acquire new references through the supported writers
while a batch runs. Row locks protect the selected rows; no workflow lock is needed.
Errors drive observer backoff and snapshots retain immutable evidence: neither is
deleted. JSON references are compared as text so malformed values cannot abort a batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class ConfirmationRetentionReport:
    cutoff: datetime
    candidates: int
    deleted: int
    dry_run: bool


_POINTERS = (
    ("delivery_artifact_bindings", "latest_attempt_confirmation_id"),
    ("delivery_artifact_bindings", "latest_success_confirmation_id"),
    ("delivery_workflows", "latest_context_attempt_confirmation_id"),
    ("delivery_workflows", "latest_context_success_confirmation_id"),
)
_JSON_SOURCES = (
    ("delivery_receipts", "payload"),
    ("delivery_events", "result"),
    ("delivery_events", "payload"),
)
_PROTECTED = (
    "WITH protected(id) AS (\n"
    + "\nUNION\n".join(
        [
            f"SELECT {column}::text FROM {table} WHERE {column} IS NOT NULL"
            for table, column in _POINTERS
        ]
        + [
            f"SELECT value #>> '{{}}' FROM {table} "
            f"CROSS JOIN LATERAL jsonb_path_query({column}, 'lax $.**.{key}') AS refs(value)"
            for table, column in _JSON_SOURCES
            for key in ("success_confirmation_id", "latest_attempt_confirmation_id")
        ]
    )
    + "\n)\n"
)
_CANDIDATES = """
FROM delivery_confirmations AS c
WHERE c.outcome = 'success' AND c.collection_finished_at < :cutoff
  AND NOT EXISTS (SELECT 1 FROM protected AS p WHERE p.id = c.id::text)
"""
_COUNT = sa.text(_PROTECTED + "SELECT count(*) " + _CANDIDATES)
_SELECT = sa.text(
    _PROTECTED
    + "SELECT c.id "
    + _CANDIDATES
    + "ORDER BY c.collection_finished_at, c.id LIMIT :batch FOR UPDATE OF c SKIP LOCKED"
)
_DELETE = sa.text(
    "DELETE FROM delivery_confirmations WHERE id = ANY(:ids) RETURNING id"
).bindparams(sa.bindparam("ids", type_=ARRAY(UUID(as_uuid=True))))


class PgDeliveryConfirmationRetention:
    """Own one transaction per batch so locks never span the complete purge."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def purge(
        self,
        *,
        older_than: timedelta,
        batch_size: int,
        max_batches: int,
        dry_run: bool,
    ) -> ConfirmationRetentionReport:
        if not isinstance(older_than, timedelta) or older_than < timedelta(days=7):
            raise ValueError("retention window must be at least seven days")
        if type(batch_size) is not int or not 1 <= batch_size <= 50_000:
            raise ValueError("batch_size must be between 1 and 50000")
        if type(max_batches) is not int or max_batches < 1:
            raise ValueError("max_batches must be positive")
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")
        cutoff = datetime.now(UTC) - older_than
        if dry_run:
            async with self._session_factory.begin() as session:
                candidates = int((await session.scalar(_COUNT, {"cutoff": cutoff})) or 0)
            return ConfirmationRetentionReport(cutoff, candidates, 0, True)

        candidates, deleted = 0, 0
        for _ in range(max_batches):
            async with self._session_factory.begin() as session:
                ids = list(await session.scalars(_SELECT, {"cutoff": cutoff, "batch": batch_size}))
                if ids:
                    removed = list(await session.scalars(_DELETE, {"ids": ids}))
                    deleted += len(removed)
                    candidates += len(ids)
            if len(ids) < batch_size:
                break
        return ConfirmationRetentionReport(cutoff, candidates, deleted, False)
