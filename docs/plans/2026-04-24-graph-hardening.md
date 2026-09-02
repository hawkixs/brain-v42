# Graph Hardening — Angles 1+4+5

**Date**: 2026-04-24
**Branch**: `feat/graph-hardening`
**Pattern**: pattern-auto (autonomous execution)
**Stack**: Python 3.12+, FastMCP 3.1, SQLAlchemy 2.0 async, asyncpg, Neo4j 5, pgvector

## Goal

Break the structural cosine-0.6 ceiling that leaves 13 cross-domain entities permanently orphan (learning `2ddc02a3`), fix the misleading "attempted vs net-new" reporting (learning `fd39a4f9`), and let Dream do its own multi-hop graph reasoning (decision `9e374bca`).

Success criteria:
- AutoLinker and `brain_backfill_links_batch` report 4 buckets `{created, updated, skipped, merged}` with `created/(created+skipped)` as the "graph freshness" ratio.
- Every current graph orphan gets assigned to ≥1 `Domain:*` node via a Dream-time LLM classifier; two orphans in the same domain become 2-hop reachable.
- New MCP tool `brain_graph_path(source_id, target_id, max_depth)` callable from Dream prompts.
- All Dream phase prompts (CONNECT, SYNTH, PROMOTE) updated to know about the new surface.
- 4-bucket reporting visible in the next nightly Dream run.

## Non-goals

- **Angle 2** (`CONTRADICTS`, `APPLIED_TO` relations) — separate future spec.
- **Angle 3** (GraphRAG community summaries, LazyGraphRAG) — material for Spec B meta-synthesis.
- FalkorDB migration, bitemporal `event_time`/`ingestion_time` tracking — out of scope.
- Raw-Cypher MCP tool — bounded traversals only (`brain_graph_path` + existing `brain_get_neighbors`).
- Migrating existing graph data — new nodes/edges created forward-only.

## Architecture

### Current state

```
Entity created (MCP tool)
  └─> PG INSERT (source of truth)
  └─> GraphService.upsert_node + link_to_project (write-through)
  └─> AutoLinker.auto_link(embedding, threshold=0.6, max_links=3)
        └─> PG UNION vector search → top-N candidates
        └─> For each above threshold: GraphService.create_relation(RELATED_TO)
        └─> Returns: list[dict] of created links  ← no net-vs-attempted distinction
```

Existing 10 relation types (decision `9e374bca`): `SUPERSEDES, MOTIVATED_BY, IMPLEMENTS, DOCUMENTS, USES, RELATED_TO, CONTAINS, DEPENDS_ON, BELONGS_TO, MERGED_INTO`. `BELONGS_TO` is used `entity → Project`.

Existing MCP graph tools: `brain_get_neighbors(entity_id, rel_types, depth)` (already registered when `graph_svc` wired), `brain_backfill_links_batch(entity_type, limit, threshold, max_links)`, `brain_get_supersession_chain(decision_id)`, `brain_get_clusters(min_size, limit, summary_only)`.

Existing `GraphService` methods: `upsert_node, delete_node, create_relation, delete_relation, link_to_project, get_neighbors, get_supersession_chain, get_project_tree, get_related_ids, count_nodes_by_label, count_edges_by_type, find_unlinked_nodes, get_all_related_edges, healthcheck`.

### Target state

```
Entity created (MCP tool)
  └─> PG INSERT
  └─> GraphService.upsert_node + link_to_project
  └─> AutoLinker.auto_link(...) → LinkJobResult
        ├─> created: list[dict]    # freshly MERGEd edges (ON CREATE branch)
        ├─> skipped: list[dict]    # candidates below threshold OR already linked
        ├─> updated: list[dict]    # MERGE matched existing edge (future: prop refresh)
        └─> merged: list[dict]     # dedup branch (future: unused for now, always empty)

Dream CONNECT phase (new 2-step flow — Graphiti pattern, the Dream AGENT is the classifier)
  ├── Step A (cosine pass, unchanged)
  │   └── brain_backfill_links_batch(...) → returns 4-bucket summary
  └── Step B (NEW: domain pass — driven by the Dream agent itself, no inline LLM in server code)
      ├── brain_list_orphans_for_classification(limit=20)
      │     → returns JSON [{id, type, topic, tags, project_key}] of entities
      │       with zero RELATED_TO edges AND no BELONGS_TO_DOMAIN
      ├── Dream agent classifies each entity locally (it IS an LLM) against
      │     the closed set ALLOWED_DOMAINS = {infra, ml, backend, memory,
      │     tooling, data, ops, frontend, security}
      └── brain_assign_domain(entity_id, domain_name) — called once per assignment
            ├── Validates domain_name ∈ ALLOWED_DOMAINS
            ├── GraphService.upsert_domain(name) + create BELONGS_TO_DOMAIN edge
            └── Returns a 1-bucket result ("created" / "matched" / "invalid_domain" / "error")

Dream SYNTH/PROMOTE prompts
  └── Mention brain_graph_path + brain_get_neighbors in "Allowed tools"
```

### Key invariants preserved

- PG is source of truth. New `Domain` nodes live ONLY in Neo4j (pattern matches existing `Project` nodes).
- Write-through fault tolerance: `auto_link` still never raises.
- `GRAPH_ENABLED=false` → DomainClassifier and `brain_graph_path` degrade gracefully (no-op + `format_error`).
- `BELONGS_TO_DOMAIN` added to the canonical relation taxonomy — **not a parallel mechanism**. The prose list in decision `9e374bca` grows 10 → 11; schema-wise, Neo4j accepts any label string so no migration.

### New files

| Path | Purpose |
|---|---|
| `src/brain_v42/services/link_result.py` | `LinkJobResult` dataclass (4-bucket accounting) |
| `tests/unit/test_link_result.py` | Unit tests for the dataclass |
| `tests/unit/test_brain_graph_path.py` | Unit tests for the new MCP tool |
| `tests/unit/test_brain_assign_domain.py` | Unit tests for the domain-assignment tool |
| `tests/integration/test_connect_domain_pass.py` | End-to-end: orphan → domain edge created |

Note: the test layout is flat `tests/unit/test_*.py` per the dominant codebase convention (e.g. `test_auto_linker.py`, `test_graph_service_dream.py`). Domain classification logic lives in the Dream agent prompt, not in a dedicated service file — matches the Graphiti pattern and avoids a new SDK dependency.

### Modified files

| Path | Change |
|---|---|
| `src/brain_v42/services/auto_linker.py` | Return `LinkJobResult`, distinguish ON CREATE vs ON MATCH via GraphService signal |
| `src/brain_v42/services/graph_service.py` | `create_relation` returns `Literal["created","matched","error"]`; new `upsert_domain`, `get_path`, `find_orphans_for_classification`, helper `_run_counted` |
| `src/brain_v42/mcp/tools/dream_tools.py` | `brain_backfill_links_batch` returns 4-bucket summary; new `brain_list_orphans_for_classification` + `brain_assign_domain` tools |
| `src/brain_v42/mcp/tools/brain_tools.py` | Register new `brain_graph_path` tool when `graph_svc` wired |
| `src/brain_v42/mcp/tools/formatters.py` | Add `format_graph_path` helper |
| `src/brain_v42/metrics/collector.py` | Add Prometheus counters `brain_graph_links_total{bucket="created|matched|skipped|errors"}` |
| `scripts/dream/phase_connect.md` | Add Step B instructions; update report format; update allowed tools |
| `scripts/dream/phase_synth.md` | Mention `brain_graph_path` + `brain_get_neighbors` in allowed tools |
| `scripts/dream/phase_promote.md` | Mention `brain_graph_path` + `brain_get_neighbors` in allowed tools |
| `tests/unit/test_auto_linker.py` | Update existing tests for `LinkJobResult` return type (indexing → attribute access) |
| `tests/unit/mcp/tools/test_dream_tools.py` | Update/extend existing tests for 4-bucket summary + new tools |

### Not migrating

No Alembic migration needed — all schema changes are in Neo4j (new `Domain` label + `BELONGS_TO_DOMAIN` relation, both schemaless). PG side only gains a dataclass, no tables/columns.

---

## Tasks — organized in 3 batches

### Batch A — Observability (Angle 5) — ~1 day

**Dependency**: none. Unlocks visibility for B.

#### Task A1 — `LinkJobResult` dataclass + unit tests (RED first)

File: `src/brain_v42/services/link_result.py` (new).

```python
"""LinkJobResult — 4-bucket accounting for idempotent graph-link jobs."""
from __future__ import annotations
from dataclasses import dataclass, field
from uuid import UUID


@dataclass
class LinkJobResult:
    """Result of a graph-link job distinguishing net-new from re-attempted writes.

    Why: learning fd39a4f9 — phases report operations attempted, not net state
    change. "120 links created" can be 0 net-new on an idempotent re-run. Fix
    pattern: return buckets. The caller can log/emit each, and the ratio
    created/(created+skipped) becomes a "graph freshness" metric.
    """
    created: list[dict] = field(default_factory=list)   # MERGE ON CREATE
    matched: list[dict] = field(default_factory=list)   # MERGE ON MATCH (edge existed)
    skipped: list[dict] = field(default_factory=list)   # below threshold / filtered
    errors: list[dict] = field(default_factory=list)    # write failed (logged)

    def extend(self, other: "LinkJobResult") -> None:
        self.created.extend(other.created)
        self.matched.extend(other.matched)
        self.skipped.extend(other.skipped)
        self.errors.extend(other.errors)

    @property
    def freshness_ratio(self) -> float:
        """created / (created + matched). 1.0 = all new, 0.0 = all re-attempts.
        Returns 0.0 when no writes were attempted (no division-by-zero)."""
        denom = len(self.created) + len(self.matched)
        return len(self.created) / denom if denom else 0.0

    def as_summary(self) -> str:
        """Single-line summary for logs and MCP tool return values."""
        return (
            f"created={len(self.created)} matched={len(self.matched)} "
            f"skipped={len(self.skipped)} errors={len(self.errors)} "
            f"freshness={self.freshness_ratio:.2f}"
        )
```

Tests: `tests/unit/test_link_result.py`. Cover: empty (`freshness_ratio == 0.0`), all created (1.0), all matched (0.0), mixed, extend.

Checklist:
- [ ] Write failing tests
- [ ] Implement `link_result.py` minimally
- [ ] Run `pytest tests/unit/test_link_result.py -v` → GREEN
- [ ] Run `ruff check src/brain_v42/services/link_result.py tests/unit/test_link_result.py`
- [ ] Commit: `feat(graph): add LinkJobResult dataclass for 4-bucket link accounting`

#### Task A2 — `GraphService.create_relation` returns `"created" | "matched"`

File: `src/brain_v42/services/graph_service.py`.

Change signature: `async def create_relation(...) -> str` where return is `"created"` if MERGE created a new edge, `"matched"` if it matched an existing one. Use Cypher's `ON CREATE SET r._created_at = timestamp()` vs `ON MATCH` to differentiate via a written property, OR use the Neo4j `SummaryCounters.relationships_created` on `ResultSummary`.

**Implementation** (prefer `SummaryCounters` — zero extra writes):

```python
from typing import Literal

RelationWriteOutcome = Literal["created", "matched", "error"]


async def create_relation(
    self, source_id: UUID, target_id: UUID, rel_type: str, props: dict | None = None
) -> RelationWriteOutcome:
    """Create or match a relation. Returns 'created' | 'matched' | 'error'.

    'created' means MERGE inserted a new edge; 'matched' means an equivalent
    edge already existed; 'error' means the write failed and was swallowed.
    """
    query = (
        "MATCH (a {id: $source_id}) "
        "MATCH (b {id: $target_id}) "
        f"MERGE (a)-[r:{rel_type}]->(b)"
    )
    if props:
        query += " SET r += $props"
    params: dict[str, object] = {"source_id": str(source_id), "target_id": str(target_id)}
    if props:
        params["props"] = props
    return await self._run_counted(query, params)


async def _run_counted(self, query: str, params: dict) -> RelationWriteOutcome:
    """Run a write and return the outcome based on the Neo4j ResultSummary.

    The authoritative signal is `summary.counters.relationships_created`:
    Neo4j's MERGE sets this to 1 on ON CREATE and 0 on ON MATCH.
    """
    for attempt in range(2):
        try:
            async with self._driver.session() as session:
                result = await session.run(query, params, timeout=self._timeout)
                summary = await result.consume()
                return "created" if summary.counters.relationships_created > 0 else "matched"
        except Exception:
            if attempt == 0 and await self._reconnect():
                continue
            logger.error("neo4j_counted_write_failed", query=query[:100], exc_info=True)
            return "error"
    return "error"
```

Tests: integration test `tests/integration/test_graph_service_counted_relation.py` — requires a live Neo4j. Create A→B, assert "created". Call again, assert "matched".

Checklist:
- [ ] Write failing test
- [ ] Implement `_run_counted` + update `create_relation`
- [ ] Run pytest → GREEN (skip if Neo4j not reachable)
- [ ] `ruff check`
- [ ] Commit: `feat(graph): create_relation returns created|matched via ResultSummary`

#### Task A3 — `AutoLinker.auto_link` returns `LinkJobResult`

File: `src/brain_v42/services/auto_linker.py`.

Change signature: `-> LinkJobResult`. Explicit bucketing rules (no ambiguity):
- `similarity < threshold` → `skipped` (reason: "below_threshold")
- `similarity >= threshold` AND already-picked count `>= max_links` → `skipped` (reason: "max_links_cap")
- `similarity >= threshold` AND `create_relation` returns `"created"` → `created`
- `similarity >= threshold` AND returns `"matched"` → `matched`
- `similarity >= threshold` AND returns `"error"` → `errors`

**IMPORTANT — no `__len__` compat hack.** The original `list[dict]` return type becomes `LinkJobResult`. All callers must be updated in the SAME commit:

- Production consumer: `dream_tools.brain_backfill_links_batch` currently does `total_links += len(links)` — change to `aggregate.extend(links)` where `aggregate: LinkJobResult` (Task A4).
- Discarding caller: `graph_helpers.auto_link_if_enabled` discards the return — no change needed.
- Test consumers: `tests/unit/test_auto_linker.py` currently indexes `result[0]["id"]` — change to `result.created[0]["id"]`. Must audit entire file and update assertions.

Tests: `tests/unit/test_auto_linker.py` — update existing to new signature, add new cases for 4-bucket split. Need a `MockGraph.create_relation` that returns `"matched"` on second call with same pair (track a set of seen pairs) to exercise the `matched` bucket unit-side.

Checklist:
- [ ] Grep all callers of `auto_linker.auto_link` (expect: dream_tools.py:137, graph_helpers.py auto_link_if_enabled, test_auto_linker.py)
- [ ] Write failing tests (new signature + 4-bucket split, including max_links cap case)
- [ ] Update `auto_link` implementation
- [ ] Update all existing callers in the SAME commit (dream_tools.py, graph_helpers.py unchanged, tests)
- [ ] Run full pytest → GREEN
- [ ] `ruff check`
- [ ] Commit: `feat(graph): AutoLinker returns LinkJobResult with 4 buckets`

#### Task A4 — `brain_backfill_links_batch` returns 4-bucket summary

File: `src/brain_v42/mcp/tools/dream_tools.py`.

Replace the current aggregation `processed += 1; total_links += len(links)` with a `LinkJobResult.extend(...)`. Return string becomes:

```
Backfill complete: entities_processed=N created=X matched=Y skipped=Z errors=E freshness=0.XX
```

Tests: `tests/unit/mcp/tools/test_dream_tools.py` (update or add). Mock auto_linker to return a known `LinkJobResult`; assert return string contains each bucket and the freshness ratio.

Checklist:
- [ ] Update failing tests (RED)
- [ ] Update `brain_backfill_links_batch` return line
- [ ] Run pytest → GREEN
- [ ] `ruff check`
- [ ] Commit: `feat(dream): brain_backfill_links_batch reports 4 buckets + freshness ratio`

#### Task A5 — ~~Prometheus counter `brain_graph_links_total{bucket="..."}`~~ **REJECTED**

**Status**: closed without implementation (decision `010f4f92-8c76-4195-81ca-052676e64514`, 2026-04-24).

**Reason**: initial spec misaligned with the actual stack. The codebase does not use `prometheus_client` — observability goes through custom `MetricsCollector` + `InstrumentedGraphService` + `TimeseriesFlusher` → PG `metrics_timeseries`. The 4 AutoLinker buckets are already visible in `logs/dream/*_connect.log` and in the return string of `brain_backfill_links_batch` (validated live Night 4 2026-04-24: `STEP_A: created=104 matched=4 skipped=156 errors=0 freshness=0.96`).

**Future extension if a dashboard is needed**: add `MetricsCollector.record_graph_link_bucket(bucket: str, count: int)` + call at the end of `AutoLinker.auto_link`. Not Prometheus.

#### Task A6 — Update CONNECT phase prompt

File: `scripts/dream/phase_connect.md`. Replace the current "Report the totals: entities processed, links created, errors." line with:

```
4. Report the totals in this exact shape:
   "Backfill complete: entities_processed=N created=X matched=Y skipped=Z errors=E freshness=0.XX"
   where freshness = created / (created + matched). A run with freshness < 0.2
   indicates the corpus is at link-equilibrium (see learning fd39a4f9).
```

Checklist:
- [ ] Edit `phase_connect.md`
- [ ] Commit: `docs(dream): CONNECT reports 4-bucket link totals with freshness ratio`

---

### Batch B — Domain bridging (Angle 1) — ~1.5 days (simplified: agent IS the classifier)

**Dependency**: Batch A (needs `LinkJobResult` for the domain-pass return type).

**Architectural pivot vs Round-1 plan**: Instead of a `DomainClassifier` service calling an LLM from the server process (which would require a new Anthropic SDK dependency, a budget guard, and a semaphore), the Dream agent — which IS already an LLM running inside the CONNECT phase — does the classification itself. The server exposes two narrow tools and stays LLM-free. This matches the Graphiti pattern (LLM-assisted entity resolution at the edge of the pipeline, not the middle).

#### Task B1 — `GraphService.upsert_domain` + `find_orphans_for_classification`

File: `src/brain_v42/services/graph_service.py`.

```python
ALLOWED_DOMAINS: frozenset[str] = frozenset({
    "infra", "ml", "backend", "memory", "tooling",
    "data", "ops", "frontend", "security",
})


async def upsert_domain(self, name: str) -> bool:
    """Create or match a Domain node. Returns True on success, False on invalid name.

    name must be ∈ ALLOWED_DOMAINS (lowercase). Invalid names are rejected
    with a warning log and False return — do NOT silently polluter the graph
    with typo domains.
    """
    if name not in ALLOWED_DOMAINS:
        logger.warning("graph.invalid_domain", name=name)
        return False
    query = "MERGE (d:Domain {name: $name}) SET d.updated_at = timestamp()"
    await self._run(query, {"name": name})
    return True


async def find_orphans_for_classification(
    self, limit: int = 20
) -> list[dict]:
    """Return entities lacking BOTH a RELATED_TO edge AND a BELONGS_TO_DOMAIN.

    These are the cross-domain orphans the cosine-0.6 AutoLinker cannot bridge.
    The caller (Dream agent) fetches metadata via PG, classifies locally, then
    calls brain_assign_domain per entity.

    Multi-label safe: uses ANY(l IN labels(n) ...) to not miss multi-labelled
    nodes (e.g. a Decision also tagged with a future label).
    """
    query = """
        MATCH (n)
        WHERE NOT (n)-[:RELATED_TO]-()
          AND NOT (n)-[:BELONGS_TO_DOMAIN]->()
          AND ANY(l IN labels(n)
                  WHERE l IN ['Decision','Learning','Snippet','Runbook','ADR'])
        RETURN n.id AS id, labels(n) AS labels LIMIT $limit
    """
    return await self._run_read(query, {"limit": limit})
```

Tests: integration in `tests/integration/test_graph_service_domain.py` — skip if Neo4j unreachable. Unit test for invalid-name branch of `upsert_domain` via mock `_run`.

Checklist:
- [ ] Write failing tests (unit for invalid-name, integration for happy path)
- [ ] Implement both methods
- [ ] Run pytest → GREEN
- [ ] `ruff check`
- [ ] Commit: `feat(graph): Domain node + find_orphans_for_classification`

#### Task B2 — `brain_list_orphans_for_classification` MCP tool

File: `src/brain_v42/mcp/tools/dream_tools.py`.

Returns orphan metadata (topic, tags, project_key) as JSON for the Dream agent to classify locally.

```python
@mcp.tool(version="1.0")
async def brain_list_orphans_for_classification(limit: int = 20) -> str:
    """List cross-domain orphans ready for Domain-node assignment.

    An orphan = entity with zero RELATED_TO edges AND no BELONGS_TO_DOMAIN.
    These are entities the cosine-0.6 AutoLinker could not bridge (learning
    2ddc02a3). The caller (Dream agent in CONNECT phase) classifies each
    locally against the ALLOWED_DOMAINS closed set, then calls
    brain_assign_domain once per assignment.

    Args:
        limit: Max orphans returned per call (default 20, capped 50).

    Returns:
        JSON array: [{id, type, topic, tags, project_key}]. Empty array
        when the graph is at domain-equilibrium.
    """
    if graph_service is None:
        return format_error("Neo4j graph not configured")
    clamped = max(1, min(50, limit))
    orphans = await graph_service.find_orphans_for_classification(limit=clamped)
    if not orphans:
        return "[]"
    # Fetch metadata from PG via UNION across the 5 entity tables (pattern
    # identical to brain_backfill_links_batch). Return as JSON.
    # See implementation below.
    ...
```

The return JSON must include:
- `id`: UUID string
- `type`: Neo4j label (Decision/Learning/Snippet/Runbook/ADR)
- `topic`: topic or title (source table-dependent)
- `tags`: list[str]
- `project_key`: str

Tests: `tests/unit/test_brain_list_orphans_for_classification.py`. Mock `graph_service.find_orphans_for_classification` and PG session — assert JSON structure.

Checklist:
- [ ] Write failing tests
- [ ] Implement tool (reuse _TYPE_META table from dream_tools.py for PG fetch)
- [ ] Include `ALLOWED_DOMAINS` constant in the tool docstring so the Dream agent sees the closed set when reading the tool schema
- [ ] Run pytest → GREEN
- [ ] `ruff check`
- [ ] Commit: `feat(dream): brain_list_orphans_for_classification for Domain-node bridging`

#### Task B3 — `brain_assign_domain` MCP tool

File: `src/brain_v42/mcp/tools/dream_tools.py`.

Atomic: single call writes one BELONGS_TO_DOMAIN edge.

```python
@mcp.tool(version="1.0")
async def brain_assign_domain(entity_id: str, domain_name: str) -> str:
    """Assign an entity to a knowledge domain via BELONGS_TO_DOMAIN edge.

    Called by the Dream CONNECT phase after local classification. Writes
    atomically: Domain node is upserted, then the edge is created.

    Args:
        entity_id: UUID of any entity present in the graph.
        domain_name: Must be ∈ ALLOWED_DOMAINS =
            {infra, ml, backend, memory, tooling, data, ops, frontend, security}.
            Invalid names are rejected with a clear error (no silent polluting).

    Returns:
        One of: "created" (new edge), "matched" (edge already existed),
        "invalid_domain" (domain_name not in ALLOWED_DOMAINS),
        "invalid_entity_id" (not a valid UUID), "error" (write failed).
    """
    if graph_service is None:
        return format_error("Neo4j graph not configured")
    try:
        eid = UUID(entity_id)
    except (ValueError, AttributeError):
        return "invalid_entity_id"
    ok = await graph_service.upsert_domain(domain_name)
    if not ok:
        return "invalid_domain"
    # Find Domain node UUID-less target: we match by name, not id. Special-case
    # create_relation query that targets (d:Domain {name: $domain}) not
    # a generic {id: $target_id} node. Implement as a dedicated method
    # GraphService.link_entity_to_domain(entity_id, domain_name) returning
    # Literal["created","matched","error"].
    outcome = await graph_service.link_entity_to_domain(eid, domain_name)
    logger.info(
        "mcp.brain_assign_domain",
        entity_id=entity_id, domain=domain_name, outcome=outcome,
    )
    return outcome
```

Additional `GraphService.link_entity_to_domain` method (added in Task B1 or here):

```python
async def link_entity_to_domain(
    self, entity_id: UUID, domain_name: str
) -> RelationWriteOutcome:
    """Create BELONGS_TO_DOMAIN edge. Domain must already be upserted."""
    query = (
        "MATCH (e {id: $entity_id}) "
        "MATCH (d:Domain {name: $domain_name}) "
        "MERGE (e)-[r:BELONGS_TO_DOMAIN]->(d)"
    )
    return await self._run_counted(
        query, {"entity_id": str(entity_id), "domain_name": domain_name}
    )
```

Tests: `tests/unit/test_brain_assign_domain.py`. Mock graph_service; cover: happy path (`created`), idempotent second call (`matched`), invalid domain, invalid UUID, upsert_domain False.

Checklist:
- [ ] Write failing tests
- [ ] Implement tool + `link_entity_to_domain` method (can land in same commit as B1 if scope allows)
- [ ] Run pytest → GREEN
- [ ] `ruff check`
- [ ] Commit: `feat(dream): brain_assign_domain writes BELONGS_TO_DOMAIN atomically`

#### Task B4 — Update CONNECT prompt (Step B, agent-driven classification)

File: `scripts/dream/phase_connect.md`. Extend to a 2-step flow. Full updated prompt:

```md
You are the Dream Agent. You execute phase CONNECT autonomously.

## Mode
- Project scope: {{PROJECT_KEY}}
- Date: {{DATE}}
- Dry run: {{DRY_RUN}}

## Task
Step A — Backfill RELATED_TO graph links for entities missing connections (cosine similarity).
Step B — Bridge the cross-domain cosine ceiling by assigning remaining orphans to abstract Domain nodes.

## Step A — Cosine pass
1. If this is a dry run, skip brain_backfill_links_batch and report "Dry run — would backfill links."
2. Call `brain_backfill_links_batch(limit=50, threshold=0.6, max_links=3)`.
3. If entities were processed, call again up to 3 more times (4 calls total = 200 entities max).
4. Note the returned summary line:
   `"Backfill complete: entities_processed=N created=X matched=Y skipped=Z errors=E freshness=0.XX"`
   Aggregate these across the calls.

## Step B — Domain pass (non-cosine bridging)
5. If dry run, skip this step.
6. Call `brain_list_orphans_for_classification(limit=20)`. Parse JSON.
7. For each orphan, assign it to 1 domain from this closed set:
   `{infra, ml, backend, memory, tooling, data, ops, frontend, security}`
   Rules: use `topic`, `tags`, `project_key` as signal. If uncertain, pick `backend`.
   Do NOT invent new domain names — they will be rejected.
8. For each (entity_id, domain_name) pair, call `brain_assign_domain(entity_id, domain_name)`.
9. Aggregate the outcomes into a domain summary.

## Output
Print BOTH summaries to stdout, each on its own line:
```
STEP_A: entities_processed=N created=X matched=Y skipped=Z errors=E freshness=0.XX
STEP_B: orphans_listed=M assigned=A matched=B invalid=C errors=D
```
The orchestrator captures them.

Do NOT call brain_learn.

## Allowed tools
- brain_backfill_links_batch
- brain_list_orphans_for_classification
- brain_assign_domain

## Guardrails
- Max 4 calls to brain_backfill_links_batch (200 entities / 5 min).
- Max 1 call to brain_list_orphans_for_classification.
- Max 20 calls to brain_assign_domain (= orphans returned by listing).
- Stay inside the closed domain set; if classification is ambiguous, pick the single best domain.
```

Checklist:
- [ ] Replace `scripts/dream/phase_connect.md` with the updated content
- [ ] Commit: `docs(dream): CONNECT runs agent-driven domain-bridging Step B`

---

### Batch C — Graph-visible MCP tools (Angle 4) — ~3 days

**Dependency**: none (independent of A and B). Can run in parallel with B if subagent-driven-development dispatches them concurrently — BUT final commit order should still be A → B → C because C's prompt updates assume B's tools exist.

#### Task C1 — `GraphService.get_path`

File: `src/brain_v42/services/graph_service.py`.

```python
# Default exclusion: BELONGS_TO_DOMAIN would otherwise make every pair of
# entities trivially 2-hop reachable via their shared Domain node once
# Batch B lands, defeating the purpose of brain_graph_path.
_DEFAULT_PATH_EXCLUDES: tuple[str, ...] = ("BELONGS_TO_DOMAIN",)


async def get_path(
    self,
    source_id: UUID,
    target_id: UUID,
    max_depth: int = 3,
    rel_types: list[str] | None = None,
    exclude_rel_types: list[str] | None = None,
) -> list[dict]:
    """Return shortest path from source to target, up to max_depth hops.

    rel_types: Whitelist of relation types to traverse. None = all types
        except the defaults in _DEFAULT_PATH_EXCLUDES.
    exclude_rel_types: Extra exclusions beyond the default (additive).
        Ignored when rel_types is explicitly set.

    Uses Neo4j's shortestPath. Returns an ordered list of hops:
        [{"id": uuid, "type": "Decision", "label": "...",
          "rel_to_next": "RELATED_TO"}, ...]
    Empty list if no path exists within max_depth.
    """
    depth = max(1, min(6, max_depth))

    if rel_types is not None:
        rel_filter = ":" + "|".join(rel_types)
    else:
        excludes = set(_DEFAULT_PATH_EXCLUDES)
        if exclude_rel_types:
            excludes.update(exclude_rel_types)
        # Build a whitelist of "all known rel types minus excludes" at query
        # time — cleaner than a post-filter. Neo4j supports only whitelist
        # syntax in variable-length patterns, so we enumerate.
        all_rels = {
            "SUPERSEDES", "MOTIVATED_BY", "IMPLEMENTS", "DOCUMENTS",
            "USES", "RELATED_TO", "CONTAINS", "DEPENDS_ON",
            "BELONGS_TO", "MERGED_INTO", "BELONGS_TO_DOMAIN",
        }
        allowed = all_rels - excludes
        rel_filter = ":" + "|".join(sorted(allowed))

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
    if not rows:
        return []
    nodes = rows[0]["nodes"]
    rels = rows[0]["rels"]
    for i, rel in enumerate(rels):
        nodes[i]["rel_to_next"] = rel
    return nodes
```

Tests: `tests/integration/test_graph_service_path.py`. Needs live Neo4j; create A→B→C chain via RELATED_TO and assert `get_path(A, C, max_depth=3)` returns 3 nodes.

Checklist:
- [ ] Write failing test
- [ ] Implement `get_path`
- [ ] `ruff check`
- [ ] Commit: `feat(graph): GraphService.get_path — shortestPath traversal`

#### Task C2 — `brain_graph_path` MCP tool

File: `src/brain_v42/mcp/tools/brain_tools.py`.

Register inside the existing `if graph_svc is not None:` block, right after `brain_get_neighbors`:

```python
@mcp.tool(version="1.0")
async def brain_graph_path(
    source_id: str, target_id: str, max_depth: int = 3
) -> str:
    """Return the shortest graph path between two entities (1-6 hops).

    Useful for Dream SYNTH/PROMOTE to discover how two seemingly unrelated
    entities are connected through the knowledge graph — e.g., via a
    SUPERSEDES chain, through a shared Domain node, or through transitive
    MOTIVATED_BY/IMPLEMENTS links.

    Args:
        source_id: UUID of the start entity.
        target_id: UUID of the end entity.
        max_depth: Maximum hops, clamped to [1, 6]. Default 3.

    Returns:
        Markdown-formatted path. Empty message when no path found.
    """
    try:
        sid = UUID(source_id)
        tid = UUID(target_id)
    except (ValueError, AttributeError):
        return format_error("Invalid entity UUID")
    clamped = max(1, min(6, max_depth))
    path = await graph_svc.get_path(sid, tid, max_depth=clamped)
    logger.info(
        "mcp.brain_graph_path",
        source_id=source_id, target_id=target_id,
        depth=clamped, hops=len(path) - 1 if path else 0,
    )
    if not path:
        return f'No path found between "{short_id(source_id)}" and "{short_id(target_id)}" within depth {clamped}.'
    return format_graph_path(path)  # new formatter helper
```

New formatter in `src/brain_v42/mcp/tools/formatters.py`:

```python
def format_graph_path(path: list[dict]) -> str:
    """Render a graph path as '[Type] Label --REL--> [Type] Label ...'"""
    if not path:
        return "(empty path)"
    parts = []
    for i, node in enumerate(path):
        parts.append(f'[{node["type"]}] {node.get("label") or "untitled"}')
        if "rel_to_next" in node and i < len(path) - 1:
            parts.append(f'--{node["rel_to_next"]}-->')
    return " ".join(parts)
```

Tests: `tests/unit/mcp/tools/test_brain_graph_path.py`. Mock graph_svc.

Checklist:
- [ ] Write failing test
- [ ] Implement tool + formatter
- [ ] Run pytest → GREEN
- [ ] `ruff check`
- [ ] Commit: `feat(mcp): brain_graph_path — bounded shortest-path traversal tool`

#### Task C3 — Update SYNTH + PROMOTE prompts

Files: `scripts/dream/phase_synth.md`, `scripts/dream/phase_promote.md`.

In both prompts, locate the "Allowed tools" / "Tools available" section and append:

```
- brain_get_neighbors(entity_id, depth=1|2|3, rel_types=None) — local neighborhood (existing tool)
- brain_graph_path(source_id, target_id, max_depth=3, rel_types=None) — shortest path between two entities (new)
```

Important: the existing tool is `brain_get_neighbors` (not `brain_graph_neighbors`) — use the exact name registered at `brain_tools.py:208`. New tool is `brain_graph_path` (with optional `rel_types` filter that defaults to all types except `BELONGS_TO_DOMAIN`, so domain-mediated paths don't mask direct semantic ones).

Add one sentence in the task description explaining when to use them (e.g., SYNTH: "Before proposing a meta-insight that spans clusters, use brain_graph_path to confirm the entities are topologically related, not just co-occurring by keyword").

Checklist:
- [ ] Edit both prompts
- [ ] Commit: `docs(dream): SYNTH/PROMOTE prompts expose brain_graph_* tools`

---

### Final Batch — wire, test end-to-end, review

#### Task Z1 — Confirm wiring of the new MCP tools in `server.py`

File: `src/brain_v42/mcp/server.py`.

No `DomainClassifier` service to wire (the Dream agent classifies). The only wiring work:
- `brain_graph_path` auto-registers inside `brain_tools.register_tools` when `graph_svc` present (existing pattern).
- `brain_list_orphans_for_classification` and `brain_assign_domain` auto-register inside `register_dream_tools` when `graph_service` present.
- Confirm the existing `register_dream_tools(mcp, *, session_factory, auto_linker, graph_service)` signature needs no new arg.

Tests: add an assertion in an existing integration wiring test (or a minimal new one) that when `graph_enabled=true`, the 3 new tool names appear in the tool registry.

Checklist:
- [ ] Verify tool registration smoke test
- [ ] Run pytest → GREEN
- [ ] `ruff check`
- [ ] Commit (only if changes): `feat(mcp): wire brain_graph_path + domain tools into server builder`

#### Task Z2 — Full suite + coverage check

```bash
BRAIN_V42_TEST_DB_URL="postgresql+asyncpg://brain:brain@localhost:5433/brain_test" \
  uv run pytest tests/unit --cov=brain_v42 --cov-report=term-missing
```

Assert overall coverage ≥60% (project gate).

Checklist:
- [ ] Run full suite
- [ ] Fix any regressions
- [ ] If coverage drops below 60% on new files, add targeted tests
- [ ] Commit any follow-up fixes

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| `SummaryCounters.relationships_created` unreliable on MERGE when `ON CREATE` writes props vs. not | Counter is the Neo4j driver's authoritative signal — confirmed by Neo4j docs. If it misfires, fallback: write `_created_at = timestamp()` on `ON CREATE` and query before/after node count — `_run_counted` can be swapped without touching callers |
| DomainClassifier heuristic misclassifies common orphans | Initial ship is fallback-only (no LLM). Observe in Night 5 what gets assigned; if ≥30% false-domain rate, wire real LLM in follow-up. Metric to watch: % of orphans reaching 2-hop reachability after CONNECT Step B |
| `brain_graph_path` LLM abuse (deep traversals, huge outputs) | Depth clamped [1,6]; formatter output bounded; `LIMIT 1` in Cypher (shortestPath returns at most one) |
| Existing `auto_link` callers break on new return type | Full-repo grep at Task A3 start; update all call sites (dream_tools.py line 145, tests/unit/test_auto_linker.py indexing) and tests in the SAME commit as the signature change. No `__len__` shim. |
| Neo4j test environment unavailable in CI | Mark integration tests with `pytest.skip` when Neo4j unreachable; unit tests via mocks cover the logic |

## Deliverable summary

- Feature branch `feat/graph-hardening` merged to main.
- 10-12 commits (1 per task), each TDD-shaped (RED → GREEN → REFACTOR).
- **3 new MCP tools**: `brain_graph_path`, `brain_list_orphans_for_classification`, `brain_assign_domain`. Existing `brain_backfill_links_batch` and `brain_get_neighbors` keep their names; `brain_backfill_links_batch` gains a 4-bucket return format.
- CONNECT phase emits 4-bucket link totals (Step A) + runs an agent-driven domain-bridging pass (Step B).
- SYNTH/PROMOTE prompts know about `brain_graph_path` and `brain_get_neighbors` for multi-hop reasoning.
- **No new Python dependency**: the Dream agent IS the domain classifier (Graphiti pattern), so no Anthropic SDK wiring, no inline LLM call, no budget guard in server code.
- Full pytest green; coverage ≥60%; ruff clean.
- `brain_log_decision` entry for the ship + `brain_update_project_focus`.
- Post-merge: next Dream run (2026-04-25 06:01 CEST) validates Step A 4-bucket reporting AND Step B orphan-assignment live.
