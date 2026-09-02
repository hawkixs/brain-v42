"""The four concurrency scenarios `SPEC-checkpoint.md` §5 requires, plus its two absences.

Every one of these is a claim about what happens when two writers meet, and a
mocked session cannot make such a claim: the unique key, the `ON CONFLICT DO
NOTHING`, and the row lock that bounds the ceiling are all database behaviour.

The pair of ABSENCE tests is the part most likely to rot. ADR D4 described a
heartbeat side effect until it was amended in place on 2026-09-02, and §0bis.4
dissolves it: an `agent` session's liveness comes from `last_observed_at`, which
every tool call moves, so a checkpoint that also refreshed `last_heartbeat_at`
would make a session keep itself alive by narrating — the false-alive state ticket
`2bd14b24` condemns. These two tests are what stops that from coming back by
accident.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.models.brain_session import (
    MAX_CHECKPOINTS_PER_SESSION,
    BrainSessionCheckpointConflictError,
    BrainSessionInputError,
)
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

pytestmark = pytest.mark.integration

_KEY = "w43-checkpoint-concurrency"


@pytest.fixture
async def scene(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[PgBrainSessionRepo, str, UUID]:
    """A project and one open session, torn down past the append-only trigger."""
    project_key = f"integ-cp-{uuid4().hex[:10]}"
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "INSERT INTO project_contexts (project_key, name, description) "
                "VALUES (:p, :p, 'checkpoint concurrency scene')"
            ),
            {"p": project_key},
        )
    repo = PgBrainSessionRepo(session_factory)
    started = await repo.start(project_key, _KEY)
    session_id = UUID(str(started.session.id))

    yield repo, project_key, session_id

    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "ALTER TABLE brain_session_checkpoints "
                "DISABLE TRIGGER brain_session_checkpoints_append_only"
            )
        )
        await session.execute(
            sa.text("DELETE FROM brain_session_checkpoints WHERE session_id = :s"),
            {"s": session_id},
        )
        await session.execute(
            sa.text(
                "ALTER TABLE brain_session_checkpoints "
                "ENABLE TRIGGER brain_session_checkpoints_append_only"
            )
        )
        await session.execute(
            sa.text("DELETE FROM brain_sessions WHERE project_key = :p"), {"p": project_key}
        )
        await session.execute(
            sa.text("DELETE FROM project_contexts WHERE project_key = :p"), {"p": project_key}
        )


async def _count(session_factory: async_sessionmaker[AsyncSession], session_id: UUID) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                sa.text("SELECT count(*) FROM brain_session_checkpoints WHERE session_id = :s"),
                {"s": session_id},
            )
            or 0
        )


async def test_1_an_exact_replay_stores_one_row_and_keeps_its_first_timestamp(
    scene: tuple[PgBrainSessionRepo, str, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§5.1 — the property the key buys and a CAS does not: a retry is free.

    `created_at` UNCHANGED is the load-bearing assertion. A replay that restamped
    the row would silently rewrite when the judgment was formed, which is the one
    fact a reader uses to place it in time.
    """
    repo, _, session_id = scene
    payload = {
        "seq": 1,
        "progress": "wired the shim",
        "next_step": "prove the trigger",
        "blocker": None,
    }

    first = await repo.checkpoint(session_id, _KEY, **payload)
    second = await repo.checkpoint(session_id, _KEY, **payload)

    assert first.replayed is False
    assert second.replayed is True
    assert second.created_at == first.created_at
    assert second.checkpoint_count == 1
    assert await _count(session_factory, session_id) == 1


async def test_2_the_same_seq_with_other_content_is_refused_and_writes_nothing(
    scene: tuple[PgBrainSessionRepo, str, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§5.2 — the cost of dropping the CAS, paid back explicitly.

    `ON CONFLICT DO NOTHING` is silent for a replay AND for a collision. Left
    there, a second caller reusing a `seq` with different judgment would be
    swallowed without a word. PLAN §4 calls that a non-destructive conflict to be
    REJECTED, so the stored row is reread and compared.
    """
    repo, _, session_id = scene
    await repo.checkpoint(session_id, _KEY, seq=1, progress="first", next_step="then", blocker=None)

    with pytest.raises(BrainSessionCheckpointConflictError):
        await repo.checkpoint(
            session_id, _KEY, seq=1, progress="DIFFERENT", next_step="then", blocker=None
        )

    async with session_factory() as session:
        kept = await session.scalar(
            sa.text("SELECT progress FROM brain_session_checkpoints WHERE session_id = :s"),
            {"s": session_id},
        )
    assert kept == "first"
    assert await _count(session_factory, session_id) == 1


async def test_3_two_concurrent_writers_on_distinct_seqs_lose_nothing(
    scene: tuple[PgBrainSessionRepo, str, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§5.3 — parallel writers, distinct seqs: both rows land, ordering stays by seq."""
    repo, _, session_id = scene

    await asyncio.gather(
        *(
            repo.checkpoint(
                session_id, _KEY, seq=seq, progress=f"p{seq}", next_step=f"n{seq}", blocker=None
            )
            for seq in (1, 2, 3, 4)
        )
    )

    assert await _count(session_factory, session_id) == 4
    recent = await repo.recent_checkpoints(session_id)
    assert [c.seq for c in recent] == [4, 3, 2, 1]


async def test_4_the_ceiling_holds_under_a_race_and_never_overshoots(
    scene: tuple[PgBrainSessionRepo, str, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§5.4 — the 201st fails fail-closed, and the real count never exceeds 200.

    The ceiling is read INSIDE the row lock the checkpoint takes on the session,
    which is what stops two writers both reading 199 and both inserting. Ten
    simultaneous attempts past the ceiling, so the race is real rather than
    narrated.
    """
    repo, _, session_id = scene
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "INSERT INTO brain_session_checkpoints (session_id, seq, progress, next_step) "
                "SELECT :s, g, 'bulk', 'bulk' FROM generate_series(1, :n) AS g"
            ),
            {"s": session_id, "n": MAX_CHECKPOINTS_PER_SESSION},
        )

    results = await asyncio.gather(
        *(
            repo.checkpoint(
                session_id,
                _KEY,
                seq=MAX_CHECKPOINTS_PER_SESSION + offset,
                progress="over",
                next_step="over",
                blocker=None,
            )
            for offset in range(1, 11)
        ),
        return_exceptions=True,
    )

    # The ceiling's OWN refusal, not merely "something raised": a test happy with
    # any exception would stay green if the ten calls started failing on a
    # deadlock, a closed pool or a typo, and would report a guard that had stopped
    # guarding.
    assert all(isinstance(outcome, BrainSessionInputError) for outcome in results), results
    assert all(str(MAX_CHECKPOINTS_PER_SESSION) in str(outcome) for outcome in results)
    assert await _count(session_factory, session_id) == MAX_CHECKPOINTS_PER_SESSION


async def test_5_a_real_checkpoint_leaves_last_heartbeat_at_untouched(
    scene: tuple[PgBrainSessionRepo, str, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§5, absence 1 — the sentence ADR D4 asserted until it was amended.

    If this ever goes green by accident, a session keeps itself alive by narrating:
    the false-alive state `2bd14b24` condemns, reintroduced through the back door.
    """
    repo, _, session_id = scene

    async with session_factory() as session:
        before = await session.scalar(
            sa.text("SELECT last_heartbeat_at FROM brain_sessions WHERE id = :s"),
            {"s": session_id},
        )

    await repo.checkpoint(session_id, _KEY, seq=1, progress="p", next_step="n", blocker=None)

    async with session_factory() as session:
        after = await session.scalar(
            sa.text("SELECT last_heartbeat_at FROM brain_sessions WHERE id = :s"),
            {"s": session_id},
        )
    assert after == before


async def test_6_a_real_checkpoint_leaves_the_project_focus_revision_untouched(
    scene: tuple[PgBrainSessionRepo, str, UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§5, absence 2 — no focus CAS, so no `focus_outcome = 'conflict'` on siblings.

    A checkpoint that bumped the revision would make every parallel session's
    pending `end` conflict, turning a note into a lifecycle event for someone else.
    """
    repo, project_key, session_id = scene

    async with session_factory() as session:
        before = await session.scalar(
            sa.text("SELECT focus_revision FROM project_contexts WHERE project_key = :p"),
            {"p": project_key},
        )

    await repo.checkpoint(session_id, _KEY, seq=1, progress="p", next_step="n", blocker=None)

    async with session_factory() as session:
        after = await session.scalar(
            sa.text("SELECT focus_revision FROM project_contexts WHERE project_key = :p"),
            {"p": project_key},
        )
    assert after == before
