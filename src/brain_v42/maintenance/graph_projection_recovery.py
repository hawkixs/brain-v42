"""Crash-safe recovery of the PostgreSQL-to-Neo4j projection lineage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ProjectionRecoveryReport:
    """Secret-free evidence emitted after one recovery attempt."""

    recovery_id: UUID
    status: Literal["recovered", "already_completed"]
    generation: int | None
    deleted_nodes: int | None
    entity_events: int | None
    relation_events: int | None
    entity_count: int
    relation_count: int
    pending_count_before: int


async def recover_projection_lineage(
    repo: Any,
    writer: Any,
    *,
    recovery_id: UUID,
    worker_id: str,
    lease_seconds: int,
) -> ProjectionRecoveryReport:
    """Resume one idempotent cross-store recovery until both markers finalize."""
    await repo.assert_schema_ready()
    inventory = await repo.projection_inventory()
    preparation = await repo.prepare_projection_recovery(
        recovery_id,
        worker_id,
        lease_seconds=max(1, lease_seconds),
    )
    if preparation is None:
        raise RuntimeError("projection recovery refused: active projection or recovery lease")
    if preparation.status == "completed":
        return ProjectionRecoveryReport(
            recovery_id=recovery_id,
            status="already_completed",
            generation=None,
            deleted_nodes=None,
            entity_events=None,
            relation_events=None,
            entity_count=inventory.entity_count,
            relation_count=inventory.relation_count,
            pending_count_before=inventory.pending_count,
        )

    state = preparation.lease
    if state is None or state.recovery_id != recovery_id:
        raise RuntimeError("PostgreSQL returned an invalid projection recovery lease")

    if state.phase not in {"prepared", "neo_ready"}:
        raise RuntimeError("PostgreSQL returned an invalid projection recovery phase")

    reset = await writer.reset_for_recovery(state)
    if not reset.accepted:
        raise RuntimeError("Neo4j recovery reset rejected")
    deleted_nodes = int(reset.deleted_nodes)

    if state.phase == "prepared":
        ready = await repo.mark_projection_recovery_neo_ready(
            state,
            lease_seconds=max(1, lease_seconds),
        )
        if ready is None:
            raise RuntimeError("PostgreSQL refused neo_ready recovery transition")
        state = ready

    finalized = await writer.finalize_recovery(state)
    if not finalized.accepted:
        raise RuntimeError("Neo4j recovery finalization rejected")
    if not await repo.finalize_projection_recovery(state):
        raise RuntimeError("PostgreSQL refused recovery finalization")

    requeued = preparation.requeued
    return ProjectionRecoveryReport(
        recovery_id=recovery_id,
        status="recovered",
        generation=state.generation,
        deleted_nodes=deleted_nodes,
        entity_events=None if requeued is None else requeued.entity_events,
        relation_events=None if requeued is None else requeued.relation_events,
        entity_count=inventory.entity_count,
        relation_count=inventory.relation_count,
        pending_count_before=inventory.pending_count,
    )
