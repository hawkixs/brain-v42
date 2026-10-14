"""Abandonment releases attribution without deleting its ledger (974f46f3)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import brain_session_artifacts, brain_sessions
from brain_v42.models.brain_session import BrainSessionCaptureConflictError
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from tests.integration.db.test_brain_sessions_derived_absorption import (
    _absorb_outcome_from_the_current_connection,
    _attribution_mode,
    _derive_one_artifact,
    _derived_capture,
    _Identity,
    _ledger_owner,
    _transport,
    absorption_project,  # noqa: F401
)

pytestmark = pytest.mark.integration


async def _held_artifact(
    session_factory: async_sessionmaker[AsyncSession], project_key: str
) -> tuple[PgBrainSessionRepo, UUID, UUID, UUID, str]:
    """Start the recipient before creation so explicit capture's window applies."""
    repo = PgBrainSessionRepo(session_factory)
    holder = await repo.start(project_key, "holder")
    target = await repo.start(project_key, "target")
    with _derived_capture(True), _transport(uuid4().hex) as connection:
        await repo.auto_open(_Identity(project_key, connection))
        artifact = await _derive_one_artifact(PgLearningRepo(session_factory), project_key)
        await repo.record_seen_connection(holder.session.id, connection)
        await repo.capture(holder.session.id, "holder", [artifact])
    assert await _ledger_owner(session_factory, artifact) == holder.session.id
    return repo, holder.session.id, target.session.id, artifact, connection


@pytest.mark.parametrize("nature", [None, "operator"])
async def test_explicit_capture_reclaims_an_abandoned_operator_ledger(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,  # noqa: F811
    nature: str | None,
) -> None:
    repo, holder, target, artifact, _ = await _held_artifact(session_factory, absorption_project)
    async with session_factory.begin() as session:
        await session.execute(
            brain_sessions.update().where(brain_sessions.c.id == holder).values(nature=nature)
        )
    await repo.abandon(holder, "holder", "Work handed over")

    result = await repo.capture(target, "target", [artifact])

    assert artifact in result.captured_knowledge_ids
    assert await _ledger_owner(session_factory, artifact) == target
    assert await _attribution_mode(session_factory, artifact) == "explicit"


@pytest.mark.parametrize("stage", ["connection", "window"])
async def test_absorption_reclaims_an_abandoned_operator_ledger(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,  # noqa: F811
    stage: str,
) -> None:
    repo, holder, target, artifact, connection = await _held_artifact(
        session_factory, absorption_project
    )
    await repo.abandon(holder, "holder", "Work handed over")
    with _derived_capture(True), _transport(connection if stage == "connection" else uuid4().hex):
        outcome = await _absorb_outcome_from_the_current_connection(repo, target, "target")

    assert outcome.total == 1
    assert (outcome.moved_by_connection, outcome.moved_by_window) == (
        (1, 0) if stage == "connection" else (0, 1)
    )
    assert await _ledger_owner(session_factory, artifact) == target
    assert await _attribution_mode(session_factory, artifact) == f"derived_{stage}"
    assert holder in outcome.donors


@pytest.mark.parametrize("status", ["open", "ended"])
async def test_explicit_capture_keeps_a_live_or_ended_operator_holder_protected(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,  # noqa: F811
    status: str,
) -> None:
    repo, holder, target, artifact, _ = await _held_artifact(session_factory, absorption_project)
    if status == "ended":
        await repo.end(
            holder, "holder", "Work complete", "Next work", 0, None, allow_focus_shrink=True
        )

    with pytest.raises(BrainSessionCaptureConflictError):
        await repo.capture(target, "target", [artifact])
    assert await _ledger_owner(session_factory, artifact) == holder


@pytest.mark.parametrize("stage", ["connection", "window"])
@pytest.mark.parametrize("status", ["open", "ended"])
async def test_absorption_keeps_a_live_or_ended_operator_holder_protected(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,  # noqa: F811
    status: str,
    stage: str,
) -> None:
    """Only an abandoned operator ledger is a donor: absorption never takes the others."""
    repo, holder, target, artifact, connection = await _held_artifact(
        session_factory, absorption_project
    )
    if status == "ended":
        await repo.end(
            holder, "holder", "Work complete", "Next work", 0, None, allow_focus_shrink=True
        )
    with _derived_capture(True), _transport(connection if stage == "connection" else uuid4().hex):
        outcome = await _absorb_outcome_from_the_current_connection(repo, target, "target")

    assert outcome.total == 0
    assert holder not in outcome.donors
    assert await _ledger_owner(session_factory, artifact) == holder


@pytest.mark.parametrize("shared_connection", [True, False])
async def test_connection_absorption_abstains_only_for_a_rival_that_saw_the_connection(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,  # noqa: F811
    shared_connection: bool,
) -> None:
    repo = PgBrainSessionRepo(session_factory)
    target = await repo.start(absorption_project, "target")
    rival = await repo.start(absorption_project, "rival")
    with _derived_capture(True), _transport(uuid4().hex) as connection:
        tracer = await repo.auto_open(_Identity(absorption_project, connection))
        artifact = await _derive_one_artifact(PgLearningRepo(session_factory), absorption_project)
        if shared_connection:
            await repo.record_seen_connection(rival.session.id, connection)
        outcome = await _absorb_outcome_from_the_current_connection(
            repo, target.session.id, "target"
        )

    if shared_connection:
        assert outcome.total == 0
        assert outcome.reason == "ambiguous"
        assert outcome.rival_artifacts == 1
        assert rival.session.id in outcome.rival_sessions
        assert await _ledger_owner(session_factory, artifact) == tracer
    else:
        assert outcome.moved_by_connection == 1
        assert await _ledger_owner(session_factory, artifact) == target.session.id


async def test_a_connection_rival_cannot_be_bypassed_by_the_temporal_fallback(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,  # noqa: F811
) -> None:
    """A rival arriving after creation blocks the shared connection, not the instant."""
    repo = PgBrainSessionRepo(session_factory)
    target = await repo.start(absorption_project, "target")
    with _derived_capture(True), _transport(uuid4().hex) as connection:
        tracer = await repo.auto_open(_Identity(absorption_project, connection))
        artifact = await _derive_one_artifact(PgLearningRepo(session_factory), absorption_project)
        rival = await repo.start(absorption_project, "late-rival")
        await repo.record_seen_connection(rival.session.id, connection)

        outcome = await _absorb_outcome_from_the_current_connection(
            repo, target.session.id, "target"
        )

    assert outcome.total == 0
    assert outcome.reason == "ambiguous"
    assert outcome.rival_artifacts == 1
    assert rival.session.id in outcome.rival_sessions
    assert await _ledger_owner(session_factory, artifact) == tracer


async def test_abandoned_operator_artifacts_count_as_unattributed(
    session_factory: async_sessionmaker[AsyncSession],
    absorption_project: str,  # noqa: F811
) -> None:
    repo, holder, target, artifact, _ = await _held_artifact(session_factory, absorption_project)
    await repo.abandon(holder, "holder", "Work handed over")
    async with session_factory() as session:
        target_row = await repo._get_row(session, target)
        assert target_row is not None
        started_at = target_row["started_at"]
        count = await repo._count_unattributed_in_window(
            session, absorption_project, started_at, datetime.now(UTC)
        )
        assert count == 1
        assert (
            await session.scalar(
                sa.select(brain_session_artifacts.c.session_id).where(
                    brain_session_artifacts.c.knowledge_id == artifact
                )
            )
            == holder
        )
