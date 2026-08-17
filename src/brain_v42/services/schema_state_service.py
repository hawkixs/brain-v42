"""SchemaStateService — the schema revision actually in force.

Read-only, one query. Exists because the session briefing must not restate a
migration number copied forward through prose: on 2026-08-04 the focus and
CLAUDE.md both asserted 037 while the database had been on 039 for three days,
and nothing reconciled the claim.

`alembic_version` is Alembic's own bookkeeping table and is deliberately not
declared in `brain_v42.db.tables`, so this reads it as literal SQL. The
statement is a module-level constant with no interpolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CURRENT_REVISION_SQL = sa.text("SELECT version_num FROM alembic_version")


class SchemaStateService:
    """Reads the migration revision the running database is stamped with."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def current_revision(self) -> str | None:
        """Return the stamped revision, or None when the table holds no row.

        Read failures propagate on purpose. Collapsing them to None would make
        an unreachable database indistinguishable from an unstamped one, and the
        briefing would then render nothing at all — the silence this whole
        section exists to remove.
        """
        async with self._sf() as session:
            revision = (await session.execute(_CURRENT_REVISION_SQL)).scalar()
        return None if revision is None else str(revision)
