"""Auto-open and observation against a real database — the PARTIAL index at work.

What the unit harness CANNOT prove, and which is the whole point:

- the ``ON CONFLICT`` does infer on ``uq_brain_sessions_connection``, and the index
  predicate's ``WHERE status = 'open'`` is what makes REOPENING possible after a
  nightly closure. A FULL index would burn the connection for life at the first
  auto-closure — the trap inherited from `SPEC-M-G` §5, which is only visible here;
- ``client_key`` receives a fresh UUID because ``uq_brain_sessions_project_client``
  is FULL: reusing a stable per-connection key would fail that same reopening, the
  trap moved one column over;
- and BOTH clocks really move, which no assertion on compiled SQL can establish.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import brain_sessions, project_contexts
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Identity:
    project_key: str
    connection_id: str
    started_by_actor: str = "integ-actor"
    nature: str = "agent"
    intent: str | None = None


@pytest_asyncio.fixture
async def autoopen_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    project_key = f"integ-autoopen-{uuid4().hex[:10]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Auto-open integration",
                description="Isolated auto-open fixture",
                current_focus="focus de la traçante",
            )
        )
    try:
        yield project_key
    finally:
        async with session_factory.begin() as session:
            await session.execute(
                brain_sessions.delete().where(brain_sessions.c.project_key == project_key)
            )
            await session.execute(
                project_contexts.delete().where(project_contexts.c.project_key == project_key)
            )


async def _row(session_factory: async_sessionmaker[AsyncSession], session_id):
    async with session_factory() as session:
        return (
            (
                await session.execute(
                    sa.select(brain_sessions).where(brain_sessions.c.id == session_id)
                )
            )
            .mappings()
            .one()
        )


async def test_the_first_call_writes_the_five_046_columns(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """046 laid down five columns and zero writers. Here are the four written."""
    identity = _Identity(autoopen_project, uuid4().hex)
    now = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)

    opened = await PgBrainSessionRepo(session_factory).auto_open(identity, now=now)

    assert opened is not None
    row = await _row(session_factory, opened)
    assert row["nature"] == "agent"
    assert row["connection_id"] == identity.connection_id
    assert row["started_by_actor"] == "integ-actor"
    assert row["last_observed_at"] == now
    # `intent` is the only JUDGEMENT field of the five: NULL means "not measured",
    # and the server does not manufacture one.
    assert row["intent"] is None
    assert row["status"] == "open"


async def test_a_second_call_on_the_same_connection_redates_the_same_row(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """`DO UPDATE`, not `DO NOTHING`: a conflict IS an observation.

    With its NEGATIVE WITNESS — the clock must have MOVED. Without it, a
    `DO NOTHING` followed by a `SELECT` would return the same id and would leave this
    test green, while `last_observed_at` stayed frozen at the opening time and the
    4 h rule ended up taking an active connection.
    """
    repo = PgBrainSessionRepo(session_factory)
    identity = _Identity(autoopen_project, uuid4().hex)
    opened_at = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
    again_at = opened_at + timedelta(hours=3)

    first = await repo.auto_open(identity, now=opened_at)
    second = await repo.auto_open(identity, now=again_at)

    assert second == first
    row = await _row(session_factory, first)
    assert row["last_observed_at"] == again_at
    assert row["last_heartbeat_at"] == again_at

    async with session_factory() as session:
        total = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(brain_sessions)
                .where(brain_sessions.c.project_key == autoopen_project)
            )
        ).scalar_one()
    assert total == 1, "un conflit ne doit jamais produire une seconde session"


async def test_a_closed_session_does_not_burn_its_connection(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """THE test the partial index exists to make true.

    After a nightly closure, the SAME connection must be able to reopen. A full
    UNIQUE index — on the model of `uq_brain_sessions_project_client` — would make
    this insert impossible forever, and `closed_inactive` precisely takes rows out of
    `status = 'open'` in bulk every night.
    """
    repo = PgBrainSessionRepo(session_factory)
    identity = _Identity(autoopen_project, uuid4().hex)
    first = await repo.auto_open(identity, now=datetime(2026, 8, 22, 3, 0, tzinfo=UTC))

    swept = await repo.sweep_open_sessions(
        older_than=timedelta(days=365),
        close_inactive_after=timedelta(hours=4),
        dry_run=False,
        now=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
    )
    assert swept.closed_inactive_count == 1

    second = await repo.auto_open(identity, now=datetime(2026, 8, 22, 9, 5, tzinfo=UTC))

    assert second is not None
    assert second != first
    assert (await _row(session_factory, first))["status"] == "closed_inactive"
    assert (await _row(session_factory, second))["status"] == "open"
    # `client_key` must differ: `uq_brain_sessions_project_client` is FULL, so a
    # stable per-connection key would have failed this reopening.
    assert (await _row(session_factory, second))["client_key"] != (
        await _row(session_factory, first)
    )["client_key"]


async def test_observe_reports_whether_the_session_is_still_open(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """The boolean that makes the memo be discarded — all three cases, in one test.

    Open `agent` ⇒ `True` and the clock moves. Closed ⇒ `False`, and that is a FACT,
    not a failure. Non-existent ⇒ `False` too: an unknown UUID must not raise on a
    path whose whole contract is never to raise.
    """
    repo = PgBrainSessionRepo(session_factory)
    identity = _Identity(autoopen_project, uuid4().hex)
    opened = await repo.auto_open(identity, now=datetime(2026, 8, 22, 3, 0, tzinfo=UTC))
    later = datetime(2026, 8, 22, 6, 0, tzinfo=UTC)

    assert await repo.observe(opened, now=later) is True
    assert (await _row(session_factory, opened))["last_observed_at"] == later
    assert await repo.observe(uuid4(), now=later) is False

    await repo.sweep_open_sessions(
        older_than=timedelta(days=365),
        close_inactive_after=timedelta(hours=1),
        dry_run=False,
        now=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
    )

    assert await repo.observe(opened, now=datetime(2026, 8, 22, 13, 0, tzinfo=UTC)) is False


async def test_observe_never_touches_an_operator_session(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """A HARD guard: a poisoned memo must not be able to date anything elsewhere.

    Auto-open only produces tracers, so this case should not exist. That is
    precisely why it is guarded in the database rather than in a comment: the
    invariants we believe unreachable are the ones whose violation goes unnoticed.
    """
    repo = PgBrainSessionRepo(session_factory)
    stamped = datetime(2026, 8, 22, 4, 0, tzinfo=UTC)
    async with session_factory.begin() as session:
        operator = (
            await session.execute(
                brain_sessions.insert()
                .values(
                    project_key=autoopen_project,
                    client_key=f"operator-{uuid4().hex[:8]}",
                    started_focus="focus opérateur",
                    started_focus_revision=1,
                    nature="operator",
                    last_observed_at=stamped,
                )
                .returning(brain_sessions.c.id)
            )
        ).scalar_one()

    assert await repo.observe(operator, now=datetime(2026, 8, 22, 10, 0, tzinfo=UTC)) is False
    assert (await _row(session_factory, operator))["last_observed_at"] == stamped


async def test_a_fresh_tracer_never_dates_its_heartbeat_before_its_start(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """`last_heartbeat_at >= started_at` — the invariant the DR contract counts.

    Measured in production on 2026-08-22: the tracer opened with a heartbeat dated
    **1.5 ms BEFORE** its own start, and
    `brain_runtime_032_036_037.focus_revision_violations` counted every row. The
    receipt went from 29/29 to 28/29 on BOTH variants of the asset.

    The cause is a clock disagreement, not a sign error: `reference` is read by the
    application BEFORE the transaction opens, while `started_at` falls on the
    database's `DEFAULT now()` — the TRANSACTION-START stamp, hence later. `start()`
    escapes the trap by setting neither column: both its clocks come from the same
    default. `auto_open` set only one, and it is the asymmetry that costs.

    This test runs WITHOUT an injected `now=` — that is the production form, and the
    only one where the two clocks' gap is the one production actually carried. An
    injected `now=` would make the gap so large that it would mask the fact that the
    defect measures in milliseconds.

    The defect is TRANSIENT and that is what makes it costly: the first `observe()`
    to come along pushes `last_heartbeat_at` forward and erases the violation. The DR
    check therefore blinks red/green depending on whether a fresh tracer has already
    called back, and a green receipt proves nothing.
    """
    identity = _Identity(autoopen_project, uuid4().hex)

    opened = await PgBrainSessionRepo(session_factory).auto_open(identity)

    assert opened is not None
    row = await _row(session_factory, opened)
    assert row["last_heartbeat_at"] >= row["started_at"], (
        "une traçante fraîche date sa présence avant son propre démarrage : "
        f"heartbeat={row['last_heartbeat_at']} < started={row['started_at']}"
    )
    # ONE single clock read, not three in a row. This is the witness that
    # distinguishes the fix from its counterfeit: bounding the gap to "less than a
    # second" would let two distinct reads through, hence the bug.
    assert row["started_at"] == row["last_heartbeat_at"] == row["last_observed_at"]


async def test_reobserving_a_tracer_moves_both_clocks_but_never_its_start(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """The fix's NEGATIVE WITNESS: `started_at` is an OPENING date.

    The shortest fix — slipping `started_at` into `_observation_columns()` — greens
    the previous test and lies: every re-observation would rewrite the opening date,
    the tracer would eternally be zero seconds old, and the 7-day sweep would never
    take anything again. A session that grows younger at every tool call is a worse
    defect than the one being repaired.

    This test therefore fails on that false fix, where the previous one would pass.
    Together the two bound the only correct form: the INSERT branch sets all three
    clocks from a single read, the `DO UPDATE` branch moves only two of them.
    """
    repo = PgBrainSessionRepo(session_factory)
    identity = _Identity(autoopen_project, uuid4().hex)
    opened_at = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
    again_at = opened_at + timedelta(hours=3)

    first = await repo.auto_open(identity, now=opened_at)
    assert first is not None
    assert (await _row(session_factory, first))["started_at"] == opened_at

    assert await repo.auto_open(identity, now=again_at) == first
    row = await _row(session_factory, first)
    assert row["last_observed_at"] == again_at
    assert row["last_heartbeat_at"] == again_at
    assert row["started_at"] == opened_at, "réobserver n'est pas rouvrir"


async def test_open_session_count_speaks_of_humans_never_of_tracers(
    session_factory: async_sessionmaker[AsyncSession],
    autoopen_project: str,
) -> None:
    """The counter displayed 36 when the real human concurrency was 2.

    Ticket 92fe7f0f: 63 open tracers measured on 2026-08-29, each pointing at a dead
    transport — and `open_session_count`, returned at every `brain_session_start`,
    adds them to the working sessions. A hurried reader sees a concurrency that does
    not exist there; this counter has already misled once, during the investigation
    of the absorption defect.

    The rule: the counter speaks of the sessions the EXPLICIT CYCLE governs —
    `nature IS NULL` (pre-046, human by construction) or non-`agent`. The tracers
    have their own counters (the sweep names them separately); hiding them from HERE
    is not hiding them, it is ceasing to pass them off as what they are not.
    """
    repo = PgBrainSessionRepo(session_factory)

    opened = await repo.auto_open(
        _Identity(autoopen_project, uuid4().hex),
        now=datetime(2026, 8, 29, 9, 0, tzinfo=UTC),
    )
    assert opened is not None

    started = await repo.start(autoopen_project, f"humain-{uuid4().hex[:8]}")

    assert started.open_session_count == 1, (
        "la traçante ouverte ne doit pas compter comme une session de travail"
    )
