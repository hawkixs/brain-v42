"""Serialize a scoped mutation with changes to the checked row's owner."""

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.provenance import get_checked_project


class CheckedProjectMismatch(Exception):
    """A checked owner changed before this transaction acquired its row lock."""


def checked_project_clause(column: sa.ColumnElement[Any] | None) -> sa.ColumnElement[bool]:
    key = get_checked_project()
    if key is None:
        return sa.true()
    if column is None:
        raise CheckedProjectMismatch
    return column == key


async def lock_checked_owner(
    session: AsyncSession,
    table: sa.Table,
    entity_id: UUID | str,
    *,
    checked: str | None = None,
) -> bool:
    key = get_checked_project() if checked is None else checked
    if key is None:
        return True
    project_column = table.c.get("project_key")
    if project_column is None:
        raise CheckedProjectMismatch
    row = (
        await session.execute(
            sa.select(project_column).where(table.c.id == entity_id).with_for_update()
        )
    ).one_or_none()
    if row is not None and row[0] != key:
        raise CheckedProjectMismatch
    return row is not None
