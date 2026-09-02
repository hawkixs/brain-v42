"""The server sweep against a real database: boundary and invariants.

A 365-day threshold everywhere: the sweep is GLOBAL by design, and the integration
database is shared. Back-dating the fixture's rows and aiming at a year makes it
structurally impossible to take away a neighbouring test's session, which is
necessarily created "now".
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    brain_session_artifacts,
    brain_sessions,
    decisions,
    project_contexts,
)
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

pytestmark = pytest.mark.integration

THRESHOLD = timedelta(days=365)


@pytest_asyncio.fixture
async def sweep_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    project_key = f"integ-sweep-{uuid4().hex[:10]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Session sweep integration",
                description="Isolated sweep fixture",
                current_focus="focus avant balayage",
            )
        )
    try:
        yield project_key
    finally:
        async with session_factory.begin() as session:
            project_sessions = sa.select(brain_sessions.c.id).where(
                brain_sessions.c.project_key == project_key
            )
            await session.execute(
                brain_session_artifacts.delete().where(
                    brain_session_artifacts.c.session_id.in_(project_sessions)
                )
            )
            await session.execute(
                brain_sessions.delete().where(brain_sessions.c.project_key == project_key)
            )
            await session.execute(decisions.delete().where(decisions.c.project_key == project_key))
            await session.execute(
                project_contexts.delete().where(project_contexts.c.project_key == project_key)
            )


async def _insert_open_session(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    client_key: str,
    heartbeat: datetime,
    *,
    nature: str | None = None,
    observed: datetime | None = None,
):
    """Insert an OPEN session, with or without 046's columns.

    ``nature=None`` and ``observed=None`` by default: that is the state from BEFORE
    046, the state of every existing row, and it must stay the fixture's default case
    so that the earlier tests still prove what they proved.
    """
    async with session_factory.begin() as session:
        row = (
            await session.execute(
                brain_sessions.insert()
                .values(
                    project_key=project_key,
                    client_key=client_key,
                    started_focus="focus avant balayage",
                    started_focus_revision=1,
                    started_at=heartbeat,
                    last_heartbeat_at=heartbeat,
                    nature=nature,
                    last_observed_at=observed,
                )
                .returning(brain_sessions.c.id)
            )
        ).scalar_one()
    return row


async def _read(session_factory: async_sessionmaker[AsyncSession], session_id):
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


async def test_predicate_boundary_spares_n_minus_one_and_takes_n_plus_one(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    inside = await _insert_open_session(
        session_factory, sweep_project, "inside", now - THRESHOLD + timedelta(days=1)
    )
    outside = await _insert_open_session(
        session_factory, sweep_project, "outside", now - THRESHOLD - timedelta(days=1)
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    swept = {candidate.id for candidate in result.candidates}
    assert outside in swept
    assert inside not in swept
    assert (await _read(session_factory, inside))["status"] == "open"
    abandoned = await _read(session_factory, outside)
    assert abandoned["status"] == "abandoned"
    assert abandoned["abandonment_reason"] == "auto_stale_7d"
    assert abandoned["ended_at"] is not None
    assert abandoned["summary"] is None
    assert abandoned["next_focus"] is None
    assert abandoned["focus_outcome"] is None


async def test_dry_run_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    ghost = await _insert_open_session(
        session_factory, sweep_project, "ghost", now - THRESHOLD - timedelta(days=1)
    )
    before = await _read(session_factory, ghost)

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, dry_run=True, now=now
    )

    assert [candidate.id for candidate in result.candidates] == [ghost]
    assert result.abandoned_count == 0
    assert dict(await _read(session_factory, ghost)) == dict(before)


async def test_sweep_preserves_focus_revision_and_attributions(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    ghost = await _insert_open_session(
        session_factory, sweep_project, "ghost-with-capture", now - THRESHOLD - timedelta(days=2)
    )
    knowledge_id = uuid4()
    async with session_factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=knowledge_id,
                project_key=sweep_project,
                title="décision capturée avant le balayage",
                description="corps",
                reasoning="corps",
            )
        )
        await session.execute(
            brain_session_artifacts.insert().values(
                session_id=ghost,
                knowledge_id=knowledge_id,
                knowledge_type="decision",
            )
        )
    async with session_factory() as session:
        focus_before = (
            (
                await session.execute(
                    sa.select(
                        project_contexts.c.current_focus,
                        project_contexts.c.focus_revision,
                        project_contexts.c.focus_updated_at,
                    ).where(project_contexts.c.project_key == sweep_project)
                )
            )
            .mappings()
            .one()
        )

    await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    async with session_factory() as session:
        focus_after = (
            (
                await session.execute(
                    sa.select(
                        project_contexts.c.current_focus,
                        project_contexts.c.focus_revision,
                        project_contexts.c.focus_updated_at,
                    ).where(project_contexts.c.project_key == sweep_project)
                )
            )
            .mappings()
            .one()
        )
        attributions = (
            (
                await session.execute(
                    sa.select(brain_session_artifacts.c.knowledge_id).where(
                        brain_session_artifacts.c.session_id == ghost
                    )
                )
            )
            .scalars()
            .all()
        )

    assert dict(focus_after) == dict(focus_before)
    assert list(attributions) == [knowledge_id]
    swept = await _read(session_factory, ghost)
    assert swept["status"] == "abandoned"
    # The terminal snapshot stays empty: that is the
    # brain_sessions_terminal_state_valid CHECK constraint for 'abandoned'. The
    # ledger, for its part, lives in brain_session_artifacts and survives.
    assert list(swept["captured_knowledge_ids"]) == []


async def test_manual_abandonment_reason_is_never_overwritten(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    manual = await _insert_open_session(
        session_factory, sweep_project, "manual", now - THRESHOLD - timedelta(days=3)
    )
    async with session_factory.begin() as session:
        await session.execute(
            brain_sessions.update()
            .where(brain_sessions.c.id == manual)
            .values(
                status="abandoned",
                abandonment_reason="abandon manuel de l'opérateur",
                ended_at=now,
            )
        )
    # Positive control: a co-resident stale *open* session in the same sweep
    # call. Without it, this test only asserts a row nobody touched stayed
    # untouched — which a no-op implementation also satisfies. This ghost
    # must actually be swept, or the negative assertions below are vacuous.
    ghost = await _insert_open_session(
        session_factory, sweep_project, "ghost", now - THRESHOLD - timedelta(days=1)
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    assert ghost in {candidate.id for candidate in result.candidates}
    assert (await _read(session_factory, ghost))["status"] == "abandoned"
    assert manual not in {candidate.id for candidate in result.candidates}
    row = await _read(session_factory, manual)
    assert row["abandonment_reason"] == "abandon manuel de l'opérateur"


# ─── M-G: the 4 h rule against a real database ───────────────────────────────
#
# The unit harness proves the predicate's SHAPE; it can say nothing about 046's
# `brain_sessions_terminal_state_valid` CHECK, which only exists in PostgreSQL. A
# malformed `closed_inactive` row — an abandonment reason left in place, a forgotten
# `ended_at` — is only visible HERE, and it would bring the whole night down on a
# constraint violation.

INACTIVE = timedelta(hours=4)


async def test_an_agent_tracer_inactive_past_the_threshold_is_closed_not_abandoned(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """M-G's central fact, against the database: the 4th state is REACHABLE.

    And it is so under 046's CHECK, which forbids on that branch everything an
    abandonment requires. That the row exists after the statement is the proof that
    the `CASE` produces a shape the database accepts.
    """
    now = datetime.now(UTC)
    tracer = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-inactive",
        now - timedelta(hours=6),
        nature="agent",
        observed=now - timedelta(hours=6),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    assert result.closed_inactive_count == 1
    assert result.abandoned_count == 0
    closed = await _read(session_factory, tracer)
    assert closed["status"] == "closed_inactive"
    assert closed["ended_at"] is not None
    # The branch's four prohibitions, read back one by one: that is what the CHECK
    # requires, and it is what distinguishes this state from an abandonment.
    assert closed["abandonment_reason"] is None
    assert closed["summary"] is None
    assert closed["next_focus"] is None
    assert closed["focus_outcome"] is None
    assert closed["nothing_to_capture_reason"] is None


async def test_a_never_observed_session_survives_the_four_hour_rule(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """S3, settled — with its NEGATIVE WITNESS in the same test.

    `last_observed_at IS NULL` means "never observed", not "observed a long time
    ago". The pair is indispensable: the survivor alone would stay green if the rule
    NEVER took anyone, and the closed one alone would say nothing about `NULL`'s
    fate. The two sessions are identical but for one column, and that column is
    exactly the one S3 settles.
    """
    now = datetime.now(UTC)
    never_observed = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-never-observed",
        now - timedelta(hours=5),
        nature="agent",
        observed=None,
    )
    observed_long_ago = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-observed-5h",
        now - timedelta(hours=5),
        nature="agent",
        observed=now - timedelta(hours=5),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    swept = {candidate.id for candidate in result.candidates}
    assert never_observed not in swept
    assert observed_long_ago in swept
    assert (await _read(session_factory, never_observed))["status"] == "open"
    assert (await _read(session_factory, observed_long_ago))["status"] == "closed_inactive"


async def test_an_operator_session_is_never_closed_by_inactivity(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """The main guarantee is not the threshold, it is the NATURE (§0bis.3).

    A `claimed` session — hence `operator` — is never closed for inactivity, whatever
    its age. 046's CHECK makes it impossible in the database anyway: the
    `closed_inactive` branch requires `nature = 'agent'`. This test proves the
    PREDICATE does not even try, hence that the night does not fall on the
    constraint.
    """
    now = datetime.now(UTC)
    operator = await _insert_open_session(
        session_factory,
        sweep_project,
        "operator-idle",
        now - timedelta(hours=48),
        nature="operator",
        observed=now - timedelta(hours=48),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    assert operator not in {candidate.id for candidate in result.candidates}
    assert (await _read(session_factory, operator))["status"] == "open"


async def test_a_recently_observed_tracer_is_not_taken(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """The exit criterion SPEC-M-G §7 explicitly says is missing elsewhere.

    "Ne prouve rien sur les sessions actives : il faut aussi montrer qu'une session
    ayant observé un appel dans les 4 h n'est PAS prise."
    """
    now = datetime.now(UTC)
    active = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-active",
        now - timedelta(hours=30),
        nature="agent",
        observed=now - timedelta(minutes=3),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    assert active not in {candidate.id for candidate in result.candidates}
    assert (await _read(session_factory, active))["status"] == "open"


async def test_seven_days_wins_over_four_hours_on_a_session_matching_both(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """PRECEDENCE, proved on the written row — not on the compiled SQL.

    A tracer inactive for longer than the PRESENCE threshold matches both
    predicates. It must leave as `abandoned` WITH its reason, never as a mute
    `closed_inactive`: losing the reason means losing the only trace of WHY the
    session is terminated.
    """
    now = datetime.now(UTC)
    both = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-both-rules",
        now - THRESHOLD - timedelta(days=1),
        nature="agent",
        observed=now - THRESHOLD - timedelta(days=1),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    assert result.abandoned_count == 1
    assert result.closed_inactive_count == 0
    row = await _read(session_factory, both)
    assert row["status"] == "abandoned"
    assert row["abandonment_reason"] == "auto_stale_7d"


async def test_a_closed_rule_leaves_an_inactive_tracer_open(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """Shipped CLOSED, proved against the database and not only against the default.

    This is the mutation that matters most here: the sweep runs WET every night. If
    `close_inactive_after=None` did not close the rule completely, merging this batch
    would close sessions from the very next night.
    """
    now = datetime.now(UTC)
    tracer = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-rule-closed",
        now - timedelta(hours=9),
        nature="agent",
        observed=now - timedelta(hours=9),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=None, dry_run=False, now=now
    )

    assert result.inactive_cutoff is None
    assert tracer not in {candidate.id for candidate in result.candidates}
    assert (await _read(session_factory, tracer))["status"] == "open"


async def test_closing_a_tracer_preserves_its_ledger_and_the_project_focus(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """046's reason to exist, and the noise it refuses to manufacture.

    `abandoned` FORCES `cardinality(captured_knowledge_ids) = 0`: a tracer that has
    done its work would declare an empty ledger there. On `closed_inactive` the
    column has NO constraint at all — that is the migration's whole point.

    And `focus_revision` must be UNCHANGED: a bulk closure attempting a CAS per row
    would produce N−1 manufactured `conflict` on the concurrent operator sessions
    (`SPEC-M-G` §3.2).
    """
    now = datetime.now(UTC)
    tracer = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-with-ledger",
        now - timedelta(hours=7),
        nature="agent",
        observed=now - timedelta(hours=7),
    )
    knowledge_id = uuid4()
    async with session_factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=knowledge_id,
                project_key=sweep_project,
                title="décision attribuée à une traçante",
                description="corps",
                reasoning="corps",
            )
        )
        await session.execute(
            brain_session_artifacts.insert().values(
                session_id=tracer,
                knowledge_id=knowledge_id,
                knowledge_type="decision",
            )
        )

    async with session_factory() as session:
        revision_before = (
            await session.execute(
                sa.select(project_contexts.c.focus_revision).where(
                    project_contexts.c.project_key == sweep_project
                )
            )
        ).scalar_one()

    await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    async with session_factory() as session:
        surviving = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(brain_session_artifacts)
                .where(brain_session_artifacts.c.session_id == tracer)
            )
        ).scalar_one()
        revision_after = (
            await session.execute(
                sa.select(project_contexts.c.focus_revision).where(
                    project_contexts.c.project_key == sweep_project
                )
            )
        ).scalar_one()

    assert (await _read(session_factory, tracer))["status"] == "closed_inactive"
    assert surviving == 1, "le ledger d'une traçante fermée survit — c'est tout M-G"
    assert revision_after == revision_before, "aucun CAS tenté, donc aucun conflit fabriqué"


async def test_a_closed_inactive_row_survives_the_pydantic_rail(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """Trap (a): the rail must not refuse what the database accepts.

    `BrainSession`'s dispatcher no longer has a catch-all branch, it RAISES. Before
    046 it ended with `else: _validate_abandoned_state()`, which requires
    `abandonment_reason` — the field the `closed_inactive` branch FORBIDS. A
    perfectly valid row in the database would therefore have blown up every read
    that crossed it: `brain_session_list`, the briefing, the resume.

    This is the only test that round-trips the REAL row — written by the sweep, read
    back by the model. The unit tests build the model by hand and cannot see a gap
    between the CHECK and the rail.
    """
    now = datetime.now(UTC)
    tracer = await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-roundtrip",
        now - timedelta(hours=6),
        nature="agent",
        observed=now - timedelta(hours=6),
    )
    repo = PgBrainSessionRepo(session_factory)
    await repo.sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )

    loaded = await repo.get_by_id(tracer)

    assert loaded is not None
    assert loaded.status.value == "closed_inactive"
    assert loaded.nature == "agent"
    # `is_stale` is only true on `open`: a terminal session carrying it would make
    # the rail raise, and the sweep produces such sessions every night.
    assert loaded.is_stale is False

    listed = await repo.list(project_key=sweep_project, status="all")
    assert tracer in {session.id for session in listed.sessions}


async def test_the_persisted_series_keeps_the_two_counters_distinct_on_a_mixed_night(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    """Ticket 24ca3b73, the terminal requirement: the counter WRITTEN TO THE DATABASE
    is distinct from abandoned_count on a night where both are non-zero.

    Without 049's column, a night that closed 200 tracers for inactivity left NO time
    series at all — and a counter that added the two opposite-meaning events together
    would be worse than absent. The dream_runs row must carry 1 (the closures), never
    2 (the sum).
    """
    from brain_v42.maintenance.session_sweep import record_dream_run

    now = datetime.now(UTC)
    await _insert_open_session(
        session_factory,
        sweep_project,
        "humaine-au-dela-du-seuil",
        now - THRESHOLD - timedelta(days=1),
    )
    await _insert_open_session(
        session_factory,
        sweep_project,
        "agent-inactive-mixte",
        now - timedelta(hours=6),
        nature="agent",
        observed=now - timedelta(hours=6),
    )

    result = await PgBrainSessionRepo(session_factory).sweep_open_sessions(
        older_than=THRESHOLD, close_inactive_after=INACTIVE, dry_run=False, now=now
    )
    assert result.abandoned_count == 1
    assert result.closed_inactive_count == 1

    await record_dream_run(
        session_factory,
        "done",
        dry=False,
        duration_s=1.0,
        error=None,
        closed_inactive_count=result.closed_inactive_count,
    )
    try:
        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        sa.text(
                            "SELECT closed_inactive_count FROM dream_runs "
                            "WHERE phase='sweep' ORDER BY id DESC LIMIT 1"
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert row["closed_inactive_count"] == 1, (
            "la colonne porte les fermetures pour inactivité SEULES, jamais la somme"
        )
        assert row["closed_inactive_count"] != result.abandoned_count + result.closed_inactive_count
    finally:
        async with session_factory.begin() as session:
            await session.execute(
                sa.text("DELETE FROM dream_runs WHERE phase='sweep' AND duration_s=1.0")
            )
