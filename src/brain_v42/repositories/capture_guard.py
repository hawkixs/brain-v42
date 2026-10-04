"""Refuse deleting knowledge that a session captured.

`brain_session_artifacts` records what each session produced, but its
`knowledge_id` has no foreign key to the knowledge tables: a plain DELETE leaves
a dangling id behind a session that still claims it. Every delete branch calls
`lock_unless_captured` inside the transaction of its DELETE.

The check is type-agnostic on purpose: any ledger row for the id refuses, a
`legacy`-typed one included, because the ledger is exclusive per id.
"""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import brain_session_artifacts
from brain_v42.models.brain_session import KnowledgeCapturedError


async def lock_unless_captured(
    session: AsyncSession,
    table: Table,
    row_id: UUID | str,
    *,
    project_key: str | None = None,
) -> bool:
    """Lock the knowledge row, then refuse if the capture ledger holds it.

    Returns False when the row does not exist (the caller keeps its own "not
    found" behaviour) and True when it is locked and free to delete. Raises
    `KnowledgeCapturedError` when a session captured it.

    Capture takes `FOR KEY SHARE` on the row, so `FOR UPDATE` here serialises
    against it: either the capture commits first and the ledger read below —
    a fresh statement, hence a fresh snapshot — sees it, or this transaction
    holds the row and the capture then finds it gone.
    """
    knowledge_id = UUID(str(row_id))
    lock = sa.select(table.c.id).where(table.c.id == knowledge_id)
    if project_key is not None:
        lock = lock.where(table.c.project_key == project_key)
    if (await session.execute(lock.with_for_update())).scalar_one_or_none() is None:
        return False
    await refuse_if_captured(session, knowledge_id)
    return True


async def refuse_if_captured(session: AsyncSession, knowledge_id: UUID) -> None:
    """Raise `KnowledgeCapturedError` when the capture ledger holds this id.

    The caller already holds the knowledge row's lock, which is what makes the
    ledger read decisive (see `lock_unless_captured`). Split out for the one
    delete that has to lock SEVERAL rows in a fixed order before reading it.
    """
    owner = (
        await session.execute(
            sa.select(brain_session_artifacts.c.session_id).where(
                brain_session_artifacts.c.knowledge_id == knowledge_id
            )
        )
    ).scalar_one_or_none()
    if owner is not None:
        raise KnowledgeCapturedError(knowledge_id, owner)
