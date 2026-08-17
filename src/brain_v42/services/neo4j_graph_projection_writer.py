"""Neo4j projection writer with a global fence and durable aggregate cursors."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from brain_v42.services.graph_service import ALLOWED_DOMAINS

if TYPE_CHECKING:
    from brain_v42.repositories.pg_graph_ledger import (
        GraphOutboxEvent,
        ProjectionClaim,
        ProjectionLeadership,
        ProjectionRecoveryLease,
    )

_GRAPH_LABELS = {
    "decision": "Decision",
    "learning": "Learning",
    "snippet": "Snippet",
    "runbook": "Runbook",
    "adr": "ADR",
    "feature": "Feature",
    "plan": "Plan",
}
_RELATION_TYPES = frozenset(
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
_PROJECT_RELATION_TYPES = frozenset({"CONTAINS", "DEPENDS_ON"})
_KNOWLEDGE_ENTITY_TYPES = frozenset(
    {"decision", "learning", "snippet", "runbook", "adr", "feature", "plan"}
)
_SAFE_PROPERTY_KEYS = frozenset(
    {"similarity", "score", "threshold", "model", "model_version", "method"}
)

_ACTIVATE_GENERATION = """
OPTIONAL MATCH (fence:BrainProjectionFence {name: 'canonical'})
FOREACH (_ IN CASE WHEN fence IS NULL THEN [] ELSE [1] END |
    SET fence._lock = randomUUID()
)
WITH fence,
     fence IS NOT NULL
     AND fence.protocol_version = 2
     AND fence.recovery_id IS NULL
     AND (
         (
             $allow_advance
             AND fence.generation = $generation - 1
         )
         OR (
             fence.generation = $generation
             AND (fence.owner_id IS NULL OR fence.owner_id = $owner_id)
         )
     ) AS accepted
FOREACH (_ IN CASE WHEN accepted THEN [1] ELSE [] END |
    SET fence.generation = $generation,
        fence.owner_id = $owner_id
)
RETURN accepted,
       coalesce(fence.generation, -1) AS current_generation
"""

_OBSERVE_RECOVERY_FENCE = """
OPTIONAL MATCH (fence:BrainProjectionFence {name: 'canonical'})
FOREACH (_ IN CASE WHEN fence IS NULL THEN [] ELSE [1] END |
    SET fence._lock = randomUUID()
)
WITH fence
RETURN fence IS NULL
       OR (
           fence.protocol_version = 2
           AND (
               (
                   fence.recovery_id = $recovery_id
                   AND fence.generation <= $generation
               )
               OR (
                   fence.recovery_id IS NULL
                   AND fence.generation < $generation
               )
               OR (
                   $allow_equal_finalized
                   AND fence.recovery_id IS NULL
                   AND fence.generation = $generation
                   AND fence.owner_id = $owner_id
               )
           )
       ) AS accepted,
       coalesce(fence.generation, -1) AS current_generation
"""

_DELETE_PROJECTION_FOR_RECOVERY = """
MATCH (business)
WHERE business:Project
   OR business:Domain
   OR business:Decision
   OR business:Learning
   OR business:Snippet
   OR business:Runbook
   OR business:ADR
   OR business:Feature
   OR business:Plan
   OR business:BrainProjectionCursor
DETACH DELETE business
RETURN count(*) AS deleted_nodes
"""

_INSTALL_RECOVERY_FENCE = """
MERGE (fence:BrainProjectionFence {name: 'canonical'})
SET fence.protocol_version = 2,
    fence.generation = $generation,
    fence.owner_id = $owner_id,
    fence.recovery_id = $recovery_id,
    fence._lock = randomUUID()
RETURN fence.generation AS current_generation
"""

_FINALIZE_RECOVERY_FENCE = """
OPTIONAL MATCH (fence:BrainProjectionFence {name: 'canonical'})
FOREACH (_ IN CASE WHEN fence IS NULL THEN [] ELSE [1] END |
    SET fence._lock = randomUUID()
)
WITH fence,
     CASE WHEN fence IS NULL THEN false ELSE
         fence.protocol_version = 2
         AND fence.generation = $generation
         AND fence.owner_id = $owner_id
         AND (
             fence.recovery_id = $recovery_id
             OR fence.recovery_id IS NULL
         )
     END AS accepted
FOREACH (_ IN CASE
    WHEN accepted AND fence.recovery_id = $recovery_id THEN [1]
    ELSE []
END |
    REMOVE fence.recovery_id
)
RETURN accepted,
       coalesce(fence.generation, -1) AS current_generation
"""

_LOCK_FENCE_AND_CURSOR = """
MATCH (fence:BrainProjectionFence {name: 'canonical'})
SET fence._lock = randomUUID()
WITH fence
WHERE fence.protocol_version = 2
  AND fence.recovery_id IS NULL
  AND fence.generation = $lease_generation
  AND fence.owner_id = $owner_id
MERGE (cursor:BrainProjectionCursor {aggregate_key: $aggregate_key})
ON CREATE SET cursor.revision = -1,
              cursor.claim_version = -1,
              cursor.event_id = '',
              cursor.operation = ''
SET cursor._lock = randomUUID()
WITH fence, cursor
RETURN CASE
    WHEN $aggregate_revision > cursor.revision THEN 'applied'
    WHEN $aggregate_revision < cursor.revision THEN 'superseded'
    WHEN cursor.event_id <> $event_id THEN 'conflict'
    WHEN $claim_version < cursor.claim_version THEN 'superseded'
    ELSE 'already_current'
END AS status,
fence.generation AS current_generation
"""

_ADVANCE_CURSOR = """
MATCH (cursor:BrainProjectionCursor {aggregate_key: $aggregate_key})
SET cursor.revision = $aggregate_revision,
    cursor.claim_version = $claim_version,
    cursor.event_id = $event_id,
    cursor.operation = $operation,
    cursor.lease_generation = $lease_generation,
    cursor.updated_at = timestamp()
RETURN 'applied' AS status
"""


@dataclass(frozen=True, slots=True)
class ProjectionActivation:
    """Result of comparing one PostgreSQL generation with the Neo4j fence."""

    accepted: bool
    current_generation: int


@dataclass(frozen=True, slots=True)
class ProjectionRecoveryReset:
    """Outcome of atomically replacing all Neo4j projection state."""

    accepted: bool
    current_generation: int
    deleted_nodes: int


@dataclass(frozen=True, slots=True)
class ProjectionRecoveryFinalization:
    """Outcome of removing one exact Neo4j recovery marker."""

    accepted: bool
    current_generation: int


class ProjectionOutcome(StrEnum):
    """Bounded result vocabulary shared with the outbox projector."""

    APPLIED = "applied"
    ALREADY_CURRENT = "already_current"
    SUPERSEDED = "superseded"
    STALE_GENERATION = "stale_generation"
    MISSING_NODE = "missing_node"
    CONFLICT = "conflict"
    INVALID_EVENT = "invalid_event"
    ERROR = "error"


class Neo4jGraphProjectionWriter:
    """Apply one claimed canonical revision in an explicit fenced transaction."""

    def __init__(self, driver: Any, *, timeout: float = 5.0) -> None:
        self._driver = driver
        self._timeout = timeout

    async def activate_generation(
        self,
        leadership: ProjectionLeadership,
    ) -> ProjectionActivation:
        """Advance the Neo4j barrier monotonically for a PostgreSQL leader."""
        params = {
            "owner_id": leadership.owner_id,
            "generation": leadership.generation,
            "allow_advance": not leadership.armed,
        }
        async with self._driver.session() as session:
            transaction = await session.begin_transaction(timeout=self._timeout)
            try:
                result = await transaction.run(self._query(_ACTIVATE_GENERATION), params)
                record = await result.single()
                if record is None:
                    raise RuntimeError("Neo4j projection fence returned no activation result")
                activation = ProjectionActivation(
                    accepted=bool(record.get("accepted")),
                    current_generation=int(record.get("current_generation", -1)),
                )
                if not activation.accepted:
                    await transaction.rollback()
                    return activation
                await transaction.commit()
                return activation
            except asyncio.CancelledError:
                transaction.cancel()
                raise
            except BaseException as exc:
                await self._rollback_preserving_error(transaction, exc)
                raise

    async def reset_for_recovery(
        self,
        state: ProjectionRecoveryLease,
    ) -> ProjectionRecoveryReset:
        """Wipe projection state and install one monotonic recovery fence atomically."""
        if state.phase not in {"prepared", "neo_ready"}:
            raise ValueError("reset requires a prepared or neo_ready PostgreSQL recovery")
        params = {
            "recovery_id": str(state.recovery_id),
            "owner_id": state.owner_id,
            "generation": state.generation,
            "allow_equal_finalized": state.phase == "neo_ready",
        }
        async with self._driver.session() as session:
            transaction = await session.begin_transaction(timeout=self._timeout)
            try:
                observation_result = await transaction.run(
                    self._query(_OBSERVE_RECOVERY_FENCE),
                    params,
                )
                observation = await observation_result.single()
                if observation is None:
                    raise RuntimeError("Neo4j recovery fence returned no observation")
                if not bool(observation.get("accepted")):
                    await transaction.rollback()
                    return ProjectionRecoveryReset(
                        accepted=False,
                        current_generation=int(observation.get("current_generation", -1)),
                        deleted_nodes=0,
                    )

                deletion_result = await transaction.run(
                    self._query(_DELETE_PROJECTION_FOR_RECOVERY),
                    params,
                )
                deletion = await deletion_result.single()
                if deletion is None:
                    raise RuntimeError("Neo4j recovery delete returned no result")
                install_result = await transaction.run(
                    self._query(_INSTALL_RECOVERY_FENCE),
                    params,
                )
                installed = await install_result.single()
                if installed is None:
                    raise RuntimeError("Neo4j recovery fence install returned no result")
                current_generation = int(installed.get("current_generation", -1))
                if current_generation != state.generation:
                    raise RuntimeError("Neo4j recovery fence installed an unexpected generation")
                await transaction.commit()
                return ProjectionRecoveryReset(
                    accepted=True,
                    current_generation=current_generation,
                    deleted_nodes=int(deletion.get("deleted_nodes", 0)),
                )
            except asyncio.CancelledError:
                transaction.cancel()
                raise
            except BaseException as exc:
                await self._rollback_preserving_error(transaction, exc)
                raise

    async def finalize_recovery(
        self,
        state: ProjectionRecoveryLease,
    ) -> ProjectionRecoveryFinalization:
        """Remove the exact recovery marker after PostgreSQL entered neo_ready."""
        if state.phase != "neo_ready":
            raise ValueError("finalization requires a neo_ready PostgreSQL recovery")
        params = {
            "recovery_id": str(state.recovery_id),
            "owner_id": state.owner_id,
            "generation": state.generation,
        }
        async with self._driver.session() as session:
            transaction = await session.begin_transaction(timeout=self._timeout)
            try:
                result = await transaction.run(
                    self._query(_FINALIZE_RECOVERY_FENCE),
                    params,
                )
                record = await result.single()
                if record is None:
                    raise RuntimeError("Neo4j recovery finalization returned no result")
                finalization = ProjectionRecoveryFinalization(
                    accepted=bool(record.get("accepted")),
                    current_generation=int(record.get("current_generation", -1)),
                )
                if not finalization.accepted:
                    await transaction.rollback()
                    return finalization
                await transaction.commit()
                return finalization
            except asyncio.CancelledError:
                transaction.cancel()
                raise
            except BaseException as exc:
                await self._rollback_preserving_error(transaction, exc)
                raise

    async def apply(self, claim: ProjectionClaim) -> ProjectionOutcome:
        """Fence, order, mutate and advance the cursor as one Neo4j commit."""
        prepared = self._prepare_claim(claim)
        if prepared is None:
            return ProjectionOutcome.INVALID_EVENT
        params, mutation = prepared

        async with self._driver.session() as session:
            transaction = await session.begin_transaction(timeout=self._timeout)
            try:
                lock_result = await transaction.run(
                    self._query(_LOCK_FENCE_AND_CURSOR),
                    params,
                )
                lock_record = await lock_result.single()
                if lock_record is None:
                    await transaction.rollback()
                    return ProjectionOutcome.STALE_GENERATION

                status = str(lock_record.get("status", ""))
                if status == ProjectionOutcome.STALE_GENERATION:
                    await transaction.rollback()
                    return ProjectionOutcome.STALE_GENERATION
                if status == ProjectionOutcome.SUPERSEDED:
                    await transaction.rollback()
                    return ProjectionOutcome.SUPERSEDED
                if status == ProjectionOutcome.CONFLICT:
                    await transaction.rollback()
                    return ProjectionOutcome.CONFLICT
                if status == ProjectionOutcome.ALREADY_CURRENT:
                    await transaction.run(self._query(_ADVANCE_CURSOR), params)
                    await transaction.commit()
                    return ProjectionOutcome.ALREADY_CURRENT
                if status != ProjectionOutcome.APPLIED:
                    await transaction.rollback()
                    return ProjectionOutcome.ERROR

                mutation_result = await transaction.run(self._query(mutation), params)
                mutation_record = await mutation_result.single()
                if mutation_record is None or mutation_record.get("anchors") is None:
                    await transaction.rollback()
                    return ProjectionOutcome.ERROR
                anchors = int(mutation_record["anchors"])
                if anchors < 1:
                    await transaction.rollback()
                    return ProjectionOutcome.MISSING_NODE

                await transaction.run(self._query(_ADVANCE_CURSOR), params)
                await transaction.commit()
                return ProjectionOutcome.APPLIED
            except asyncio.CancelledError:
                transaction.cancel()
                raise
            except BaseException as exc:
                await self._rollback_preserving_error(transaction, exc)
                raise

    @staticmethod
    async def _rollback_preserving_error(transaction: Any, error: BaseException) -> None:
        if transaction.closed():
            return
        try:
            await transaction.rollback()
        except asyncio.CancelledError:
            transaction.cancel()
            raise
        except BaseException as rollback_error:
            error.add_note(f"Neo4j rollback also failed: {type(rollback_error).__name__}")

    @staticmethod
    def _query(statement: str) -> str:
        return statement

    def _prepare_claim(
        self,
        claim: ProjectionClaim,
    ) -> tuple[dict[str, Any], str] | None:
        event = claim.event
        if claim.lease_generation < 1 or claim.claim_version < 1 or event.aggregate_revision < 1:
            return None

        if event.entity_id is not None and event.relation_id is None:
            aggregate_key = f"entity:{event.entity_id}"
            mutation = self._entity_mutation(event)
        elif event.relation_id is not None and event.entity_id is None:
            aggregate_key = f"relation:{event.relation_id}"
            mutation = self._relation_mutation(event)
        else:
            return None
        if mutation is None:
            return None
        statement, extra, operation = mutation
        params: dict[str, Any] = {
            "aggregate_key": aggregate_key,
            "aggregate_revision": event.aggregate_revision,
            "claim_version": claim.claim_version,
            "event_id": str(event.event_id),
            "lease_generation": claim.lease_generation,
            "owner_id": claim.owner_id,
            "operation": operation,
            **extra,
        }
        return params, statement

    def _entity_mutation(
        self,
        event: GraphOutboxEvent,
    ) -> tuple[str, dict[str, Any], str] | None:
        entity_type = str(event.entity_type or "").lower()
        delete_requested = (
            event.lifecycle == "deleted"
            if event.lifecycle is not None
            else event.operation == "delete_entity"
        )
        operation = "delete_entity" if delete_requested else "upsert_entity"

        if entity_type == "project":
            if not event.entity_key:
                return None
            if delete_requested:
                statement = """
                OPTIONAL MATCH (node:Project {project_key: $entity_key})
                WITH collect(node) AS nodes
                FOREACH (node IN nodes | DETACH DELETE node)
                RETURN 1 AS anchors
                """
                return statement, {"entity_key": event.entity_key}, operation
            statement = """
            MERGE (node:Project {project_key: $entity_key})
            SET node.id = $graph_id,
                node.name = $display_label
            RETURN 1 AS anchors
            """
            project_graph_id = event.source_uuid or event.entity_id
            return (
                statement,
                {
                    "entity_key": event.entity_key,
                    "graph_id": str(project_graph_id),
                    "display_label": event.display_label or event.entity_key,
                },
                operation,
            )

        if entity_type == "domain":
            if delete_requested or event.entity_key not in ALLOWED_DOMAINS:
                return None
            statement = """
            MERGE (node:Domain {name: $entity_key})
            SET node.updated_at = timestamp()
            RETURN 1 AS anchors
            """
            return statement, {"entity_key": event.entity_key}, operation

        label = _GRAPH_LABELS.get(entity_type)
        if label is None:
            return None
        knowledge_graph_id = event.source_uuid or event.entity_key
        if knowledge_graph_id is None:
            return None
        if delete_requested:
            statement = f"""
            OPTIONAL MATCH (node:{label} {{id: $graph_id}})
            WITH collect(node) AS nodes
            FOREACH (node IN nodes | DETACH DELETE node)
            RETURN 1 AS anchors
            """
            return statement, {"graph_id": str(knowledge_graph_id)}, operation

        if entity_type == "learning":
            label_property = "topic"
        elif entity_type == "feature":
            label_property = "name"
        else:
            label_property = "title"
        statement = f"""
        MERGE (node:{label} {{id: $graph_id}})
        SET node.{label_property} = $display_label,
            node.project_key = $project_key
        RETURN 1 AS anchors
        """
        return (
            statement,
            {
                "graph_id": str(knowledge_graph_id),
                "display_label": event.display_label,
                "project_key": event.project_key,
            },
            operation,
        )

    def _relation_mutation(
        self,
        event: GraphOutboxEvent,
    ) -> tuple[str, dict[str, Any], str] | None:
        relation_type = str(event.relation_type or "")
        if relation_type not in _RELATION_TYPES or not event.source_key or not event.target_key:
            return None
        source_type = str(event.source_type or "").lower()
        target_type = str(event.target_type or "").lower()
        if not self._valid_relation_shape(source_type, target_type, relation_type):
            return None
        delete_requested = (
            event.relation_lifecycle != "active"
            if event.relation_lifecycle is not None
            else event.operation == "delete_relation"
        )
        operation = "delete_relation" if delete_requested else "upsert_relation"
        props = self._sanitize_properties(event.properties)
        params: dict[str, Any] = {
            "source_key": event.source_key,
            "target_key": event.target_key,
            "properties": props,
        }

        if source_type == "project" and target_type == "project":
            if relation_type not in _PROJECT_RELATION_TYPES:
                return None
            anchors = (
                "MATCH (source:Project {project_key: $source_key}) "
                "MATCH (target:Project {project_key: $target_key}) "
            )
        elif source_type in {"project", "domain"}:
            return None
        elif target_type == "project":
            if relation_type != "BELONGS_TO":
                return None
            anchors = (
                "MATCH (source {id: $source_key}) "
                "MATCH (target:Project {project_key: $target_key}) "
            )
        elif target_type == "domain":
            if relation_type != "BELONGS_TO_DOMAIN" or event.target_key not in ALLOWED_DOMAINS:
                return None
            if delete_requested:
                anchors = (
                    "MATCH (source {id: $source_key}) MATCH (target:Domain {name: $target_key}) "
                )
            else:
                anchors = (
                    "MERGE (target:Domain {name: $target_key}) "
                    "SET target.updated_at = timestamp() "
                    "WITH target MATCH (source {id: $source_key}) "
                )
        else:
            anchors = "MATCH (source {id: $source_key}) MATCH (target {id: $target_key}) "

        relation_pattern = (
            f"-[relation:{relation_type}]-"
            if relation_type == "RELATED_TO"
            else f"-[relation:{relation_type}]->"
        )
        if delete_requested:
            statement = (
                anchors.replace("MATCH (source", "OPTIONAL MATCH (source", 1)
                + f"OPTIONAL MATCH (source){relation_pattern}(target) "
                "WITH collect(relation) AS relations "
                "FOREACH (relation IN relations | DELETE relation) "
                "RETURN 1 AS anchors"
            )
            return statement, params, operation

        statement = (
            anchors + f"MERGE (source){relation_pattern}(target) "
            "SET relation = $properties "
            "RETURN count(source) AS anchors"
        )
        return statement, params, operation

    @staticmethod
    def _valid_relation_shape(
        source_type: str,
        target_type: str,
        relation_type: str,
    ) -> bool:
        if relation_type == "BELONGS_TO":
            return source_type in _KNOWLEDGE_ENTITY_TYPES and target_type == "project"
        if relation_type == "BELONGS_TO_DOMAIN":
            return source_type in _KNOWLEDGE_ENTITY_TYPES and target_type == "domain"
        if relation_type in _PROJECT_RELATION_TYPES:
            return source_type == target_type == "project"
        if relation_type in {"SUPERSEDES", "MERGED_INTO"}:
            return source_type == target_type and source_type in _KNOWLEDGE_ENTITY_TYPES
        return (
            relation_type in _RELATION_TYPES
            and source_type in _KNOWLEDGE_ENTITY_TYPES
            and target_type in _KNOWLEDGE_ENTITY_TYPES
        )

    @staticmethod
    def _sanitize_properties(properties: dict[str, Any] | None) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key in _SAFE_PROPERTY_KEYS:
            value = (properties or {}).get(key)
            if isinstance(value, str):
                cleaned[key] = value[:128]
            elif isinstance(value, int | float | bool):
                cleaned[key] = value
        return cleaned
