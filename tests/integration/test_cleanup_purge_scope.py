"""The integration teardown only purges what carries the ``integ-`` prefix.

Ticket adfc24eb. Measured on 2026-08-10 on ``brain_test``: 159 learnings under
``project_key='brain-v42'``, all carrying ``topic='sujet'`` — hence 100 % coming from
``tests/integration/dream/test_promote_prepare_provenance.py``, which seeded under a
PRODUCTION key the purge predicate does not match. Under ``integ%``: zero rows. The
purge does exactly what it aims at; it does not aim widely enough.

WHERE THE TICKET IS WRONG, and it must be said: it designates
``test_real_content_edit_readmits_the_candidate`` as the victim. That was true on
2026-08-06. Commit 508439d2 of 08-08 switched ``promote_prepare``'s ``ORDER BY`` from
``access_count`` to ``access_count_human``, which inverts the ranking. With today's code
it is ``test_human_reads_mature_a_learning`` (acch=4) that will be evicted, and the
first (acch=9) is rank 1 and will never fall. Writing the reproduction from the
ticket's prose would target the one test that cannot break.

WHAT WE WILL NEVER DO: add ``DELETE FROM learnings WHERE project_key='brain-v42'`` to
the teardown. The conftest's guardrail only refuses the database NAME ``brain``; a
``BRAIN_V42_TEST_DB_URL`` pointed at a restoration would erase the project's real
learnings. The fix is upstream — unique keys — not downstream.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from tests.integration.conftest import (
    INTEGRATION_DB_URL,
    purge_integration_rows,
    shared_database_run,
)
from tests.integration.dream.test_promote_prepare_provenance import make_promote_project_key

pytestmark = pytest.mark.integration


async def _seed(session, project_key: str, *, access_count_human: int, age_days: int) -> uuid.UUID:
    learning_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO learnings (id, topic, insight, project_key, access_count, "
            "access_count_human, confidence, created_at, updated_at) "
            "VALUES (:id, 'sujet', 'corps', :pk, :ac, :acch, 'high', "
            "NOW() - make_interval(days => :age), NOW() - make_interval(days => :age))"
        ),
        {
            "id": learning_id,
            "pk": project_key,
            "ac": access_count_human,
            "acch": access_count_human,
            "age": age_days,
        },
    )
    return learning_id


@pytest.mark.asyncio
async def test_each_promote_test_gets_its_own_project_key() -> None:
    """The DIRECT witness of hermeticity, with no database.

    It falls if someone goes back to a shared constant, even while keeping the
    ``integ-`` prefix: the prefix governs the purge, uniqueness governs the coupling
    between tests of the same run.
    """
    assert make_promote_project_key() != make_promote_project_key()


@pytest.mark.asyncio
async def test_the_promote_fixture_key_is_actually_purged(engine, db_session) -> None:
    key = make_promote_project_key()
    await _seed(db_session, key, access_count_human=9, age_days=30)
    await db_session.commit()

    async with engine.begin() as conn:
        await purge_integration_rows(conn, only_project_keys=[key])

    remaining = (
        await db_session.execute(
            sa.text("SELECT count(*) FROM learnings WHERE project_key = :pk"), {"pk": key}
        )
    ).scalar_one()
    assert remaining == 0, (
        "la clé semée par les tests PROMOTE survit au teardown : elle s'accumulera "
        "d'un run à l'autre jusqu'à évincer un vrai candidat"
    )


@pytest.mark.asyncio
async def test_the_purge_leaves_a_non_integration_key_alone(engine, db_session) -> None:
    """The purge's NEGATIVE probe, and the guardrail against the dangerous fix.

    It is green from the moment it is written — this is not a RED. Its reason to
    exist is to fail the day someone widens the predicate to "also clean brain-v42"
    and erases real data.
    """
    key = f"notinteg-{uuid.uuid4().hex[:8]}"
    learning_id = await _seed(db_session, key, access_count_human=1, age_days=1)
    await db_session.commit()
    try:
        async with engine.begin() as conn:
            await purge_integration_rows(conn, only_project_keys=[key])

        remaining = (
            await db_session.execute(
                sa.text("SELECT count(*) FROM learnings WHERE project_key = :pk"), {"pk": key}
            )
        ).scalar_one()
        assert remaining == 1, (
            "le teardown a effacé une clé hors de son périmètre — c'est le mode de "
            "panne qui détruirait des données réelles sur une base restaurée"
        )
    finally:
        await db_session.execute(
            sa.text("DELETE FROM learnings WHERE id = :id"), {"id": learning_id}
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_accumulated_survivors_do_not_evict_a_freshly_matured_row(
    engine, db_session, session_factory
) -> None:
    """The reproduction of the REAL failure mode — the only test that proves the point.

    Ten acch=9 survivors left by ten earlier runs, then a freshly matured acch=4 row.
    With the shared key, ``ORDER BY access_count_human DESC`` and ``LIMIT 10`` return
    the ten old ones and the new one is at rank eleven: the test looking for it fails,
    with nothing having changed in the production code.
    """
    from scripts.dream.promote_prepare import fetch_candidates

    old_key = make_promote_project_key()
    for _ in range(10):
        await _seed(db_session, old_key, access_count_human=9, age_days=30)

    new_key = make_promote_project_key()
    # 8 days, not 1: the filter requires (NOW() - created_at) >= 7 days. "Fresh"
    # means freshly MATURED — it has just crossed the human-read threshold — not
    # freshly created.
    await _seed(db_session, new_key, access_count_human=4, age_days=8)
    await db_session.commit()

    try:
        candidates = await fetch_candidates(session_factory, new_key, limit=10)
        assert len(candidates) == 1, (
            "les survivants d'anciens runs ont évincé la ligne fraîchement mûrie : "
            f"{len(candidates)} candidats rendus"
        )
    finally:
        async with engine.begin() as conn:
            await purge_integration_rows(conn, only_project_keys=[old_key, new_key])


# --- Ticket 8409719c: one run's purge must never erase another run's rows ------
#
# Reproduced 2026-10-05: test_session_capture_e2e.py passes 9/9 alone and fails
# 7/9 while another process runs this purge in a loop — its projects and
# sessions vanish mid-test ("Unknown project", "Session ... was not found").
# The prefix says WHAT a test wrote, never WHICH run wrote it.


def _probe_lock_key() -> int:
    """A lock key of its own, so the probe never contends with the real run marker."""
    return uuid.uuid4().int % (2**62)


async def _count_learnings(engine, project_key: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.text("SELECT count(*) FROM learnings WHERE project_key = :pk"),
                {"pk": project_key},
            )
        ).scalar_one()


async def test_the_end_of_run_purge_spares_a_run_still_in_progress(engine, db_session) -> None:
    lock_key = _probe_lock_key()
    key = make_promote_project_key()
    await _seed(db_session, key, access_count_human=1, age_days=1)
    await db_session.commit()
    other_run = await engine.connect()
    try:
        await other_run.execute(sa.text("SELECT pg_advisory_lock_shared(:key)"), {"key": lock_key})
        async with shared_database_run(
            INTEGRATION_DB_URL, lock_key=lock_key, only_project_keys=[key]
        ) as run:
            pass
        assert run.purged is False
        assert await _count_learnings(engine, key) == 1, (
            "a run that finished erased the rows of a run still in progress"
        )
    finally:
        await other_run.close()
        async with engine.begin() as conn:
            await purge_integration_rows(conn, only_project_keys=[key])


async def test_the_last_run_out_purges(engine, db_session) -> None:
    """The probe is the last run out of its OWN lock key, never of the real one.

    Unnarrowed, its purge would erase every concurrent run's ``integ-`` rows in
    the middle of their tests: the very failure ticket 8409719c removed.
    """
    mine, theirs = make_promote_project_key(), make_promote_project_key()
    await _seed(db_session, mine, access_count_human=1, age_days=1)
    await _seed(db_session, theirs, access_count_human=1, age_days=1)
    await db_session.commit()
    try:
        async with shared_database_run(
            INTEGRATION_DB_URL, lock_key=_probe_lock_key(), only_project_keys=[mine]
        ) as run:
            pass

        assert run.purged is True
        assert await _count_learnings(engine, mine) == 0
        assert await _count_learnings(engine, theirs) == 1, (
            "a probe of the end-of-run purge erased another run's integration key"
        )
    finally:
        async with engine.begin() as conn:
            await purge_integration_rows(conn, only_project_keys=[mine, theirs])


async def test_a_narrowed_purge_leaves_the_other_integration_keys_alone(engine, db_session) -> None:
    mine, theirs = make_promote_project_key(), make_promote_project_key()
    await _seed(db_session, mine, access_count_human=1, age_days=1)
    await _seed(db_session, theirs, access_count_human=1, age_days=1)
    await db_session.commit()
    try:
        async with engine.begin() as conn:
            await purge_integration_rows(conn, only_project_keys=[mine])

        assert await _count_learnings(engine, mine) == 0
        assert await _count_learnings(engine, theirs) == 1, (
            "a purge narrowed to one test's keys erased another integration key"
        )
    finally:
        async with engine.begin() as conn:
            await purge_integration_rows(conn, only_project_keys=[mine, theirs])
