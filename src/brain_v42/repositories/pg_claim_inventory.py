"""One read-only aggregate counting stored claim rows.

The inventory is a count of rows, never a verdict: it says how many claims the
ledger holds and how many were created lately, not whether any assertion is
true. It is a single statement so its cost does not depend on how many facts,
projects or entries the ledger spans -- there is deliberately no per-claim,
per-fact or per-project query to fan out.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import knowledge_claims

_CREATED_WINDOW = "7 days"


@dataclass(frozen=True, slots=True)
class ClaimInventory:
    """Active claims by provenance, plus claims created in the last seven days."""

    extracted: int
    declared: int
    measured: int
    #: Every claim recorded in the window, retired or not: production volume.
    created_7d: int

    @property
    def active(self) -> int:
        return self.extracted + self.declared + self.measured


def claim_inventory_statement() -> sa.Select[tuple[int, int, int, int]]:
    """The whole inventory as one aggregate over `knowledge_claims`."""
    active = knowledge_claims.c.retired_at.is_(None)
    provenance = knowledge_claims.c.provenance

    def active_with(value: str) -> sa.ColumnElement[int]:
        return sa.func.count().filter(active, provenance == value)

    return sa.select(
        active_with("extracted").label("extracted"),
        active_with("declared").label("declared"),
        active_with("measured").label("measured"),
        sa.func.count()
        .filter(
            knowledge_claims.c.recorded_at
            >= sa.func.now() - sa.text(f"interval '{_CREATED_WINDOW}'")
        )
        .label("created_7d"),
    ).select_from(knowledge_claims)


async def read_claim_inventory(session: AsyncSession) -> ClaimInventory:
    """Read the inventory in one SELECT; writes nothing."""
    row = (await session.execute(claim_inventory_statement())).one()
    return ClaimInventory(
        extracted=row.extracted,
        declared=row.declared,
        measured=row.measured,
        created_7d=row.created_7d,
    )
