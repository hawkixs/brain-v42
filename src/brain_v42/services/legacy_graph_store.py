"""PostgreSQL persistence primitives for the one-time legacy graph import."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db.tables import brain_entities, entity_relations, graph_outbox, projects
from brain_v42.repositories.pg_graph_ledger import (
    canonicalize_relation_endpoints,
    validate_relation_shape,
)
from brain_v42.services.legacy_graph_models import (
    LegacyGraphNode,
    LegacyGraphRelation,
    StoredEntity,
    canonicalize_legacy_node,
    canonicalize_legacy_relation,
)

_NODE_NAMESPACE = UUID("f96c0f6f-2500-46e9-9c4f-9df376d42a9a")
_RELATION_NAMESPACE = UUID("eb334ecf-b27f-476f-a3bc-ff4d1fcf11d9")


def _chunks[T](values: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


class PgLegacyGraphStore:
    """Insert legacy facts without creating pending Neo4j projection work."""

    def __init__(self, session: AsyncSession, *, batch_size: int) -> None:
        self._session = session
        self._batch_size = batch_size

    async def ensure_projects(self, nodes: Sequence[LegacyGraphNode]) -> None:
        nodes = [canonicalize_legacy_node(node) for node in nodes]
        labels = {
            node.entity_key: node.display_label for node in nodes if node.entity_type == "project"
        }
        keys = sorted(
            {node.project_key for node in nodes if node.project_key}
            | {node.entity_key for node in nodes if node.entity_type == "project"}
        )
        if not keys:
            return
        values = [
            {
                "project_key": key,
                "display_name": labels.get(key),
                "registry_status": "unclaimed",
                "source": "reference",
                "metadata": {"origin": "legacy_neo4j"},
            }
            for key in keys
        ]
        for batch in _chunks(values, self._batch_size):
            statement = (
                pg_insert(projects)
                .values(list(batch))
                .on_conflict_do_nothing(index_elements=[projects.c.project_key])
            )
            await self._session.execute(statement)

        implicit_project_keys = [key for key in keys if key not in labels]
        for project_key_batch in _chunks(implicit_project_keys, self._batch_size):
            referenced_projects = sa.values(
                sa.column("project_key", sa.String(length=50)),
                name="legacy_project_keys",
            ).data([(key,) for key in project_key_batch])
            await self._session.execute(
                sa.select(
                    sa.func.register_referenced_project(referenced_projects.c.project_key)
                ).select_from(referenced_projects)
            )

    async def insert_entities(self, nodes: Sequence[LegacyGraphNode]) -> list[UUID]:
        inserted: list[UUID] = []
        nodes = [canonicalize_legacy_node(node) for node in nodes]
        values = [
            {
                "id": node.source_uuid
                or uuid5(_NODE_NAMESPACE, f"{node.entity_type}:{node.entity_key}"),
                "entity_type": node.entity_type,
                "entity_key": node.entity_key,
                "source_uuid": node.source_uuid,
                "project_key": node.project_key,
                "scope_kind": node.scope_kind,
                "display_label": node.display_label,
                "lifecycle": "active",
                "revision": 1,
                "metadata": {"origin": "legacy_neo4j"},
            }
            for node in nodes
        ]
        for batch in _chunks(values, self._batch_size):
            result = await self._session.execute(
                pg_insert(brain_entities)
                .values(list(batch))
                .on_conflict_do_nothing()
                .returning(brain_entities.c.id)
            )
            inserted.extend(row["id"] for row in result.mappings().all())
        return inserted

    async def load_entities(
        self, nodes: Sequence[LegacyGraphNode]
    ) -> dict[tuple[str, str], StoredEntity]:
        nodes = [canonicalize_legacy_node(node) for node in nodes]
        keys = sorted({(node.entity_type, node.entity_key) for node in nodes})
        output: dict[tuple[str, str], StoredEntity] = {}
        for batch in _chunks(keys, self._batch_size):
            statement = sa.select(
                brain_entities.c.id,
                brain_entities.c.entity_type,
                brain_entities.c.entity_key,
                brain_entities.c.revision,
                brain_entities.c.lifecycle,
            ).where(sa.tuple_(brain_entities.c.entity_type, brain_entities.c.entity_key).in_(batch))
            rows = (await self._session.execute(statement)).mappings().all()
            for row in rows:
                entity = StoredEntity(**row)
                output[(entity.entity_type, entity.entity_key)] = entity
        return output

    async def insert_relations(
        self,
        relations: Sequence[LegacyGraphRelation],
        entities: dict[tuple[str, str], StoredEntity],
    ) -> tuple[list[UUID], int]:
        values: list[dict[str, Any]] = []
        skipped = 0
        seen: set[tuple[UUID, UUID, str]] = set()
        for relation in relations:
            relation = canonicalize_legacy_relation(relation)
            source = entities.get((relation.source_type, relation.source_key))
            target = entities.get((relation.target_type, relation.target_key))
            if (
                source is None
                or target is None
                or source.lifecycle == "deleted"
                or target.lifecycle == "deleted"
            ):
                skipped += 1
                continue
            try:
                validate_relation_shape(
                    source.entity_type,
                    target.entity_type,
                    relation.relation_type,
                )
            except ValueError:
                skipped += 1
                continue
            source_id, target_id = canonicalize_relation_endpoints(
                source.id,
                target.id,
                relation.relation_type,
            )
            key = (source_id, target_id, relation.relation_type)
            if source_id == target_id:
                skipped += 1
                continue
            if key in seen:
                continue
            seen.add(key)
            values.append(
                {
                    "id": uuid5(_RELATION_NAMESPACE, ":".join(map(str, key))),
                    "source_entity_id": source_id,
                    "target_entity_id": target_id,
                    "relation_type": relation.relation_type,
                    "origin": "legacy_neo4j",
                    "origin_ref": "neo4j_snapshot",
                    "properties": relation.properties,
                    "lifecycle": "active",
                    "revision": 1,
                }
            )
        inserted: list[UUID] = []
        for batch in _chunks(values, self._batch_size):
            result = await self._session.execute(
                pg_insert(entity_relations)
                .values(list(batch))
                .on_conflict_do_nothing()
                .returning(entity_relations.c.id)
            )
            inserted.extend(row["id"] for row in result.mappings().all())
        return inserted, skipped

    async def record_delivered_entities(self, entity_ids: Sequence[UUID]) -> int:
        values = [
            {
                "entity_id": entity_id,
                "aggregate_revision": 1,
                "operation": "upsert_entity",
                "delivered_at": sa.func.now(),
            }
            for entity_id in entity_ids
        ]
        return await self._record_delivered(values)

    async def record_delivered_relations(self, relation_ids: Sequence[UUID]) -> int:
        values = [
            {
                "relation_id": relation_id,
                "aggregate_revision": 1,
                "operation": "upsert_relation",
                "delivered_at": sa.func.now(),
            }
            for relation_id in relation_ids
        ]
        return await self._record_delivered(values)

    async def _record_delivered(self, values: list[dict[str, Any]]) -> int:
        recorded = 0
        for batch in _chunks(values, self._batch_size):
            statement = (
                pg_insert(graph_outbox)
                .values(list(batch))
                .on_conflict_do_nothing()
                .returning(graph_outbox.c.event_id)
            )
            result = await self._session.execute(statement)
            recorded += len(result.mappings().all())
        return recorded
