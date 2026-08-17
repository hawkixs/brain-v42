"""Neo4j async client — lightweight nodes + explicit relations.

Stores identity-only nodes (UUID, type, label) and relationships.
PG remains source of truth. All methods are fault-tolerant: Neo4j
errors are logged but never raised to callers.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import structlog
from neo4j import Query

if TYPE_CHECKING:
    from neo4j import AsyncDriver

# Fix 3: 'missing_node' distinguishes a MERGE no-op caused by absent anchor
# nodes from 'matched' (existing edge). The 435-edge drift found in the
# 2026-06-22 audit was caused by silent MATCH-MISS: MATCH (a {id:...})
# finds no rows, MERGE is never reached, and relationships_created==0 is
# indistinguishable from a real MATCH at the call site.
RelationWriteOutcome = Literal["created", "matched", "missing_node", "error"]

# Outcome for simple write operations (_run). Distinct from RelationWriteOutcome.
NodeWriteOutcome = Literal["ok", "error"]
AnchoredWriteOutcome = Literal["ok", "missing_node", "error"]

ALLOWED_DOMAINS: frozenset[str] = frozenset(
    {
        "infra",
        "ml",
        "backend",
        "memory",
        "tooling",
        "data",
        "ops",
        "frontend",
        "security",
    }
)

# Default path-traversal exclusions. BELONGS_TO_DOMAIN would otherwise make
# every pair of classified entities trivially 2-hop reachable via their
# shared Domain node, defeating the purpose of brain_graph_path.
_DEFAULT_PATH_EXCLUDES: tuple[str, ...] = ("BELONGS_TO_DOMAIN",)

# Canonical relation taxonomy (keep in sync with decision 9e374bca).
# Exhaustive whitelist used to build shortestPath rel-type filters when the
# caller wants "all types except <excludes>".
_CANONICAL_REL_TYPES: frozenset[str] = frozenset(
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

_PROJECT_REL_TYPES: frozenset[str] = frozenset({"CONTAINS", "DEPENDS_ON"})

logger = structlog.get_logger()


class GraphService:
    def __init__(self, driver: AsyncDriver, timeout: float = 5.0):
        self._driver = driver
        self._timeout = timeout

    # ── Nodes ──

    async def upsert_node(self, entity_type: str, id: UUID, props: dict) -> NodeWriteOutcome:
        """Create or update a lightweight node. Label = entity_type.

        Returns 'ok' on success, 'error' when the Neo4j write was swallowed.
        Callers (graph_helpers.graph_upsert_entity) use this to surface
        write-through drift via structured WARN instead of silent data loss.
        """
        query = f"MERGE (n:{entity_type} {{id: $id}}) SET n += $props"
        return await self._run(query, {"id": str(id), "props": props})

    async def delete_node(
        self,
        entity_type: str,
        id: UUID,
        *,
        project_key: str | None = None,
    ) -> NodeWriteOutcome:
        """Delete node and all its relations.

        A project-scoped delete only matches a knowledge node with exactly one
        owner, whose key equals ``project_key``. Admin calls keep the original
        unrestricted Cypher and parameter shape.

        Returns 'ok' on success, 'error' when the Neo4j delete was swallowed.
        """
        if project_key is None:
            query = f"MATCH (n:{entity_type} {{id: $id}}) DETACH DELETE n"
            params: dict[str, object] = {"id": str(id)}
        else:
            query = (
                f"MATCH (n:{entity_type} {{id: $id}}) "
                "WHERE size(labels(n)) > 0 "
                "AND all(label IN labels(n) WHERE label IN $knowledge_labels) "
                "AND size([(n)-[:BELONGS_TO]->(owner:Project) | owner]) = 1 "
                "AND [(n)-[:BELONGS_TO]->(owner:Project) | owner.project_key] = "
                "[$project_key] "
                "DETACH DELETE n"
            )
            params = {
                "id": str(id),
                "project_key": project_key,
                "knowledge_labels": ["Decision", "Learning", "Snippet", "Runbook", "ADR"],
            }
        return await self._run(query, params)

    async def upsert_project(
        self,
        project_key: str,
        project_id: UUID,
        name: str,
    ) -> NodeWriteOutcome:
        """Create or update a Project using its unique business key."""
        query = "MERGE (p:Project {project_key: $project_key}) SET p.id = $id, p.name = $name"
        return await self._run(
            query,
            {
                "project_key": project_key,
                "id": str(project_id),
                "name": name,
            },
        )

    async def delete_project(self, project_key: str) -> NodeWriteOutcome:
        """Delete a Project using its unique business key."""
        query = "MATCH (p:Project {project_key: $project_key}) DETACH DELETE p"
        return await self._run(query, {"project_key": project_key})

    async def create_project_relation(
        self,
        source_key: str,
        target_key: str,
        rel_type: str,
    ) -> RelationWriteOutcome:
        """Create a whitelisted hierarchy relation between Project keys."""
        if rel_type not in _PROJECT_REL_TYPES:
            raise ValueError(f"unsupported project relation: {rel_type}")
        query = (
            "MATCH (a:Project {project_key: $source_key}) "
            "MATCH (b:Project {project_key: $target_key}) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            "RETURN count(a) AS anchors"
        )
        return await self._run_counted(
            query,
            {"source_key": source_key, "target_key": target_key},
        )

    async def delete_project_relation(
        self,
        source_key: str,
        target_key: str,
        rel_type: str,
    ) -> NodeWriteOutcome:
        """Delete a whitelisted hierarchy relation between Project keys."""
        if rel_type not in _PROJECT_REL_TYPES:
            raise ValueError(f"unsupported project relation: {rel_type}")
        query = (
            "MATCH (a:Project {project_key: $source_key})"
            f"-[r:{rel_type}]->"
            "(b:Project {project_key: $target_key}) DELETE r"
        )
        return await self._run(
            query,
            {"source_key": source_key, "target_key": target_key},
        )

    # ── Relations ──

    async def create_relation(
        self,
        source_id: UUID,
        target_id: UUID,
        rel_type: str,
        props: dict | None = None,
        *,
        project_key: str | None = None,
        secret_safe: bool = False,
    ) -> RelationWriteOutcome:
        """Create or match a relation between two entity nodes.

        The optional project scope restricts both anchors to knowledge labels
        with exactly one matching Project owner. Admin calls keep the original
        unrestricted Cypher and parameters. ``secret_safe`` suppresses internal
        query/exception logs so a higher-level orchestrator can own one bounded
        degradation event; the historical logging path remains the default.

        Fix 3: the query explicitly returns ``count(a) AS anchors`` so that
        ``_run_counted`` can distinguish 'missing_node' (MATCH found no anchor
        nodes, anchors==0) from 'matched' (edge already existed, anchors>0 but
        relationships_created==0). The 2026-06-22 audit traced 435 missing
        MERGED_INTO edges to the silent MATCH-MISS that previously returned
        'matched'.

        Returns:
            'created'      — MERGE inserted a new edge
            'matched'      — equivalent edge already existed (MERGE ON MATCH)
            'missing_node' — anchor node(s) absent; MERGE was never reached
            'error'        — write failed (logged, never raised)
        """
        relation_pattern = f"-[r:{rel_type}]-" if rel_type == "RELATED_TO" else f"-[r:{rel_type}]->"
        if project_key is None:
            query = (
                "MATCH (a {id: $source_id}) "
                "MATCH (b {id: $target_id}) "
                f"MERGE (a){relation_pattern}(b)"
            )
            params: dict[str, object] = {
                "source_id": str(source_id),
                "target_id": str(target_id),
            }
        else:
            query = (
                "MATCH (a {id: $source_id}) "
                "MATCH (b {id: $target_id}) "
                "WHERE size(labels(a)) > 0 "
                "AND all(label IN labels(a) WHERE label IN $knowledge_labels) "
                "AND size(labels(b)) > 0 "
                "AND all(label IN labels(b) WHERE label IN $knowledge_labels) "
                "AND size([(a)-[:BELONGS_TO]->(owner:Project) | owner]) = 1 "
                "AND [(a)-[:BELONGS_TO]->(owner:Project) | owner.project_key] = [$project_key] "
                "AND size([(b)-[:BELONGS_TO]->(owner:Project) | owner]) = 1 "
                "AND [(b)-[:BELONGS_TO]->(owner:Project) | owner.project_key] = [$project_key] "
                f"MERGE (a){relation_pattern}(b)"
            )
            params = {
                "source_id": str(source_id),
                "target_id": str(target_id),
                "project_key": project_key,
                "knowledge_labels": ["Decision", "Learning", "Snippet", "Runbook", "ADR"],
            }
        if props:
            query += " SET r += $props"
        # Explicit RETURN exposes whether anchor nodes were found (Fix 3).
        # count(a) is 1 when both anchors matched (MATCH always returns the same
        # pair), 0 when the MATCH pattern found no rows.
        query += " RETURN count(a) AS anchors"
        if props:
            params["props"] = props
        if secret_safe:
            return await self._run_counted(query, params, secret_safe=True)
        return await self._run_counted(query, params)

    async def delete_relation(
        self, source_id: UUID, target_id: UUID, rel_type: str
    ) -> NodeWriteOutcome:
        """Delete a specific relation by type between two nodes.

        MAJOR 2 fix: previously discarded the _run outcome and returned None,
        making Neo4j failures 100% silent — inconsistent with upsert_node,
        delete_node, and link_to_project which all propagate NodeWriteOutcome.

        Returns 'ok' on success, 'error' when the Neo4j write was swallowed.
        """
        relation_pattern = f"-[r:{rel_type}]-" if rel_type == "RELATED_TO" else f"-[r:{rel_type}]->"
        query = f"MATCH (a {{id: $source_id}}){relation_pattern}(b {{id: $target_id}}) DELETE r"
        return await self._run(query, {"source_id": str(source_id), "target_id": str(target_id)})

    async def link_to_project(self, entity_id: UUID, project_key: str) -> AnchoredWriteOutcome:
        """Link an entity node to a Project node via BELONGS_TO.

        Returns 'ok' on success, 'error' when the Neo4j write was swallowed.
        """
        query = (
            "MATCH (e {id: $entity_id}) "
            "MATCH (p:Project {project_key: $project_key}) "
            "MERGE (e)-[r:BELONGS_TO]->(p) "
            "RETURN count(e) AS anchors"
        )
        outcome = await self._run_counted(
            query,
            {"entity_id": str(entity_id), "project_key": project_key},
        )
        if outcome in {"created", "matched"}:
            return "ok"
        if outcome == "missing_node":
            return "missing_node"
        return "error"

    async def unlink_from_project(self, entity_id: UUID, project_key: str) -> NodeWriteOutcome:
        """Delete BELONGS_TO using the Project node's stable business key."""
        query = (
            "MATCH (e {id: $entity_id})-[r:BELONGS_TO]->"
            "(p:Project {project_key: $project_key}) DELETE r"
        )
        return await self._run(
            query,
            {"entity_id": str(entity_id), "project_key": project_key},
        )

    # ── Traversals ──

    async def get_neighbors(
        self,
        id: UUID,
        rel_types: list[str] | None = None,
        depth: int = 1,
        *,
        project_key: str | None = None,
    ) -> list[dict]:
        """Get neighboring nodes up to given depth.

        rel_types are whitelist-filtered against _CANONICAL_REL_TYPES before
        being interpolated into the Cypher rel filter (these cannot be passed
        as bind parameters). This blocks Cypher injection via crafted rel_types,
        mirroring the guard in get_path. If rel_types is given but contains no
        canonical type, the result is empty (no query is issued).
        """
        if rel_types is not None:
            allowed = {r for r in rel_types if r in _CANONICAL_REL_TYPES}
            if not allowed:
                return []
            rel_filter = ":" + "|".join(sorted(allowed))
        else:
            rel_filter = ""
        if project_key is None:
            query = (
                f"MATCH (start {{id: $id}})-[r{rel_filter}*1..{depth}]-(neighbor) "
                "WHERE neighbor.id <> $id "
                "RETURN DISTINCT neighbor.id AS id, labels(neighbor)[0] AS type, "
                "type(r[0]) AS rel, "
                "coalesce(neighbor.title, neighbor.topic) AS label "
                "LIMIT 20"
            )
            return await self._run_read(query, {"id": str(id)})

        scoped_depth = max(1, min(3, depth))
        query = (
            f"MATCH path = (start {{id: $id}})-[r{rel_filter}*1..{scoped_depth}]-(neighbor) "
            "WHERE neighbor.id <> $id "
            "AND all(node IN nodes(path) WHERE "
            "size(labels(node)) > 0 "
            "AND all(label IN labels(node) WHERE label IN $knowledge_labels) "
            "AND size([(node)-[:BELONGS_TO]->(owner:Project) | owner]) = 1 "
            "AND [(node)-[:BELONGS_TO]->(owner:Project) | owner.project_key] = "
            "[$project_key]) "
            "RETURN DISTINCT neighbor.id AS id, labels(neighbor)[0] AS type, "
            "type(r[0]) AS rel, "
            "coalesce(neighbor.title, neighbor.topic) AS label "
            "LIMIT 20"
        )
        return await self._run_read(
            query,
            {
                "id": str(id),
                "project_key": project_key,
                "knowledge_labels": ["Decision", "Learning", "Snippet", "Runbook", "ADR"],
            },
        )

    async def get_supersession_chain(self, decision_id: UUID) -> list[str]:
        """Get full supersession chain (newest to oldest).
        SUPERSEDES points from new to old: (new)-[:SUPERSEDES]->(old).
        """
        query = """
            MATCH (start:Decision {id: $decision_id})
            OPTIONAL MATCH path_back = (newest:Decision)-[:SUPERSEDES*]->(start)
            WHERE NOT ()-[:SUPERSEDES]->(newest)
            WITH coalesce(newest, start) AS root
            MATCH chain = (root)-[:SUPERSEDES*0..]->(leaf)
            RETURN [n IN nodes(chain) | n.id] AS chain_ids
            ORDER BY length(chain) DESC LIMIT 1
        """
        rows = await self._run_read(query, {"decision_id": str(decision_id)})
        if rows:
            return list(rows[0]["chain_ids"])
        return [str(decision_id)]

    async def get_project_tree(self, project_key: str) -> list[str]:
        """Get all sub-project keys (recursive CONTAINS traversal)."""
        query = """
            MATCH (root:Project {project_key: $project_key})
            OPTIONAL MATCH (root)-[:CONTAINS*]->(sub:Project)
            RETURN collect(DISTINCT sub.project_key) AS sub_keys
        """
        rows = await self._run_read(query, {"project_key": project_key})
        if rows:
            return list(rows[0]["sub_keys"])
        return []

    async def get_related_ids(
        self, ids: list[UUID], *, project_key: str | None = None
    ) -> dict[str, list[dict]]:
        """Batch: get neighbors for multiple entity IDs.
        Returns {entity_id_str: [{id, type, rel, title}, ...]}.
        Max 5 neighbors per entity.
        """
        if not ids:
            return {}
        if project_key is None:
            query = """
            UNWIND $ids AS eid
            MATCH (e {id: eid})-[r]-(neighbor)
            WHERE neighbor.id <> eid
            WITH eid, neighbor, type(r) AS rel_type, labels(neighbor)[0] AS ntype
            RETURN eid,
                   collect({id: neighbor.id, type: ntype, rel: rel_type,
                            title: coalesce(neighbor.title, neighbor.topic)})[..5] AS neighbors
        """
            rows = await self._run_read(query, {"ids": [str(i) for i in ids]})
            return {row["eid"]: row["neighbors"] for row in rows}

        query = """
            UNWIND $ids AS eid
            MATCH path = (e {id: eid})-[r]-(neighbor)
            WHERE neighbor.id <> eid
              AND all(node IN nodes(path) WHERE
                  size(labels(node)) > 0
                  AND all(label IN labels(node) WHERE label IN $knowledge_labels)
                  AND size([(node)-[:BELONGS_TO]->(owner:Project) | owner]) = 1
                  AND [(node)-[:BELONGS_TO]->(owner:Project) | owner.project_key]
                      = [$project_key])
            WITH eid, neighbor, type(r) AS rel_type, labels(neighbor)[0] AS ntype
            RETURN eid,
                   collect({id: neighbor.id, type: ntype, rel: rel_type,
                            title: coalesce(neighbor.title, neighbor.topic)})[..5] AS neighbors
        """
        rows = await self._run_read(
            query,
            {
                "ids": [str(i) for i in ids],
                "project_key": project_key,
                "knowledge_labels": ["Decision", "Learning", "Snippet", "Runbook", "ADR"],
            },
        )
        return {row["eid"]: row["neighbors"] for row in rows}

    # ── Inventory queries ──

    async def count_nodes_by_label(self) -> dict[str, int]:
        """Return {label: count} for every label present in the graph.

        Uses the first label only — multi-labelled nodes are bucketed by
        their primary label, matching how this codebase upserts nodes.
        """
        query = (
            "MATCH (n) "
            "WHERE NOT n:BrainProjectionFence AND NOT n:BrainProjectionCursor "
            "RETURN labels(n)[0] AS label, count(n) AS count ORDER BY count DESC"
        )
        rows = await self._run_read(query, {})
        return {row["label"]: row["count"] for row in rows if row.get("label")}

    async def count_edges_by_type(self) -> dict[str, int]:
        """Return {rel_type: count} for every relationship type in the graph."""
        query = "MATCH ()-[r]->() RETURN type(r) AS type, count(r) AS count ORDER BY count DESC"
        rows = await self._run_read(query, {})
        return {row["type"]: row["count"] for row in rows if row.get("type")}

    async def healthcheck(self) -> bool:
        try:
            async with self._driver.session() as session:
                await session.run("RETURN 1")
            return True
        except Exception:
            return False

    # ── Dream Mode queries ──

    async def find_unlinked_nodes(
        self,
        entity_type: str | None = None,
        limit: int = 50,
        *,
        project_key: str | None = None,
    ) -> list[str]:
        """Return entity IDs that have a node but zero RELATED_TO edges."""
        if project_key is None:
            query = """
            MATCH (n)
            WHERE NOT (n)-[:RELATED_TO]-()
            AND ($type IS NULL OR $type IN labels(n))
            AND NOT n:BrainProjectionFence
            AND NOT n:BrainProjectionCursor
            RETURN n.id AS id
            LIMIT $limit
        """
            rows = await self._run_read(query, {"type": entity_type, "limit": limit})
            return [row["id"] for row in rows]

        query = """
            MATCH (n)
            WHERE NOT (n)-[:RELATED_TO]-()
              AND ($type IS NULL OR $type IN labels(n))
              AND NOT n:BrainProjectionFence
              AND NOT n:BrainProjectionCursor
              AND size(labels(n)) > 0
              AND all(label IN labels(n) WHERE label IN $knowledge_labels)
              AND size([(n)-[:BELONGS_TO]->(owner:Project) | owner]) = 1
              AND [(n)-[:BELONGS_TO]->(owner:Project) | owner.project_key]
                  = [$project_key]
            RETURN n.id AS id
            LIMIT $limit
        """
        rows = await self._run_read(
            query,
            {
                "type": entity_type,
                "limit": limit,
                "project_key": project_key,
                "knowledge_labels": ["Decision", "Learning", "Snippet", "Runbook", "ADR"],
            },
        )
        return [row["id"] for row in rows]

    async def get_all_related_edges(
        self, *, project_key: str | None = None
    ) -> list[tuple[str, str]]:
        """Return all (source_id, target_id) pairs for RELATED_TO edges.

        Deduplicates by requiring a.id < b.id so each edge appears once.
        """
        if project_key is None:
            query = """
            MATCH (a)-[:RELATED_TO]-(b)
            WHERE a.id < b.id
            RETURN DISTINCT a.id AS src, b.id AS tgt
        """
            rows = await self._run_read(query, {})
            return [(row["src"], row["tgt"]) for row in rows]

        query = """
            MATCH (a)-[:RELATED_TO]-(b)
            WHERE a.id < b.id
              AND all(node IN [a, b] WHERE
                  size(labels(node)) > 0
                  AND all(label IN labels(node) WHERE label IN $knowledge_labels)
                  AND size([(node)-[:BELONGS_TO]->(owner:Project) | owner]) = 1
                  AND [(node)-[:BELONGS_TO]->(owner:Project) | owner.project_key]
                      = [$project_key])
            RETURN DISTINCT a.id AS src, b.id AS tgt
        """
        rows = await self._run_read(
            query,
            {
                "project_key": project_key,
                "knowledge_labels": ["Decision", "Learning", "Snippet", "Runbook", "ADR"],
            },
        )
        return [(row["src"], row["tgt"]) for row in rows]

    async def get_path(
        self,
        source_id: UUID,
        target_id: UUID,
        max_depth: int = 3,
        rel_types: list[str] | None = None,
        exclude_rel_types: list[str] | None = None,
        *,
        project_key: str | None = None,
    ) -> list[dict]:
        """Return shortest path from source to target, up to max_depth hops.

        rel_types: Whitelist of relation types. None = all canonical types
            except the defaults in _DEFAULT_PATH_EXCLUDES.
        exclude_rel_types: Additional exclusions (additive to defaults).
            Ignored when rel_types is set.

        Neo4j shortestPath returns at most one path. Empty list if no path
        exists within max_depth.

        Returns: [{"id": uuid, "type": "Decision", "label": "...",
                   "rel_to_next": "RELATED_TO"}, ...]
        The last node has no "rel_to_next" key.
        """
        depth = max(1, min(6, max_depth))

        allowed: set[str]
        if rel_types is not None:
            allowed = {r for r in rel_types if r in _CANONICAL_REL_TYPES}
        else:
            excludes = set(_DEFAULT_PATH_EXCLUDES)
            if exclude_rel_types:
                excludes.update(exclude_rel_types)
            allowed = set(_CANONICAL_REL_TYPES - excludes)

        if not allowed:
            return []

        rel_filter = ":" + "|".join(sorted(allowed))

        if project_key is None:
            query = f"""
            MATCH p = shortestPath(
                (a {{id: $source_id}})-[{rel_filter}*1..{depth}]-(b {{id: $target_id}})
            )
            RETURN [n IN nodes(p) |
                    {{id: n.id, type: labels(n)[0],
                      label: coalesce(n.title, n.topic, n.name)}}] AS nodes,
                   [r IN relationships(p) | type(r)] AS rels
            LIMIT 1
        """
            rows = await self._run_read(
                query, {"source_id": str(source_id), "target_id": str(target_id)}
            )
        else:
            query = f"""
                MATCH p =
                    (a {{id: $source_id}})-[{rel_filter}*1..{depth}]-(b {{id: $target_id}})
                WHERE all(node IN nodes(p) WHERE
                    size(labels(node)) > 0
                    AND all(label IN labels(node) WHERE label IN $knowledge_labels)
                    AND size([(node)-[:BELONGS_TO]->(owner:Project) | owner]) = 1
                    AND [(node)-[:BELONGS_TO]->(owner:Project) | owner.project_key]
                        = [$project_key])
                RETURN [n IN nodes(p) |
                        {{id: n.id, type: labels(n)[0],
                          label: coalesce(n.title, n.topic, n.name)}}] AS nodes,
                       [r IN relationships(p) | type(r)] AS rels
                ORDER BY length(p)
                LIMIT 1
            """
            rows = await self._run_read(
                query,
                {
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "project_key": project_key,
                    "knowledge_labels": [
                        "Decision",
                        "Learning",
                        "Snippet",
                        "Runbook",
                        "ADR",
                    ],
                },
            )
        if not rows:
            return []
        nodes: list[dict[Any, Any]] = rows[0]["nodes"]
        rels = rows[0]["rels"]
        for i, rel in enumerate(rels):
            nodes[i]["rel_to_next"] = rel
        return nodes

    # ── Domain node bridging (Graphiti pattern) ──

    async def upsert_domain(self, name: str) -> Literal["ok", "invalid_domain", "error"]:
        """Create or match a Domain node.

        name must be ∈ ALLOWED_DOMAINS (lowercase). Invalid names are rejected
        with a warning log — do NOT silently pollute the graph with typos.

        Returns:
            'ok'           — Domain node upserted (or already existed)
            'invalid_domain' — name not in ALLOWED_DOMAINS; no write performed.
                               Callers (brain_assign_domain) must reserve this
                               value exclusively for ALLOWED_DOMAINS validation
                               failures — NOT for Neo4j infra errors.
            'error'        — _run failed (Neo4j down, timeout, etc.)

        Design rationale: the old bool return collapsed 'invalid_domain' and 'error'
        into the same False value. brain_assign_domain mapped both to 'invalid_domain',
        causing the LLM agent to conclude the domain name was wrong when Neo4j was
        actually down — preventing retries.
        """
        if name not in ALLOWED_DOMAINS:
            logger.warning("graph.invalid_domain", name_length=len(name))
            return "invalid_domain"
        query = "MERGE (d:Domain {name: $name}) SET d.updated_at = timestamp()"
        return await self._run(query, {"name": name})

    async def link_entity_to_domain(
        self,
        entity_id: UUID,
        domain_name: str,
        *,
        project_key: str | None = None,
    ) -> RelationWriteOutcome:
        """Create BELONGS_TO_DOMAIN edge from entity to Domain node.

        Assumes the Domain node was upserted by a prior call (or matches by
        name if already present). Returns the outcome tag via _run_counted.

        Fix 3: includes ``RETURN count(e) AS anchors`` so _run_counted can
        detect 'missing_node' when the entity or Domain node is absent.
        """
        if project_key is None:
            query = (
                "MATCH (e {id: $entity_id}) "
                "MATCH (d:Domain {name: $domain_name}) "
                "MERGE (e)-[r:BELONGS_TO_DOMAIN]->(d) "
                "RETURN count(e) AS anchors"
            )
            return await self._run_counted(
                query, {"entity_id": str(entity_id), "domain_name": domain_name}
            )

        query = (
            "MATCH (e {id: $entity_id}) "
            "WHERE size(labels(e)) > 0 "
            "AND all(label IN labels(e) WHERE label IN $knowledge_labels) "
            "AND size([(e)-[:BELONGS_TO]->(owner:Project) | owner]) = 1 "
            "AND [(e)-[:BELONGS_TO]->(owner:Project) | owner.project_key] = [$project_key] "
            "MATCH (d:Domain {name: $domain_name}) "
            "MERGE (e)-[r:BELONGS_TO_DOMAIN]->(d) "
            "RETURN count(e) AS anchors"
        )
        return await self._run_counted(
            query,
            {
                "entity_id": str(entity_id),
                "domain_name": domain_name,
                "project_key": project_key,
                "knowledge_labels": ["Decision", "Learning", "Snippet", "Runbook", "ADR"],
            },
        )

    async def unlink_from_domain(self, entity_id: UUID, domain_name: str) -> NodeWriteOutcome:
        """Delete BELONGS_TO_DOMAIN using the Domain node's stable name."""
        query = (
            "MATCH (e {id: $entity_id})-[r:BELONGS_TO_DOMAIN]->"
            "(d:Domain {name: $domain_name}) DELETE r"
        )
        return await self._run(
            query,
            {"entity_id": str(entity_id), "domain_name": domain_name},
        )

    async def find_orphans_for_classification(
        self, limit: int = 20, *, project_key: str | None = None
    ) -> list[dict]:
        """Return entities lacking RELATED_TO AND BELONGS_TO_DOMAIN edges.

        These are the cross-domain orphans the cosine-0.6 AutoLinker cannot
        bridge (learning 2ddc02a3). The caller (Dream agent via the CONNECT
        phase) fetches PG metadata, classifies locally against ALLOWED_DOMAINS,
        then calls brain_assign_domain per entity.

        Multi-label safe — uses ANY(l IN labels(n) ...) instead of labels(n)[0]
        so future multi-labelled nodes aren't silently dropped.
        """
        if project_key is None:
            query = """
        MATCH (n)
        WHERE NOT (n)-[:RELATED_TO]-()
          AND NOT (n)-[:BELONGS_TO_DOMAIN]->()
          AND ANY(l IN labels(n)
                  WHERE l IN ['Decision','Learning','Snippet','Runbook','ADR'])
        RETURN n.id AS id, labels(n) AS labels LIMIT $limit
    """
            return await self._run_read(query, {"limit": limit})

        query = """
            MATCH (n)
            WHERE NOT (n)-[:RELATED_TO]-()
              AND NOT (n)-[:BELONGS_TO_DOMAIN]->()
              AND size(labels(n)) > 0
              AND all(label IN labels(n) WHERE label IN $knowledge_labels)
              AND size([(n)-[:BELONGS_TO]->(owner:Project) | owner]) = 1
              AND [(n)-[:BELONGS_TO]->(owner:Project) | owner.project_key]
                  = [$project_key]
            RETURN n.id AS id, labels(n) AS labels LIMIT $limit
        """
        return await self._run_read(
            query,
            {
                "limit": limit,
                "project_key": project_key,
                "knowledge_labels": ["Decision", "Learning", "Snippet", "Runbook", "ADR"],
            },
        )

    # ── Cross-project (Spec C MVP β) ──

    async def fetch_active_domains(self, project_key: str, top_n: int = 2) -> list[str]:
        """Top-N domains of a project, ranked by classified-entity count."""
        query = (
            "MATCH (e)-[:BELONGS_TO]->(:Project {project_key: $project_key}) "
            "MATCH (e)-[:BELONGS_TO_DOMAIN]->(d:Domain) "
            "WITH d.name AS domain, count(e) AS n "
            "ORDER BY n DESC LIMIT $top_n "
            "RETURN domain"
        )
        rows = await self._run_read(query, {"project_key": project_key, "top_n": top_n})
        return [r["domain"] for r in rows]

    async def fetch_cross_project_entity_ids(
        self, domains: list[str], exclude_project_key: str, limit: int = 50
    ) -> list[dict]:
        """Candidate entities from OTHER projects in the given domains.

        Nodes carry no created_at — recency ordering happens later in PG.
        limit caps the candidate set handed to the PG brief query.
        """
        query = (
            "MATCH (e)-[:BELONGS_TO_DOMAIN]->(d:Domain) "
            "WHERE d.name IN $domains "
            "MATCH (e)-[:BELONGS_TO]->(p:Project) "
            "WHERE p.project_key <> $exclude "
            "RETURN e.id AS id, labels(e) AS labels, p.project_key AS project_key "
            "LIMIT $limit"
        )
        return await self._run_read(
            query, {"domains": domains, "exclude": exclude_project_key, "limit": limit}
        )

    async def fetch_decision_ids_in_domain(self, domain: str) -> list[str]:
        """All Decision node ids classified in a domain (resonance candidate pool)."""
        query = (
            "MATCH (e:Decision)-[:BELONGS_TO_DOMAIN]->(:Domain {name: $domain}) RETURN e.id AS id"
        )
        rows = await self._run_read(query, {"domain": domain})
        return [r["id"] for r in rows]

    # ── Internal ──

    async def _reconnect(self) -> bool:
        """Attempt to refresh the driver connection pool.

        Returns True if connectivity is restored.
        """
        try:
            await self._driver.verify_connectivity()
            logger.info("neo4j_reconnected")
            return True
        except Exception:
            logger.error("neo4j_reconnect_failed", exc_info=True)
            return False

    async def _reconnect_secret_safe(self) -> bool:
        """Attempt one bounded reconnect without emitting protected details."""
        try:
            async with asyncio.timeout(self._timeout):
                await self._driver.verify_connectivity()
            return True
        except Exception:
            return False

    async def _run(self, query: str, params: dict) -> NodeWriteOutcome:
        """Execute a write query with one reconnect retry. Never raises.

        Fix 1: wraps the query string in ``neo4j.Query(text, timeout=...)``
        instead of passing ``timeout`` as a kwarg to ``session.run``. The
        neo4j-driver ≥ 5 fuses unknown kwargs into the Cypher parameter dict
        rather than interpreting them as driver options, so the previous call
        ``session.run(query, params, timeout=self._timeout)`` silently passed
        ``timeout`` as a Cypher bind parameter and applied *no* network timeout.

        Fix 2: returns 'ok' | 'error' so callers (upsert_node, delete_node,
        link_to_project, upsert_domain) can propagate write-through status
        and surface drift via structured WARN instead of silent data loss.
        """
        neo4j_query = Query(query, timeout=self._timeout)
        for attempt in range(2):
            try:
                async with self._driver.session() as session:
                    result = await session.run(neo4j_query, params)
                    await result.consume()
                return "ok"
            except Exception:
                if attempt == 0 and await self._reconnect():
                    continue
                logger.error("neo4j_write_failed", query=query[:100], exc_info=True)
                return "error"
        return "error"  # pragma: no cover — loop always returns on attempt 1

    async def _run_counted(
        self,
        query: str,
        params: dict,
        *,
        secret_safe: bool = False,
    ) -> RelationWriteOutcome:
        """Run a write and return outcome based on Neo4j ResultSummary counters.

        Fix 1: wraps in ``neo4j.Query(text, timeout=...)`` (same as _run).

        Fix 3: distinguishes 'missing_node' from 'matched'. Previously both
        returned 'matched' when relationships_created==0, masking MATCH-MISS
        (anchor nodes absent) from genuine MERGE ON MATCH (edge already exists).
        The 2026-06-22 audit traced 435 missing MERGED_INTO edges to this bug.

        The callers (create_relation, link_entity_to_domain) now include
        ``RETURN count(<anchor>) AS anchors`` in their queries. _run_counted
        reads the ``anchors`` field from the first returned record to distinguish:

        - anchors == 0 → MATCH found no nodes → 'missing_node'
        - anchors > 0 and relationships_created == 0 → edge already existed → 'matched'
        - relationships_created > 0 → new edge inserted → 'created'

        Fallback: if the query has no RETURN clause (e.g. future callers), the
        loop falls through to the summary-only path (no 'missing_node' detection).
        ``secret_safe`` selects a bounded silent reconnect and suppresses the
        internal failure log, leaving observability to the caller.
        """
        neo4j_query = Query(query, timeout=self._timeout)
        for attempt in range(2):
            try:
                async with self._driver.session() as session:
                    result = await session.run(neo4j_query, params)
                    records = [dict(r) async for r in result]
                    summary = await result.consume()
                    if summary.counters.relationships_created > 0:
                        return "created"
                    # Inspect anchor count from RETURN clause (Fix 3).
                    if records and "anchors" in records[0]:
                        if records[0]["anchors"] == 0:
                            # MATCH found no anchor nodes — MERGE was never reached.
                            return "missing_node"
                    return "matched"
            except Exception:
                if attempt == 0:
                    reconnect = self._reconnect_secret_safe if secret_safe else self._reconnect
                    if await reconnect():
                        continue
                if not secret_safe:
                    logger.error("neo4j_counted_write_failed", query=query[:100], exc_info=True)
                return "error"
        return "error"  # pragma: no cover — loop always returns on attempt 1

    async def _run_read(self, query: str, params: dict) -> list[dict]:
        """Execute a read query with one reconnect retry.

        Fix 1: wraps in ``neo4j.Query(text, timeout=...)`` (same as _run).
        """
        neo4j_query = Query(query, timeout=self._timeout)
        for attempt in range(2):
            try:
                async with self._driver.session() as session:
                    result = await session.run(neo4j_query, params)
                    return [dict(record) async for record in result]
            except Exception:
                if attempt == 0 and await self._reconnect():
                    continue
                logger.error("neo4j_read_failed", query=query[:100], exc_info=True)
                return []
        return []
