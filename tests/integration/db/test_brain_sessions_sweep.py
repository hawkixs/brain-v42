"""Le balayage serveur contre une vraie base : frontière et invariants.

Seuil de 365 jours partout : le balayage est GLOBAL par conception, et la base
d'intégration est partagée. Antidater les lignes de la fixture et viser un an
rend structurellement impossible d'emporter la session d'un test voisin, qui
est forcément créée « maintenant ».
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
):
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

    result = await PgBrainSessionRepo(session_factory).abandon_stale(
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

    result = await PgBrainSessionRepo(session_factory).abandon_stale(
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

    await PgBrainSessionRepo(session_factory).abandon_stale(
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
    # Le snapshot terminal reste vide : c'est la contrainte CHECK
    # brain_sessions_terminal_state_valid pour 'abandoned'. Le ledger, lui,
    # vit dans brain_session_artifacts et survit.
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

    result = await PgBrainSessionRepo(session_factory).abandon_stale(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    assert ghost in {candidate.id for candidate in result.candidates}
    assert (await _read(session_factory, ghost))["status"] == "abandoned"
    assert manual not in {candidate.id for candidate in result.candidates}
    row = await _read(session_factory, manual)
    assert row["abandonment_reason"] == "abandon manuel de l'opérateur"
