"""Real PostgreSQL proof for the feature-dedup ownership mutation fence."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brain_v42.automation.dedup import run_dedup_loop
from brain_v42.automation.ownership import AutomationOwnershipLease, OwnershipLostError
from brain_v42.db.tables import feature_artifacts, features, gitlab_events, project_contexts
from brain_v42.services.feature_dedup_job import FeatureDedupJob

from .conftest import INTEGRATION_DB_URL

pytestmark = pytest.mark.integration


class BlockingEmbedding:
    """Hold the real merge after its authoritative read and before persistence."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def embed(self, _text: str) -> list[float]:
        self.entered.set()
        await self.resume.wait()
        return [0.9] * 1536

    async def embed_query(self, _text: str) -> list[float]:
        self.entered.set()
        await self.resume.wait()
        return [0.9] * 1536


class SingleCandidateJob:
    """Use real merge logic while replacing only candidate discovery."""

    def __init__(
        self,
        inner: FeatureDedupJob,
        project_key: str,
        target: SimpleNamespace,
        source: SimpleNamespace,
    ) -> None:
        self._inner = inner
        self._project_key = project_key
        self._target = target
        self._source = source

    async def find_candidates(
        self,
        project_key: str,
    ) -> list[tuple[SimpleNamespace, SimpleNamespace, float]]:
        if project_key != self._project_key:
            return []
        return [(self._target, self._source, 0.99)]

    async def merge_features(
        self,
        session: AsyncSession,
        target: SimpleNamespace,
        source: SimpleNamespace,
    ) -> bool:
        return await self._inner.merge_features(session, target, source)


def _engine() -> AsyncEngine:
    return create_async_engine(
        INTEGRATION_DB_URL,
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
    )


async def _assert_test_database(*engines: AsyncEngine) -> None:
    for engine in engines:
        async with engine.connect() as connection:
            database = await connection.scalar(sa.text("SELECT current_database()"))
        assert database == "brain_test", f"refusing dedup fence test against {database!r}"


async def _terminate_backend(admin_engine: AsyncEngine, pid: int) -> bool:
    async with admin_engine.begin() as connection:
        terminated = await connection.scalar(
            sa.text("SELECT pg_terminate_backend(:pid)"),
            {"pid": pid},
        )
    return bool(terminated)


async def _acquire_successor_after_detected_loss(
    predecessor: AutomationOwnershipLease,
    successor: AutomationOwnershipLease,
    *,
    timeout: float = 3.0,
) -> int:
    """Wait on the loss Event, then retry lock acquisition to a hard deadline."""
    await asyncio.wait_for(predecessor.ownership_lost.wait(), timeout=timeout)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    attempts = 0
    while loop.time() < deadline:
        attempts += 1
        if await successor.acquire():
            return attempts
        await asyncio.sleep(min(0.02, max(0.0, deadline - loop.time())))
    raise AssertionError(f"successor did not acquire before deadline after {attempts} attempts")


async def _dedup_state(
    engine: AsyncEngine,
    *,
    project_key: str,
    artifact_id: uuid.UUID,
    event_uuid: str,
) -> tuple[object, object, object]:
    async with engine.connect() as connection:
        feature_rows = (
            await connection.execute(
                sa.select(
                    features.c.id,
                    features.c.name,
                    features.c.description,
                    features.c.embedding,
                )
                .where(features.c.project_key == project_key)
                .order_by(features.c.name)
            )
        ).all()
        artifact_rows = (
            await connection.execute(
                sa.select(
                    feature_artifacts.c.feature_id,
                    feature_artifacts.c.artifact_type,
                    feature_artifacts.c.artifact_id,
                    feature_artifacts.c.similarity_score,
                ).where(feature_artifacts.c.artifact_id == artifact_id)
            )
        ).all()
        event_rows = (
            await connection.execute(
                sa.select(
                    gitlab_events.c.gitlab_event_id,
                    gitlab_events.c.feature_id,
                    gitlab_events.c.ref,
                    gitlab_events.c.title,
                ).where(gitlab_events.c.gitlab_event_id == event_uuid)
            )
        ).all()

    feature_state = tuple(
        (
            str(row.id),
            row.name,
            row.description,
            None if row.embedding is None else tuple(float(value) for value in row.embedding),
        )
        for row in feature_rows
    )
    artifact_state = tuple(
        (str(row.feature_id), row.artifact_type, str(row.artifact_id), row.similarity_score)
        for row in artifact_rows
    )
    event_state = tuple(
        (row.gitlab_event_id, str(row.feature_id), row.ref, row.title) for row in event_rows
    )
    return feature_state, artifact_state, event_state


async def test_real_dedup_rolls_back_without_post_loss_dml_after_lease_handover() -> None:
    owner_engine = _engine()
    successor_engine = _engine()
    admin_engine = _engine()
    owner = AutomationOwnershipLease(
        owner_engine,
        heartbeat_interval=0.02,
        heartbeat_timeout=0.2,
    )
    successor = AutomationOwnershipLease(
        successor_engine,
        heartbeat_interval=0.02,
        heartbeat_timeout=0.2,
    )
    session_factory = async_sessionmaker(
        owner_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    embedding = BlockingEmbedding()
    project_key = f"integ-arc1-dedup-{uuid.uuid4().hex[:12]}"
    target_id = uuid.uuid4()
    source_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    event_uuid = f"arc1-dedup-{uuid.uuid4()}"
    target = SimpleNamespace(id=target_id, name="ARC1 dedup target")
    source = SimpleNamespace(id=source_id, name="ARC1 dedup source")
    real_job = FeatureDedupJob(session_factory, MagicMock(name="reranker"), embedding)
    # Keep the RED semantic on the in-flight merge; builder unit tests separately
    # require this callback to enter through the public constructor.
    real_job._mutation_guard = owner.ensure_owned  # type: ignore[attr-defined]
    job = SingleCandidateJob(real_job, project_key, target, source)
    task: asyncio.Task[None] | None = None
    listener_installed = False
    dml_after_loss: list[str] = []

    def record_post_loss_dml(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.lstrip().upper()
        is_dedup_dml = normalized.startswith(
            (
                "UPDATE FEATURE_ARTIFACTS",
                "UPDATE GITLAB_EVENTS",
                "UPDATE FEATURES",
                "DELETE FROM FEATURES",
            )
        )
        if owner.ownership_lost.is_set() and is_dedup_dml:
            dml_after_loss.append(normalized.splitlines()[0])

    await _assert_test_database(owner_engine, successor_engine, admin_engine)
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                sa.insert(project_contexts).values(
                    project_key=project_key,
                    name="ARC1 dedup mutation fence",
                    description="temporary real PostgreSQL ownership probe",
                )
            )
            await connection.execute(
                sa.insert(features),
                [
                    {
                        "id": target_id,
                        "project_key": project_key,
                        "name": target.name,
                        "description": "authoritative target description",
                        "embedding": [0.1] * 1536,
                    },
                    {
                        "id": source_id,
                        "project_key": project_key,
                        "name": source.name,
                        "description": "authoritative source description",
                        "embedding": [0.2] * 1536,
                    },
                ],
            )
            await connection.execute(
                sa.insert(feature_artifacts).values(
                    feature_id=source_id,
                    artifact_type="decision",
                    artifact_id=artifact_id,
                    similarity_score=0.91,
                )
            )
            await connection.execute(
                sa.insert(gitlab_events).values(
                    gitlab_event_id=event_uuid,
                    event_type="merge_request",
                    project_key=project_key,
                    ref="feat/dedup-mutation-fence",
                    title="ARC1 dedup mutation fence",
                    feature_id=source_id,
                )
            )

        before = await _dedup_state(
            admin_engine,
            project_key=project_key,
            artifact_id=artifact_id,
            event_uuid=event_uuid,
        )
        assert await owner.acquire(), "the predecessor must own the automation lease"
        sa.event.listen(owner_engine.sync_engine, "before_cursor_execute", record_post_loss_dml)
        listener_installed = True
        task = asyncio.create_task(
            run_dedup_loop(job, session_factory, interval=0.0, ownership=owner)
        )

        await asyncio.wait_for(embedding.entered.wait(), timeout=3.0)
        pid = owner.backend_pid
        assert pid is not None
        assert await _terminate_backend(admin_engine, pid)
        acquisition_attempts = await _acquire_successor_after_detected_loss(owner, successor)
        assert acquisition_attempts >= 1

        embedding.resume.set()
        outcome = (
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True),
                timeout=3.0,
            )
        )[0]
        after = await _dedup_state(
            admin_engine,
            project_key=project_key,
            artifact_id=artifact_id,
            event_uuid=event_uuid,
        )

        assert isinstance(outcome, OwnershipLostError), (
            "the predecessor must fail closed after the successor acquires: "
            f"outcome={outcome!r}, post_loss_dml={dml_after_loss}"
        )
        assert after == before, (
            "source, target, artifact and event must all roll back unchanged; "
            f"post_loss_dml={dml_after_loss}"
        )
        assert dml_after_loss == [], (
            f"the predecessor issued dedup DML after loss detection: statements={dml_after_loss}"
        )

        async with admin_engine.begin() as connection:
            unlocked_rows = (
                await connection.execute(
                    sa.select(features.c.id)
                    .where(features.c.id.in_([target_id, source_id]))
                    .with_for_update(nowait=True)
                )
            ).scalars()
            assert set(unlocked_rows) == {target_id, source_id}, (
                "the failed predecessor must release both feature row locks on rollback"
            )
    finally:
        embedding.resume.set()
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if listener_installed:
            sa.event.remove(owner_engine.sync_engine, "before_cursor_execute", record_post_loss_dml)
        await successor.release()
        await owner.release()
        async with admin_engine.begin() as connection:
            await connection.execute(
                sa.delete(feature_artifacts).where(feature_artifacts.c.artifact_id == artifact_id)
            )
            await connection.execute(
                sa.delete(gitlab_events).where(gitlab_events.c.gitlab_event_id == event_uuid)
            )
            await connection.execute(
                sa.delete(features).where(features.c.project_key == project_key)
            )
            await connection.execute(
                sa.delete(project_contexts).where(project_contexts.c.project_key == project_key)
            )
        await admin_engine.dispose()
        await successor_engine.dispose()
        await owner_engine.dispose()
