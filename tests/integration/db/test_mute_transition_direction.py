"""The mute-transition counter separates a corpus EXIT from a corpus RE-ENTRY.

Step 2 of `55a21fb8`, proved against PostgreSQL and its 043 trigger — not against a
`MagicMock`. The unit modules pin the query's shape; only this one proves that the
trigger, the constraint and the counter agree on a real transition.

The witness carries BOTH DIRECTIONS in the same transaction: an entity leaving the
corpus (`fresh → archived`) and one re-entering it (`archived → fresh`), both MUTE.
Before this step, they produced two identical rows. Without the second direction, a
counter that simply returned the total would pass the test.

Everything lives in a ROLLED-BACK transaction, and the counts are DELTAS measured
around the witness: `fetch_mute_transitions` scans the whole table, so an assertion
on an absolute value would depend on the integration database's state.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
import sqlalchemy as sa
from scripts.dream import post_run_alert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

_INSERT = sa.text(
    "INSERT INTO public.learnings (id, topic, insight, project_key, freshness_status) "
    "VALUES (:id, :topic, 'témoin de marche 2', 'brain-v42', :status)"
)
_TRANSITION = sa.text("UPDATE public.learnings SET freshness_status = :status WHERE id = :id")


async def _learnings_counts(
    connection: AsyncConnection, run_date: dt.date
) -> post_run_alert.ProvenanceCount:
    report = await post_run_alert.fetch_mute_transitions(connection, run_date)
    return next(count for count in report.counts if count.table == "learnings")


@pytest.mark.asyncio
async def test_a_mute_return_to_fresh_is_counted_apart_from_a_mute_archival(
    engine: AsyncEngine,
) -> None:
    today = dt.datetime.now(tz=dt.UTC).date()
    # TWO exits for ONE re-entry, and the asymmetry is the control: at one against
    # one, inverting the compared destination would return the SAME delta and the
    # test would pass on wrong code. Here, an inversion returns 2 instead of 1.
    leaving = (uuid4(), uuid4())
    returning = uuid4()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            before = await _learnings_counts(connection, today)

            # Two entities LEAVE the corpus, one RE-ENTERS it. None declares a
            # provenance: 043's trigger makes them all mute.
            for entity_id in leaving:
                await connection.execute(
                    _INSERT, {"id": entity_id, "topic": f"sortie-{entity_id}", "status": "fresh"}
                )
                await connection.execute(_TRANSITION, {"id": entity_id, "status": "archived"})
            await connection.execute(
                _INSERT, {"id": returning, "topic": f"rentrée-{returning}", "status": "archived"}
            )
            await connection.execute(_TRANSITION, {"id": returning, "status": "fresh"})

            # The trigger did its job: dated, and provenance erased.
            stamped = (
                await connection.execute(
                    sa.text(
                        "SELECT freshness_status, freshness_source, "
                        "freshness_status_updated_at IS NOT NULL AS dated "
                        "FROM public.learnings WHERE id = ANY(:ids) ORDER BY freshness_status"
                    ).bindparams(sa.bindparam("ids", value=[*leaving, returning]))
                )
            ).all()
            assert [(row[0], row[1], row[2]) for row in stamped] == [
                ("archived", None, True),
                ("archived", None, True),
                ("fresh", None, True),
            ]

            after = await _learnings_counts(connection, today)

            # THREE more mute transitions, of which only ONE is a return.
            assert after.night - before.night == 3
            assert after.standing - before.standing == 3
            assert after.to_fresh_night - before.to_fresh_night == 1
            assert after.to_fresh_standing - before.to_fresh_standing == 1
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_a_declared_return_to_fresh_is_not_muet_and_leaves_the_count_alone(
    engine: AsyncEngine,
) -> None:
    """The NEGATIVE witness: `brain_refresh_entity` declares `revive`, so nothing to count.

    Without it, a counter that ignored `freshness_source` would also count the
    already-traced unarchivals — and the number would stop designating a hole.
    """
    today = dt.datetime.now(tz=dt.UTC).date()
    revived = uuid4()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            before = await _learnings_counts(connection, today)

            await connection.execute(
                _INSERT, {"id": revived, "topic": f"revive-{revived}", "status": "archived"}
            )
            await connection.execute(
                sa.text(
                    "UPDATE public.learnings SET freshness_status = 'fresh', "
                    "freshness_source = 'revive' WHERE id = :id"
                ),
                {"id": revived},
            )

            source = await connection.scalar(
                sa.text("SELECT freshness_source FROM public.learnings WHERE id = :id"),
                {"id": revived},
            )
            assert source == "revive", "le trigger a effacé une provenance pourtant redéclarée"

            after = await _learnings_counts(connection, today)
            assert after.night == before.night
            assert after.to_fresh_night == before.to_fresh_night
        finally:
            await transaction.rollback()
