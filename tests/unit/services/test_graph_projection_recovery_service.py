"""Recovery orchestration contracts across PostgreSQL and Neo4j."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from brain_v42.repositories.pg_graph_ledger import (
    ProjectionRecoveryLease,
    ProjectionRecoveryPreparation,
    ProjectionRequeueReport,
)


def _module() -> Any:
    return importlib.import_module("brain_v42.maintenance.graph_projection_recovery")


def _lease(*, phase: str = "prepared") -> Any:
    return ProjectionRecoveryLease(
        recovery_id=uuid4(),
        owner_id="recovery-worker",
        generation=8,
        lease_until=datetime.now(UTC) + timedelta(minutes=5),
        phase=phase,  # type: ignore[arg-type]
    )


class _Repo:
    def __init__(
        self,
        preparation: ProjectionRecoveryPreparation | None,
        *,
        ready: ProjectionRecoveryLease | None = None,
        finalized: bool = True,
    ) -> None:
        self.preparation = preparation
        self.ready = ready
        self.finalized = finalized
        self.calls: list[str] = []

    async def assert_schema_ready(self) -> None:
        self.calls.append("schema")

    async def projection_inventory(self) -> Any:
        self.calls.append("inventory")
        return SimpleNamespace(entity_count=3, relation_count=2, pending_count=1)

    async def prepare_projection_recovery(
        self,
        recovery_id: Any,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> ProjectionRecoveryPreparation | None:
        self.calls.append(f"prepare:{recovery_id}:{worker_id}:{lease_seconds}")
        return self.preparation

    async def mark_projection_recovery_neo_ready(
        self,
        state: ProjectionRecoveryLease,
        *,
        lease_seconds: int,
    ) -> ProjectionRecoveryLease | None:
        self.calls.append(f"neo_ready:{state.phase}:{lease_seconds}")
        return self.ready

    async def finalize_projection_recovery(self, state: ProjectionRecoveryLease) -> bool:
        self.calls.append(f"finalize:{state.phase}")
        return self.finalized


class _Writer:
    def __init__(self, *, reset_accepted: bool = True, finalize_accepted: bool = True) -> None:
        self.reset_accepted = reset_accepted
        self.finalize_accepted = finalize_accepted
        self.calls: list[str] = []

    async def reset_for_recovery(self, state: ProjectionRecoveryLease) -> Any:
        self.calls.append(f"reset:{state.phase}")
        return SimpleNamespace(
            accepted=self.reset_accepted,
            current_generation=state.generation,
            deleted_nodes=4,
        )

    async def finalize_recovery(self, state: ProjectionRecoveryLease) -> Any:
        self.calls.append(f"finalize:{state.phase}")
        return SimpleNamespace(
            accepted=self.finalize_accepted,
            current_generation=state.generation,
        )


@pytest.mark.asyncio
async def test_new_recovery_resets_marks_ready_then_finalizes_both_stores() -> None:
    lease = _lease()
    ready = ProjectionRecoveryLease(
        recovery_id=lease.recovery_id,
        owner_id=lease.owner_id,
        generation=lease.generation,
        lease_until=lease.lease_until,
        phase="neo_ready",
    )
    repo = _Repo(
        ProjectionRecoveryPreparation(
            status="started",
            lease=lease,
            requeued=ProjectionRequeueReport(entity_events=3, relation_events=2),
        ),
        ready=ready,
    )
    writer = _Writer()

    report = await _module().recover_projection_lineage(
        repo,
        writer,
        recovery_id=lease.recovery_id,
        worker_id=lease.owner_id,
        lease_seconds=300,
    )

    assert repo.calls == [
        "schema",
        "inventory",
        f"prepare:{lease.recovery_id}:{lease.owner_id}:300",
        "neo_ready:prepared:300",
        "finalize:neo_ready",
    ]
    assert writer.calls == ["reset:prepared", "finalize:neo_ready"]
    assert report.status == "recovered"
    assert report.recovery_id == lease.recovery_id
    assert report.generation == 8
    assert report.deleted_nodes == 4
    assert report.entity_events == 3
    assert report.relation_events == 2


@pytest.mark.asyncio
async def test_neo_ready_resume_rebuilds_before_finalizing() -> None:
    ready = _lease(phase="neo_ready")
    repo = _Repo(
        ProjectionRecoveryPreparation(status="resumed", lease=ready, requeued=None),
        finalized=True,
    )
    writer = _Writer()

    report = await _module().recover_projection_lineage(
        repo,
        writer,
        recovery_id=ready.recovery_id,
        worker_id=ready.owner_id,
        lease_seconds=300,
    )

    assert "neo_ready:neo_ready:300" not in repo.calls
    assert writer.calls == ["reset:neo_ready", "finalize:neo_ready"]
    assert report.status == "recovered"
    assert report.deleted_nodes == 4
    assert report.entity_events is None


@pytest.mark.asyncio
async def test_neo_ready_resume_stays_interlocked_when_rebuild_is_rejected() -> None:
    ready = _lease(phase="neo_ready")
    repo = _Repo(
        ProjectionRecoveryPreparation(status="resumed", lease=ready, requeued=None),
        finalized=True,
    )

    writer = _Writer(reset_accepted=False)

    with pytest.raises(RuntimeError, match="Neo4j recovery reset rejected"):
        await _module().recover_projection_lineage(
            repo,
            writer,
            recovery_id=ready.recovery_id,
            worker_id=ready.owner_id,
            lease_seconds=300,
        )

    assert writer.calls == ["reset:neo_ready"]


@pytest.mark.asyncio
async def test_completed_recovery_id_is_a_cross_store_noop() -> None:
    recovery_id = uuid4()
    repo = _Repo(ProjectionRecoveryPreparation(status="completed", lease=None, requeued=None))
    writer = _Writer()

    report = await _module().recover_projection_lineage(
        repo,
        writer,
        recovery_id=recovery_id,
        worker_id="recovery-worker",
        lease_seconds=300,
    )

    assert report.status == "already_completed"
    assert report.recovery_id == recovery_id
    assert report.generation is None
    assert writer.calls == []
    assert not any(call.startswith("neo_ready") for call in repo.calls)
    assert not any(call.startswith("finalize") for call in repo.calls)


@pytest.mark.asyncio
async def test_prepare_conflict_never_touches_neo4j() -> None:
    repo = _Repo(None)
    writer = _Writer()

    with pytest.raises(RuntimeError, match="active projection or recovery lease"):
        await _module().recover_projection_lineage(
            repo,
            writer,
            recovery_id=uuid4(),
            worker_id="recovery-worker",
            lease_seconds=300,
        )

    assert writer.calls == []


@pytest.mark.asyncio
async def test_reset_rejection_leaves_postgres_interlocked_for_resume() -> None:
    lease = _lease()
    repo = _Repo(ProjectionRecoveryPreparation(status="resumed", lease=lease, requeued=None))
    writer = _Writer(reset_accepted=False)

    with pytest.raises(RuntimeError, match="Neo4j recovery reset rejected"):
        await _module().recover_projection_lineage(
            repo,
            writer,
            recovery_id=lease.recovery_id,
            worker_id=lease.owner_id,
            lease_seconds=300,
        )

    assert writer.calls == ["reset:prepared"]
    assert not any(call.startswith("finalize") for call in repo.calls)


@pytest.mark.asyncio
async def test_failed_postgres_ready_transition_keeps_both_recovery_markers() -> None:
    lease = _lease()
    repo = _Repo(
        ProjectionRecoveryPreparation(status="resumed", lease=lease, requeued=None),
        ready=None,
    )
    writer = _Writer()

    with pytest.raises(RuntimeError, match="PostgreSQL refused neo_ready"):
        await _module().recover_projection_lineage(
            repo,
            writer,
            recovery_id=lease.recovery_id,
            worker_id=lease.owner_id,
            lease_seconds=300,
        )

    assert writer.calls == ["reset:prepared"]


@pytest.mark.asyncio
async def test_failed_final_cas_never_reports_success() -> None:
    ready = _lease(phase="neo_ready")
    repo = _Repo(
        ProjectionRecoveryPreparation(status="resumed", lease=ready, requeued=None),
        finalized=False,
    )
    writer = _Writer()

    with pytest.raises(RuntimeError, match="PostgreSQL refused recovery finalization"):
        await _module().recover_projection_lineage(
            repo,
            writer,
            recovery_id=ready.recovery_id,
            worker_id=ready.owner_id,
            lease_seconds=300,
        )

    assert writer.calls == ["reset:neo_ready", "finalize:neo_ready"]
