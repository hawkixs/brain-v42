# Neo4j Knowledge Graph Integration — brain_v42

**Date:** 2026-03-16
**Status:** Draft
**Author:** Claude + Hawixs

## Problem

brain_v42 stores 6 entity types (decisions, learnings, snippets, runbooks, ADRs, project contexts) in PostgreSQL with pgvector for semantic search. Entities are siloed — no cross-entity relationships beyond `project_key` string matching and `superseded_by` self-FK on decisions. The supersession chain CTE is unidirectional (can't traverse backward from new to old). Project hierarchy (ReD → sub-projects) is a flat `related_projects: list[str]` with no direction or traversal.

Meanwhile, MCP tool count is at 35 (LLM accuracy threshold ~37), with significant overlap between search tools.

## Decision

Add Neo4j 5 Community as a relationship index alongside PostgreSQL. PG remains source of truth for all entity data, FTS, and vector search. Neo4j stores lightweight nodes (UUID + type + label) and explicit relationships only. The graph is invisible to the LLM — no new MCP tools. Existing tools are consolidated from 35 to 29, and enriched internally with graph traversal results.

## Architecture

### Overview

```
Claude Code (MCP stdio)
         ↓
FastMCP Server (in-process)
  ├── 29 brain_* tools (consolidated from 35)
  ├── Service Layer + GraphService
  │     ├── PgXxxRepo (CRUD, FTS, pgvector) — source of truth
  │     └── GraphService (Neo4j async) — relations only
  └── Metrics (InstrumentedGraphService)
         ↓                    ↓
PostgreSQL + pgvector    Neo4j 5 Community
  (port 5433)              (bolt 7687, browser 7474)
```

### Write Path (write-through)

```
brain_log_decision(title, ..., related_to=[{id, type}])
  → DecisionService.create()
    → PgDecisionRepo.create()                   # PG write (source of truth)
    → GraphService.upsert_node()                # Neo4j node (id, type, project_key, title)
    → GraphService.link_to_project(id, key)     # BELONGS_TO project
    → GraphService.create_relation()            # MOTIVATED_BY, IMPLEMENTS, etc. (from related_to)
```

All Neo4j calls are wrapped in try/except. If Neo4j is down, PG write succeeds and graph write is logged as a structured error. Graceful degradation — the system works without Neo4j, just without graph enrichment.

### Read Path (graph-enriched)

```
brain_search(query, types=[...])
  → BrainService.search()
    → fan_out pgvector search across services       # existing behavior
    → GraphService.get_related_ids(result_ids)      # NEW: get graph neighbors
    → batch get_by_id() for neighbor_ids             # fetch details from PG
    → merge: primary results + "Related" section     # enriched response
```

The "Related" section appends graph neighbors to the formatted response, grouped by relation type, max 5 neighbors per result entity.

## Neo4j Schema

### Nodes (lightweight — identity only)

```cypher
(:Decision {id: UUID, project_key: str, title: str})
(:Learning {id: UUID, project_key: str, topic: str})
(:Snippet   {id: UUID, project_key: str, title: str})
(:Runbook   {id: UUID, project_key: str, title: str})
(:ADR       {id: UUID, project_key: str, title: str, number: int})
(:Project   {project_key: str, name: str})
```

Note: Uniqueness constraints on node properties are supported in Neo4j 5 Community Edition.

### Relations

The `SUPERSEDES` relation points from the **new** decision to the **old** decision it replaces: `(new_decision)-[:SUPERSEDES]->(old_decision)`. This matches English semantics: "Decision B supersedes Decision A."

| Relation | Source | Target | Direction semantics | Created by |
|----------|--------|--------|---------------------|------------|
| `SUPERSEDES` | New Decision | Old Decision | new replaces old | brain_supersede_decision |
| `MOTIVATED_BY` | Decision | Learning | decision was motivated by learning | related_to param |
| `IMPLEMENTS` | Snippet | Decision | snippet implements decision | related_to param |
| `DOCUMENTS` | ADR | Decision | ADR documents decision | related_to param |
| `USES` | Runbook | Snippet | runbook uses snippet | related_to param |
| `RELATED_TO` | Any | Any | generic association (catch-all) | related_to param |
| `CONTAINS` | Project | Project | parent contains sub-project | init_graph.py / brain_set_project_context |
| `DEPENDS_ON` | Project | Project | project depends on another | init_graph.py / brain_set_project_context |
| `BELONGS_TO` | Entity | Project | entity belongs to project | write-through (automatic) |
| `MERGED_INTO` | Entity | Entity | source was merged into target | brain_decay merge action |

### Constraints & Indexes

```cypher
CREATE CONSTRAINT decision_id FOR (d:Decision) REQUIRE d.id IS UNIQUE;
CREATE CONSTRAINT learning_id FOR (l:Learning) REQUIRE l.id IS UNIQUE;
CREATE CONSTRAINT snippet_id  FOR (s:Snippet)  REQUIRE s.id IS UNIQUE;
CREATE CONSTRAINT runbook_id  FOR (r:Runbook)  REQUIRE r.id IS UNIQUE;
CREATE CONSTRAINT adr_id      FOR (a:ADR)      REQUIRE a.id IS UNIQUE;
CREATE CONSTRAINT project_pk  FOR (p:Project)  REQUIRE p.project_key IS UNIQUE;
```

## GraphService

### Module: `src/brain_v42/services/graph_service.py`

```python
class GraphService:
    """Neo4j async client — lightweight nodes + relations."""

    def __init__(self, driver: AsyncDriver):
        self.driver = driver

    # Nodes
    async def upsert_node(self, entity_type: str, id: UUID, props: dict) -> None
    async def delete_node(self, entity_type: str, id: UUID) -> None

    # Relations (source/target can be UUID for entities or str for project_key)
    async def create_relation(self, source_id: UUID, target_id: UUID, rel_type: str, props: dict | None = None) -> None
    async def delete_relation(self, source_id: UUID, target_id: UUID, rel_type: str) -> None
    async def link_to_project(self, entity_id: UUID, project_key: str) -> None

    # Traversals
    async def get_neighbors(self, id: UUID, rel_types: list[str] | None = None, depth: int = 1) -> list[dict]
    async def get_supersession_chain(self, decision_id: UUID) -> list[UUID]
    async def get_project_tree(self, project_key: str) -> list[str]

    # Bulk
    async def get_related_ids(self, ids: list[UUID]) -> dict[UUID, list[dict]]
    # Returns: {entity_id: [{"id": UUID, "type": "Decision", "rel": "MOTIVATED_BY", "title": "..."}, ...]}

    # Health
    async def healthcheck(self) -> bool
```

### Integration in Service Layer

GraphService is injected as `graph: GraphService | None` in all entity services. When None (Neo4j unavailable or disabled), services operate in PG-only mode — identical to current behavior.

```python
class DecisionService:
    def __init__(self, repo, embedding, graph: GraphService | None = None):
        self.graph = graph

    async def create(self, data, related_to=None):
        decision = await self.repo.create(data, embedding)
        if self.graph:
            try:
                await self.graph.upsert_node("Decision", decision.id, {
                    "project_key": data.project_key,
                    "title": data.title,
                })
                if data.project_key:
                    await self.graph.link_to_project(decision.id, data.project_key)
                for rel in (related_to or []):
                    await self.graph.create_relation(decision.id, UUID(rel["id"]), rel["type"])
            except Exception:
                logger.error("graph_write_failed", decision_id=str(decision.id), exc_info=True)
        return decision

    async def delete(self, decision_id):
        deleted = await self.repo.delete(decision_id)
        if deleted and self.graph:
            try:
                await self.graph.delete_node("Decision", decision_id)
            except Exception:
                logger.error("graph_delete_failed", decision_id=str(decision_id), exc_info=True)
        return deleted
```

### Supersession Chain Fix

The current recursive CTE bug (unidirectional traversal) is resolved by delegating to Neo4j. The `SUPERSEDES` relation goes from new to old: `(new)-[:SUPERSEDES]->(old)`.

```cypher
// Find root: walk SUPERSEDES backward from start (new→old means backward = toward newest)
MATCH (start:Decision {id: $decision_id})
OPTIONAL MATCH path = (newest:Decision)-[:SUPERSEDES*]->(start)
WHERE NOT ()-[:SUPERSEDES]->(newest)
WITH coalesce(newest, start) AS root
// Walk full chain from root (newest) to oldest
MATCH chain = (root)-[:SUPERSEDES*0..]->(leaf)
RETURN [n IN nodes(chain) | n.id] AS chain_ids
ORDER BY length(chain) DESC LIMIT 1
```

This naturally handles bidirectional traversal — finding the root first, then the full chain. Result is ordered newest → oldest.

## Error Handling

### Write-through failures

All Neo4j operations in the service layer are wrapped in `try/except Exception`:
- **Partial failure is acceptable**: if `upsert_node` succeeds but `create_relation` fails, the orphan node is harmless — the reconciliation job catches it.
- **Structured logging**: every failure logs `entity_id`, `operation`, and full traceback via structlog.
- **No transaction rollback of PG**: PG write is already committed before graph write starts.

### Neo4j timeout

Configurable via `NEO4J_TIMEOUT` (default 5s). Applied to all Cypher queries via `session.run(query, timeout=self.timeout)`.

### Reconciliation

`scripts/reconcile_graph.py` — run on-demand or on startup:
1. Scan PG for all entity IDs → verify each has a Neo4j node (create missing ones)
2. Scan PG `superseded_by` / `merged_into` fields → verify Neo4j relations exist
3. Scan Neo4j for orphan nodes (no PG counterpart) → delete them
4. Idempotent (safe to run multiple times)

## Rollback Strategy

### Feature flag: `BRAIN_GRAPH_ENABLED`

```python
# config.py Settings
neo4j_url: str | None = None       # None = graph disabled entirely
neo4j_user: str = "neo4j"
neo4j_password: str = ""            # loaded from env
neo4j_timeout: float = 5.0         # seconds
graph_enabled: bool = False         # explicit opt-in
```

When `graph_enabled=False` or `neo4j_url=None`:
- `GraphService` is not instantiated
- All services receive `graph=None`
- System behaves identically to current production
- Zero performance overhead

### Phased rollout

1. **Phase 1**: Deploy Neo4j container + GraphService + write-through. `graph_enabled=true`. Verify graph populates correctly.
2. **Phase 2**: Enable read enrichment (graph neighbors in brain_search results). Verify quality.
3. **Phase 3**: Tool consolidation (separate from graph — can be done independently).

Tool consolidation and graph integration are independent changes. Either can be reverted without affecting the other.

## Tool Consolidation (35 → 29)

### Exhaustive Tool Mapping

| # | Current Tool (36) | After | Action |
|---|-------------------|-------|--------|
| 1 | `brain_log_decision` | `brain_log_decision` | KEPT — add `related_to` param |
| 2 | `brain_search_decisions` | — | MERGED into `brain_search(types=["decision"])` |
| 3 | `brain_supersede_decision` | `brain_supersede_decision` | KEPT |
| 4 | `brain_get_supersession_chain` | `brain_get_supersession_chain` | KEPT — delegates to Neo4j internally |
| 5 | `brain_learn` | `brain_learn` | KEPT — add `related_to` param |
| 6 | `brain_recall` | — | MERGED into `brain_search(types=["learning"])` |
| 7 | `brain_validate_learning` | `brain_validate_learning` | KEPT |
| 8 | `brain_propose_adr` | `brain_propose_adr` | KEPT (distinct schema) |
| 9 | `brain_accept_adr` | `brain_accept_adr` | KEPT (distinct schema) |
| 10 | `brain_deprecate_adr` | `brain_deprecate_adr` | KEPT (distinct schema) |
| 11 | `brain_list_adrs` | `brain_list_adrs` | KEPT |
| 12 | `brain_search` | `brain_search` | KEPT — absorbs 5 search tools + what_do_i_know |
| 13 | `brain_what_do_i_know_about` | — | MERGED into `brain_search(group_by_type=true)` |
| 14 | `brain_save_snippet` | `brain_save_snippet` | KEPT — add `related_to` param |
| 15 | `brain_find_snippet` | — | MERGED into `brain_search(types=["snippet"])` |
| 16 | `brain_use_snippet` | `brain_use_snippet` | KEPT |
| 17 | `brain_create_runbook` | `brain_create_runbook` | KEPT |
| 18 | `brain_get_runbook` | `brain_get_runbook` | KEPT |
| 19 | `brain_execute_runbook` | `brain_execute_runbook` | KEPT |
| 20 | `brain_search_runbooks` | — | MERGED into `brain_search(types=["runbook"])` |
| 21 | `brain_set_project_context` | `brain_set_project_context` | KEPT |
| 22 | `brain_get_project_context` | — | MERGED into `brain_session_start` |
| 23 | `brain_update_project_focus` | `brain_update_project_focus` | KEPT |
| 24 | `brain_session_start` | `brain_session_start` | KEPT — absorbs get_project_context |
| 25 | `brain_get` | `brain_get` | KEPT |
| 26 | `brain_delete` | `brain_delete` | KEPT |
| 27 | `brain_update` | `brain_update` | KEPT — add `related_to` param |
| 28 | `brain_list` | `brain_list` | KEPT |
| 29 | `brain_decay_status` | `brain_decay_status` | KEPT |
| 30 | `brain_refresh_entity` | `brain_refresh_entity` | KEPT |
| 31 | `brain_consolidation_candidates` | `brain_consolidation_candidates` | KEPT |
| 32 | `brain_merge_entities` | `brain_merge_entities` | KEPT |
| 33 | `brain_get_roadmap` | `brain_get_roadmap` | KEPT |
| 34 | `brain_reindex_plans` | `brain_reindex_plans` | KEPT |
| 35 | `brain_list_projects` | `brain_list_projects` | KEPT |

**Summary: 35 current tools. 6 MERGED away → 29 tools remaining.**

Rationale for keeping ADR tools separate (not compound): LLMs select better from distinct tools with clear schemas than from compound tools with union-typed action params. `brain_propose_adr` (needs title, context, decision) has a completely different schema from `brain_accept_adr` (needs just an ID). Same logic applies to decay tools.

### New Parameter: `related_to`

Added to write tools (`brain_log_decision`, `brain_learn`, `brain_save_snippet`, `brain_update`):

```python
# Pydantic model for validation (in models/relation.py)
class RelationInput(BaseModel):
    id: str  # UUID string of the target entity
    type: Literal["MOTIVATED_BY", "IMPLEMENTS", "DOCUMENTS", "USES", "RELATED_TO"]

# Tool parameter
related_to: list[RelationInput] | None = None
```

Validation happens at the tool layer (Pydantic). Invalid UUIDs or unknown relation types return a clear error message before reaching the service layer.

## Metrics Sidecar Update

### New Section in GET /metrics Response

```json
{
  "graph": {
    "status": "up|down|degraded",
    "url": "bolt://localhost:7687",
    "total_queries": 850,
    "total_errors": 2,
    "recent_errors": 0,
    "avg_latency_ms": 3.2,
    "write_through": {
      "success": 340,
      "failures": 1,
      "last_failure_at": "2026-03-16T10:30:00Z"
    },
    "stats": {
      "nodes": {"Decision": 120, "Learning": 193, "Snippet": 19, "Runbook": 4, "ADR": 1, "Project": 18},
      "relations": {"BELONGS_TO": 337, "SUPERSEDES": 5, "MOTIVATED_BY": 12, "IMPLEMENTS": 8, "CONTAINS": 6},
      "total_nodes": 355,
      "total_relations": 368
    }
  }
}
```

### Implementation

- **`InstrumentedGraphService`** in `metrics/instrument.py` — wraps GraphService, records latency/errors per query
- **`collector.record_graph_query(latency_ms, error)`** — new counter in MetricsCollector
- **`collector.collect_graph_stats()`** — queries Neo4j for node/relation counts
- **`MetricsFlusher`** — adds `graph_stats` to process_metrics JSONB
- **Health check** — `GraphService.healthcheck()` called on each /metrics request

## Docker Compose Update

```yaml
neo4j:
  image: neo4j:5-community
  container_name: brain_v42_neo4j
  ports:
    - "7687:7687"
    - "7474:7474"
  environment:
    NEO4J_AUTH: ${NEO4J_USER:-neo4j}/${NEO4J_PASSWORD}
    NEO4J_PLUGINS: '[]'
  volumes:
    - ./data/neo4j:/data
  healthcheck:
    test: ["CMD", "cypher-shell", "RETURN 1"]
    interval: 10s
    retries: 5
```

Note: `cypher-shell` inside the container reads auth from the NEO4J_AUTH env var, no need to pass `-u/-p` flags. Credentials are set via `.env` file (not hardcoded in docker-compose).

### Settings (.env)

```bash
# Neo4j (optional — graph disabled if NEO4J_URL is unset)
NEO4J_URL=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<your-password-here>
NEO4J_TIMEOUT=5.0
BRAIN_GRAPH_ENABLED=true
```

### Settings class additions (`config.py`)

See Settings fields defined in the [Rollback Strategy](#rollback-strategy) section. Note: the existing config.py docstring ("No Redis, Neo4j, or HTTP configuration") must be updated.

## Migration Script

### `scripts/init_graph.py`

Populates Neo4j from existing PG data:

1. **Create Project nodes** — from `project_contexts` table (18 entries)
2. **Create entity nodes** — from all 5 entity tables (~500 total)
3. **Create BELONGS_TO relations** — entity → project (by project_key)
4. **Create SUPERSEDES relations** — from `decisions.superseded_by` and `adrs.superseded_by`
5. **Create MERGED_INTO relations** — from all entities where `merged_into IS NOT NULL`
6. **Create project hierarchy** — from `config/project_hierarchy.yml`:

```yaml
# config/project_hierarchy.yml
project_hierarchy:
  red:
    contains: [red-monitor, red-orchestrator, auto-discord, brain_v42, lyriks-v3]
    depends_on: []
  red-monitor:
    depends_on: [brain_v42]
```

Script is idempotent (uses MERGE in Cypher). Verifies Neo4j health before starting.

### `scripts/reconcile_graph.py`

On-demand PG↔Neo4j consistency check:
1. Find PG entities missing from Neo4j → create nodes
2. Find Neo4j orphans (no PG match) → delete nodes
3. Verify relation integrity (superseded_by, merged_into)
4. Report drift count and actions taken

## Testing Strategy

### Unit Tests

- `tests/unit/services/test_graph_service.py` — mock AsyncDriver, test all methods
- Update `test_decision_service.py` — verify write-through with mocked GraphService
- Update `test_decision_service.py` — verify delete removes graph node
- Tests for consolidated `brain_search` (unified search with types filter, group_by_type)

### Integration Tests

- `tests/integration/test_graph_integration.py` — real Neo4j container
  - Upsert node + verify existence
  - Create relation + traverse neighbors
  - Supersession chain via Cypher (bidirectional — test from both old and new decision)
  - Project tree traversal (CONTAINS recursive)
  - Graceful degradation (stop Neo4j → verify PG-only mode works)
  - Write-through partial failure (Neo4j error mid-write → PG data intact)

### E2E Tests

- Update existing E2E suite — verify `brain_search` returns "Related" entities when graph enabled
- Test `related_to` param on `brain_log_decision`, `brain_learn`, `brain_save_snippet`
- Verify supersession chain works bidirectionally (query from new decision returns full chain)
- Test consolidated tools (unified search replaces brain_recall, brain_find_snippet, etc.)
