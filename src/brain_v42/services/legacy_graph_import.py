"""Orchestrate an idempotent import of legacy Neo4j facts into PostgreSQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from brain_v42.services.legacy_graph_models import (
    LegacyGraphNode,
    LegacyGraphSnapshot,
    canonicalize_legacy_snapshot,
)
from brain_v42.services.legacy_graph_store import PgLegacyGraphStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class LegacyGraphImportBlocked(RuntimeError):
    """Raised when applying a snapshot cannot prove it is complete enough."""


@dataclass(frozen=True, slots=True)
class LegacyGraphImportReport:
    applied: bool
    candidate_entities: int
    candidate_relations: int
    imported_entities: int = 0
    imported_relations: int = 0
    acknowledged_entities: int = 0
    acknowledged_relations: int = 0
    skipped_nodes: int = 0
    skipped_relations: int = 0
    truncated_nodes: bool = False
    truncated_relations: bool = False
    canonical_project_keys: tuple[str, ...] = ()


def _deduplicate_nodes(nodes: tuple[LegacyGraphNode, ...]) -> list[LegacyGraphNode]:
    return list({(node.entity_type, node.entity_key): node for node in nodes}.values())


def _canonical_project_keys(snapshot: LegacyGraphSnapshot) -> tuple[str, ...]:
    keys = {node.project_key for node in snapshot.nodes if node.project_key}
    keys.update(node.entity_key for node in snapshot.nodes if node.entity_type == "project")
    keys.update(
        relation.source_key for relation in snapshot.relations if relation.source_type == "project"
    )
    keys.update(
        relation.target_key for relation in snapshot.relations if relation.target_type == "project"
    )
    return tuple(sorted(keys))


class LegacyGraphImporter:
    """Stage legacy facts atomically and leave no initial event pending."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        batch_size: int = 500,
    ) -> None:
        if batch_size <= 0 or batch_size > 1_000:
            raise ValueError("batch_size must be between 1 and 1000")
        self._session_factory = session_factory
        self._batch_size = batch_size

    async def import_snapshot(
        self,
        snapshot: LegacyGraphSnapshot,
        *,
        apply: bool = False,
        allow_skips: bool = False,
    ) -> LegacyGraphImportReport:
        if snapshot.status != "ok":
            raise LegacyGraphImportBlocked("Neo4j snapshot is unavailable")
        snapshot = canonicalize_legacy_snapshot(snapshot)
        nodes = _deduplicate_nodes(snapshot.nodes)
        canonical_project_keys = _canonical_project_keys(snapshot)
        report = LegacyGraphImportReport(
            applied=False,
            candidate_entities=len(nodes),
            candidate_relations=len(snapshot.relations),
            skipped_nodes=snapshot.skipped_nodes,
            skipped_relations=snapshot.skipped_relations,
            truncated_nodes=snapshot.truncated_nodes,
            truncated_relations=snapshot.truncated_relations,
            canonical_project_keys=canonical_project_keys,
        )
        if not apply:
            return report
        if snapshot.truncated_nodes or snapshot.truncated_relations:
            raise LegacyGraphImportBlocked(
                "Neo4j snapshot is truncated; raise the explicit limits before applying"
            )
        if not allow_skips and (snapshot.skipped_nodes or snapshot.skipped_relations):
            raise LegacyGraphImportBlocked(
                "Neo4j snapshot contains skipped facts; inspect them or opt in explicitly"
            )

        async with self._session_factory() as session:
            store = PgLegacyGraphStore(session, batch_size=self._batch_size)
            try:
                await store.ensure_projects(nodes)
                imported_entity_ids = await store.insert_entities(nodes)
                entities = await store.load_entities(nodes)
                unresolved_nodes = len(nodes) - len(entities)
                if unresolved_nodes and not allow_skips:
                    raise LegacyGraphImportBlocked(
                        f"{unresolved_nodes} unresolved Neo4j node identities"
                    )
                imported_relation_ids, unresolved_relations = await store.insert_relations(
                    snapshot.relations,
                    entities,
                )
                if unresolved_relations and not allow_skips:
                    raise LegacyGraphImportBlocked(
                        f"{unresolved_relations} unresolved Neo4j relation endpoints"
                    )
                acknowledged_entities = await store.record_delivered_entities(imported_entity_ids)
                if acknowledged_entities != len(imported_entity_ids):
                    raise LegacyGraphImportBlocked(
                        "outbox did not record every newly imported Neo4j entity"
                    )
                acknowledged_relations = await store.record_delivered_relations(
                    imported_relation_ids
                )
                if acknowledged_relations != len(imported_relation_ids):
                    raise LegacyGraphImportBlocked(
                        "outbox did not record every newly imported Neo4j relation"
                    )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        return LegacyGraphImportReport(
            applied=True,
            candidate_entities=len(nodes),
            candidate_relations=len(snapshot.relations),
            imported_entities=len(imported_entity_ids),
            imported_relations=len(imported_relation_ids),
            acknowledged_entities=acknowledged_entities,
            acknowledged_relations=acknowledged_relations,
            skipped_nodes=snapshot.skipped_nodes + unresolved_nodes,
            skipped_relations=snapshot.skipped_relations + unresolved_relations,
            truncated_nodes=snapshot.truncated_nodes,
            truncated_relations=snapshot.truncated_relations,
            canonical_project_keys=canonical_project_keys,
        )
