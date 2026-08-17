"""Read-only projection of Brain's PostgreSQL data and Neo4j topology.

The projection deliberately contains lightweight metadata only. PostgreSQL is
authoritative for entity labels and state; Neo4j contributes the relation graph
and exposes topology orphans instead of hiding drift.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

import sqlalchemy as sa
import structlog
from neo4j import Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    adrs,
    brain_sessions,
    consolidation_log,
    decisions,
    dream_promotions,
    dream_runs,
    feature_artifacts,
    features,
    gitlab_events,
    indexed_plans,
    learnings,
    project_contexts,
    roadmap_curation_proposals,
    runbooks,
    snippets,
    ticket_extraction_proposals,
    tickets,
)
from brain_v42.services.graph_service import _CANONICAL_REL_TYPES

logger = structlog.get_logger(__name__)

SCHEMA_VERSION = "brain-graph.v1"
DEFAULT_MAX_NODES = 20_000
DEFAULT_MAX_EDGES = 100_000
DEFAULT_CACHE_TTL_SECONDS = 15.0
_LABEL_LIMIT = 180


@dataclass(slots=True)
class PostgresGraphRows:
    """Slim rows read from the PostgreSQL source."""

    tables: dict[str, list[dict[str, Any]]]
    status: str = "ok"
    truncated: bool = False


@dataclass(slots=True)
class Neo4jGraphRows:
    """Identity-only nodes and bounded canonical relations from Neo4j."""

    status: str = "ok"
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    truncated_nodes: bool = False
    truncated_edges: bool = False


class PostgresGraphSource(Protocol):
    async def read(self, max_rows: int) -> PostgresGraphRows: ...


class Neo4jGraphSource(Protocol):
    async def read(self, max_nodes: int, max_edges: int) -> Neo4jGraphRows: ...


@dataclass(frozen=True, slots=True)
class _PostgresTableSpec:
    table: sa.Table
    columns: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.table.name

    def statement(self, limit: int) -> sa.Select[Any]:
        selected = [self.table.c[name] for name in self.columns]
        primary_key = list(self.table.primary_key.columns)
        order_columns = primary_key or [selected[0]]
        return sa.select(*selected).order_by(*order_columns).limit(limit)


# Column lists are an allowlist. Heavy bodies, embeddings, vectors, local paths,
# source code, proposal rationale and Dream error text never enter the snapshot.
_POSTGRES_TABLE_SPECS: tuple[_PostgresTableSpec, ...] = (
    _PostgresTableSpec(
        project_contexts,
        (
            "project_key",
            "name",
            "current_phase",
            "project_group",
            "blockers",
            "related_projects",
            "created_at",
            "updated_at",
        ),
    ),
    _PostgresTableSpec(
        decisions,
        (
            "id",
            "title",
            "project_key",
            "status",
            "freshness_status",
            "superseded_by",
            "merged_into",
            "access_count",
            "created_at",
            "updated_at",
        ),
    ),
    _PostgresTableSpec(
        learnings,
        (
            "id",
            "topic",
            "project_key",
            "confidence",
            "source_type",
            "freshness_status",
            "merged_into",
            "access_count",
            "created_at",
            "updated_at",
        ),
    ),
    _PostgresTableSpec(
        snippets,
        (
            "id",
            "title",
            "project_key",
            "language",
            "freshness_status",
            "merged_into",
            "use_count",
            "access_count",
            "created_at",
            "updated_at",
        ),
    ),
    _PostgresTableSpec(
        runbooks,
        (
            "id",
            "title",
            "project_key",
            "freshness_status",
            "merged_into",
            "execution_count",
            "last_execution_status",
            "access_count",
            "created_at",
            "updated_at",
        ),
    ),
    _PostgresTableSpec(
        adrs,
        (
            "id",
            "number",
            "title",
            "project_key",
            "status",
            "freshness_status",
            "superseded_by",
            "merged_into",
            "access_count",
            "created_at",
            "updated_at",
        ),
    ),
    _PostgresTableSpec(
        features,
        (
            "id",
            "project_key",
            "name",
            "status",
            "pinned",
            "merged_into",
            "created_at",
            "updated_at",
        ),
    ),
    _PostgresTableSpec(
        indexed_plans,
        (
            "id",
            "title",
            "plan_type",
            "project_key",
            "status",
            "freshness_status",
            "chunk_count",
            "word_count",
            "access_count",
            "created_at",
            "updated_at",
        ),
    ),
    _PostgresTableSpec(
        gitlab_events,
        (
            "id",
            "event_type",
            "project_key",
            "title",
            "ref",
            "feature_id",
            "processed_at",
        ),
    ),
    _PostgresTableSpec(
        dream_runs,
        (
            "id",
            "run_date",
            "phase",
            "model",
            "status",
            "duration_s",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "cost_usd",
            "api_calls",
            "tool_calls",
            "phase_dry_run",
            "created_at",
        ),
    ),
    _PostgresTableSpec(
        dream_promotions,
        (
            "id",
            "dream_run_id",
            "source_learning_id",
            "target_type",
            "target_adr_id",
            "target_runbook_id",
            "cosine_observed",
            "skipped_reason",
            "created_at",
        ),
    ),
    _PostgresTableSpec(
        tickets,
        (
            "id",
            "kind",
            "title",
            "from_project",
            "to_project",
            "status",
            "extraction_status",
            "resolved_at",
            "closed_at",
            "created_at",
            "updated_at",
        ),
    ),
    _PostgresTableSpec(
        brain_sessions,
        (
            "id",
            "project_key",
            "status",
            "captured_knowledge_ids",
            "started_at",
            "ended_at",
            "updated_at",
        ),
    ),
    _PostgresTableSpec(
        ticket_extraction_proposals,
        (
            "id",
            "ticket_id",
            "target_type",
            "target_project",
            "status",
            "applied_entity_id",
            "created_at",
            "applied_at",
        ),
    ),
    _PostgresTableSpec(
        roadmap_curation_proposals,
        (
            "id",
            "op",
            "feature_id",
            "payload",
            "status",
            "created_at",
            "applied_at",
        ),
    ),
    _PostgresTableSpec(
        feature_artifacts,
        (
            "feature_id",
            "artifact_type",
            "artifact_id",
            "similarity_score",
            "created_at",
        ),
    ),
    _PostgresTableSpec(
        consolidation_log,
        (
            "id",
            "source_id",
            "target_id",
            "entity_type",
            "similarity",
            "action",
            "created_at",
        ),
    ),
)


class PostgresGraphSnapshotReader:
    """Sequential, globally bounded PostgreSQL reader with explicit columns."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        timeout: float = 8.0,
    ) -> None:
        self._session_factory = session_factory
        self._timeout = timeout

    async def read(self, max_rows: int) -> PostgresGraphRows:
        budget = max(1, max_rows)
        output: dict[str, list[dict[str, Any]]] = {spec.name: [] for spec in _POSTGRES_TABLE_SPECS}
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session_factory() as session:
                    for spec in _POSTGRES_TABLE_SPECS:
                        if budget <= 0:
                            return PostgresGraphRows(output, truncated=True)
                        result = await session.execute(spec.statement(budget + 1))
                        fetched = [dict(row) for row in result.mappings().all()]
                        if len(fetched) > budget:
                            output[spec.name] = fetched[:budget]
                            return PostgresGraphRows(output, truncated=True)
                        output[spec.name] = fetched
                        budget -= len(fetched)
        except Exception as exc:
            logger.error(
                "brain_graph.postgres_unavailable",
                error_type=type(exc).__name__,
            )
            return PostgresGraphRows(
                {spec.name: [] for spec in _POSTGRES_TABLE_SPECS},
                status="unavailable",
            )
        return PostgresGraphRows(output)


class Neo4jGraphSnapshotReader:
    """Bounded Neo4j export that preserves multi-label identity and direction."""

    _NODE_QUERY = """
        MATCH (n)
        WITH n, labels(n) AS node_labels,
             CASE
               WHEN 'Project' IN labels(n) THEN n.project_key
               WHEN 'Domain' IN labels(n) THEN n.name
               ELSE toString(n.id)
             END AS identity
        WHERE identity IS NOT NULL
        RETURN identity, node_labels AS labels,
               coalesce(n.title, n.topic, n.name, n.project_key, n.label, identity) AS label,
               n.project_key AS project_key
        ORDER BY identity
        LIMIT $limit
    """
    _EDGE_QUERY = """
        MATCH (source)-[relation]->(target)
        WHERE type(relation) IN $relation_types
        WITH source, target, relation,
             labels(source) AS source_labels,
             labels(target) AS target_labels,
             CASE
               WHEN 'Project' IN labels(source) THEN source.project_key
               WHEN 'Domain' IN labels(source) THEN source.name
               ELSE toString(source.id)
             END AS source_identity,
             CASE
               WHEN 'Project' IN labels(target) THEN target.project_key
               WHEN 'Domain' IN labels(target) THEN target.name
               ELSE toString(target.id)
             END AS target_identity
        WHERE source_identity IS NOT NULL AND target_identity IS NOT NULL
        RETURN source_identity, source_labels, target_identity, target_labels,
               type(relation) AS type,
               1.0 AS weight
        ORDER BY source_identity, target_identity, type
        LIMIT $limit
    """

    def __init__(self, driver: Any, *, timeout: float = 5.0) -> None:
        self._driver = driver
        self._timeout = timeout

    async def read(self, max_nodes: int, max_edges: int) -> Neo4jGraphRows:
        node_limit = max(1, max_nodes)
        edge_limit = max(1, max_edges)
        try:
            async with asyncio.timeout(self._timeout):
                await self._driver.verify_connectivity()
                async with self._driver.session() as session:
                    node_result = await session.run(
                        Query(self._NODE_QUERY, timeout=self._timeout),
                        {"limit": node_limit + 1},
                    )
                    node_rows = [dict(record) async for record in node_result]
                    edge_result = await session.run(
                        Query(self._EDGE_QUERY, timeout=self._timeout),
                        {
                            "limit": edge_limit + 1,
                            "relation_types": sorted(_CANONICAL_REL_TYPES),
                        },
                    )
                    edge_rows = [dict(record) async for record in edge_result]
        except Exception as exc:
            logger.error(
                "brain_graph.neo4j_unavailable",
                error_type=type(exc).__name__,
            )
            return Neo4jGraphRows(status="unavailable")
        return Neo4jGraphRows(
            nodes=node_rows[:node_limit],
            edges=edge_rows[:edge_limit],
            truncated_nodes=len(node_rows) > node_limit,
            truncated_edges=len(edge_rows) > edge_limit,
        )


_GROUP_BY_KIND = {
    "project": "projects",
    "decision": "knowledge",
    "learning": "knowledge",
    "snippet": "knowledge",
    "runbook": "knowledge",
    "adr": "knowledge",
    "feature": "roadmap",
    "plan": "roadmap",
    "gitlab-event": "delivery",
    "dream-night": "dream",
    "dream-phase": "dream",
    "dream-promotion": "dream",
    "ticket": "coordination",
    "session": "coordination",
    "ticket-proposal": "coordination",
    "roadmap-proposal": "coordination",
    "domain": "topology",
}

_NEO_KIND_PRIORITY: tuple[tuple[str, str], ...] = (
    ("Project", "project"),
    ("Domain", "domain"),
    ("Decision", "decision"),
    ("Learning", "learning"),
    ("Snippet", "snippet"),
    ("Runbook", "runbook"),
    ("ADR", "adr"),
    ("Feature", "feature"),
    ("Plan", "plan"),
)

_ARTIFACT_KIND = {
    "decision": "decision",
    "learning": "learning",
    "snippet": "snippet",
    "runbook": "runbook",
    "adr": "adr",
    "plan": "plan",
    "gitlab_event": "gitlab-event",
}


def _safe_text(value: Any, fallback: str) -> str:
    collapsed = " ".join(str(value or fallback).split())
    return collapsed[:_LABEL_LIMIT]


def _native(value: Any) -> str:
    return str(value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _compact(mapping: Mapping[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, (datetime, date)):
            compact[key] = _iso(value)
        elif isinstance(value, tuple):
            compact[key] = list(value)
        else:
            compact[key] = value
    return compact


def _neo_kind(labels: Sequence[str]) -> str:
    label_set = set(labels)
    for label, kind in _NEO_KIND_PRIORITY:
        if label in label_set:
            return kind
    if not labels:
        return "unknown"
    return str(labels[0]).replace("_", "-").lower()


def _typed_id(kind: str, native_id: Any) -> str:
    return f"{kind}:{_native(native_id)}"


class _GraphBuilder:
    def __init__(self, max_nodes: int, max_edges: int) -> None:
        self.max_nodes = max_nodes
        self.max_edges = max_edges
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.truncated_nodes = False
        self.truncated_edges = False
        self.dangling_edges = 0
        self.ambiguous_references = 0
        self.unresolved_references = 0
        self.neo4j_orphans = 0
        self.polymorphic_ids: dict[str, list[str]] = defaultdict(list)

    def add_node(
        self,
        *,
        node_id: str,
        native_id: Any,
        kind: str,
        label: Any,
        origin: str,
        project_key: str | None = None,
        status: Any = None,
        freshness: Any = None,
        created_at: Any = None,
        updated_at: Any = None,
        attributes: Mapping[str, Any] | None = None,
        orphaned: bool = False,
    ) -> bool:
        existing = self.nodes.get(node_id)
        if existing is not None:
            existing["origins"].add(origin)
            if origin == "postgres":
                existing["label"] = _safe_text(label, node_id)
                existing["project_key"] = project_key
                existing["status"] = status
                existing["freshness"] = freshness
                existing["created_at"] = _iso(created_at)
                existing["updated_at"] = _iso(updated_at)
                existing["attributes"].update(_compact(attributes or {}))
                existing.pop("orphaned", None)
            return True
        if len(self.nodes) >= self.max_nodes:
            self.truncated_nodes = True
            return False
        node: dict[str, Any] = {
            "id": node_id,
            "native_id": _native(native_id),
            "kind": kind,
            "group": _GROUP_BY_KIND.get(kind, "topology"),
            "label": _safe_text(label, node_id),
            "project_key": project_key,
            "status": status,
            "freshness": freshness,
            "created_at": _iso(created_at),
            "updated_at": _iso(updated_at),
            "origins": {origin},
            "attributes": _compact(attributes or {}),
        }
        if orphaned:
            node["orphaned"] = True
        self.nodes[node_id] = node
        return True

    def ensure_project(self, project_key: Any, *, origin: str = "postgres") -> str | None:
        if project_key is None or not str(project_key).strip():
            return None
        key = str(project_key)
        node_id = _typed_id("project", key)
        if node_id not in self.nodes:
            added = self.add_node(
                node_id=node_id,
                native_id=key,
                kind="project",
                label=key,
                origin=origin,
                project_key=key,
                attributes={"missing_context": True},
            )
            if not added:
                return None
        return node_id

    def register_polymorphic(self, native_id: Any, node_id: str) -> None:
        key = _native(native_id)
        if node_id not in self.polymorphic_ids[key]:
            self.polymorphic_ids[key].append(node_id)

    def resolve_polymorphic(self, native_id: Any) -> str | None:
        matches = self.polymorphic_ids.get(_native(native_id), [])
        if len(matches) == 1:
            return matches[0]
        if matches:
            self.ambiguous_references += 1
        else:
            self.unresolved_references += 1
        return None

    def add_edge(
        self,
        source: str | None,
        target: str | None,
        edge_type: str,
        *,
        origin: str,
        directed: bool = True,
        weight: Any = 1.0,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        if source is None or target is None or source not in self.nodes or target not in self.nodes:
            self.dangling_edges += 1
            return
        if not directed and source > target:
            source, target = target, source
        key = (source, target, edge_type)
        existing = self.edges.get(key)
        if existing is not None:
            existing["origins"].add(origin)
            try:
                existing["weight"] = max(float(existing["weight"]), float(weight))
            except (TypeError, ValueError):
                pass
            existing["attributes"].update(_compact(attributes or {}))
            return
        if len(self.edges) >= self.max_edges:
            self.truncated_edges = True
            return
        digest = hashlib.sha1(
            f"{source}\0{target}\0{edge_type}".encode(), usedforsecurity=False
        ).hexdigest()[:20]
        try:
            numeric_weight = float(weight)
        except (TypeError, ValueError):
            numeric_weight = 1.0
        self.edges[key] = {
            "id": f"edge:{digest}",
            "source": source,
            "target": target,
            "type": edge_type,
            "directed": directed,
            "weight": numeric_weight,
            "origins": {origin},
            "attributes": _compact(attributes or {}),
        }

    def output_nodes(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for node_id in sorted(self.nodes):
            node = dict(self.nodes[node_id])
            node["origins"] = sorted(node["origins"])
            result.append(node)
        return result

    def output_edges(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for key in sorted(self.edges):
            edge = dict(self.edges[key])
            edge["origins"] = sorted(edge["origins"])
            result.append(edge)
        return result


class BrainGraphProjectionService:
    """Merge Brain entities and relations into a stable read-only v1 contract."""

    def __init__(
        self,
        *,
        postgres_source: PostgresGraphSource,
        neo4j_source: Neo4jGraphSource | None,
        max_nodes: int = DEFAULT_MAX_NODES,
        max_edges: int = DEFAULT_MAX_EDGES,
        cache_ttl_s: float = DEFAULT_CACHE_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._postgres_source = postgres_source
        self._neo4j_source = neo4j_source
        self._max_nodes = max(1, max_nodes)
        self._max_edges = max(1, max_edges)
        self._cache_ttl_s = max(0.0, cache_ttl_s)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._cache: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def snapshot(self) -> dict[str, Any]:
        now = self._monotonic()
        if self._cache is not None and now - self._cached_at < self._cache_ttl_s:
            return self._cache
        async with self._lock:
            now = self._monotonic()
            if self._cache is not None and now - self._cached_at < self._cache_ttl_s:
                return self._cache
            payload = await self._build_snapshot()
            self._cache = payload
            self._cached_at = self._monotonic()
            return payload

    async def _build_snapshot(self) -> dict[str, Any]:
        try:
            pg_rows = await self._postgres_source.read(self._max_nodes + self._max_edges + 1)
        except Exception as exc:
            logger.error(
                "brain_graph.postgres_projection_failed",
                error_type=type(exc).__name__,
            )
            pg_rows = PostgresGraphRows(tables={}, status="unavailable")

        if self._neo4j_source is None:
            neo_rows = Neo4jGraphRows(status="disabled")
        else:
            try:
                neo_rows = await self._neo4j_source.read(self._max_nodes, self._max_edges)
            except Exception as exc:
                logger.error(
                    "brain_graph.neo4j_projection_failed",
                    error_type=type(exc).__name__,
                )
                neo_rows = Neo4jGraphRows(status="unavailable")

        builder = _GraphBuilder(self._max_nodes, self._max_edges)
        self._add_postgres_nodes(builder, pg_rows.tables)
        self._add_neo4j_nodes(builder, neo_rows.nodes)
        self._add_postgres_edges(builder, pg_rows.tables)
        self._add_neo4j_edges(builder, neo_rows.edges)

        nodes = builder.output_nodes()
        edges = builder.output_edges()
        node_counts = Counter(node["kind"] for node in nodes)
        edge_counts = Counter(edge["type"] for edge in edges)
        all_sources_ok = pg_rows.status == "ok" and neo_rows.status == "ok"
        if all_sources_ok:
            status = "ok"
        elif nodes:
            status = "degraded"
        else:
            status = "unavailable"
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(self._clock()),
            "mode": "full",
            "status": status,
            "sources": {
                "postgres": {
                    "status": pg_rows.status,
                    "records": sum(len(rows) for rows in pg_rows.tables.values()),
                },
                "neo4j": {
                    "status": neo_rows.status,
                    "nodes": len(neo_rows.nodes),
                    "edges": len(neo_rows.edges),
                },
            },
            "limits": {"max_nodes": self._max_nodes, "max_edges": self._max_edges},
            "truncated": {
                "nodes": builder.truncated_nodes or pg_rows.truncated or neo_rows.truncated_nodes,
                "edges": builder.truncated_edges or pg_rows.truncated or neo_rows.truncated_edges,
            },
            "integrity": {
                "dangling_edges": builder.dangling_edges,
                "ambiguous_references": builder.ambiguous_references,
                "unresolved_references": builder.unresolved_references,
                "neo4j_orphans": builder.neo4j_orphans,
            },
            "counts": {
                "nodes": {"total": len(nodes), "by_kind": dict(sorted(node_counts.items()))},
                "edges": {"total": len(edges), "by_type": dict(sorted(edge_counts.items()))},
            },
            "nodes": nodes,
            "edges": edges,
        }

    @staticmethod
    def _rows(tables: Mapping[str, list[dict[str, Any]]], name: str) -> list[dict[str, Any]]:
        return tables.get(name, [])

    def _add_postgres_nodes(
        self,
        builder: _GraphBuilder,
        tables: Mapping[str, list[dict[str, Any]]],
    ) -> None:
        for row in self._rows(tables, "project_contexts"):
            key = row.get("project_key")
            builder.add_node(
                node_id=_typed_id("project", key),
                native_id=key,
                kind="project",
                label=row.get("name"),
                origin="postgres",
                project_key=str(key) if key is not None else None,
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
                attributes={
                    "current_phase": row.get("current_phase"),
                    "project_group": row.get("project_group"),
                    "blocker_count": len(row.get("blockers") or []),
                },
            )

        referenced_projects: set[str] = set()
        for row in self._rows(tables, "project_contexts"):
            referenced_projects.update(str(key) for key in row.get("related_projects") or [] if key)
        for table_name in (
            "decisions",
            "learnings",
            "snippets",
            "runbooks",
            "adrs",
            "features",
            "indexed_plans",
            "gitlab_events",
            "brain_sessions",
        ):
            referenced_projects.update(
                str(row["project_key"])
                for row in self._rows(tables, table_name)
                if row.get("project_key")
            )
        for row in self._rows(tables, "tickets"):
            referenced_projects.update(
                str(value) for value in (row.get("from_project"), row.get("to_project")) if value
            )
        referenced_projects.update(
            str(row["target_project"])
            for row in self._rows(tables, "ticket_extraction_proposals")
            if row.get("target_project")
        )
        for project_key in sorted(referenced_projects):
            builder.ensure_project(project_key)

        knowledge_specs = (
            ("decisions", "decision", "title", ("status", "access_count")),
            (
                "learnings",
                "learning",
                "topic",
                ("confidence", "source_type", "access_count"),
            ),
            ("snippets", "snippet", "title", ("language", "use_count", "access_count")),
            (
                "runbooks",
                "runbook",
                "title",
                ("execution_count", "last_execution_status", "access_count"),
            ),
        )
        for table_name, kind, label_key, attribute_keys in knowledge_specs:
            for row in self._rows(tables, table_name):
                native_id = row.get("id")
                node_id = _typed_id(kind, native_id)
                if builder.add_node(
                    node_id=node_id,
                    native_id=native_id,
                    kind=kind,
                    label=row.get(label_key),
                    origin="postgres",
                    project_key=row.get("project_key"),
                    status=row.get("status"),
                    freshness=row.get("freshness_status"),
                    created_at=row.get("created_at"),
                    updated_at=row.get("updated_at"),
                    attributes={key: row.get(key) for key in attribute_keys},
                ):
                    builder.register_polymorphic(native_id, node_id)

        for row in self._rows(tables, "adrs"):
            native_id = row.get("id")
            node_id = _typed_id("adr", native_id)
            if builder.add_node(
                node_id=node_id,
                native_id=native_id,
                kind="adr",
                label=f"ADR {row.get('number')} · {row.get('title')}",
                origin="postgres",
                project_key=row.get("project_key"),
                status=row.get("status"),
                freshness=row.get("freshness_status"),
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
                attributes={"number": row.get("number"), "access_count": row.get("access_count")},
            ):
                builder.register_polymorphic(native_id, node_id)

        for row in self._rows(tables, "features"):
            builder.add_node(
                node_id=_typed_id("feature", row.get("id")),
                native_id=row.get("id"),
                kind="feature",
                label=row.get("name"),
                origin="postgres",
                project_key=row.get("project_key"),
                status=row.get("status"),
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
                attributes={"pinned": row.get("pinned")},
            )

        for row in self._rows(tables, "indexed_plans"):
            native_id = row.get("id")
            node_id = _typed_id("plan", native_id)
            if builder.add_node(
                node_id=node_id,
                native_id=native_id,
                kind="plan",
                label=row.get("title"),
                origin="postgres",
                project_key=row.get("project_key"),
                status=row.get("status"),
                freshness=row.get("freshness_status"),
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
                attributes={
                    "plan_type": row.get("plan_type"),
                    "chunk_count": row.get("chunk_count"),
                    "word_count": row.get("word_count"),
                    "access_count": row.get("access_count"),
                },
            ):
                builder.register_polymorphic(native_id, node_id)

        for row in self._rows(tables, "gitlab_events"):
            label = row.get("title") or row.get("ref") or row.get("event_type")
            builder.add_node(
                node_id=_typed_id("gitlab-event", row.get("id")),
                native_id=row.get("id"),
                kind="gitlab-event",
                label=label,
                origin="postgres",
                project_key=row.get("project_key"),
                created_at=row.get("processed_at"),
                updated_at=row.get("processed_at"),
                attributes={"event_type": row.get("event_type"), "ref": row.get("ref")},
            )

        nights: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self._rows(tables, "dream_runs"):
            day = _iso(row.get("run_date")) or "unknown"
            nights[day].append(row)
            builder.add_node(
                node_id=_typed_id("dream-phase", row.get("id")),
                native_id=row.get("id"),
                kind="dream-phase",
                label=f"{str(row.get('phase') or 'phase').upper()} · {row.get('status') or 'unknown'}",
                origin="postgres",
                status=row.get("status"),
                created_at=row.get("created_at"),
                updated_at=row.get("created_at"),
                attributes={
                    "run_date": day,
                    "phase": row.get("phase"),
                    "model": row.get("model"),
                    "duration_s": row.get("duration_s"),
                    "input_tokens": row.get("input_tokens"),
                    "output_tokens": row.get("output_tokens"),
                    "cache_read_tokens": row.get("cache_read_tokens"),
                    "cache_creation_tokens": row.get("cache_creation_tokens"),
                    "cost_usd": row.get("cost_usd"),
                    "api_calls": row.get("api_calls"),
                    "tool_calls": row.get("tool_calls"),
                    "dry_run": row.get("phase_dry_run"),
                },
            )
        for day, phases in nights.items():
            timestamps = [
                value for row in phases if isinstance((value := row.get("created_at")), datetime)
            ]
            statuses = {str(row.get("status") or "unknown") for row in phases}
            if statuses & {"failed", "error"}:
                night_status = "failed"
            elif statuses & {"running", "started"}:
                night_status = "running"
            elif statuses == {"success"}:
                night_status = "success"
            else:
                night_status = "mixed"
            builder.add_node(
                node_id=_typed_id("dream-night", day),
                native_id=day,
                kind="dream-night",
                label=f"Dream {day}",
                origin="postgres",
                status=night_status,
                created_at=min(timestamps, default=None),
                updated_at=max(timestamps, default=None),
                attributes={
                    "phase_count": len(phases),
                    "duration_s": sum(float(row.get("duration_s") or 0) for row in phases),
                    "cost_usd": sum(float(row.get("cost_usd") or 0) for row in phases),
                },
            )

        for row in self._rows(tables, "dream_promotions"):
            outcome = "skipped" if row.get("skipped_reason") else "materialized"
            builder.add_node(
                node_id=_typed_id("dream-promotion", row.get("id")),
                native_id=row.get("id"),
                kind="dream-promotion",
                label=f"{row.get('target_type') or 'promotion'} · {outcome}",
                origin="postgres",
                status=outcome,
                created_at=row.get("created_at"),
                updated_at=row.get("created_at"),
                attributes={
                    "target_type": row.get("target_type"),
                    "cosine_observed": row.get("cosine_observed"),
                    "skipped": bool(row.get("skipped_reason")),
                },
            )

        for row in self._rows(tables, "tickets"):
            builder.add_node(
                node_id=_typed_id("ticket", row.get("id")),
                native_id=row.get("id"),
                kind="ticket",
                label=row.get("title"),
                origin="postgres",
                status=row.get("status"),
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
                attributes={
                    "kind": row.get("kind"),
                    "extraction_status": row.get("extraction_status"),
                    "from_project": row.get("from_project"),
                    "to_project": row.get("to_project"),
                },
            )

        for row in self._rows(tables, "brain_sessions"):
            builder.add_node(
                node_id=_typed_id("session", row.get("id")),
                native_id=row.get("id"),
                kind="session",
                label=f"{row.get('project_key') or 'unknown'} · {row.get('status') or 'unknown'}",
                origin="postgres",
                project_key=row.get("project_key"),
                status=row.get("status"),
                created_at=row.get("started_at"),
                updated_at=row.get("updated_at"),
                attributes={
                    "captured_count": len(row.get("captured_knowledge_ids") or []),
                    "ended_at": row.get("ended_at"),
                },
            )

        for row in self._rows(tables, "ticket_extraction_proposals"):
            builder.add_node(
                node_id=_typed_id("ticket-proposal", row.get("id")),
                native_id=row.get("id"),
                kind="ticket-proposal",
                label=f"{row.get('target_type') or 'ticket'} proposal #{row.get('id')}",
                origin="postgres",
                project_key=row.get("target_project"),
                status=row.get("status"),
                created_at=row.get("created_at"),
                updated_at=row.get("applied_at") or row.get("created_at"),
                attributes={"target_type": row.get("target_type")},
            )

        for row in self._rows(tables, "roadmap_curation_proposals"):
            builder.add_node(
                node_id=_typed_id("roadmap-proposal", row.get("id")),
                native_id=row.get("id"),
                kind="roadmap-proposal",
                label=f"{row.get('op') or 'roadmap'} proposal #{row.get('id')}",
                origin="postgres",
                status=row.get("status"),
                created_at=row.get("created_at"),
                updated_at=row.get("applied_at") or row.get("created_at"),
                attributes={"op": row.get("op")},
            )

    def _add_neo4j_nodes(self, builder: _GraphBuilder, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            labels = [str(label) for label in row.get("labels") or []]
            kind = _neo_kind(labels)
            identity = row.get("identity")
            node_id = _typed_id(kind, identity)
            existed = node_id in builder.nodes
            if (
                builder.add_node(
                    node_id=node_id,
                    native_id=identity,
                    kind=kind,
                    label=row.get("label") or identity,
                    origin="neo4j",
                    project_key=row.get("project_key"),
                    attributes={"labels": labels},
                    orphaned=not existed and kind not in {"domain", "project"},
                )
                and not existed
                and kind not in {"domain", "project"}
            ):
                builder.neo4j_orphans += 1

    def _add_postgres_edges(
        self,
        builder: _GraphBuilder,
        tables: Mapping[str, list[dict[str, Any]]],
    ) -> None:
        for row in self._rows(tables, "project_contexts"):
            source = _typed_id("project", row.get("project_key"))
            for related in row.get("related_projects") or []:
                builder.add_edge(
                    source,
                    builder.ensure_project(related),
                    "RELATED_PROJECT",
                    origin="postgres",
                    directed=False,
                )

        for table_name, kind in (
            ("decisions", "decision"),
            ("learnings", "learning"),
            ("snippets", "snippet"),
            ("runbooks", "runbook"),
            ("adrs", "adr"),
            ("features", "feature"),
            ("indexed_plans", "plan"),
            ("gitlab_events", "gitlab-event"),
            ("brain_sessions", "session"),
        ):
            for row in self._rows(tables, table_name):
                project = builder.ensure_project(row.get("project_key"))
                if project is not None:
                    builder.add_edge(
                        _typed_id(kind, row.get("id")),
                        project,
                        "BELONGS_TO",
                        origin="postgres",
                    )

        for row in self._rows(tables, "decisions"):
            if row.get("superseded_by"):
                builder.add_edge(
                    _typed_id("decision", row["superseded_by"]),
                    _typed_id("decision", row.get("id")),
                    "SUPERSEDES",
                    origin="postgres",
                )

        adr_by_number = {
            (row.get("project_key"), row.get("number")): _typed_id("adr", row.get("id"))
            for row in self._rows(tables, "adrs")
        }
        for row in self._rows(tables, "adrs"):
            if row.get("superseded_by") is not None:
                adr_target = adr_by_number.get((row.get("project_key"), row.get("superseded_by")))
                if adr_target is None:
                    builder.unresolved_references += 1
                else:
                    builder.add_edge(
                        adr_target,
                        _typed_id("adr", row.get("id")),
                        "SUPERSEDES",
                        origin="postgres",
                    )

        for table_name, kind in (
            ("decisions", "decision"),
            ("learnings", "learning"),
            ("snippets", "snippet"),
            ("runbooks", "runbook"),
            ("adrs", "adr"),
            ("features", "feature"),
        ):
            for row in self._rows(tables, table_name):
                if row.get("merged_into"):
                    builder.add_edge(
                        _typed_id(kind, row.get("id")),
                        _typed_id(kind, row["merged_into"]),
                        "MERGED_INTO",
                        origin="postgres",
                    )

        for row in self._rows(tables, "feature_artifacts"):
            artifact_kind = _ARTIFACT_KIND.get(str(row.get("artifact_type")))
            if artifact_kind is None:
                builder.unresolved_references += 1
                continue
            builder.add_edge(
                _typed_id("feature", row.get("feature_id")),
                _typed_id(artifact_kind, row.get("artifact_id")),
                "HAS_ARTIFACT",
                origin="postgres",
                weight=(
                    row["similarity_score"] if row.get("similarity_score") is not None else 1.0
                ),
                attributes={"similarity_score": row.get("similarity_score")},
            )

        for row in self._rows(tables, "gitlab_events"):
            if row.get("feature_id"):
                builder.add_edge(
                    _typed_id("feature", row["feature_id"]),
                    _typed_id("gitlab-event", row.get("id")),
                    "TRACKS_EVENT",
                    origin="postgres",
                )

        run_dates = {
            _native(row.get("id")): _iso(row.get("run_date")) or "unknown"
            for row in self._rows(tables, "dream_runs")
        }
        for row in self._rows(tables, "dream_runs"):
            builder.add_edge(
                _typed_id("dream-phase", row.get("id")),
                _typed_id("dream-night", _iso(row.get("run_date")) or "unknown"),
                "BELONGS_TO_NIGHT",
                origin="postgres",
            )
        for row in self._rows(tables, "dream_promotions"):
            proposal = _typed_id("dream-promotion", row.get("id"))
            if row.get("dream_run_id") is not None:
                builder.add_edge(
                    proposal,
                    _typed_id("dream-phase", row["dream_run_id"]),
                    "BELONGS_TO_RUN",
                    origin="postgres",
                    attributes={"run_date": run_dates.get(_native(row["dream_run_id"]))},
                )
            if row.get("source_learning_id"):
                builder.add_edge(
                    proposal,
                    _typed_id("learning", row["source_learning_id"]),
                    "EVALUATES",
                    origin="postgres",
                )
            promotion_target: str | None = None
            if row.get("target_adr_id"):
                promotion_target = _typed_id("adr", row["target_adr_id"])
            elif row.get("target_runbook_id"):
                promotion_target = _typed_id("runbook", row["target_runbook_id"])
            if promotion_target is not None:
                builder.add_edge(
                    proposal,
                    promotion_target,
                    "MATERIALIZED_AS",
                    origin="postgres",
                    weight=(
                        row["cosine_observed"] if row.get("cosine_observed") is not None else 1.0
                    ),
                )

        for row in self._rows(tables, "tickets"):
            ticket = _typed_id("ticket", row.get("id"))
            builder.add_edge(
                ticket,
                builder.ensure_project(row.get("from_project")),
                "SENT_BY",
                origin="postgres",
            )
            builder.add_edge(
                ticket,
                builder.ensure_project(row.get("to_project")),
                "ASSIGNED_TO",
                origin="postgres",
            )

        for row in self._rows(tables, "brain_sessions"):
            session = _typed_id("session", row.get("id"))
            for captured_id in row.get("captured_knowledge_ids") or []:
                resolved = builder.resolve_polymorphic(captured_id)
                if resolved is not None:
                    builder.add_edge(session, resolved, "CAPTURED", origin="postgres")

        for row in self._rows(tables, "ticket_extraction_proposals"):
            proposal = _typed_id("ticket-proposal", row.get("id"))
            if row.get("ticket_id"):
                builder.add_edge(
                    proposal,
                    _typed_id("ticket", row["ticket_id"]),
                    "EXTRACTS_FROM",
                    origin="postgres",
                )
            builder.add_edge(
                proposal,
                builder.ensure_project(row.get("target_project")),
                "TARGETS_PROJECT",
                origin="postgres",
            )
            if row.get("applied_entity_id"):
                builder.add_edge(
                    proposal,
                    _typed_id(str(row.get("target_type")), row["applied_entity_id"]),
                    "MATERIALIZED_AS",
                    origin="postgres",
                )

        for row in self._rows(tables, "roadmap_curation_proposals"):
            proposal = _typed_id("roadmap-proposal", row.get("id"))
            builder.add_edge(
                proposal,
                _typed_id("feature", row.get("feature_id")),
                "TARGETS",
                origin="postgres",
            )
            payload = row.get("payload") or {}
            if row.get("op") == "merge" and isinstance(payload, Mapping) and payload.get("into"):
                builder.add_edge(
                    proposal,
                    _typed_id("feature", payload["into"]),
                    "MERGE_TARGET",
                    origin="postgres",
                )

        for row in self._rows(tables, "consolidation_log"):
            audit_kind = _ARTIFACT_KIND.get(str(row.get("entity_type")))
            if audit_kind is None:
                builder.unresolved_references += 1
                continue
            builder.add_edge(
                _typed_id(audit_kind, row.get("source_id")),
                _typed_id(audit_kind, row.get("target_id")),
                "MERGED_INTO",
                origin="postgres",
                weight=row["similarity"] if row.get("similarity") is not None else 1.0,
                attributes={
                    "audit_action": row.get("action"),
                    "similarity": row.get("similarity"),
                },
            )

    def _add_neo4j_edges(self, builder: _GraphBuilder, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            source_kind = _neo_kind([str(label) for label in row.get("source_labels") or []])
            target_kind = _neo_kind([str(label) for label in row.get("target_labels") or []])
            edge_type = str(row.get("type") or "RELATED_TO")
            builder.add_edge(
                _typed_id(source_kind, row.get("source_identity")),
                _typed_id(target_kind, row.get("target_identity")),
                edge_type,
                origin="neo4j",
                directed=edge_type != "RELATED_TO",
                weight=row["weight"] if row.get("weight") is not None else 1.0,
            )


__all__ = [
    "BrainGraphProjectionService",
    "Neo4jGraphRows",
    "Neo4jGraphSnapshotReader",
    "PostgresGraphRows",
    "PostgresGraphSnapshotReader",
]
