"""PostgreSQL source of truth for graph relations and projection work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain_v42.db.tables import brain_entities, entity_relations, graph_outbox

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GraphOperation = Literal[
    "upsert_entity",
    "delete_entity",
    "upsert_relation",
    "delete_relation",
]
RecoveryPhase = Literal["prepared", "neo_ready"]
RecoveryPreparationStatus = Literal["started", "resumed", "completed"]

_CANONICAL_RELATION_TYPES = frozenset(
    {
        "SUPERSEDES",
        "MOTIVATED_BY",
        "IMPLEMENTS",
        "DOCUMENTS",
        "USES",
        "RELATED_TO",
        "CONTAINS",
        "DEPENDS_ON",
        "BELONGS_TO",
        "MERGED_INTO",
        "BELONGS_TO_DOMAIN",
    }
)
_SAFE_PROPERTY_KEYS = frozenset(
    {"similarity", "score", "threshold", "model", "model_version", "method"}
)
_SAFE_ERROR_CODES = frozenset({"missing_node", "neo4j_error", "invalid_event", "projection_failed"})
_KNOWLEDGE_ENTITY_TYPES = frozenset(
    {"decision", "learning", "snippet", "runbook", "adr", "feature", "plan"}
)


class UnknownGraphEndpoint(ValueError):
    """Raised when a relation endpoint is absent from ``brain_entities``."""


@dataclass(frozen=True, slots=True)
class GraphOutboxEvent:
    """Secret-free projection instruction claimed from ``graph_outbox``."""

    event_id: UUID
    operation: GraphOperation
    aggregate_revision: int = 0
    relation_id: UUID | None = None
    entity_id: UUID | None = None
    entity_type: str | None = None
    entity_key: str | None = None
    source_uuid: UUID | None = None
    project_key: str | None = None
    display_label: str | None = None
    lifecycle: str | None = None
    source_type: str | None = None
    source_key: str | None = None
    target_type: str | None = None
    target_key: str | None = None
    relation_type: str | None = None
    relation_lifecycle: str | None = None
    properties: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ProjectionLeadership:
    """One PostgreSQL-issued generation allowed to advance the Neo4j fence."""

    owner_id: str
    generation: int
    lease_until: datetime
    armed: bool


@dataclass(frozen=True, slots=True)
class ProjectionClaim:
    """A canonical event bound to one live PostgreSQL fencing claim."""

    event: GraphOutboxEvent
    owner_id: str
    lease_generation: int
    claim_version: int
    leased_until: datetime


@dataclass(frozen=True, slots=True)
class ProjectionRequeueReport:
    """Number of current canonical aggregates scheduled for a full rebuild."""

    entity_events: int
    relation_events: int


@dataclass(frozen=True, slots=True)
class ProjectionRecoveryLease:
    """One durable, resumable recovery operation fenced by PostgreSQL."""

    recovery_id: UUID
    owner_id: str
    generation: int
    lease_until: datetime
    phase: RecoveryPhase


@dataclass(frozen=True, slots=True)
class ProjectionRecoveryPreparation:
    """Result of starting, resuming, or recognizing one recovery ID."""

    status: RecoveryPreparationStatus
    lease: ProjectionRecoveryLease | None
    requeued: ProjectionRequeueReport | None


@dataclass(frozen=True, slots=True)
class ProjectionInventory:
    """Bounded counts used to preview or observe graph projection work."""

    entity_count: int
    relation_count: int
    pending_count: int


def canonicalize_relation_endpoints(
    source_id: UUID,
    target_id: UUID,
    relation_type: str,
) -> tuple[UUID, UUID]:
    """Give symmetric ``RELATED_TO`` facts one stable orientation."""
    if relation_type == "RELATED_TO" and source_id.int > target_id.int:
        return target_id, source_id
    return source_id, target_id


def sanitize_relation_properties(props: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only bounded, non-content projection metadata."""
    if not props:
        return {}
    cleaned: dict[str, Any] = {}
    for key in _SAFE_PROPERTY_KEYS:
        value = props.get(key)
        if isinstance(value, str):
            cleaned[key] = value[:128]
        elif isinstance(value, int | float | bool):
            cleaned[key] = value
    return cleaned


def validate_relation_shape(
    source_type: str,
    target_type: str,
    relation_type: str,
) -> None:
    """Reject endpoint/type combinations outside the canonical graph model."""
    valid = False
    if relation_type == "BELONGS_TO":
        valid = source_type in _KNOWLEDGE_ENTITY_TYPES and target_type == "project"
    elif relation_type == "BELONGS_TO_DOMAIN":
        valid = source_type in _KNOWLEDGE_ENTITY_TYPES and target_type == "domain"
    elif relation_type in {"CONTAINS", "DEPENDS_ON"}:
        valid = source_type == target_type == "project"
    elif relation_type in {"SUPERSEDES", "MERGED_INTO"}:
        valid = source_type == target_type and source_type in _KNOWLEDGE_ENTITY_TYPES
    elif relation_type in _CANONICAL_RELATION_TYPES:
        valid = source_type in _KNOWLEDGE_ENTITY_TYPES and target_type in _KNOWLEDGE_ENTITY_TYPES
    if not valid:
        raise ValueError(f"invalid relation shape: {source_type}-[{relation_type}]->{target_type}")


class PgGraphLedgerRepo:
    """Atomically persist canonical relations and their outbox events."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_active_classification_orphans(
        self,
        *,
        limit: int = 20,
        project_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return active knowledge entities without graph or domain relations."""
        statement = sa.text(
            """
            SELECT candidate.source_uuid, candidate.entity_type
            FROM brain_entities AS candidate
            WHERE candidate.lifecycle = 'active'
              AND candidate.entity_type IN (
                  'decision', 'learning', 'snippet', 'runbook', 'adr'
              )
              AND candidate.source_uuid IS NOT NULL
              AND (CAST(:project_key AS VARCHAR) IS NULL OR candidate.project_key = :project_key)
              AND NOT EXISTS (
                  SELECT 1
                  FROM entity_relations AS relation
                  WHERE relation.lifecycle = 'active'
                    AND relation.relation_type = 'RELATED_TO'
                    AND (
                        relation.source_entity_id = candidate.id
                        OR relation.target_entity_id = candidate.id
                    )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM entity_relations AS domain_relation
                  WHERE domain_relation.lifecycle = 'active'
                    AND domain_relation.relation_type = 'BELONGS_TO_DOMAIN'
                    AND domain_relation.source_entity_id = candidate.id
              )
            ORDER BY candidate.created_at, candidate.id
            LIMIT :limit
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                statement,
                {"limit": max(1, limit), "project_key": project_key},
            )
        return [dict(row) for row in result.mappings().all()]

    async def assert_schema_ready(self) -> None:
        """Fail startup closed unless the migration 035 recovery shape is installed."""
        statement = sa.text(
            """
            SELECT
                to_regclass('public.projects') IS NOT NULL
                AND to_regclass('public.project_aliases') IS NOT NULL
                AND to_regclass('public.brain_entities') IS NOT NULL
                AND to_regclass('public.entity_relations') IS NOT NULL
                AND to_regclass('public.graph_outbox') IS NOT NULL
                AND to_regclass('public.graph_projection_leases') IS NOT NULL
                AND EXISTS (
                    SELECT 1
                    FROM graph_projection_leases
                    WHERE slot = 'neo4j'
                      AND protocol_version = 2
                      AND recovery_phase IN ('idle', 'prepared', 'neo_ready')
                      AND (
                          (recovery_id IS NULL AND recovery_phase = 'idle')
                          OR (
                              recovery_id IS NOT NULL
                              AND recovery_phase IN ('prepared', 'neo_ready')
                          )
                      )
                      AND (
                          neo4j_armed_generation IS NULL
                          OR neo4j_armed_generation = generation
                      )
                )
                AND EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'graph_outbox'
                      AND column_name = 'lease_generation'
                )
                AND EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'graph_outbox'
                      AND column_name = 'claim_version'
                )
                AND 3 = (
                    SELECT COUNT(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'graph_projection_leases'
                      AND column_name IN (
                          'recovery_id',
                          'recovery_phase',
                          'last_completed_recovery_id'
                      )
                )
                AND EXISTS (
                    SELECT 1
                    FROM pg_constraint AS constraint_record
                    JOIN pg_class AS relation
                      ON relation.oid = constraint_record.conrelid
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'public'
                      AND relation.relname = 'graph_projection_leases'
                      AND constraint_record.conname =
                          'graph_projection_leases_recovery_state_valid'
                      AND constraint_record.convalidated
                )
                AS ready
            """
        )
        async with self._session_factory() as session:
            ready = bool((await session.execute(statement)).scalar_one())
        if not ready:
            raise RuntimeError("graph ledger cutover requires the migration 035 recovery schema")

    async def acquire_leadership(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> ProjectionLeadership | None:
        """Acquire or renew the singleton projector generation."""
        statement = sa.text(
            """
            WITH locked AS MATERIALIZED (
                SELECT slot,
                       owner,
                       generation,
                       leased_until,
                       neo4j_armed_generation
                FROM graph_projection_leases
                WHERE slot = 'neo4j'
                  AND recovery_id IS NULL
                FOR UPDATE
            ), acquired AS (
                UPDATE graph_projection_leases AS lease
                SET owner = :worker_id,
                    generation = CASE
                        WHEN locked.owner = :worker_id
                         AND locked.leased_until > clock_timestamp()
                        THEN locked.generation
                        WHEN locked.neo4j_armed_generation = locked.generation
                        THEN locked.generation + 1
                        ELSE locked.generation
                    END,
                    leased_until = clock_timestamp()
                        + (:lease_seconds * INTERVAL '1 second'),
                    neo4j_armed_generation = CASE
                        WHEN locked.owner = :worker_id
                         AND locked.leased_until > clock_timestamp()
                        THEN locked.neo4j_armed_generation
                        ELSE NULL
                    END,
                    updated_at = clock_timestamp()
                FROM locked
                WHERE lease.slot = locked.slot
                  AND (
                      locked.owner = :worker_id
                      OR locked.leased_until IS NULL
                      OR locked.leased_until <= clock_timestamp()
                  )
                RETURNING lease.owner AS owner_id,
                          lease.generation,
                          lease.leased_until AS lease_until,
                          lease.neo4j_armed_generation = lease.generation AS armed
            )
            SELECT owner_id, generation, lease_until, armed FROM acquired
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                statement,
                {
                    "worker_id": worker_id[:128],
                    "owner_id": worker_id[:128],
                    "lease_seconds": max(1, lease_seconds),
                },
            )
            row = result.mappings().one_or_none()
            await session.commit()
        if row is None:
            return None
        return ProjectionLeadership(
            owner_id=str(row["owner_id"]),
            generation=int(row["generation"]),
            lease_until=row["lease_until"],
            armed=bool(row["armed"]),
        )

    async def arm_leadership(
        self,
        leadership: ProjectionLeadership,
    ) -> bool:
        """Publish that Neo4j durably accepted the current generation."""
        statement = sa.text(
            """
            UPDATE graph_projection_leases
            SET neo4j_armed_generation = generation,
                updated_at = clock_timestamp()
            WHERE slot = 'neo4j'
              AND recovery_id IS NULL
              AND owner = :owner_id
              AND generation = :generation
              AND leased_until > clock_timestamp()
              AND protocol_version = 2
            RETURNING generation
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                statement,
                {"owner_id": leadership.owner_id, "generation": leadership.generation},
            )
            armed = result.scalar_one_or_none() is not None
            await session.commit()
        return armed

    async def release_leadership(self, leadership: ProjectionLeadership) -> bool:
        """Release a live generation without erasing its durable arm state."""
        statement = sa.text(
            """
            UPDATE graph_projection_leases
            SET owner = NULL,
                leased_until = NULL,
                updated_at = clock_timestamp()
            WHERE slot = 'neo4j'
              AND recovery_id IS NULL
              AND owner = :owner_id
              AND generation = :generation
              AND leased_until > clock_timestamp()
            RETURNING generation
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                statement,
                {"owner_id": leadership.owner_id, "generation": leadership.generation},
            )
            released = result.scalar_one_or_none() is not None
            await session.commit()
        return released

    async def prepare_projection_recovery(
        self,
        recovery_id: UUID,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> ProjectionRecoveryPreparation | None:
        """Atomically interlock runtime projection and enqueue one full rebuild."""
        lock_statement = sa.text(
            """
            SELECT generation,
                   owner AS owner_id,
                   leased_until AS lease_until,
                   leased_until > clock_timestamp() AS lease_live,
                   neo4j_armed_generation AS armed_generation,
                   recovery_id,
                   recovery_phase,
                   last_completed_recovery_id
            FROM graph_projection_leases
            WHERE slot = 'neo4j'
              AND protocol_version = 2
            FOR UPDATE
            """
        )
        resume_statement = sa.text(
            """
            UPDATE graph_projection_leases
            SET owner = :worker_id,
                leased_until = clock_timestamp()
                    + (:lease_seconds * INTERVAL '1 second'),
                updated_at = clock_timestamp()
            WHERE slot = 'neo4j'
              AND protocol_version = 2
              AND recovery_id = :recovery_id
              AND generation = :generation
              AND recovery_phase IN ('prepared', 'neo_ready')
              AND (
                  owner = :worker_id
                  OR leased_until <= clock_timestamp()
              )
            RETURNING recovery_id,
                      owner AS owner_id,
                      generation,
                      leased_until AS lease_until,
                      recovery_phase
            """
        )
        prepare_statement = sa.text(
            """
            WITH locked AS MATERIALIZED (
                SELECT generation
                FROM graph_projection_leases
                WHERE slot = 'neo4j'
                  AND protocol_version = 2
                  AND recovery_id IS NULL
                  AND generation = :observed_generation
                FOR UPDATE
            )
            UPDATE graph_projection_leases AS lease
            SET owner = :worker_id,
                generation = locked.generation + 1,
                leased_until = clock_timestamp()
                    + (:lease_seconds * INTERVAL '1 second'),
                neo4j_armed_generation = NULL,
                recovery_id = :recovery_id,
                recovery_phase = 'prepared',
                updated_at = clock_timestamp()
            FROM locked
            WHERE lease.slot = 'neo4j'
            RETURNING lease.recovery_id,
                      lease.owner AS owner_id,
                      lease.generation,
                      lease.leased_until AS lease_until,
                      lease.recovery_phase
            """
        )
        params = {
            "recovery_id": recovery_id,
            "worker_id": worker_id[:128],
            "lease_seconds": max(1, lease_seconds),
        }

        async with self._session_factory() as session:
            try:
                locked = (await session.execute(lock_statement)).mappings().one()
                active_id = locked["recovery_id"]
                if active_id is not None:
                    if UUID(str(active_id)) != recovery_id:
                        await session.rollback()
                        return None
                    if locked["owner_id"] != params["worker_id"]:
                        await session.rollback()
                        return None
                    resumed = (
                        (
                            await session.execute(
                                resume_statement,
                                {
                                    **params,
                                    "generation": int(locked["generation"]),
                                },
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if resumed is None:
                        await session.rollback()
                        return None
                    await session.commit()
                    return ProjectionRecoveryPreparation(
                        status="resumed",
                        lease=self._recovery_lease_from_mapping(resumed),
                        requeued=None,
                    )

                completed_id = locked["last_completed_recovery_id"]
                if completed_id is not None and UUID(str(completed_id)) == recovery_id:
                    await session.rollback()
                    return ProjectionRecoveryPreparation(
                        status="completed",
                        lease=None,
                        requeued=None,
                    )
                if bool(locked["lease_live"]):
                    await session.rollback()
                    return None

                prepared = (
                    (
                        await session.execute(
                            prepare_statement,
                            {
                                **params,
                                "observed_generation": int(locked["generation"]),
                            },
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if prepared is None:
                    await session.rollback()
                    return None
                requeued = await self._requeue_full_projection_in_session(session)
                await session.commit()
                return ProjectionRecoveryPreparation(
                    status="started",
                    lease=self._recovery_lease_from_mapping(prepared),
                    requeued=requeued,
                )
            except BaseException:
                await session.rollback()
                raise

    async def mark_projection_recovery_neo_ready(
        self,
        state: ProjectionRecoveryLease,
        *,
        lease_seconds: int,
    ) -> ProjectionRecoveryLease | None:
        """Arm exactly one live recovery after Neo4j completed its atomic reset."""
        statement = sa.text(
            """
            UPDATE graph_projection_leases
            SET recovery_phase = 'neo_ready',
                neo4j_armed_generation = generation,
                leased_until = clock_timestamp()
                    + (:lease_seconds * INTERVAL '1 second'),
                updated_at = clock_timestamp()
            WHERE slot = 'neo4j'
              AND protocol_version = 2
              AND recovery_id = :recovery_id
              AND owner = :owner_id
              AND generation = :generation
              AND recovery_phase IN ('prepared', 'neo_ready')
              AND leased_until > clock_timestamp()
            RETURNING recovery_id,
                      owner AS owner_id,
                      generation,
                      leased_until AS lease_until,
                      recovery_phase
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                statement,
                {
                    "recovery_id": state.recovery_id,
                    "owner_id": state.owner_id,
                    "generation": state.generation,
                    "lease_seconds": max(1, lease_seconds),
                },
            )
            row = result.mappings().one_or_none()
            await session.commit()
        if row is None:
            return None
        return self._recovery_lease_from_mapping(row)

    async def finalize_projection_recovery(self, state: ProjectionRecoveryLease) -> bool:
        """Release the interlock only after Neo4j removed its recovery marker."""
        statement = sa.text(
            """
            UPDATE graph_projection_leases
            SET last_completed_recovery_id = recovery_id,
                recovery_id = NULL,
                recovery_phase = 'idle',
                owner = NULL,
                leased_until = NULL,
                updated_at = clock_timestamp()
            WHERE slot = 'neo4j'
              AND protocol_version = 2
              AND recovery_id = :recovery_id
              AND owner = :owner_id
              AND generation = :generation
              AND recovery_phase = 'neo_ready'
              AND neo4j_armed_generation = generation
              AND leased_until > clock_timestamp()
            RETURNING generation
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                statement,
                {
                    "recovery_id": state.recovery_id,
                    "owner_id": state.owner_id,
                    "generation": state.generation,
                },
            )
            finalized = result.scalar_one_or_none() is not None
            await session.commit()
        return finalized

    async def requeue_full_projection(self) -> ProjectionRequeueReport:
        """Requeue each aggregate's current revision for a clean Neo4j rebuild."""
        async with self._session_factory() as session:
            try:
                report = await self._requeue_full_projection_in_session(session)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return report

    async def _requeue_full_projection_in_session(
        self,
        session: AsyncSession,
    ) -> ProjectionRequeueReport:
        """Requeue current revisions without owning the surrounding transaction."""
        entity_statement, relation_statement = self._full_projection_statements()
        entity_rows = (await session.execute(entity_statement)).mappings().all()
        relation_rows = (await session.execute(relation_statement)).mappings().all()
        return ProjectionRequeueReport(
            entity_events=len(entity_rows),
            relation_events=len(relation_rows),
        )

    @staticmethod
    def _full_projection_statements() -> tuple[Any, Any]:
        entity_source = sa.select(
            brain_entities.c.id,
            brain_entities.c.revision,
            sa.case(
                (brain_entities.c.lifecycle == "deleted", "delete_entity"),
                else_="upsert_entity",
            ),
        )
        entity_insert = pg_insert(graph_outbox).from_select(
            ["entity_id", "aggregate_revision", "operation"],
            entity_source,
        )
        entity_statement = entity_insert.on_conflict_do_update(
            constraint="uq_graph_outbox_entity_revision",
            set_={
                "operation": entity_insert.excluded.operation,
                "attempt_count": 0,
                "available_at": sa.func.now(),
                "leased_until": None,
                "lease_owner": None,
                "lease_generation": None,
                "delivered_at": None,
                "last_error_code": None,
            },
        ).returning(graph_outbox.c.id)

        relation_source = sa.select(
            entity_relations.c.id,
            entity_relations.c.revision,
            sa.case(
                (entity_relations.c.lifecycle == "active", "upsert_relation"),
                else_="delete_relation",
            ),
        )
        relation_insert = pg_insert(graph_outbox).from_select(
            ["relation_id", "aggregate_revision", "operation"],
            relation_source,
        )
        relation_statement = relation_insert.on_conflict_do_update(
            constraint="uq_graph_outbox_relation_revision",
            set_={
                "operation": relation_insert.excluded.operation,
                "attempt_count": 0,
                "available_at": sa.func.now(),
                "leased_until": None,
                "lease_owner": None,
                "lease_generation": None,
                "delivered_at": None,
                "last_error_code": None,
            },
        ).returning(graph_outbox.c.id)

        return entity_statement, relation_statement

    @staticmethod
    def _recovery_lease_from_mapping(row: Any) -> ProjectionRecoveryLease:
        phase = str(row["recovery_phase"])
        if phase not in {"prepared", "neo_ready"}:
            raise RuntimeError(f"invalid graph projection recovery phase: {phase}")
        return ProjectionRecoveryLease(
            recovery_id=UUID(str(row["recovery_id"])),
            owner_id=str(row["owner_id"]),
            generation=int(row["generation"]),
            lease_until=row["lease_until"],
            phase=cast(RecoveryPhase, phase),
        )

    async def projection_inventory(self) -> ProjectionInventory:
        """Return the canonical rebuild scope and current pending depth."""
        statement = sa.text(
            """
            SELECT
                (SELECT COUNT(*) FROM brain_entities) AS entity_count,
                (SELECT COUNT(*) FROM entity_relations) AS relation_count,
                (
                    SELECT COUNT(*)
                    FROM graph_outbox
                    WHERE delivered_at IS NULL
                ) AS pending_count
            """
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).mappings().one()
        return ProjectionInventory(
            entity_count=int(row["entity_count"]),
            relation_count=int(row["relation_count"]),
            pending_count=int(row["pending_count"]),
        )

    async def stage_uuid_relation(
        self,
        source_id: UUID,
        target_id: UUID,
        relation_type: str,
        *,
        props: dict[str, Any] | None = None,
        origin: str = "explicit",
        confidence: float | None = None,
        project_key: str | None = None,
    ) -> GraphOutboxEvent:
        """Upsert one UUID-anchored relation and enqueue its projection."""
        self._validate_relation(relation_type, confidence)
        source_id, target_id = canonicalize_relation_endpoints(source_id, target_id, relation_type)
        async with self._session_factory() as session:
            try:
                endpoints = await self._resolve_uuid_endpoints(session, source_id, target_id)
                self._validate_project_scope(endpoints, project_key)
                event = await self._stage_relation_in_session(
                    session,
                    endpoints=endpoints,
                    relation_type=relation_type,
                    operation="upsert_relation",
                    properties=sanitize_relation_properties(props),
                    origin=origin,
                    confidence=confidence,
                )
                await session.commit()
                return event
            except Exception:
                await session.rollback()
                raise

    async def stage_project_membership(
        self,
        entity_id: UUID,
        project_key: str,
    ) -> GraphOutboxEvent:
        """Persist ``entity -[:BELONGS_TO]-> project``."""
        return await self._stage_named_target(
            entity_id,
            target_type="project",
            target_key=project_key,
            relation_type="BELONGS_TO",
            origin="project_membership",
            required_source_project_key=project_key,
        )

    async def stage_domain_membership(
        self,
        entity_id: UUID,
        domain_name: str,
        *,
        project_key: str | None = None,
    ) -> GraphOutboxEvent:
        """Persist ``entity -[:BELONGS_TO_DOMAIN]-> domain``."""
        return await self._stage_named_target(
            entity_id,
            target_type="domain",
            target_key=domain_name,
            relation_type="BELONGS_TO_DOMAIN",
            origin="domain_classifier",
            required_source_project_key=project_key,
        )

    async def stage_uuid_relation_delete(
        self,
        source_id: UUID,
        target_id: UUID,
        relation_type: str,
    ) -> GraphOutboxEvent:
        """Tombstone one canonical relation and enqueue its Neo4j deletion."""
        self._validate_relation(relation_type, None)
        source_id, target_id = canonicalize_relation_endpoints(source_id, target_id, relation_type)
        async with self._session_factory() as session:
            try:
                endpoints = await self._resolve_uuid_endpoints(
                    session,
                    source_id,
                    target_id,
                    require_active=False,
                )
                event = await self._stage_relation_in_session(
                    session,
                    endpoints=endpoints,
                    relation_type=relation_type,
                    operation="delete_relation",
                    properties={},
                    origin="explicit",
                    confidence=None,
                )
                await session.commit()
                return event
            except Exception:
                await session.rollback()
                raise

    async def claim_pending(
        self,
        leadership: ProjectionLeadership,
        *,
        limit: int,
        lease_seconds: int,
        max_attempts: int = 10,
    ) -> list[ProjectionClaim]:
        """Lease a disjoint batch only for the live, Neo4j-armed generation."""
        statement = sa.text(
            """
            WITH current_leader AS MATERIALIZED (
                SELECT owner, generation
                FROM graph_projection_leases
                WHERE slot = 'neo4j'
                  AND recovery_id IS NULL
                  AND owner = :worker_id
                  AND generation = :generation
                  AND neo4j_armed_generation = generation
                  AND leased_until > clock_timestamp()
                FOR UPDATE
            ), exhausted AS (
                UPDATE graph_outbox AS exhausted
                SET available_at = 'infinity'::timestamptz,
                    leased_until = NULL,
                    lease_owner = NULL,
                    lease_generation = NULL,
                    last_error_code = 'max_attempts'
                WHERE exhausted.delivered_at IS NULL
                  AND exhausted.last_error_code IS DISTINCT FROM 'max_attempts'
                  AND exhausted.attempt_count >= :max_attempts
                  AND EXISTS (SELECT 1 FROM current_leader)
                RETURNING exhausted.id
            ), candidates AS (
                SELECT pending.id
                FROM graph_outbox AS pending
                CROSS JOIN current_leader AS leader
                WHERE pending.delivered_at IS NULL
                  AND pending.last_error_code IS DISTINCT FROM 'max_attempts'
                  AND pending.attempt_count < :max_attempts
                  AND pending.available_at <= NOW()
                  AND (
                      pending.leased_until IS NULL
                      OR pending.leased_until <= clock_timestamp()
                      OR pending.lease_generation IS DISTINCT FROM leader.generation
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM graph_outbox AS earlier
                      WHERE earlier.delivered_at IS NULL
                        AND earlier.last_error_code IS DISTINCT FROM 'max_attempts'
                        AND earlier.attempt_count < :max_attempts
                        AND (
                            earlier.entity_id = pending.entity_id
                            OR earlier.relation_id = pending.relation_id
                        )
                        AND earlier.aggregate_revision < pending.aggregate_revision
                  )
                ORDER BY pending.available_at, pending.id
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            ), leased AS (
                UPDATE graph_outbox AS outbox
                SET lease_owner = :worker_id,
                    lease_generation = :generation,
                    claim_version = outbox.claim_version + 1,
                    leased_until = clock_timestamp()
                        + (:lease_seconds * INTERVAL '1 second')
                FROM candidates
                WHERE outbox.id = candidates.id
                RETURNING outbox.id, outbox.event_id, outbox.operation,
                          outbox.aggregate_revision,
                          outbox.lease_owner,
                          outbox.lease_generation,
                          outbox.claim_version,
                          outbox.leased_until,
                          outbox.entity_id, outbox.relation_id
            )
            SELECT leased.event_id,
                   leased.operation,
                   leased.aggregate_revision,
                   leased.lease_owner AS owner_id,
                   leased.lease_generation,
                   leased.claim_version,
                   leased.leased_until,
                   entity.id AS entity_id,
                   entity.entity_type,
                   entity.entity_key,
                   entity.source_uuid,
                   entity.project_key,
                   entity.display_label,
                   entity.lifecycle,
                   relation.id AS relation_id,
                   source.entity_type AS source_type,
                   source.entity_key AS source_key,
                   target.entity_type AS target_type,
                   target.entity_key AS target_key,
                   relation.relation_type,
                   relation.lifecycle AS relation_lifecycle,
                   relation.properties
            FROM leased
            LEFT JOIN brain_entities AS entity ON entity.id = leased.entity_id
            LEFT JOIN entity_relations AS relation ON relation.id = leased.relation_id
            LEFT JOIN brain_entities AS source ON source.id = relation.source_entity_id
            LEFT JOIN brain_entities AS target ON target.id = relation.target_entity_id
            ORDER BY leased.id
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                statement,
                {
                    "worker_id": leadership.owner_id,
                    "owner_id": leadership.owner_id,
                    "generation": leadership.generation,
                    "limit": max(1, limit),
                    "lease_seconds": max(1, lease_seconds),
                    "max_attempts": max(1, max_attempts),
                },
            )
            rows = result.mappings().all()
            await session.commit()
        return [self._claim_from_mapping(row) for row in rows]

    async def renew_claim(
        self,
        claim: ProjectionClaim,
        *,
        lease_seconds: int,
    ) -> ProjectionClaim | None:
        """Atomically renew both leadership and one exact event claim."""
        claim_params = self._claim_params(claim)
        statement = sa.text(
            """
            WITH renewed_leader AS MATERIALIZED (
                UPDATE graph_projection_leases
                SET leased_until = clock_timestamp()
                        + (:lease_seconds * INTERVAL '1 second'),
                    updated_at = clock_timestamp()
                WHERE slot = 'neo4j'
                  AND recovery_id IS NULL
                  AND owner = :worker_id
                  AND generation = :generation
                  AND neo4j_armed_generation = generation
                  AND leased_until > clock_timestamp()
                RETURNING generation
            )
            UPDATE graph_outbox AS outbox
            SET leased_until = clock_timestamp()
                    + (:lease_seconds * INTERVAL '1 second')
            WHERE outbox.event_id = :event_id
              AND outbox.delivered_at IS NULL
              AND outbox.lease_owner = :worker_id
              AND outbox.lease_generation = :generation
              AND outbox.claim_version = :claim_version
              AND outbox.leased_until > clock_timestamp()
              AND EXISTS (SELECT 1 FROM renewed_leader)
            RETURNING outbox.leased_until
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                statement,
                {**claim_params, "lease_seconds": max(1, lease_seconds)},
            )
            row = result.mappings().one_or_none()
            if row is None:
                await session.rollback()
                return None
            await session.commit()
        return ProjectionClaim(
            event=claim.event,
            owner_id=claim.owner_id,
            lease_generation=claim.lease_generation,
            claim_version=claim.claim_version,
            leased_until=row["leased_until"],
        )

    async def mark_delivered(self, claim: ProjectionClaim) -> bool:
        """CAS-ack a live claim and retire obsolete terminal revisions."""
        statement = sa.text(
            """
            WITH current_leader AS MATERIALIZED (
                SELECT generation
                FROM graph_projection_leases
                WHERE slot = 'neo4j'
                  AND recovery_id IS NULL
                  AND owner = :worker_id
                  AND generation = :generation
                  AND neo4j_armed_generation = generation
                  AND leased_until > clock_timestamp()
                FOR UPDATE
            ), current_event AS MATERIALIZED (
                SELECT current.id,
                       current.entity_id,
                       current.relation_id,
                       current.aggregate_revision
                FROM graph_outbox AS current
                WHERE current.event_id = :event_id
                  AND current.delivered_at IS NULL
                  AND current.lease_owner = :worker_id
                  AND current.lease_generation = :generation
                  AND current.claim_version = :claim_version
                  AND current.leased_until > clock_timestamp()
                  AND EXISTS (SELECT 1 FROM current_leader)
                FOR UPDATE OF current
            ), acknowledge_current AS (
                UPDATE graph_outbox AS current
                SET delivered_at = COALESCE(current.delivered_at, NOW()),
                    leased_until = NULL,
                    lease_owner = NULL,
                    last_error_code = NULL
                FROM current_event
                WHERE current.id = current_event.id
                RETURNING current.event_id,
                          current_event.entity_id,
                          current_event.relation_id,
                          current_event.aggregate_revision
            ), retire_obsolete AS (
                UPDATE graph_outbox AS earlier
                SET delivered_at = clock_timestamp(),
                    leased_until = NULL,
                    lease_owner = NULL,
                    last_error_code = 'superseded'
                FROM acknowledge_current AS acknowledged
                WHERE earlier.delivered_at IS NULL
                  AND earlier.last_error_code = 'max_attempts'
                  AND earlier.aggregate_revision < acknowledged.aggregate_revision
                  AND (
                      (
                          acknowledged.entity_id IS NOT NULL
                          AND earlier.entity_id = acknowledged.entity_id
                      )
                      OR (
                          acknowledged.relation_id IS NOT NULL
                          AND earlier.relation_id = acknowledged.relation_id
                      )
                  )
                RETURNING earlier.event_id
            )
            SELECT EXISTS(SELECT 1 FROM acknowledge_current) AS acknowledged
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(statement, self._claim_params(claim))
            acknowledged = bool(result.scalar_one_or_none())
            await session.commit()
        return acknowledged

    async def mark_failed(
        self,
        claim: ProjectionClaim,
        error_code: str,
        *,
        max_attempts: int,
    ) -> bool:
        """CAS-release a failed claim without storing exception text or payloads."""
        safe_code = error_code if error_code in _SAFE_ERROR_CODES else "projection_failed"
        statement = sa.text(
            """
            WITH current_leader AS MATERIALIZED (
                SELECT generation
                FROM graph_projection_leases
                WHERE slot = 'neo4j'
                  AND recovery_id IS NULL
                  AND owner = :worker_id
                  AND generation = :generation
                  AND neo4j_armed_generation = generation
                  AND leased_until > clock_timestamp()
                FOR UPDATE
            )
            UPDATE graph_outbox AS outbox
            SET attempt_count = attempt_count + 1,
                available_at = CASE
                    WHEN attempt_count + 1 >= :max_attempts THEN 'infinity'::timestamptz
                    ELSE NOW() + (LEAST(300, POWER(2, attempt_count)) * INTERVAL '1 second')
                END,
                leased_until = NULL,
                lease_owner = NULL,
                last_error_code = CASE
                    WHEN attempt_count + 1 >= :max_attempts THEN 'max_attempts'
                    ELSE :error_code
                END
            WHERE outbox.event_id = :event_id
              AND outbox.delivered_at IS NULL
              AND outbox.lease_owner = :worker_id
              AND outbox.lease_generation = :generation
              AND outbox.claim_version = :claim_version
              AND outbox.leased_until > clock_timestamp()
              AND EXISTS (SELECT 1 FROM current_leader)
            RETURNING outbox.event_id
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                statement,
                {
                    **self._claim_params(claim),
                    "error_code": safe_code,
                    "max_attempts": max(1, max_attempts),
                },
            )
            failed = result.scalar_one_or_none() is not None
            await session.commit()
        return failed

    async def _stage_named_target(
        self,
        entity_id: UUID,
        *,
        target_type: str,
        target_key: str,
        relation_type: str,
        origin: str,
        required_source_project_key: str | None,
    ) -> GraphOutboxEvent:
        async with self._session_factory() as session:
            try:
                endpoints = await self._resolve_named_target(
                    session,
                    entity_id=entity_id,
                    target_type=target_type,
                    target_key=target_key,
                )
                self._validate_source_project_scope(
                    endpoints,
                    required_source_project_key,
                )
                event = await self._stage_relation_in_session(
                    session,
                    endpoints=endpoints,
                    relation_type=relation_type,
                    operation="upsert_relation",
                    properties={},
                    origin=origin,
                    confidence=None,
                )
                await session.commit()
                return event
            except Exception:
                await session.rollback()
                raise

    async def _resolve_uuid_endpoints(
        self,
        session: AsyncSession,
        source_id: UUID,
        target_id: UUID,
        *,
        require_active: bool = True,
    ) -> dict[str, Any]:
        statement = sa.text(
            """
            WITH locked_endpoints AS MATERIALIZED (
                SELECT id, source_uuid, entity_type, entity_key, project_key, lifecycle
                FROM brain_entities
                WHERE source_uuid IN (:source_id, :target_id)
                ORDER BY id
                FOR UPDATE
            )
            SELECT source.id AS source_entity_id,
                   source.entity_type AS source_entity_type,
                   source.entity_key AS source_entity_key,
                   source.project_key AS source_project_key,
                   target.id AS target_entity_id,
                   target.entity_type AS target_entity_type,
                   target.entity_key AS target_entity_key,
                   target.project_key AS target_project_key
            FROM locked_endpoints AS source
            CROSS JOIN locked_endpoints AS target
            WHERE source.source_uuid = :source_id
              AND target.source_uuid = :target_id
              AND (
                  NOT :require_active
                  OR (
                      source.lifecycle = 'active'
                      AND target.lifecycle = 'active'
                  )
              )
            """
        )
        result = await session.execute(
            statement,
            {
                "source_id": source_id,
                "target_id": target_id,
                "require_active": require_active,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise UnknownGraphEndpoint("one or more UUID endpoints are not registered")
        return dict(row)

    async def _resolve_named_target(
        self,
        session: AsyncSession,
        *,
        entity_id: UUID,
        target_type: str,
        target_key: str,
    ) -> dict[str, Any]:
        statement = sa.text(
            """
            WITH locked_endpoints AS MATERIALIZED (
                SELECT id, source_uuid, entity_type, entity_key, project_key, lifecycle
                FROM brain_entities
                WHERE source_uuid = :source_id
                   OR (
                       entity_type = :target_type
                       AND entity_key = :target_key
                   )
                ORDER BY id
                FOR UPDATE
            )
            SELECT source.id AS source_entity_id,
                   source.entity_type AS source_entity_type,
                   source.entity_key AS source_entity_key,
                   source.project_key AS source_project_key,
                   target.id AS target_entity_id,
                   target.entity_type AS target_entity_type,
                   target.entity_key AS target_entity_key
            FROM locked_endpoints AS source
            CROSS JOIN locked_endpoints AS target
            WHERE source.source_uuid = :source_id
              AND target.entity_type = :target_type
              AND target.entity_key = :target_key
              AND source.lifecycle = 'active'
              AND target.lifecycle = 'active'
            """
        )
        result = await session.execute(
            statement,
            {
                "source_id": entity_id,
                "target_type": target_type,
                "target_key": target_key,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise UnknownGraphEndpoint("the named graph endpoint is not registered")
        return dict(row)

    @staticmethod
    def _validate_source_project_scope(
        endpoints: dict[str, Any],
        project_key: str | None,
    ) -> None:
        if project_key is None:
            return
        if endpoints.get("source_project_key") != project_key:
            raise ValueError("relation source must remain in the authorized project")

    async def _stage_relation_in_session(
        self,
        session: AsyncSession,
        *,
        endpoints: dict[str, Any],
        relation_type: str,
        operation: GraphOperation,
        properties: dict[str, Any],
        origin: str,
        confidence: float | None,
    ) -> GraphOutboxEvent:
        validate_relation_shape(
            str(endpoints["source_entity_type"]),
            str(endpoints["target_entity_type"]),
            relation_type,
        )
        lifecycle = "deleted" if operation == "delete_relation" else "active"
        relation_insert = pg_insert(entity_relations).values(
            source_entity_id=endpoints["source_entity_id"],
            target_entity_id=endpoints["target_entity_id"],
            relation_type=relation_type,
            origin=origin[:64],
            confidence=confidence,
            properties=properties,
            lifecycle=lifecycle,
            deleted_at=sa.func.now() if lifecycle == "deleted" else None,
        )
        material_change = sa.or_(
            entity_relations.c.confidence.is_distinct_from(relation_insert.excluded.confidence),
            entity_relations.c.properties.is_distinct_from(relation_insert.excluded.properties),
            entity_relations.c.lifecycle.is_distinct_from(relation_insert.excluded.lifecycle),
        )
        relation_stmt = relation_insert.on_conflict_do_update(
            constraint="uq_entity_relations_endpoints_type",
            set_={
                "origin": entity_relations.c.origin,
                "confidence": relation_insert.excluded.confidence,
                "properties": relation_insert.excluded.properties,
                "lifecycle": relation_insert.excluded.lifecycle,
                "revision": sa.case(
                    (material_change, entity_relations.c.revision + 1),
                    else_=entity_relations.c.revision,
                ),
                "updated_at": sa.case(
                    (material_change, sa.func.now()),
                    else_=entity_relations.c.updated_at,
                ),
                "deleted_at": sa.case(
                    (material_change, relation_insert.excluded.deleted_at),
                    else_=entity_relations.c.deleted_at,
                ),
            },
        ).returning(entity_relations.c.id, entity_relations.c.revision)
        relation_result = await session.execute(relation_stmt)
        relation = relation_result.mappings().one_or_none()
        if relation is None:  # pragma: no cover - PostgreSQL RETURNING invariant
            raise RuntimeError("relation upsert returned no row")

        outbox_stmt = (
            pg_insert(graph_outbox)
            .values(
                relation_id=relation["id"],
                aggregate_revision=relation["revision"],
                operation=operation,
            )
            .on_conflict_do_update(
                constraint="uq_graph_outbox_relation_revision",
                set_={"operation": graph_outbox.c.operation},
            )
            .returning(graph_outbox.c.event_id)
        )
        outbox_result = await session.execute(outbox_stmt)
        outbox = outbox_result.mappings().one_or_none()
        if outbox is None:  # pragma: no cover - PostgreSQL RETURNING invariant
            raise RuntimeError("outbox insert returned no row")

        return GraphOutboxEvent(
            event_id=outbox["event_id"],
            operation=operation,
            aggregate_revision=int(relation["revision"]),
            relation_id=relation["id"],
            source_type=endpoints["source_entity_type"],
            source_key=endpoints["source_entity_key"],
            target_type=endpoints["target_entity_type"],
            target_key=endpoints["target_entity_key"],
            relation_type=relation_type,
            properties=properties,
        )

    @staticmethod
    def _validate_project_scope(
        endpoints: dict[str, Any],
        project_key: str | None,
    ) -> None:
        if project_key is None:
            return
        if (
            endpoints.get("source_project_key") != project_key
            or endpoints.get("target_project_key") != project_key
        ):
            raise ValueError("relation endpoints must remain in the authorized project")

    @staticmethod
    def _event_from_mapping(row: Any) -> GraphOutboxEvent:
        return GraphOutboxEvent(
            event_id=row["event_id"],
            operation=row["operation"],
            aggregate_revision=int(row.get("aggregate_revision") or 0),
            relation_id=row.get("relation_id"),
            entity_id=row.get("entity_id"),
            entity_type=row.get("entity_type"),
            entity_key=row.get("entity_key"),
            source_uuid=row.get("source_uuid"),
            project_key=row.get("project_key"),
            display_label=row.get("display_label"),
            lifecycle=row.get("lifecycle"),
            source_type=row.get("source_type"),
            source_key=row.get("source_key"),
            target_type=row.get("target_type"),
            target_key=row.get("target_key"),
            relation_type=row.get("relation_type"),
            relation_lifecycle=row.get("relation_lifecycle"),
            properties=dict(row.get("properties") or {}),
        )

    @staticmethod
    def _claim_from_mapping(row: Any) -> ProjectionClaim:
        return ProjectionClaim(
            event=PgGraphLedgerRepo._event_from_mapping(row),
            owner_id=str(row["owner_id"]),
            lease_generation=int(row["lease_generation"]),
            claim_version=int(row["claim_version"]),
            leased_until=row["leased_until"],
        )

    @staticmethod
    def _claim_params(claim: ProjectionClaim) -> dict[str, Any]:
        return {
            "event_id": claim.event.event_id,
            "worker_id": claim.owner_id,
            "owner_id": claim.owner_id,
            "generation": claim.lease_generation,
            "lease_generation": claim.lease_generation,
            "claim_version": claim.claim_version,
        }

    @staticmethod
    def _validate_relation(relation_type: str, confidence: float | None) -> None:
        if relation_type not in _CANONICAL_RELATION_TYPES:
            raise ValueError(f"unsupported relation type: {relation_type}")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
