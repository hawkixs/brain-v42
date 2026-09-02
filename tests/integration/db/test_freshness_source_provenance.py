"""Every `freshness_status` transition must say where it comes from.

043 lays down a CLOSED vocabulary of four terms and an explicit doctrine: *an absent
provenance is visible, a false provenance is believed*. The trigger therefore resets
it to `NULL` as soon as a writer does not redeclare it.

**That doctrine only holds if absence is RARE.** Batch B's census, replayed here:
six writers of `freshness_status`, of which **five** mute — the column said "unknown"
for almost everything, and the writer that JUDGES was among the mute ones.

**WHY THESE TESTS WRITE THEN READ BACK FROM THE DATABASE.** It is the TRIGGER that
nulls the provenance, not the application code. A test that checked the value passed
to `values()` would prove we wrote it, never that it survived — and that is exactly
the missing half. So we read the row back.

**TWO TRIGGER TRAPS, wired into the fixtures and not discovered by accident:**

1. It only fires if the status CHANGES (`WHEN OLD IS DISTINCT FROM NEW`). Seeding a
   row already at the target status would make these tests green without the trigger
   ever having run. Each fixture therefore seeds the OPPOSITE status.
2. It also nulls the source when it is rewritten IDENTICALLY. The rows are therefore
   seeded at `freshness_source = NULL`, so that the redeclaration is genuinely
   DISTINCT.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import learnings

pytestmark = pytest.mark.integration

_PROJECT = "integ-freshness-source"


async def _seed(db_session, *, status: str) -> uuid.UUID:
    """A real row, at the OPPOSITE status to the one the writer will set."""
    row_id = uuid.uuid4()
    await db_session.execute(
        sa.insert(learnings).values(
            id=row_id,
            project_key=_PROJECT,
            topic="provenance du statut de fraîcheur",
            insight="Le trigger nulle ce que l'écrivain ne redéclare pas.",
            freshness_status=status,
            freshness_source=None,
        )
    )
    await db_session.commit()
    return row_id


async def _read_source(db_session, row_id: uuid.UUID) -> str | None:
    db_session.expire_all()
    return (
        await db_session.execute(
            sa.select(learnings.c.freshness_source).where(learnings.c.id == row_id)
        )
    ).scalar_one()


async def _read_status(db_session, row_id: uuid.UUID) -> str:
    db_session.expire_all()
    return (
        await db_session.execute(
            sa.select(learnings.c.freshness_status).where(learnings.c.id == row_id)
        )
    ).scalar_one()


class TestTheTriggerIsActuallyExercised:
    """A MECHANISM witness: without it, the tests below could be hollow.

    If the trigger did not run — status unchanged, or trigger absent — a written
    provenance would survive trivially and every test in this file would pass while
    proving nothing at all.
    """

    async def test_a_writer_that_stays_mute_loses_its_provenance(self, db_session) -> None:
        """The reference NEGATIVE WITNESS: mute ⇒ NULL, through the trigger."""
        row_id = await _seed(db_session, status="stale")
        await db_session.execute(
            sa.update(learnings)
            .where(learnings.c.id == row_id)
            .values(freshness_status="fresh")  # no source redeclared
        )
        await db_session.commit()

        assert await _read_status(db_session, row_id) == "fresh"
        assert await _read_source(db_session, row_id) is None

    async def test_an_unchanged_status_does_not_fire_the_trigger(self, db_session) -> None:
        """Trap no. 1, pinned: rewriting the SAME status triggers nothing.

        A source set on a rewrite at constant status SURVIVES — not because the
        writer declared it properly, but because the trigger never ran. A test seeded
        at the target status would therefore be green for the wrong reason. It is
        this test that makes the others readable.
        """
        row_id = await _seed(db_session, status="fresh")
        await db_session.execute(
            sa.update(learnings)
            .where(learnings.c.id == row_id)
            .values(freshness_status="fresh", freshness_source="revive")
        )
        await db_session.commit()

        assert await _read_source(db_session, row_id) == "revive"


class TestMechanicalWritersDeclareTheirProvenance:
    async def test_merge_declares_merge(self, db_session, session_factory) -> None:
        from brain_v42.repositories.pg_consolidation_log import PgConsolidationLogRepo
        from brain_v42.services.consolidation import ConsolidationJob

        source_id = await _seed(db_session, status="fresh")
        target_id = await _seed(db_session, status="fresh")

        await ConsolidationJob(session_factory, PgConsolidationLogRepo(session_factory)).merge(
            "learning", source_id, target_id
        )

        assert await _read_status(db_session, source_id) == "archived"
        assert await _read_source(db_session, source_id) == "merge"

    async def test_the_refresh_tool_declares_revive(self, db_session, session_factory) -> None:
        """`brain_refresh_entity` — the same gesture as the gateway route, through MCP."""
        from typing import Any

        from brain_v42.mcp.tools.decay_tools import register_decay_tools

        class _CollectingMCP:
            def __init__(self) -> None:
                self.registered: dict[str, Any] = {}

            def tool(self, **_kwargs: Any) -> Any:
                def decorator(fn: Any) -> Any:
                    self.registered[fn.__name__] = fn
                    return fn

                return decorator

        row_id = await _seed(db_session, status="archived")
        mcp = _CollectingMCP()
        register_decay_tools(mcp, session_factory)

        await mcp.registered["brain_refresh_entity"]("learning", str(row_id))

        assert await _read_status(db_session, row_id) == "fresh"
        assert await _read_source(db_session, row_id) == "revive"

    async def test_gateway_refresh_declares_revive(self, db_session, session_factory) -> None:
        from brain_v42.services.entity_maintenance_service import EntityMaintenanceService

        row_id = await _seed(db_session, status="archived")

        refreshed = await EntityMaintenanceService(session_factory).refresh("learning", row_id)

        assert refreshed is not None
        assert await _read_status(db_session, row_id) == "fresh"
        assert await _read_source(db_session, row_id) == "revive"
