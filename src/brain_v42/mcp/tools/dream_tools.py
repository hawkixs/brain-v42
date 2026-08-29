"""Dream Mode MCP tools: brain_backfill_links_batch, brain_get_clusters,
brain_list_orphans_for_classification.

Dream mode runs maintenance tasks that would be too slow during real-time
entity creation — specifically, backfilling RELATED_TO graph edges for
entities that were created before AutoLinker was active, clustering
knowledge by connected components, and surfacing cross-domain orphans for
the Dream agent to classify.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
import structlog

from brain_v42.db.tables import adrs, brain_entities, decisions, learnings, runbooks, snippets
from brain_v42.mcp.dream_project_authorization import get_dream_project_scope
from brain_v42.mcp.tools.formatters import clamp_list_limit, format_error
from brain_v42.mcp.tools.tool_annotations import (
    _DESTRUCTIVE_ANNOTATIONS,
    _HEARTBEAT_ANNOTATIONS,
    _READ_ANNOTATIONS,
    _TERMINAL_ANNOTATIONS,
    _WRITE_ANNOTATIONS,
)
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.repositories.pg_graph_ledger import UnknownGraphEndpoint
from brain_v42.services.link_result import LinkJobResult

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CURATION_STATUSES = frozenset({"proposed", "applied", "rejected"})
#: Plafond du lot de REJETS. Le rejet est le chemin confortable — les
#: propositions lues sont des dégradations de titre — mais un lot illimité
#: rendrait le résultat illisible et le timeout probable.
_CURATION_REJECT_MAX = 50
_CURATION_PAYLOAD_MAX = 200

logger = structlog.get_logger(__name__)

# Capitalized keys matching Neo4j node labels
_ENTITY_TABLES: dict[str, sa.Table] = {
    "Decision": decisions,
    "Learning": learnings,
    "Snippet": snippets,
    "Runbook": runbooks,
    "ADR": adrs,
}

# Maps Neo4j label → (SA table, title column name)
_TYPE_META: dict[str, tuple[sa.Table, str]] = {
    "Decision": (decisions, "title"),
    "Learning": (learnings, "topic"),
    "Snippet": (snippets, "title"),
    "Runbook": (runbooks, "title"),
    "ADR": (adrs, "title"),
}


# Maximum members rendered per cluster in non-summary mode.
# Clusters larger than this threshold get a truncation notice so the caller
# is aware that content was bounded, not silently dropped.
_CLUSTER_MEMBERS_PER_CLUSTER_MAX = 30


def _proposal_service_factory(session_factory: Any) -> Any:
    """Construire le service de propositions — indirection pour les tests.

    Import différé : `proposal_service` tire la couche services entière, que
    ce module feuille n'a pas besoin de charger à l'import du catalogue.
    """
    from brain_v42.services.proposal_service import ProposalService  # noqa: PLC0415

    return ProposalService(session_factory, None, None)


def register_dream_tools(
    mcp: FastMCP,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    auto_linker: Any | None,
    graph_service: Any | None,
) -> None:
    """Register dream-mode MCP tools."""

    @mcp.tool(version="1.0", annotations=_HEARTBEAT_ANNOTATIONS)
    async def brain_backfill_links_batch(
        entity_type: str | None = None,
        limit: int = 50,
        threshold: float = 0.6,
        max_links: int = 3,
    ) -> str:
        """Backfill RELATED_TO graph edges for entities missing links.

        Finds entities in Neo4j that have no RELATED_TO edges, fetches
        their embeddings from PostgreSQL, and calls AutoLinker to create
        the missing semantic links.

        Args:
            entity_type: Neo4j label to filter (Decision/Learning/Snippet/Runbook/ADR).
                         None processes all types.
            limit: Maximum number of entities to process per call.
            threshold: Minimum cosine similarity for creating a link (default 0.6).
            max_links: Cap on SUCCESSFUL links (created + matched) per entity
                       (default 3). Errors never consume the cap; attempts are
                       bounded by the 2*max_links candidate fetch, so a fully
                       failing entity can report up to 2*max_links errors.

        Returns:
            Summary string with entities processed, links created, and error count.
        """
        if graph_service is None or auto_linker is None:
            return format_error(
                "Neo4j graph not configured — enable graph_enabled=true in settings"
            )

        scope = get_dream_project_scope()
        if scope is None:
            unlinked_ids = await graph_service.find_unlinked_nodes(
                entity_type=entity_type, limit=limit
            )
        else:
            unlinked_ids = await graph_service.find_unlinked_nodes(
                entity_type=entity_type,
                limit=limit,
                project_key=scope.project_key,
            )

        if not unlinked_ids:
            return f"Backfill complete: entities_processed=0 {LinkJobResult().as_summary()}"

        if scope is not None:
            scoped_ids = [
                raw_id if isinstance(raw_id, (UUID, str)) else "" for raw_id in unlinked_ids
            ]
            await scope.revalidate_ids(scoped_ids)

        # Determine which tables to query
        if entity_type is not None:
            tables_to_query = (
                {entity_type: _ENTITY_TABLES[entity_type]} if entity_type in _ENTITY_TABLES else {}
            )
        else:
            tables_to_query = _ENTITY_TABLES

        # Build a map of id -> (entity_type, embedding) from PG.
        # find_unlinked_nodes can return None entries when a Neo4j node is
        # missing its `id` property (orphan/legacy nodes); these surface
        # most often when entity_type=None because the label filter does
        # not exclude them. Skip None and any malformed string instead of
        # crashing the whole call (2026-04-09 connect phase regression).
        id_to_info: dict[UUID, tuple[str, list[float]]] = {}
        unlinked_uuid_set: set[UUID] = set()
        for raw_id in unlinked_ids:
            if raw_id is None:
                logger.warning("dream_backfill.null_id_skipped")
                continue
            try:
                unlinked_uuid_set.add(raw_id if isinstance(raw_id, UUID) else UUID(raw_id))
            except (TypeError, ValueError, AttributeError):
                logger.warning("dream_backfill.invalid_uuid", raw_id=raw_id)

        if not unlinked_uuid_set:
            return f"Backfill complete: entities_processed=0 {LinkJobResult().as_summary()}"

        async with session_factory() as session:
            for etype, table in tables_to_query.items():
                stmt = sa.select(table.c.id, table.c.embedding).where(
                    sa.and_(
                        table.c.id.in_(list(unlinked_uuid_set)),
                        table.c.embedding.is_not(None),
                        # Ticket 6d2cf2a9 — le résolveur exige `active` sur les DEUX
                        # ancres. Le filtre de _find_similar ne couvre que la CIBLE ;
                        # find_unlinked_nodes rend aussi des SOURCES archived (mesuré
                        # le 2026-08-18 : brain-v42, 82 non liées actives ET 21
                        # archived). Une source archived fait lever le résolveur pour
                        # CHACUN de ses candidats, ne peut jamais gagner d'arête, donc
                        # revient à chaque nuit : connect reste partial à perpétuité.
                        # Même prédicat que list_active_classification_orphans, la
                        # liste de sources de STEP_B, qui filtre déjà au ledger.
                        sa.exists().where(
                            sa.and_(
                                brain_entities.c.source_uuid == table.c.id,
                                brain_entities.c.lifecycle == "active",
                            )
                        ),
                        *((table.c.project_key == scope.project_key,) if scope is not None else ()),
                    )
                )
                result = await session.execute(stmt)
                for row in result.fetchall():
                    id_to_info[row.id] = (etype, row.embedding)

        # Call auto_link for each entity that has an embedding
        processed = 0
        aggregate = LinkJobResult()

        for entity_uuid in unlinked_uuid_set:
            if entity_uuid not in id_to_info:
                continue
            etype, embedding = id_to_info[entity_uuid]
            if scope is not None:
                try:
                    links = await auto_linker.auto_link(
                        entity_type=etype,
                        entity_id=entity_uuid,
                        embedding=embedding,
                        threshold=threshold,
                        max_links=max_links,
                        authorization=scope,
                    )
                except UnknownGraphEndpoint:
                    # Ticket 6d2cf2a9 — symétrie avec la branche non-scopée, mais
                    # NARROW à dessein. La branche scopée reste hors du wrapper de
                    # dégradation pour qu'un refus d'autorisation propage (contrat
                    # graph_helpers) ; seule la pathologie de données est absorbée,
                    # et elle reste COMPTÉE : un connect sur données sales doit
                    # continuer à sortir partial, pas vert.
                    aggregate.errors.append(
                        {
                            "id": entity_uuid,
                            "entity_type": etype,
                            "reason": "unknown_endpoint",
                        }
                    )
                    logger.warning(
                        "dream_backfill.unknown_graph_endpoint",
                        entity_type=etype,
                        entity_id=str(entity_uuid),
                    )
                    continue
                processed += 1
                aggregate.extend(links)
                continue
            try:
                links = await auto_linker.auto_link(
                    entity_type=etype,
                    entity_id=entity_uuid,
                    embedding=embedding,
                    threshold=threshold,
                    max_links=max_links,
                )
                processed += 1
                aggregate.extend(links)
            except Exception:
                logger.error(
                    "dream_backfill.auto_link_failed",
                    entity_id=str(entity_uuid),
                    exc_info=True,
                )

        logger.info(
            "dream_backfill.complete",
            processed=processed,
            **{
                f"count_{k}": len(getattr(aggregate, k))
                for k in ("created", "matched", "skipped", "errors")
            },
        )
        return f"Backfill complete: entities_processed={processed} {aggregate.as_summary()}"

    @mcp.tool(version="1.2", annotations=_READ_ANNOTATIONS)
    async def brain_get_clusters(
        min_size: int = 2,
        limit: int = 20,
        summary_only: bool = False,
        max_members_per_cluster: int = _CLUSTER_MEMBERS_PER_CLUSTER_MAX,
    ) -> str:
        """Find knowledge clusters via connected components in the graph.

        Retrieves all RELATED_TO edges from Neo4j, runs union-find to identify
        connected components, filters by minimum size, and enriches each cluster
        member with metadata from PostgreSQL.

        Args:
            min_size: Minimum number of members for a cluster to be included (default 2).
            limit: Maximum number of clusters to return, sorted by size DESC (default 20).
            summary_only: When True, omit per-member enrichment and PG lookup.
                Returns cluster sizes only. Use this when a cluster is too
                large to fit in the caller's token budget — output stays
                bounded regardless of member count.
            max_members_per_cluster: Cap on rendered members per cluster (default 30).
                Clusters larger than this cap get a truncation notice — use
                summary_only=True or filter by entity type to page through them.

        Returns:
            Markdown string listing clusters with their members (or sizes when summary_only).
        """
        if graph_service is None:
            return format_error(
                "Neo4j graph not configured — enable graph_enabled=true in settings"
            )

        scope = get_dream_project_scope()
        if scope is None:
            edges: list[tuple[str, str]] = await graph_service.get_all_related_edges()
        else:
            edges = await graph_service.get_all_related_edges(project_key=scope.project_key)
            edge_ids: list[UUID | str] = []
            for edge in edges:
                if not isinstance(edge, (tuple, list)) or len(edge) != 2:
                    edge_ids.append("")
                    continue
                edge_ids.extend(
                    raw_id if isinstance(raw_id, (UUID, str)) else "" for raw_id in edge
                )
            await scope.revalidate_ids(edge_ids)

        # --- Union-Find ---
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            if x not in parent:
                parent[x] = x
            while parent[x] != x:
                parent[x] = parent[parent[x]]  # path compression
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for src, tgt in edges:
            union(src, tgt)

        # Group nodes into components
        components: dict[str, list[str]] = {}
        for node in parent:
            root = find(node)
            components.setdefault(root, []).append(node)

        # Filter by min_size and sort by size DESC
        clusters = sorted(
            [members for members in components.values() if len(members) >= min_size],
            key=lambda m: len(m),
            reverse=True,
        )[:limit]

        total = len(clusters)

        if total == 0:
            return "## Knowledge Clusters\n\n0 clusters found."

        if summary_only:
            lines = [
                "## Knowledge Clusters (summary)",
                "",
                f"{total} cluster(s) found.",
                "",
            ]
            for i, members in enumerate(clusters, 1):
                lines.append(f"- Cluster {i}: {len(members)} members")
            logger.info("dream_get_clusters.complete", clusters=total, summary_only=True)
            return "\n".join(lines)

        # --- Enrich with PG metadata ---
        # Collect all node IDs that appear in the retained clusters
        all_node_ids = {node_id for members in clusters for node_id in members}

        # Map id -> (entity_type, title)
        id_to_meta: dict[str, tuple[str, str]] = {}

        async with session_factory() as session:
            for etype, (table, title_col) in _TYPE_META.items():
                stmt = sa.select(
                    table.c.id,
                    sa.literal(etype).label("entity_type"),
                    sa.column(title_col).label("title"),
                ).where(
                    sa.and_(
                        sa.cast(table.c.id, sa.Text).in_(list(all_node_ids)),
                        *((table.c.project_key == scope.project_key,) if scope is not None else ()),
                    )
                )
                result = await session.execute(stmt)
                for row in result.fetchall():
                    id_to_meta[str(row.id)] = (row.entity_type, row.title)

        # --- Format output ---
        cap = max(1, max_members_per_cluster)
        lines = ["## Knowledge Clusters", "", f"{total} cluster(s) found."]
        for i, members in enumerate(clusters, 1):
            lines.append(f"\n### Cluster {i} ({len(members)} members)")
            rendered = members[:cap]
            omitted_members = len(members) - len(rendered)
            for node_id in rendered:
                if node_id in id_to_meta:
                    etype, title = id_to_meta[node_id]
                    lines.append(f"- [{etype}] {title} (id:{node_id})")
                else:
                    lines.append(f"- [unknown] (id:{node_id})")
            if omitted_members > 0:
                lines.append(
                    f"… ({omitted_members} membres omis — augmentez max_members_per_cluster"
                    f" ou utilisez summary_only=True pour une vue compacte)"
                )

        logger.info("dream_get_clusters.complete", clusters=total)
        return "\n".join(lines)

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_list_orphans_for_classification(limit: int = 20) -> str:
        """List cross-domain orphans ready for Domain-node assignment.

        An orphan = entity with zero RELATED_TO edges AND no BELONGS_TO_DOMAIN.
        These are the entities the cosine-0.6 AutoLinker could not bridge
        (learning 2ddc02a3). Caller (Dream agent in CONNECT phase) classifies
        each locally against the closed set of knowledge domains, then calls
        brain_assign_domain(entity_id, domain_name) once per assignment.

        Allowed domains (closed set — use these exact lowercase names):
            infra, ml, backend, memory, tooling, data, ops, frontend, security

        Args:
            limit: Max orphans returned per call (default 20, capped 50).

        Returns:
            JSON array of {id, type, topic, tags, project_key}. Empty array
            ("[]") when the graph is at domain-equilibrium.
        """
        if graph_service is None:
            return format_error(
                "Neo4j graph not configured — enable graph_enabled=true in settings"
            )

        clamped = max(1, min(50, limit))
        scope = get_dream_project_scope()
        if scope is None:
            orphans = await graph_service.find_orphans_for_classification(limit=clamped)
        else:
            orphans = await graph_service.find_orphans_for_classification(
                limit=clamped,
                project_key=scope.project_key,
            )
        if not orphans:
            return "[]"

        if scope is not None:
            orphan_ids = [
                raw_id if isinstance(raw_id, (UUID, str)) else ""
                for row in orphans
                for raw_id in [row.get("id") if isinstance(row, dict) else ""]
            ]
            await scope.revalidate_ids(orphan_ids)

        # Pick one Neo4j label per orphan (first that's in _TYPE_META)
        id_to_type: dict[str, str] = {}
        for row in orphans:
            for lbl in row.get("labels", []):
                if lbl in _TYPE_META:
                    id_to_type[row["id"]] = lbl
                    break

        if not id_to_type:
            return "[]"

        # Group by type so we query each table once
        by_type: dict[str, list[UUID]] = {}
        for raw_id, t in id_to_type.items():
            try:
                by_type.setdefault(t, []).append(UUID(raw_id))
            except (TypeError, ValueError, AttributeError):
                logger.warning("brain_list_orphans.invalid_uuid", raw_id=raw_id)

        out: list[dict] = []
        async with session_factory() as session:
            for t, ids in by_type.items():
                table, topic_col = _TYPE_META[t]
                stmt = sa.select(
                    table.c.id,
                    getattr(table.c, topic_col).label("topic"),
                    table.c.tags,
                    table.c.project_key,
                ).where(
                    sa.and_(
                        table.c.id.in_(ids),
                        *((table.c.project_key == scope.project_key,) if scope is not None else ()),
                    )
                )
                result = await session.execute(stmt)
                for row in result.fetchall():
                    out.append(
                        {
                            "id": str(row.id),
                            "type": t,
                            "topic": row.topic,
                            "tags": list(row.tags or []),
                            "project_key": row.project_key,
                        }
                    )

        logger.info(
            "mcp.brain_list_orphans_for_classification",
            requested=clamped,
            returned=len(out),
        )
        return json.dumps(out)

    @mcp.tool(version="1.0", annotations=_WRITE_ANNOTATIONS)
    async def brain_assign_domain(entity_id: str, domain_name: str) -> str:
        """Write a BELONGS_TO_DOMAIN edge from an entity to a Domain node.

        Called by the Dream CONNECT phase after local classification. Atomic:
        upserts the Domain node first, then creates the edge via _run_counted.

        Args:
            entity_id: UUID of any entity present in the graph.
            domain_name: Must be ∈ ALLOWED_DOMAINS =
                {infra, ml, backend, memory, tooling, data, ops, frontend, security}.

        Returns:
            One of:
              "created"           — new BELONGS_TO_DOMAIN edge written
              "matched"           — edge already existed
              "missing_node"      — anchor node absent in graph
              "invalid_domain"    — domain_name not in ALLOWED_DOMAINS (no write)
              "invalid_entity_id" — entity_id not a valid UUID
              "error"             — Neo4j write failed, or the entity is no
                longer an active graph endpoint (e.g. archived between the
                orphan listing and this call; logged as
                mcp.brain_assign_domain.unknown_graph_endpoint)
        """
        if graph_service is None:
            return format_error("Neo4j graph not configured — enable graph_enabled=true")
        try:
            eid = UUID(entity_id)
        except (ValueError, AttributeError):
            return "invalid_entity_id"
        scope = get_dream_project_scope()
        if scope is not None:
            await scope.revalidate_id(eid)
        domain_outcome: str = await graph_service.upsert_domain(domain_name)
        # upsert_domain returns Literal['ok','invalid_domain','error'].
        # 'invalid_domain' means the name failed ALLOWED_DOMAINS validation (no write).
        # 'error' means Neo4j infra failure — must NOT be mapped to 'invalid_domain'
        # or the LLM agent will wrongly conclude the name is bad and stop retrying.
        if domain_outcome != "ok":
            return domain_outcome
        try:
            if scope is None:
                outcome: str = await graph_service.link_entity_to_domain(eid, domain_name)
            else:
                await scope.revalidate_id(eid)
                outcome = await graph_service.link_entity_to_domain(
                    eid,
                    domain_name,
                    project_key=scope.project_key,
                )
        except UnknownGraphEndpoint:
            # Ticket fb62624f — miroir du traitement STEP_A (auto_linker) :
            # `_resolve_named_target` exige `lifecycle='active'` sur les deux
            # ancres. Une entité archivée ENTRE le listing des orphelins et
            # l'assignation lèverait ici, s'échapperait du tool et serait
            # réduite par `mask_error_details` à un message opaque. Le catch
            # reste volontairement étroit : `DreamProjectAuthorizationError`
            # dérive d'`AuthorizationError` et doit continuer à propager.
            # Le WARN nomme l'entité et le domaine — un refus incomptable
            # forcerait l'opérateur à rejouer du SQL pour retrouver la ligne.
            logger.warning(
                "mcp.brain_assign_domain.unknown_graph_endpoint",
                entity_id=entity_id,
                domain=domain_name,
            )
            outcome = "error"
        logger.info(
            "mcp.brain_assign_domain",
            entity_id=entity_id,
            domain=domain_name,
            outcome=outcome,
        )
        return outcome

    @mcp.tool(version="1.0", annotations=_READ_ANNOTATIONS)
    async def brain_list_curation_proposals(
        project_key: str,
        status: str = "proposed",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """List roadmap curation proposals for one project, item by item.

        The nightly ROADMAP phase writes proposals here and never applies them —
        it is proposer-only by design. Until now nothing in the MCP catalogue
        could read them back: the only apply/reject surface lives behind the
        Codex gateway, and a reviewer had to open a read-only psql transaction
        to decide. 499 rows accumulated that way.

        Scoping goes through a join on ``features``: this table carries no
        project key of its own, so an unscoped read would return every project's
        proposals under a scoped request.

        Args:
            project_key: Required. Proposals are only ever reviewed per project.
            status: proposed (default), applied or rejected. The default matters —
                the applied and rejected rows outnumber the ones left to decide.
            limit: Rows per page, capped at 100. The cap is announced when it bites.
            offset: Rows skipped, for paging through a large backlog.
        """
        project_key = canonicalize_project_key(project_key, strict=False)
        if not project_key:
            return format_error("project_key is required — proposals are reviewed per project")
        if status not in _CURATION_STATUSES:
            return format_error(
                f"Invalid status '{status}' (valid: {', '.join(sorted(_CURATION_STATUSES))})"
            )
        capped, limit_notice = clamp_list_limit(limit)

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(
                            "SELECT p.id, p.op, p.feature_id, p.payload, p.rationale, "
                            "p.status, p.created_at, f.name AS feature_name, "
                            "f.project_key AS project_key "
                            "FROM roadmap_curation_proposals p "
                            "JOIN features f ON f.id = p.feature_id "
                            "WHERE f.project_key = :project_key AND p.status = :status "
                            "ORDER BY p.created_at DESC, p.id DESC "
                            "LIMIT :limit OFFSET :offset"
                        ),
                        {
                            "project_key": project_key,
                            "status": status,
                            "limit": capped,
                            "offset": offset,
                        },
                    )
                )
                .mappings()
                .all()
            )

        header = f"## Curation proposals — {project_key} [{status}]"
        if not rows:
            # Une sortie vide serait indiscernable d'une panne de lecture.
            return f"{header}\n\nAucune proposition à ce statut."

        lines = [header, ""]
        for row in rows:
            payload = json.dumps(row["payload"], ensure_ascii=False, sort_keys=True)
            if len(payload) > _CURATION_PAYLOAD_MAX:
                payload = payload[: _CURATION_PAYLOAD_MAX - 1] + "…"
            created = row["created_at"]
            lines.append(
                f"- **#{row['id']}** `{row['op']}` — {row['feature_name']} "
                f"(feature {row['feature_id']}, {created:%Y-%m-%d})"
            )
            if row["rationale"]:
                lines.append(f"  {row['rationale']}")
            lines.append(f"  payload: {payload}")
        return "\n".join(lines) + limit_notice

    async def _curation_ownership(
        proposal_ids: list[int],
    ) -> dict[int, dict[str, Any]]:
        """(statut, projet, feature) de chaque id — la jointure EST le scope."""
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        sa.text(
                            "SELECT p.id, p.status, p.op, f.project_key, "
                            "f.name AS feature_name "
                            "FROM roadmap_curation_proposals p "
                            "JOIN features f ON f.id = p.feature_id "
                            "WHERE p.id IN :ids"
                        ).bindparams(sa.bindparam("ids", expanding=True)),
                        {"ids": list(dict.fromkeys(proposal_ids))},
                    )
                )
                .mappings()
                .all()
            )
        return {int(row["id"]): dict(row) for row in rows}

    # Terminal, pas additive : un rejet ne ressuscite jamais (le dedup nocturne
    # skippe les doublons de lignes rejected) — destructiveHint=True ; et
    # idempotent : re-rejeter rend « déjà rejected », aucune mutation neuve.
    @mcp.tool(version="1.0", annotations=_TERMINAL_ANNOTATIONS)
    async def brain_reject_curation_proposals(
        project_key: str,
        proposal_ids: list[int],
    ) -> str:
        """Reject proposed roadmap curations for one project — the COMFORTABLE path.

        The proposals read so far are title DEGRADATIONS (« Disaster recovery
        vérifiable — PostgreSQL + Neo4j + off-site » → « Infrastructure
        PostgreSQL sécurisée ») : rejecting must cost one call for a whole
        batch, applying must cost one call PER proposal — the asymmetry is the
        contract (ticket 2547b4a2), mirrored by brain_apply_curation_proposal.

        Scoping goes through the join on ``features`` : an id belonging to
        another project is refused BY NAME and never reaches the service. Only
        ``proposed`` rows mutate ; anything else is skipped with its status.

        Args:
            project_key: Required — proposals are only ever reviewed per project.
            proposal_ids: 1 à 50 ids (dédupliqués), verdict rendu par id.
        """
        project_key = canonicalize_project_key(project_key, strict=False)
        if not project_key:
            return format_error("project_key is required — proposals are reviewed per project")
        unique_ids = list(dict.fromkeys(proposal_ids))
        if not unique_ids:
            return format_error("proposal_ids is empty — nothing to reject")
        if len(unique_ids) > _CURATION_REJECT_MAX:
            return format_error(
                f"{len(unique_ids)} ids — le lot de rejet est plafonné à "
                f"{_CURATION_REJECT_MAX} ; découpe en plusieurs appels"
            )

        ownership = await _curation_ownership(unique_ids)
        service = _proposal_service_factory(session_factory)
        from brain_v42.services.proposal_service import (  # noqa: PLC0415
            ProposalServiceError,
        )

        lines: list[str] = []
        rejected = 0
        for proposal_id in unique_ids:
            row = ownership.get(proposal_id)
            if row is None:
                lines.append(f"- **#{proposal_id}** — introuvable")
                continue
            if row["project_key"] != project_key:
                lines.append(
                    f"- **#{proposal_id}** — REFUSÉ : appartient à "
                    f"`{row['project_key']}`, pas à `{project_key}`"
                )
                continue
            if row["status"] != "proposed":
                lines.append(f"- **#{proposal_id}** — sauté : déjà `{row['status']}`")
                continue
            try:
                await service.reject_roadmap_curation(proposal_id)
            except ProposalServiceError as exc:
                lines.append(f"- **#{proposal_id}** — échec : {exc}")
                continue
            rejected += 1
            lines.append(f"- **#{proposal_id}** `{row['op']}` — rejetée ({row['feature_name']})")

        header = (
            f"## Rejet de curations — {project_key} : "
            f"{rejected} rejetée(s) / {len(unique_ids)} demandée(s)"
        )
        return "\n".join([header, "", *lines])

    # Destructif non idempotent : un merge ARCHIVE la feature perdante, un
    # rename écrase un titre — la famille de brain_feature_update.
    @mcp.tool(version="1.0", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_apply_curation_proposal(
        project_key: str,
        proposal_id: int,
    ) -> str:
        """Apply ONE proposed roadmap curation — deliberately SINGULAR.

        One integer, never a list : the cost lives in the signature, not in a
        guideline. The proposals read so far are title degradations, and an
        « apply all » surface would be a trap (ticket 2547b4a2, fil du
        2026-08-11). L'appelant relit la proposition (via
        brain_list_curation_proposals) puis l'applique une par une ; le rejet
        en lot vit dans brain_reject_curation_proposals.

        Toutes les ops sont applicables ici (allowed_ops=None) : c'est la
        review HUMAINE, pas le wet nocturne qui se borne à WET_APPLYABLE_OPS.

        Args:
            project_key: Required — la jointure sur features est le scope.
            proposal_id: L'unique proposition à appliquer.
        """
        project_key = canonicalize_project_key(project_key, strict=False)
        if not project_key:
            return format_error("project_key is required — proposals are reviewed per project")

        ownership = await _curation_ownership([proposal_id])
        row = ownership.get(proposal_id)
        if row is None:
            return format_error(f"proposal #{proposal_id} introuvable")
        if row["project_key"] != project_key:
            return format_error(
                f"proposal #{proposal_id} appartient à `{row['project_key']}`, "
                f"pas à `{project_key}` — refusé"
            )
        if row["status"] != "proposed":
            return format_error(f"proposal #{proposal_id} est déjà `{row['status']}`")

        service = _proposal_service_factory(session_factory)
        from brain_v42.services.proposal_service import (  # noqa: PLC0415
            ProposalServiceError,
        )

        try:
            result = await service.apply_roadmap_curation(proposal_id, allowed_ops=None)
        except ProposalServiceError as exc:
            return format_error(f"apply #{proposal_id} a échoué : {exc}")
        return (
            f"## Curation appliquée — {project_key}\n\n"
            f"- **#{proposal_id}** `{result.operation}` — {row['feature_name']}\n"
            f"- apply_log : {result.apply_log}"
        )
