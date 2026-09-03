"""Migration 052 — the access journal that outlives the queue it is fed by.

Ticket b93e32be. `access_log` is drained every 300 s and its rows deleted after
`is_human_actor()` has folded the actor into a boolean. Everything this file
proves is unprovable in a unit test: that the aggregation and the journal share
ONE transaction, that a second flush ACCUMULATES rather than overwrites, and
that the downgrade refuses while naming what it would destroy. A double can show
that an INSERT is emitted; only the database can show what the table holds
afterwards.

The accumulation test is the load-bearing one. `ON CONFLICT DO UPDATE` with
`count = excluded.count` instead of `existing + excluded` passes every
single-flush test and silently turns a daily total into "whatever the last
300 s window saw" — the exact class of failure this table exists to prevent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tests.integration.disposable_db import repository_head

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

_DAY_ONE = datetime(2026, 8, 30, 9, 15, tzinfo=UTC)
_DAY_TWO = datetime(2026, 8, 31, 22, 40, tzinfo=UTC)


async def _queue(engine: AsyncEngine, rows: list[tuple[str, UUID, str, datetime]]) -> None:
    """Put raw access events in the queue, exactly as `AccessLogger` would."""
    async with engine.begin() as conn:
        for entity_type, entity_id, actor, accessed_at in rows:
            await conn.execute(
                sa.text(
                    "INSERT INTO access_log (entity_type, entity_id, access_type,"
                    " accessed_at, actor)"
                    " VALUES (:t, :i, 'search', :a, :actor)"
                ),
                {"t": entity_type, "i": entity_id, "a": accessed_at, "actor": actor},
            )


async def _flush(engine: AsyncEngine) -> None:
    """The PRODUCTION path, and deliberately not the other one.

    `decay_flusher.py:122` calls `aggregate_in_session(session)` and owns the
    transaction. `aggregate_and_flush` is a deprecated duplicate with its own
    query which does not even select `actor` — a bench calling it would prove a
    path production does not take, and could never feed this journal.
    """
    from brain_v42.repositories.pg_access_log import PgAccessLogRepo

    factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = PgAccessLogRepo(session_factory=factory)
    async with factory() as session:
        await repo.aggregate_in_session(session)
        await session.commit()


async def _journal(engine: AsyncEngine, entity_id: UUID) -> list[dict]:
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT entity_type, actor, day, count, last_accessed_at"
                " FROM access_log_daily WHERE entity_id = :i"
                " ORDER BY day, actor"
            ),
            {"i": entity_id},
        )
        return [dict(row) for row in result.mappings().all()]


async def _cleanup(engine: AsyncEngine, entity_id: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("DELETE FROM access_log_daily WHERE entity_id = :i"), {"i": entity_id}
        )
        await conn.execute(sa.text("DELETE FROM access_log WHERE entity_id = :i"), {"i": entity_id})


class TestTheJournalSurvivesTheFlush:
    async def test_one_row_per_actor_and_per_day_and_the_queue_is_emptied(
        self, engine: AsyncEngine
    ) -> None:
        """Three events, two actors, two days → three journal rows, empty queue."""
        entity_id = uuid4()
        try:
            await _queue(
                engine,
                [
                    ("learning", entity_id, "brain_v42", _DAY_ONE),
                    ("learning", entity_id, "brain_v42", _DAY_ONE + timedelta(hours=2)),
                    ("learning", entity_id, "dream-codex-reorg", _DAY_TWO),
                ],
            )

            await _flush(engine)

            journal = await _journal(engine, entity_id)
            assert [(r["actor"], str(r["day"]), r["count"]) for r in journal] == [
                ("brain_v42", "2026-08-30", 2),
                ("dream-codex-reorg", "2026-08-31", 1),
            ]
            # The queue is still emptied: ADR #21 keeps `access_log` transient,
            # the journal is a SECOND store and not a replacement.
            async with engine.connect() as conn:
                left = await conn.scalar(
                    sa.text("SELECT count(*) FROM access_log WHERE entity_id = :i"),
                    {"i": entity_id},
                )
            assert left == 0
        finally:
            await _cleanup(engine, entity_id)

    async def test_the_actor_string_is_kept_verbatim_not_folded_to_a_boolean(
        self, engine: AsyncEngine
    ) -> None:
        """The whole point: `is_human_actor` must stay replayable afterwards.

        The counters keep a boolean; this table keeps the string that produced
        it. A `dream-` actor and a human one must remain distinguishable rows.
        """
        entity_id = uuid4()
        try:
            await _queue(
                engine,
                [
                    ("decision", entity_id, "dream-codex-promote", _DAY_ONE),
                    ("decision", entity_id, "brain_v42", _DAY_ONE),
                ],
            )

            await _flush(engine)

            actors = {row["actor"] for row in await _journal(engine, entity_id)}
            assert actors == {"dream-codex-promote", "brain_v42"}
        finally:
            await _cleanup(engine, entity_id)

    async def test_a_second_flush_ACCUMULATES_and_does_not_overwrite(
        self, engine: AsyncEngine
    ) -> None:
        """`count = existing + excluded`, never `= excluded`.

        With the wrong operator every single-flush assertion above still passes,
        and the daily total silently becomes "whatever the last 300 s saw".
        """
        entity_id = uuid4()
        try:
            await _queue(engine, [("learning", entity_id, "brain_v42", _DAY_ONE)])
            await _flush(engine)
            await _queue(
                engine,
                [
                    ("learning", entity_id, "brain_v42", _DAY_ONE + timedelta(minutes=30)),
                    ("learning", entity_id, "brain_v42", _DAY_ONE + timedelta(minutes=40)),
                ],
            )

            await _flush(engine)

            journal = await _journal(engine, entity_id)
            assert len(journal) == 1, "same entity, same actor, same day = ONE row"
            assert journal[0]["count"] == 3
            # The freshest instant wins, and a late flush of older events must
            # not walk it backwards.
            assert journal[0]["last_accessed_at"] == _DAY_ONE + timedelta(minutes=40)
        finally:
            await _cleanup(engine, entity_id)

    async def test_an_older_event_flushed_late_does_not_rewind_the_last_access(
        self, engine: AsyncEngine
    ) -> None:
        entity_id = uuid4()
        try:
            await _queue(engine, [("learning", entity_id, "brain_v42", _DAY_ONE)])
            await _flush(engine)
            await _queue(
                engine, [("learning", entity_id, "brain_v42", _DAY_ONE - timedelta(hours=3))]
            )

            await _flush(engine)

            journal = await _journal(engine, entity_id)
            assert journal[0]["count"] == 2
            assert journal[0]["last_accessed_at"] == _DAY_ONE
        finally:
            await _cleanup(engine, entity_id)


class TestTheDowngradeIsAFence:
    async def test_it_refuses_and_names_the_days_it_would_destroy(
        self, engine: AsyncEngine, migration_downgrade_fence
    ) -> None:
        """A mute downgrade would succeed and look healthy — nothing opposes a DROP.

        What disappears is the only surviving trace of who read what: the source
        events were deleted 300 s after they happened.
        """
        entity_id = uuid4()
        await _queue(engine, [("learning", entity_id, "brain_v42", _DAY_ONE)])
        await _flush(engine)
        migration_downgrade_fence("051")
        db_url = os.environ["BRAIN_V42_TEST_DB_URL"]

        try:
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "downgrade", "051"],
                env={**os.environ, "POSTGRES_URL": db_url},
                cwd=str(_PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )

            assert result.returncode != 0, "the downgrade destroyed the journal in silence"
            assert "cannot downgrade 052" in result.stderr
            assert "2026-08-30" in result.stderr, "a count without the days leaves no move"
            assert "allow_access_log_daily_downgrade=yes" in result.stderr

            async with engine.connect() as conn:
                head = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
                still_there = await conn.scalar(
                    sa.text("SELECT count(*) FROM access_log_daily WHERE entity_id = :i"),
                    {"i": entity_id},
                )
            # DERIVED: a refused downgrade leaves the head where it was. Written
            # as "052" this would fail the day 053 lands, for a reason that has
            # nothing to do with what it tests — the 051 bench learned that today.
            assert head == repository_head()
            assert still_there == 1
        finally:
            await _cleanup(engine, entity_id)

    async def test_the_named_opt_in_lets_a_deliberate_operator_through(
        self, engine: AsyncEngine, migration_downgrade_fence
    ) -> None:
        """A fence, not a wall."""
        entity_id = uuid4()
        await _queue(engine, [("learning", entity_id, "brain_v42", _DAY_ONE)])
        await _flush(engine)
        migration_downgrade_fence("051")
        db_url = os.environ["BRAIN_V42_TEST_DB_URL"]

        try:
            down = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-x",
                    "allow_access_log_daily_downgrade=yes",
                    "downgrade",
                    "051",
                ],
                env={**os.environ, "POSTGRES_URL": db_url},
                cwd=str(_PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert down.returncode == 0, down.stderr

            async with engine.connect() as conn:
                head = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
                present = await conn.scalar(sa.text("SELECT to_regclass('access_log_daily')"))
            assert head == "051"
            assert present is None
        finally:
            # This bench shares its database. The fence fixture would restore the
            # head and SAY SO — that message is a defect report, not a service.
            # `head`, never a literal: this bench shares its database with every
            # test that runs after it.
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                env={**os.environ, "POSTGRES_URL": db_url},
                cwd=str(_PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )
            await _cleanup(engine, entity_id)
