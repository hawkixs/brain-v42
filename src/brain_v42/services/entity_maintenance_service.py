"""Canonical decay refresh mutation shared by HTTP management surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import sqlalchemy as sa

from brain_v42.db.project_group_scope import project_key_in_group
from brain_v42.db.tables import adrs, decisions, indexed_plans, learnings, runbooks, snippets

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


REFRESHABLE_ENTITY_TABLES: dict[str, sa.Table] = {
    "decision": decisions,
    "learning": learnings,
    "snippet": snippets,
    "runbook": runbooks,
    "adr": adrs,
    "plan": indexed_plans,
}


class UnknownEntityTypeError(ValueError):
    """The requested family does not participate in decay refresh."""


@dataclass(frozen=True, slots=True)
class EntityRefreshResult:
    entity_type: str
    entity_id: UUID
    freshness_status: str
    last_accessed_at: datetime


class EntityMaintenanceService:
    """Apply small, typed lifecycle mutations to decay-managed entities."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        tables: Mapping[str, Table] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tables = dict(tables) if tables is not None else REFRESHABLE_ENTITY_TABLES

    async def refresh(
        self,
        entity_type: str,
        entity_id: UUID,
        *,
        project_group: str | None = None,
    ) -> EntityRefreshResult | None:
        """Set one entity to fresh and stamp its last access time."""
        table = self._tables.get(entity_type)
        if table is None:
            allowed = ", ".join(sorted(self._tables))
            raise UnknownEntityTypeError(
                f"unknown entity type {entity_type!r}; expected one of: {allowed}"
            )

        accessed_at = datetime.now(UTC)
        conditions = [table.c.id == entity_id]
        if project_group is not None:
            conditions.append(project_key_in_group(table.c.project_key, project_group))
        statement = (
            table.update()
            .where(*conditions)
            .values(
                freshness_status="fresh",
                last_accessed_at=accessed_at,
                # Same gesture as `brain_refresh_entity`, hence the same term
                # from 043's closed vocabulary: two entry doors onto a single
                # transition, which must declare themselves identically — seeing
                # them diverge in the column would be worse than silence.
                freshness_source="revive",
            )
            .returning(table.c.id, table.c.last_accessed_at)
        )
        async with self._session_factory() as session:
            async with session.begin():
                row = (await session.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        return EntityRefreshResult(
            entity_type=entity_type,
            entity_id=row["id"],
            freshness_status="fresh",
            last_accessed_at=row["last_accessed_at"],
        )
