"""Bounded, secret-free snapshot of legacy Neo4j graph facts."""

from __future__ import annotations

import asyncio
import math
from typing import Any
from uuid import UUID

import structlog
from neo4j import unit_of_work

from brain_v42.repositories.pg_graph_ledger import sanitize_relation_properties
from brain_v42.services.legacy_graph_models import (
    CANONICAL_RELATION_TYPES,
    LEGACY_LABEL_TYPES,
    LegacyGraphNode,
    LegacyGraphRelation,
    LegacyGraphSnapshot,
    canonicalize_legacy_project_key,
)

logger = structlog.get_logger(__name__)


def _entity_identity(identity: Any, labels: Any) -> tuple[str, str, UUID | None] | None:
    if not isinstance(identity, str) or not identity or len(identity) > 128:
        return None
    label_set = set(labels) if isinstance(labels, list | tuple) else set()
    entity_type = next((kind for label, kind in LEGACY_LABEL_TYPES if label in label_set), None)
    if entity_type is None:
        return None
    if entity_type == "project":
        project_key = canonicalize_legacy_project_key(identity)
        return (entity_type, project_key, None) if 0 < len(project_key) <= 50 else None
    if entity_type == "domain":
        return entity_type, identity, None
    try:
        source_uuid = UUID(identity)
    except (TypeError, ValueError):
        return None
    return entity_type, str(source_uuid), source_uuid


def _node_from_row(row: dict[str, Any]) -> LegacyGraphNode | None:
    identity = _entity_identity(row.get("identity"), row.get("labels"))
    if identity is None:
        return None
    entity_type, entity_key, source_uuid = identity
    project_key = row.get("project_key")
    if project_key is not None:
        if not isinstance(project_key, str):
            return None
        project_key = canonicalize_legacy_project_key(project_key)
        if not project_key or len(project_key) > 50:
            return None
    if entity_type == "project":
        project_key = entity_key
    elif entity_type == "domain":
        project_key = None
    label = row.get("label")
    display_label = str(label)[:180] if label is not None else None
    return LegacyGraphNode(
        entity_type=entity_type,
        entity_key=entity_key,
        source_uuid=source_uuid,
        project_key=project_key,
        scope_kind="project" if project_key else "global",
        display_label=display_label,
    )


def _relation_from_row(row: dict[str, Any]) -> LegacyGraphRelation | None:
    relation_type = row.get("type")
    if relation_type not in CANONICAL_RELATION_TYPES:
        return None
    source = _entity_identity(row.get("source_identity"), row.get("source_labels"))
    target = _entity_identity(row.get("target_identity"), row.get("target_labels"))
    if source is None or target is None or source[:2] == target[:2]:
        return None
    source_type, source_key, _ = source
    target_type, target_key, _ = target
    if relation_type == "RELATED_TO" and (source_type, source_key) > (target_type, target_key):
        source_type, target_type = target_type, source_type
        source_key, target_key = target_key, source_key
    properties = sanitize_relation_properties(row)
    properties = {
        key: value
        for key, value in properties.items()
        if not isinstance(value, float) or math.isfinite(value)
    }
    return LegacyGraphRelation(
        source_type=source_type,
        source_key=source_key,
        target_type=target_type,
        target_key=target_key,
        relation_type=relation_type,
        properties=properties,
    )


class LegacyGraphSnapshotReader:
    """Read at most ``max + 1`` rows per Neo4j fact category."""

    _NODE_QUERY = """
        MATCH (n)
        WITH n, labels(n) AS node_labels,
             CASE WHEN 'Project' IN labels(n) THEN n.project_key
                  WHEN 'Domain' IN labels(n) THEN n.name
                  ELSE toString(n.id) END AS identity
        RETURN substring(toString(identity), 0, 129) AS identity,
               node_labels AS labels,
               substring(toString(coalesce(n.title, n.topic, n.name, n.project_key,
                                           n.label, identity)), 0, 180) AS label,
               substring(toString(n.project_key), 0, 51) AS project_key
        LIMIT $limit
    """
    _RELATION_QUERY = """
        MATCH (source)-[relation]->(target)
        WITH source, target, relation, labels(source) AS source_labels,
             labels(target) AS target_labels,
             CASE WHEN 'Project' IN labels(source) THEN source.project_key
                  WHEN 'Domain' IN labels(source) THEN source.name
                  ELSE toString(source.id) END AS source_identity,
             CASE WHEN 'Project' IN labels(target) THEN target.project_key
                  WHEN 'Domain' IN labels(target) THEN target.name
                  ELSE toString(target.id) END AS target_identity
        RETURN substring(toString(source_identity), 0, 129) AS source_identity,
               source_labels,
               substring(toString(target_identity), 0, 129) AS target_identity,
               target_labels,
               type(relation) AS type, relation.similarity AS similarity,
               relation.score AS score, relation.threshold AS threshold,
               substring(toString(relation.model), 0, 128) AS model,
               substring(toString(relation.model_version), 0, 128) AS model_version,
               substring(toString(relation.method), 0, 128) AS method
        LIMIT $limit
    """

    def __init__(self, driver: Any, *, timeout: float = 8.0) -> None:
        self._driver = driver
        self._timeout = timeout

    async def read(self, max_nodes: int, max_relations: int) -> LegacyGraphSnapshot:
        if max_nodes <= 0 or max_relations <= 0:
            raise ValueError("snapshot limits must be positive")
        try:
            async with asyncio.timeout(self._timeout):
                await self._driver.verify_connectivity()
                async with self._driver.session() as session:

                    @unit_of_work(timeout=self._timeout)
                    async def read_transaction(
                        transaction: Any,
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
                        node_result = await transaction.run(
                            self._NODE_QUERY,
                            {"limit": max_nodes + 1},
                        )
                        nodes = [dict(record) async for record in node_result]
                        relation_result = await transaction.run(
                            self._RELATION_QUERY,
                            {"limit": max_relations + 1},
                        )
                        relations = [dict(record) async for record in relation_result]
                        return nodes, relations

                    raw_nodes, raw_relations = await session.execute_read(read_transaction)
        except Exception as exc:
            logger.error("legacy_graph.snapshot_unavailable", error_type=type(exc).__name__)
            return LegacyGraphSnapshot(status="unavailable")

        node_values = [_node_from_row(row) for row in raw_nodes[:max_nodes]]
        relation_values = [_relation_from_row(row) for row in raw_relations[:max_relations]]
        return LegacyGraphSnapshot(
            nodes=tuple(node for node in node_values if node is not None),
            relations=tuple(relation for relation in relation_values if relation is not None),
            truncated_nodes=len(raw_nodes) > max_nodes,
            truncated_relations=len(raw_relations) > max_relations,
            skipped_nodes=sum(node is None for node in node_values),
            skipped_relations=sum(relation is None for relation in relation_values),
        )
