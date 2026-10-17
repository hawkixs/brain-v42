"""Authoritative read-only ownership lookup from a closed table map."""

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    adrs,
    brain_entities,
    brain_sessions,
    decisions,
    focus_slots,
    indexed_plans,
    knowledge_claims,
    learnings,
    runbooks,
    snippets,
)
from brain_v42.entity_ids import normalize_uuid_prefix
from brain_v42.repositories.pg_base import BasePgRepository

ENTITY_TABLES = {
    "decision": decisions,
    "learning": learnings,
    "snippet": snippets,
    "runbook": runbooks,
    "adr": adrs,
    "plan": indexed_plans,
}
ROW_TABLES = {
    **ENTITY_TABLES,
    "session": brain_sessions,
    "slot": focus_slots,
    "claim": knowledge_claims,
}


class Missing:
    """Distinguish absent rows from rows with NULL ownership inside the resolver."""


MISSING = Missing()


async def claim_source(session: AsyncSession, claim_id: UUID) -> tuple[sa.Table, UUID] | None:
    """The immutable claim reference identifies the concrete source table and id."""
    row = (
        await session.execute(
            sa.select(
                knowledge_claims.c.entity_type,
                brain_entities.c.source_uuid,
            )
            .join(brain_entities, brain_entities.c.id == knowledge_claims.c.entity_ref_id)
            .where(knowledge_claims.c.id == claim_id)
        )
    ).one_or_none()
    if row is None or row.entity_type not in ENTITY_TABLES:
        return None
    if row.source_uuid is None:
        return None
    return ENTITY_TABLES[row.entity_type], row.source_uuid


class PgScopeLookup:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(self, kind: str, entity_id: object) -> str | None | Missing:
        table = ROW_TABLES.get("runbook" if kind == "runbook_prefix" else kind)
        if table is None and kind != "claim_source":
            return MISSING
        if not isinstance(entity_id, str):
            return MISSING
        async with self._session_factory() as session:
            try:
                uid = UUID(entity_id)
            except ValueError:
                if kind != "runbook_prefix":
                    return MISSING
                prefix = normalize_uuid_prefix(entity_id)
                if prefix is None:
                    return MISSING
                repo = BasePgRepository(self._session_factory)
                repo.table = runbooks
                matches = await repo.resolve_id_prefix(prefix, session=session)
                if len(matches) != 1:
                    return MISSING
                uid = matches[0]
            if kind == "claim_source":
                source = await claim_source(session, uid)
                if source is None:
                    return MISSING
                table, uid = source
            assert table is not None
            row = (
                await session.execute(sa.select(table.c.project_key).where(table.c.id == uid))
            ).one_or_none()
            if row is None:
                return MISSING
            value = row[0]
            return value if isinstance(value, str) else None
