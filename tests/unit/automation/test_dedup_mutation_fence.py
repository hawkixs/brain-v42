"""Mutation-fence contract for automation-owned feature deduplication."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.automation.ownership import OwnershipLostError
from brain_v42.services.feature_dedup_job import FeatureDedupJob


class MutableOwnershipGate:
    """Deterministic lease gate whose loss can be injected across an await."""

    def __init__(self) -> None:
        self.owned = True
        self.checks = 0

    def ensure_owned(self) -> None:
        self.checks += 1
        if not self.owned:
            raise OwnershipLostError("dedup ownership lost")


class BlockingEmbedding:
    """Embedding boundary controlled by the test."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def embed(self, _text: str) -> list[float]:
        self.entered.set()
        await self.resume.wait()
        return [0.9] * 1536


def _candidate(name: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name=name)


def _session_factory() -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    result = MagicMock()
    result.fetchall.return_value = [("brain-v42",)]
    session.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


def _feature_row(feature: SimpleNamespace, description: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=feature.id,
        name=feature.name,
        description=description,
        embedding=[0.1] * 1536,
        status="research",
        merged_into=None,
        # Sans ce champ, la ligne feinte n'a pas l'attribut que la vraie a, et
        # la garde `pinned` de `merge_features` lèverait AttributeError avant
        # d'atteindre la clôture de mutation que ce fichier teste.
        pinned=False,
    )


def _dml_label(statement: object) -> str | None:
    table = getattr(getattr(statement, "table", None), "name", None)
    if table is None:
        return None
    if bool(getattr(statement, "is_delete", False)):
        return f"delete:{table}"
    if bool(getattr(statement, "is_update", False)):
        return f"update:{table}"
    return str(table)


def _dedup_job(
    *,
    gate: MutableOwnershipGate,
    embedding: object,
) -> FeatureDedupJob:
    job = FeatureDedupJob(
        session_factory=MagicMock(),
        reranker=MagicMock(),
        embedding_svc=embedding,  # type: ignore[arg-type]
    )
    # White-box injection keeps this RED focused on missing fence behavior;
    # composition tests below independently require the public constructor wiring.
    job._mutation_guard = gate.ensure_owned  # type: ignore[attr-defined]
    return job


async def test_scheduler_does_not_commit_when_ownership_is_lost_during_merge() -> None:
    from brain_v42.automation.dedup import run_dedup_loop

    gate = MutableOwnershipGate()
    target = _candidate("target")
    source = _candidate("source")
    job = MagicMock()
    job.find_candidates = AsyncMock(return_value=[(target, source, 0.91)])

    async def lose_ownership_during_merge(*_args: object) -> bool:
        gate.owned = False
        return True

    job.merge_features = AsyncMock(side_effect=lose_ownership_during_merge)
    factory, session = _session_factory()
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

    with patch("brain_v42.automation.dedup.asyncio.sleep", sleep):
        outcome = (
            await asyncio.gather(
                run_dedup_loop(job, factory, interval=0.0, ownership=gate),
                return_exceptions=True,
            )
        )[0]

    assert isinstance(outcome, OwnershipLostError), (
        "the scheduler committed or advanced after merge_features returned under a lost lease: "
        f"outcome={outcome!r}"
    )
    session.commit.assert_not_awaited()


async def test_scheduler_detects_loss_during_non_fencing_commit_before_advancing() -> None:
    from brain_v42.automation.dedup import run_dedup_loop

    gate = MutableOwnershipGate()
    target = _candidate("target")
    source = _candidate("source")
    job = MagicMock()
    job.find_candidates = AsyncMock(return_value=[(target, source, 0.91)])
    job.merge_features = AsyncMock(return_value=True)
    factory, session = _session_factory()

    async def commit_then_lose() -> None:
        gate.owned = False

    session.commit = AsyncMock(side_effect=commit_then_lose)
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

    with (
        patch("brain_v42.automation.dedup.asyncio.sleep", sleep),
        patch("brain_v42.automation.dedup.logger") as logger,
    ):
        outcome = (
            await asyncio.gather(
                run_dedup_loop(job, factory, interval=0.0, ownership=gate),
                return_exceptions=True,
            )
        )[0]

    assert isinstance(outcome, OwnershipLostError), (
        "a commit already in flight is non-fencing, but its detected lease loss must stop the pass: "
        f"outcome={outcome!r}"
    )
    session.commit.assert_awaited_once_with()
    logger.info.assert_not_called()


async def test_merge_stops_after_ownership_loss_during_reembedding() -> None:
    gate = MutableOwnershipGate()
    embedding = BlockingEmbedding()
    target = _candidate("target")
    source = _candidate("source")
    target_row = _feature_row(target, "target description")
    source_row = _feature_row(source, "source description")
    recheck = MagicMock()
    recheck.fetchall.return_value = [target_row, source_row]
    dml_tables: list[str] = []
    dml_after_loss: list[str] = []

    async def execute(statement: object) -> MagicMock:
        label = _dml_label(statement)
        if label is None:
            return recheck
        dml_tables.append(label)
        if not gate.owned:
            dml_after_loss.append(label)
        return MagicMock()

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    job = _dedup_job(gate=gate, embedding=embedding)
    task = asyncio.create_task(job.merge_features(session, target, source))

    try:
        await asyncio.wait_for(embedding.entered.wait(), timeout=0.5)
        gate.owned = False
        embedding.resume.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
    finally:
        embedding.resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert isinstance(outcome, OwnershipLostError), (
        "the post-embed guard must stay outside the best-effort embed exception handler: "
        f"outcome={outcome!r}, dml={dml_tables}"
    )
    assert dml_tables == [], (
        "remote re-embedding must finish before any DML is staged, and loss must fence all DML: "
        f"all_dml={dml_tables}, dml_after_loss={dml_after_loss}"
    )


async def test_merge_stops_when_ownership_is_lost_during_for_update_recheck() -> None:
    gate = MutableOwnershipGate()
    target = _candidate("target")
    source = _candidate("source")
    target_row = _feature_row(target, "target description")
    source_row = _feature_row(source, "source description")
    recheck = MagicMock()
    recheck.fetchall.return_value = [target_row, source_row]
    select_entered = asyncio.Event()
    select_resume = asyncio.Event()
    dml_tables: list[str] = []

    async def execute(statement: object) -> MagicMock:
        label = _dml_label(statement)
        if label is None:
            select_entered.set()
            await select_resume.wait()
            return recheck
        dml_tables.append(label)
        return MagicMock()

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    embedding = AsyncMock()
    embedding.embed = AsyncMock(return_value=[0.9] * 1536)
    job = _dedup_job(gate=gate, embedding=embedding)
    task = asyncio.create_task(job.merge_features(session, target, source))

    try:
        await asyncio.wait_for(select_entered.wait(), timeout=0.5)
        gate.owned = False
        select_resume.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
    finally:
        select_resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert isinstance(outcome, OwnershipLostError), (
        "the completed FOR UPDATE await must be followed by an ownership boundary: "
        f"outcome={outcome!r}, dml={dml_tables}"
    )
    assert dml_tables == []
    embedding.embed.assert_not_awaited()


@pytest.mark.parametrize("loss_after_dml", range(5))
async def test_merge_stops_at_every_dml_boundary(loss_after_dml: int) -> None:
    gate = MutableOwnershipGate()
    target = _candidate("target")
    source = _candidate("source")
    target_row = _feature_row(target, "target description")
    source_row = _feature_row(source, "source description")
    recheck = MagicMock()
    recheck.fetchall.return_value = [target_row, source_row]
    dml_labels: list[str] = []

    async def execute(statement: object) -> MagicMock:
        label = _dml_label(statement)
        if label is None:
            return recheck
        dml_labels.append(label)
        if len(dml_labels) == loss_after_dml + 1:
            gate.owned = False
        return MagicMock()

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    embedding = AsyncMock()
    embedding.embed = AsyncMock(return_value=[0.9] * 1536)
    job = _dedup_job(gate=gate, embedding=embedding)

    outcome = (
        await asyncio.gather(
            job.merge_features(session, target, source),
            return_exceptions=True,
        )
    )[0]

    assert isinstance(outcome, OwnershipLostError), (
        "every completed DML must be followed by an ownership boundary: "
        f"loss_after={loss_after_dml}, outcome={outcome!r}, dml={dml_labels}"
    )
    expected_prefix = [
        "update:feature_artifacts",
        "update:gitlab_events",
        "update:features",
        "update:features",
        "update:features",
    ][: loss_after_dml + 1]
    assert dml_labels == expected_prefix


def test_automation_builder_wires_the_runtime_lease_into_dedup_mutations() -> None:
    from brain_v42.automation.runtime import build_automation_runtime
    from brain_v42.config import Settings

    runtime = build_automation_runtime(
        settings=Settings(
            postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
            gitlab_webhook_secret="secret",
            _env_file=None,  # type: ignore[call-arg]
        ),
        engine=MagicMock(name="engine"),
    )
    lease = runtime._resources.lease
    guard = getattr(runtime._resources.dedup_job, "_mutation_guard", None)

    assert guard == lease.ensure_owned
    assert getattr(guard, "__self__", None) is lease


def test_legacy_builder_wires_the_runtime_lease_into_dedup_mutations() -> None:
    from brain_v42.automation.ownership import AutomationOwnershipLease
    from brain_v42.config import Settings
    from brain_v42.metrics.runtime import _build_legacy_resources

    lease = AutomationOwnershipLease(MagicMock(name="engine"))
    legacy = _build_legacy_resources(
        settings=Settings(
            postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
            metrics_legacy_automation_enabled=True,
            _env_file=None,  # type: ignore[call-arg]
        ),
        session_factory=MagicMock(),
        embedding_svc=MagicMock(),
        lease=lease,
    )
    guard = getattr(legacy.dedup_job, "_mutation_guard", None)

    assert guard == lease.ensure_owned
    assert getattr(guard, "__self__", None) is lease
