"""The bite-check: break what the preflight guards, and watch it name the break.

A preflight that returns `ready` against a healthy database proves only that it
can print a word. What it must prove is that it REDDENS, and that it reddens with
the name of the thing that moved — `security_barrier: false` alone would send an
operator to read seven view definitions.

Both bites run inside a transaction that is rolled back, so the disposable
database is handed to the next test exactly as it was found. The last test in
this file is the residue check: it is the one that would catch a bite that
escaped its transaction.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from brain_v42.codex_gateway.preflight import inspect_contract

pytestmark = pytest.mark.asyncio


async def test_a_view_stripped_of_its_barrier_is_named_and_refused(engine: AsyncEngine) -> None:
    """`DROP+CREATE` without `WITH (security_barrier=true)` — the b3331691 move.

    The control BEFORE the bite is what makes the red meaningful: without it, a
    preflight hard-wired to refuse would pass this test.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            control = await inspect_contract(connection)
            assert control.ready is True, "the disposable database must start compliant"
            assert control.exit_code == 0

            await connection.execute(sa.text("ALTER VIEW codex_ticket_v1 RESET (security_barrier)"))

            bitten = await inspect_contract(connection)
            assert bitten.ready is False, "the authority itself must refuse"
            assert bitten.clauses["security_barrier"] is False
            assert bitten.clauses["views"] is True, "an unrelated clause stays green"
            assert bitten.missing["security_barrier"] == ["codex_ticket_v1"]
            assert "codex_ticket_v1" in bitten.as_json()
            assert bitten.unexplained is False, "this refusal IS explained"
            assert bitten.exit_code == 1
        finally:
            await transaction.rollback()


async def test_a_vanished_view_is_named_by_its_own_clause(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(sa.text("DROP VIEW codex_consolidation_log_v1"))

            bitten = await inspect_contract(connection)
            assert bitten.ready is False
            assert bitten.clauses["views"] is False
            assert bitten.missing["views"] == ["codex_consolidation_log_v1"]
            assert bitten.exit_code == 1
        finally:
            await transaction.rollback()


async def test_the_drills_left_the_database_as_they_found_it(engine: AsyncEngine) -> None:
    """The finalizer of the two bites above, asserted rather than assumed."""
    async with engine.connect() as connection:
        report = await inspect_contract(connection)

    assert report.ready is True
    assert report.missing == {}
    assert report.exit_code == 0
