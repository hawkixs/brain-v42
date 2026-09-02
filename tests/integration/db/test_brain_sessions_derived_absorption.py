"""Derived absorption facing a DEAD connection — the half that serves the user.

What the existing tests already prove, and why it is not enough: the derivation
deposits the artifact into ITS connection's tracer session, and the absorption
returns it to the user's session *as long as the connection has not changed*. Both
live in the same scene, under the same transport identifier, so the pairing cannot
fail there.

In production it does change. `connection_id` is the `Mcp-Session-Id`, a TRANSPORT
identifier minted by the SDK, killed by the 900 s idle timeout
(`mcp_http_session_idle_seconds`) long before the user closes their session.
Measured on 2026-08-25 on the first real closure after arming: the artifact was in
the tracer of connection `7588e2a2…`, dead 37 minutes earlier; the
`brain_session_end` arrived on `b7d8e65b…`, whose tracer was empty. The user's
ledger: 0. A tracer's median lifetime measured in the database is under 2 minutes,
against 16 h for the session it serves.

**The acceptance criterion is the PROMISE, not the mechanism**: an artifact created
without `brain_session_capture` lands in the user's session at closing time, even
if the connection that deposited it has died in between. These tests say NOTHING
about which pairing key to keep — that is under arbitration — and must stay green
whichever it is.

**They cannot self-fulfil their hypothesis.** The test never hands an identifier to
production: it sets the contextvar `ProvenanceMiddleware` would set, and the code
under test resolves whatever it wants from there. Setting the identifier by hand is
exactly the gesture that makes the current suite green while production is inert.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    brain_session_artifacts,
    brain_sessions,
    learnings,
    project_contexts,
)
from brain_v42.models.learning import LearningCreate
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Identity:
    """A mirror of `AutoOpenIdentity` — auto-open reads only these fields."""

    project_key: str
    connection_id: str
    started_by_actor: str = "integ-w20"
    nature: str = "agent"
    intent: str | None = None


@contextmanager
def _derived_capture(enabled: bool) -> Iterator[None]:
    """Open the derivation for real: the flag is read AT CALL TIME."""
    from brain_v42.config import get_settings

    key = "BRAIN_SESSION_DERIVED_CAPTURE_ENABLED"
    previous = os.environ.get(key)
    os.environ[key] = "true" if enabled else "false"
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous
        get_settings.cache_clear()


@contextmanager
def _transport(connection_id: str) -> Iterator[str]:
    """Play the middleware, and NOTHING else.

    This is the file's anti-cheat guard. `ProvenanceMiddleware` sets the
    `Mcp-Session-Id` on this contextvar and says nothing more about it; all the rest
    — which tracer, which donor — is a production decision. A test that handed the
    identifier to `absorb_derived_capture` would prove the SQL query and never the
    pairing.
    """
    from brain_v42.provenance import get_current_transport, set_current_transport

    previous = get_current_transport()
    set_current_transport(connection_id)
    try:
        yield connection_id
    finally:
        set_current_transport(previous)


async def _absorb_from_the_current_connection(
    repo: PgBrainSessionRepo, session_id: UUID, client_key: str
) -> int:
    """Absorb exactly as `BrainSessionService._absorb_derived` does.

    The connection comes from the contextvar, never from the caller: that is all
    the server knows at the moment the user closes their session.

    `client_key` is passed because the IDENTITY GUARD lives in the absorption
    itself, in the same transaction as the mutation. A bench that did not pass it
    would prove a path production does not take.
    """
    from brain_v42.provenance import get_current_transport

    connection_id = (get_current_transport() or "").strip()
    assert connection_id, "le banc doit tourner sous un transport"
    return await repo.absorb_derived_capture(session_id, connection_id, client_key)


@pytest_asyncio.fixture
async def absorption_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    project_key = f"integ-w20-{uuid4().hex[:10]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Derived absorption integration",
                description="Isolated fixture for the derived-absorption bench",
                current_focus="focus de la session utilisateur",
            )
        )
    try:
        yield project_key
    finally:
        async with session_factory.begin() as session:
            await session.execute(
                brain_session_artifacts.delete().where(
                    brain_session_artifacts.c.session_id.in_(
                        sa.select(brain_sessions.c.id).where(
                            brain_sessions.c.project_key == project_key
                        )
                    )
                )
            )
            await session.execute(
                brain_sessions.delete().where(brain_sessions.c.project_key == project_key)
            )
            await session.execute(learnings.delete().where(learnings.c.project_key == project_key))
            await session.execute(
                project_contexts.delete().where(project_contexts.c.project_key == project_key)
            )


async def _ledger_owner(
    session_factory: async_sessionmaker[AsyncSession], knowledge_id: UUID
) -> UUID | None:
    """Re-read the owner FROM THE DATABASE, never from the tool's return value."""
    async with session_factory() as session:
        owners = (
            (
                await session.execute(
                    sa.select(brain_session_artifacts.c.session_id).where(
                        brain_session_artifacts.c.knowledge_id == knowledge_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(owners) <= 1, "le ledger est EXCLUSIF : un artefact, un propriétaire"
    return UUID(str(owners[0])) if owners else None


async def _derive_one_artifact(learning_repo: PgLearningRepo, project_key: str) -> UUID:
    """Create an artifact WITHOUT any explicit capture. That is the whole point.

    Through the REAL repository: `derive_capture` is called from
    `BasePgRepository.create`, so an insert written by hand into `learnings` would
    derive nothing and the bench would describe an empty world.
    """
    created = await learning_repo.create(
        LearningCreate(
            topic=f"w20 derived {uuid4().hex[:8]}",
            insight="Artefact du banc d'absorption ; aucune portée opérationnelle.",
            project_key=project_key,
            confidence="low",
        )
    )
    return UUID(str(created.id))


async def test_the_user_session_absorbs_what_a_dead_connection_left_behind(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """THE promise, across a connection change. RED today.

    Three moments, and the second is the one production goes through ~26 times a
    day: the connection that deposited the artifact no longer exists when the user
    closes. No server restart is simulated — there is no need, and simulating one
    would test the wrong thing: changing `Mcp-Session-Id` is the WHOLE hole.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        # (1) The user's session. It carries NO connection — measured in
        # production: 490 non-`agent` rows, zero `connection_id`.
        started = await repo.start(absorption_project, "task-w20")
        user_session = UUID(str(started.session.id))

        # (2) Connection A: a tracer opens, the artifact derives itself into it.
        with _transport(uuid4().hex) as connection_a:
            tracer = await repo.auto_open(_Identity(absorption_project, connection_a))
            assert tracer is not None, "sans traçante, la scène ne prouve rien"
            artifact = await _derive_one_artifact(learning_repo, absorption_project)
            assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer)), (
                "la dérivation elle-même est cassée — ce rouge ne dirait rien de l'absorption"
            )

        # (3) Connection A is DEAD. In production the 900 s idle timeout killed it,
        # and nothing replays it. The user comes back on a fresh connection, whose
        # tracer is empty, and closes their session.
        with _transport(uuid4().hex) as connection_b:
            assert connection_b != connection_a
            await repo.auto_open(_Identity(absorption_project, connection_b))
            moved = await _absorb_from_the_current_connection(repo, user_session, "task-w20")

    assert moved == 1, (
        "la session de l'utilisateur n'a rien absorbé : l'artefact est resté "
        "dans la traçante d'une connexion morte"
    )
    assert await _ledger_owner(session_factory, artifact) == user_session


async def test_the_same_scene_on_a_single_connection_already_converges(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """A BENCH witness, and it claims nothing about the defect.

    An identical scene, one variable apart: the connection does not change. If this
    one is green and the other red, the red bears on the connection CHANGE and not
    on a broken bench, a closed flag or a failing derivation. Without this witness,
    a red would be worth nothing.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True), _transport(uuid4().hex) as connection:
        started = await repo.start(absorption_project, "task-w20-witness")
        user_session = UUID(str(started.session.id))
        await repo.auto_open(_Identity(absorption_project, connection))
        artifact = await _derive_one_artifact(learning_repo, absorption_project)
        moved = await _absorb_from_the_current_connection(repo, user_session, "task-w20-witness")

    assert moved == 1
    assert await _ledger_owner(session_factory, artifact) == user_session


async def test_the_user_session_never_carries_the_connection_that_served_it(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """Why the session cannot find "its" connection on its own.

    Measured in production on 2026-08-25: `nature='user'` exists NOWHERE, and no
    non-`agent` session has ever carried a `connection_id` (490 rows, zero).
    Resolution can therefore only go through the CURRENT connection of the call that
    closes — the one that, precisely, is no longer the right one.

    This test is GREEN today and must stay so: 046 refuses to promote a user session
    into a tracer, on pain of making it a ghost the 7-day sweep can no longer
    reach.
    """
    repo = PgBrainSessionRepo(session_factory)

    with _derived_capture(True), _transport(uuid4().hex):
        started = await repo.start(absorption_project, "task-w20-identity")

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(brain_sessions.c.connection_id, brain_sessions.c.nature).where(
                        brain_sessions.c.id == started.session.id
                    )
                )
            )
            .mappings()
            .one()
        )

    assert (row["connection_id"], row["nature"]) == (None, None)


_ATTRIBUTION_MODE = sa.text(
    "SELECT attribution_mode FROM brain_session_artifacts WHERE knowledge_id = :knowledge_id"
)


async def _attribution_mode(
    session_factory: async_sessionmaker[AsyncSession], knowledge_id: UUID
) -> str | None:
    """Re-read the pairing KEY from the database — raw text, never the Table object.

    A SQLAlchemy constant would stay silent about a column missing at compilation
    time; this text reddens plainly as long as 048 is not there.
    """
    async with session_factory() as session:
        rows = (await session.execute(_ATTRIBUTION_MODE, {"knowledge_id": knowledge_id})).all()
    assert rows, "aucune ligne de ledger pour cet artefact"
    return None if rows[0][0] is None else str(rows[0][0])


async def test_two_open_user_sessions_covering_the_instant_block_the_absorption(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """The AMBIGUITY witness. Without it, the rule structurally cannot fail.

    Two open non-`agent` sessions cover the creation instant. Neither has more claim
    than the other on the artifact: the rule REFUSES, and the artifact stays with the
    tracer — visible, not lost.

    This test must stay GREEN whichever key is kept. If it ever reddens, the rule has
    become permissive and attributes at random.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        mine = await repo.start(absorption_project, "task-w20-mine")
        rival = await repo.start(absorption_project, "task-w20-rival")
        assert rival.session.id != mine.session.id

        with _transport(uuid4().hex) as connection_a:
            tracer = await repo.auto_open(_Identity(absorption_project, connection_a))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)
            assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))

        with _transport(uuid4().hex) as connection_b:
            await repo.auto_open(_Identity(absorption_project, connection_b))
            moved = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-mine"
            )

    assert moved == 0, "deux prétendantes valent une abstention, jamais un tirage au sort"
    assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))


async def test_a_rival_that_closed_before_the_instant_is_not_a_rival(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """A rival closed BEFORE the instant covers nothing. RED today.

    Two reasons to redden today, and that is intended: nothing moves (the window
    layer does not exist) and `attribution_mode` does not exist yet. The second
    assertion is what forbids a silent regression: on a day when the pairing became
    a guess again, a total would stay green.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        rival = await repo.start(absorption_project, "task-w20-departed")
        await repo.abandon(rival.session.id, "task-w20-departed", "partie avant l'instant")
        mine = await repo.start(absorption_project, "task-w20-mine")

        with _transport(uuid4().hex) as connection_a:
            await repo.auto_open(_Identity(absorption_project, connection_a))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)

        with _transport(uuid4().hex) as connection_b:
            await repo.auto_open(_Identity(absorption_project, connection_b))
            moved = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-mine"
            )

    assert moved == 1
    assert await _ledger_owner(session_factory, artifact) == UUID(str(mine.session.id))
    assert await _attribution_mode(session_factory, artifact) == "derived_window"


async def test_a_rival_open_at_the_instant_still_blocks_after_it_has_closed(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """Coverage is judged at the INSTANT, not at command time.

    This is the only case that separates two possible readings of the rule: the
    rival was open when the artifact was born, then closed before the user issued
    the command. Judging "is it open NOW" would make the attribution depend on the
    closing order — two ambiguous sessions, and the last to close takes everything.
    So we judge the coverage.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        mine = await repo.start(absorption_project, "task-w20-mine")
        rival = await repo.start(absorption_project, "task-w20-rival")

        with _transport(uuid4().hex) as connection_a:
            tracer = await repo.auto_open(_Identity(absorption_project, connection_a))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)

        # The rival leaves AFTER the artifact's birth: it covered it.
        await repo.abandon(rival.session.id, "task-w20-rival", "partie après l'instant")

        with _transport(uuid4().hex) as connection_b:
            await repo.auto_open(_Identity(absorption_project, connection_b))
            moved = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-mine"
            )

    assert moved == 0
    assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))


async def test_a_human_can_reclaim_what_the_rule_refused_to_attribute(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """The counterpart of fail-closed: refusing is not losing.

    The exclusivity rule abstains as soon as two sessions overlap, and the artifact
    stays with the server. Without this path it would be a dead loss: `capture()`
    raised "session artifact ownership could not be resolved" on a row held by a
    tracer, so nobody could get it out any more. A human who NAMES the UUID must
    always be able to take it back.

    The mode becomes `explicit`: it is the only one that is a proof. The row stops
    being a server deduction the moment someone claims it.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        mine = await repo.start(absorption_project, "task-w20-claimant")
        await repo.start(absorption_project, "task-w20-rival")

        with _transport(uuid4().hex) as connection_a:
            tracer = await repo.auto_open(_Identity(absorption_project, connection_a))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)

        # The rule abstains — two claimants — and this is indeed the case we want to
        # repair by hand, not an artificial one.
        with _transport(uuid4().hex) as connection_b:
            await repo.auto_open(_Identity(absorption_project, connection_b))
            refused = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-claimant"
            )
        assert refused == 0
        assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))

        result = await repo.capture(mine.session.id, "task-w20-claimant", [artifact])

    assert artifact in result.captured_knowledge_ids
    assert await _ledger_owner(session_factory, artifact) == UUID(str(mine.session.id))
    assert await _attribution_mode(session_factory, artifact) == "explicit"


async def test_capture_never_takes_what_another_human_already_holds(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """The take-back is bounded by the holder's NATURE, never by the named UUID.

    This is what stops the graft from becoming a free pass: naming a UUID gives the
    right to take it back FROM THE SERVER, not from another human. The ledger's
    exclusivity exists precisely for that, and a repair path that stepped over it
    would be worse than the hole it plugs.
    """
    from brain_v42.models.brain_session import BrainSessionCaptureConflictError

    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        holder = await repo.start(absorption_project, "task-w20-holder")
        other = await repo.start(absorption_project, "task-w20-other")

        with _transport(uuid4().hex) as connection:
            tracer = await repo.auto_open(_Identity(absorption_project, connection))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)
            # The row really leaves the SERVER: with no tracer, this test would
            # exercise only the normal insertion and the boundary it claims to guard
            # — take back from the server, never from a human — would stay
            # untouched.
            assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))

        # A human claims it first, through the TAKE-BACK.
        await repo.capture(holder.session.id, "task-w20-holder", [artifact])
        assert await _attribution_mode(session_factory, artifact) == "explicit"

        with pytest.raises(BrainSessionCaptureConflictError):
            await repo.capture(other.session.id, "task-w20-other", [artifact])

    assert await _ledger_owner(session_factory, artifact) == UUID(str(holder.session.id))


async def _derive_one_decision(decision_repo: Any, project_key: str) -> UUID:
    """An artifact in the FIRST branch of the `UNION ALL` (`decisions`).

    The branch matters: with no `ORDER BY`, a `LIMIT` placed on the union serves the
    first branches first. Putting the contested artifacts at the front is what makes
    the witness deterministic instead of probabilistic.
    """
    from brain_v42.models.decision import DecisionCreate

    created = await decision_repo.create(
        DecisionCreate(
            title=f"w20 contested {uuid4().hex[:8]}",
            description="Artefact du banc de bornage ; aucune portée opérationnelle.",
            reasoning="Occuper la première branche du UNION ALL.",
            project_key=project_key,
        )
    )
    return UUID(str(created.id))


async def test_the_bound_never_hides_an_eligible_artifact_behind_contested_ones(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A1 — filter, THEN bound. A witness that does not exceed the cap would see nothing.

    The six tables' `UNION ALL` has no `ORDER BY`. Bounding BEFORE the rivalry filter
    let Postgres return an arbitrary batch — here the two contested decisions, served
    by the first branch — and the legitimate artifact, in the second branch, was
    never absorbed. Silently, and differently from one call to the next.

    The real threshold is not the cap of 100 but `100 - occupied`, which shrinks as a
    session accumulates: the case happens at the end of a long session, exactly when
    the absorption is most useful. So we lower the cap rather than manufacture a
    hundred artifacts — the defect bears on the RATIO between `remaining` and the
    number of eligible artifacts, never on the value 100.
    """
    import brain_v42.db.session_derived_capture as module
    from brain_v42.repositories.pg_decision import PgDecisionRepo

    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)
    decision_repo = PgDecisionRepo(session_factory)

    with _derived_capture(True):
        mine = await repo.start(absorption_project, "task-w20-bound")
        rival = await repo.start(absorption_project, "task-w20-bound-rival")

        with _transport(uuid4().hex) as connection_a:
            tracer = await repo.auto_open(_Identity(absorption_project, connection_a))
            # Two CONTESTED ones, in the UNION ALL's first branch, and as many as
            # `remaining`: under the old order they filled the batch on their own.
            contested = [await _derive_one_decision(decision_repo, absorption_project)]
            contested.append(await _derive_one_decision(decision_repo, absorption_project))

            # The rival leaves AFTER them: it covers them, and it will not cover
            # what follows.
            await repo.abandon(rival.session.id, "task-w20-bound-rival", "partie")

            legitimate = await _derive_one_artifact(learning_repo, absorption_project)

        for item in (*contested, legitimate):
            assert await _ledger_owner(session_factory, item) == UUID(str(tracer))

        # The cap is lowered only HERE. `derive_capture` reads the SAME cap:
        # lowering it earlier would have refused to deposit the third artifact, and
        # the witness would have described a window that does not overflow — exactly
        # the test that sees nothing.
        monkeypatch.setattr(module, "_capture_cap", lambda: 2)

        with _transport(uuid4().hex) as connection_b:
            await repo.auto_open(_Identity(absorption_project, connection_b))
            moved = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-bound"
            )

    assert moved == 1, (
        "les artefacts contestés ont rempli le lot avant que le filtre ne "
        "s'applique : l'artefact légitime est resté invisible"
    )
    assert await _ledger_owner(session_factory, legitimate) == UUID(str(mine.session.id))
    assert await _attribution_mode(session_factory, legitimate) == "derived_window"
    # The contested ones have not moved: bounding after filtering does not make the
    # rule permissive.
    for item in contested:
        assert await _ledger_owner(session_factory, item) == UUID(str(tracer))


async def test_the_repository_reads_the_ledger_not_the_terminal_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """`attributed_knowledge_ids` reads the SOURCE, not the terminal snapshot.

    `brain_sessions.captured_knowledge_ids` is only written at closing time, and the
    `open` constraint forbids it being filled before: measured on 2026-08-25, no open
    session has ever carried a non-empty one. An implementation that read the array
    would therefore ALWAYS return `[]` on a live session — and that is precisely the
    one-call lag this batch repairs.

    This test exists because `start` depends on it: it is the only one of the five
    that cannot absorb before materialising, and here it re-reads what it has just
    moved.
    """
    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        mine = await repo.start(absorption_project, "task-w20-read")
        with _transport(uuid4().hex) as connection:
            await repo.auto_open(_Identity(absorption_project, connection))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)
            moved = await _absorb_from_the_current_connection(
                repo, UUID(str(mine.session.id)), "task-w20-read"
            )

    assert moved == 1
    ids = await repo.attributed_knowledge_ids(mine.session.id)
    assert [UUID(str(item)) for item in ids] == [artifact]

    # And the terminal array is ALWAYS empty: the session is open.
    async with session_factory() as session:
        snapshot = (
            await session.execute(
                sa.select(brain_sessions.c.captured_knowledge_ids).where(
                    brain_sessions.c.id == mine.session.id
                )
            )
        ).scalar_one()
    assert list(snapshot or []) == [], (
        "si l'instantané se remplit sur une session ouverte, la contrainte `open` "
        "a changé et ce lot doit être relu en entier"
    )


async def test_a_mistargeted_absorption_moves_NOTHING_before_it_refuses(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,
) -> None:
    """The identity guard must precede the MUTATION, not only the command.

    `CLAUDE.md` is literal: "le serveur refuse une paire incohérente AVANT TOUTE
    MUTATION. Cette garde protège du mauvais ciblage entre sessions parallèles."
    The absorption mutated with no ownership check at all: a mis-targeted call moved
    a tracer's ledger into someone else's session, THEN got refused. The ledger being
    EXCLUSIVE, that move is IRREVERSIBLE — and the caller, who only sees a refusal,
    has no reason to suspect it.

    This test verifies the ABSENCE OF MUTATION, not the presence of the refusal: the
    refusal already existed, and it is what made the defect invisible.
    """
    from brain_v42.models.brain_session import BrainSessionIdentityConflictError

    repo = PgBrainSessionRepo(session_factory)
    learning_repo = PgLearningRepo(session_factory)

    with _derived_capture(True):
        victim = await repo.start(absorption_project, "task-w20-victim")

        with _transport(uuid4().hex) as connection:
            tracer = await repo.auto_open(_Identity(absorption_project, connection))
            artifact = await _derive_one_artifact(learning_repo, absorption_project)
            assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer))

            with pytest.raises(BrainSessionIdentityConflictError):
                await repo.absorb_derived_capture(
                    victim.session.id, connection, "task-w20-SOMEONE-ELSE"
                )

    assert await _ledger_owner(session_factory, artifact) == UUID(str(tracer)), (
        "le ledger a bougé avant que la paire incohérente ne soit refusée — et "
        "l'exclusivité du ledger rend ce déplacement irréversible"
    )
