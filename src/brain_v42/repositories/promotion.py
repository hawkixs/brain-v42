"""Shared helper for the learning-stamp + dream_promotions audit insert.

Both PgADRRepo.create_with_promotion and PgRunbookRepo.create_with_promotion
perform the same two trailing statements inside their local transactions:

  1. UPDATE learnings SET metadata = {back-ref} || metadata
     WHERE id = source_learning_id          -- stamp
  2. INSERT INTO dream_promotions (...)     -- audit row (partial-unique index
                                           --   enforces one-per-source)

The statement ORDER is load-bearing: the target entity (ADR / runbook) must
already be inserted (FK-satisfiable) before the dream_promotions INSERT; and
the stamp must precede the audit row so callers can distinguish a missing
learning (rowcount == 0) from a duplicate-promotion (IntegrityError on the
unique index).

This module exposes the shared promotion primitives:

- ``lock_runbook_promotion_target`` — serializes one unique Runbook identity.
- ``lock_source_learning`` — locks the source, with an optional ownership predicate.
- ``stamp_learning`` — emits only the UPDATE and returns its rowcount.
- ``insert_promotion_audit`` — emits only the INSERT (dream_promotions row).
- ``record_promotion`` — calls both in the canonical order; convenience wrapper
  for PgRunbookRepo and any future caller that does not need to inspect the
  rowcount between the two statements.

PgADRRepo uses ``stamp_learning`` + ``insert_promotion_audit`` directly so it
can raise SourceLearningNotFound between the two statements — ensuring the two
failure modes (missing learning vs. duplicate promotion) remain distinguishable
even though dream_promotions.source_learning_id carries a non-deferrable FK
to learnings.id.

Composition, not inheritance: each repo keeps its own transaction and its own
"missing learning" policy.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import dream_promotions, learnings


class SourceLearningNotFound(LookupError):
    """Raised when a promotion cannot access its required source learning."""


async def lock_runbook_promotion_target(
    session: AsyncSession,
    *,
    title: str,
    project_key: str,
) -> None:
    """Serialize promotions competing for one unique Runbook target."""
    lock_key = f"brain_v42_runbook_promotion:{len(project_key)}:{project_key}:{len(title)}:{title}"
    statement = sa.text("SELECT pg_advisory_xact_lock(hashtext(:key))").bindparams(key=lock_key)
    await session.execute(statement)


async def lock_source_learning(
    session: AsyncSession,
    *,
    source_learning_id: UUID,
    project_key: str | None = None,
) -> bool:
    """Lock a promotion source, optionally constrained to its owning project."""
    statement = sa.select(learnings.c.id).where(learnings.c.id == source_learning_id)
    if project_key is not None:
        statement = statement.where(learnings.c.project_key == project_key)
    result = await session.execute(statement.with_for_update())
    return result.scalar_one_or_none() is not None


async def stamp_learning(
    session: AsyncSession,
    *,
    source_learning_id: UUID,
    target_entity_id: UUID,
    project_key: str | None = None,
) -> int:
    """Stamp the source learning's metadata with the promotion back-reference.

    Must be called within an already-open transaction.  The caller owns the
    transaction lifetime.

    Args:
        session: Open AsyncSession in an active transaction.
        source_learning_id: UUID of the learning being promoted.
        target_entity_id: UUID of the newly created ADR or runbook.
        project_key: Optional ownership predicate for scoped Dream calls.

    Returns:
        rowcount of the UPDATE statement (1 if learning found, 0 if not found).
        PgADRRepo checks this and raises SourceLearningNotFound when it is 0,
        BEFORE calling insert_promotion_audit() — this is the only way to
        distinguish a missing learning from a FK violation on the INSERT.
    """
    statement = sa.update(learnings).where(learnings.c.id == source_learning_id)
    if project_key is not None:
        statement = statement.where(learnings.c.project_key == project_key)
    update_result = await session.execute(
        statement.values(
            metadata=sa.func.jsonb_build_object(
                "target_entity_id",
                str(target_entity_id),
                "promoted_at",
                sa.func.to_char(
                    sa.func.now(),
                    sa.literal('YYYY-MM-DD"T"HH24:MI:SSOF'),
                ),
            ).op("||")(learnings.c.metadata),
        )
    )
    return cast("CursorResult[Any]", update_result).rowcount


async def insert_promotion_audit(
    session: AsyncSession,
    *,
    source_learning_id: UUID,
    target_type: str,
    dream_run_id: int | None,
    target_adr_id: UUID | None = None,
    target_runbook_id: UUID | None = None,
) -> None:
    """Insert the dream_promotions audit row for a completed promotion.

    Must be called within an already-open transaction.  The caller owns the
    transaction lifetime.

    The partial unique index on dream_promotions enforces one-per-source;
    a duplicate call raises IntegrityError (duplicate promotion), which callers
    translate into a typed error message at the tool layer.

    Args:
        session: Open AsyncSession in an active transaction.
        source_learning_id: UUID of the learning being promoted.
        target_type: ``"adr"`` or ``"runbook"``.
        dream_run_id: Optional dream-run identifier (may be None).
        target_adr_id: UUID of the target ADR, or None.
        target_runbook_id: UUID of the target runbook, or None.
    """
    promotion_values: dict[str, Any] = {
        "dream_run_id": dream_run_id,
        "source_learning_id": source_learning_id,
        "target_type": target_type,
        "target_adr_id": target_adr_id,
        "target_runbook_id": target_runbook_id,
    }
    await session.execute(dream_promotions.insert().values(**promotion_values))


async def record_promotion(
    session: AsyncSession,
    *,
    source_learning_id: UUID,
    target_entity_id: UUID,
    target_type: str,
    dream_run_id: int | None,
    target_adr_id: UUID | None = None,
    target_runbook_id: UUID | None = None,
    project_key: str | None = None,
) -> int:
    """Stamp the source learning's metadata and insert a dream_promotions row.

    Convenience wrapper that calls stamp_learning() then insert_promotion_audit()
    in the canonical order.  Used by PgRunbookRepo (which does not need to
    inspect the rowcount between the two statements).

    PgADRRepo calls stamp_learning() and insert_promotion_audit() directly so it
    can raise SourceLearningNotFound between the two calls — required because
    dream_promotions.source_learning_id has a non-deferrable FK to learnings.id:
    if the learning is missing and the INSERT were reached, PostgreSQL would raise
    ForeignKeyViolationError rather than allowing the rowcount check.

    Must be called within an already-open transaction (``async with session.begin()``
    or equivalent).  The caller owns the transaction lifetime.

    Statement order (load-bearing):
      1. UPDATE learnings.metadata — back-reference stamp; rowcount returned.
      2. INSERT dream_promotions — audit row (partial unique index enforces
         one-per-source; raises IntegrityError on duplicate, caller passes through).

    Args:
        session: Open AsyncSession in an active transaction.
        source_learning_id: UUID of the learning being promoted.
        target_entity_id: UUID of the newly created ADR or runbook.
        target_type: ``"adr"`` or ``"runbook"``.
        dream_run_id: Optional dream-run identifier (may be None).
        target_adr_id: UUID of the target ADR, or None.
        target_runbook_id: UUID of the target runbook, or None.
        project_key: Optional ownership predicate for the source stamp; None
            preserves historical admin behavior.

    Returns:
        rowcount of the UPDATE statement (1 if learning found, 0 if not found).
        Callers should raise SourceLearningNotFound when this is 0.
    """
    if project_key is None:
        rowcount = await stamp_learning(
            session,
            source_learning_id=source_learning_id,
            target_entity_id=target_entity_id,
        )
    else:
        rowcount = await stamp_learning(
            session,
            source_learning_id=source_learning_id,
            target_entity_id=target_entity_id,
            project_key=project_key,
        )
    await insert_promotion_audit(
        session,
        source_learning_id=source_learning_id,
        target_type=target_type,
        dream_run_id=dream_run_id,
        target_adr_id=target_adr_id,
        target_runbook_id=target_runbook_id,
    )
    return rowcount
