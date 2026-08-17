# Dream Mode — Design Spec

**Date:** 2026-04-05
**Project:** brain-v42
**Status:** Draft (post-critique revision)

> **Historical note (2026-07-13):** This document preserves the Claude-era design. A [provider-aware migration design](../../../.specs/plans/dream-codex-agent-migration.design.md) now covers all six agent phases: Claude OTEL remains the rollback and historical path, while Codex uses JSONL. ROADMAP and EXTRACT remain outside this migration. This note does not indicate that live deployment is complete.

## Problem

brain-v42 has 1300+ entities across 5 types (1109 learnings, 192 decisions, 29 snippets, 12 runbooks, 1 ADR). Existing maintenance is fragmented:

- DecayFlusher runs in-process but only updates freshness scores
- ConsolidationJob detects duplicates but doesn't auto-merge
- AutoLinker creates graph edges only at entity creation (920+ pre-existing entities have zero links)
- No cross-entity synthesis, no tag normalization, no reorganization

The brain accumulates knowledge but never consolidates it. Dream Mode is the brain's sleep cycle — a nightly autonomous process that cleans, connects, synthesizes, and reorganizes.

## Architecture: Dream Phases

An external bash orchestrator (`scripts/dream.sh`) sequences 5 phases. Each phase is a `claude -p` headless run with a dedicated prompt. Phases communicate via the brain itself — each writes a tagged report that the next phase reads.

```
[Cron 3am] → dream.sh
  Phase 1 — SCAN    (Sonnet)  read-only audit
  Phase 2 — CLEAN   (Sonnet)  merge duplicates, delete dead entities
  Phase 3 — CONNECT (Sonnet)  backfill graph links
  Phase 4 — SYNTH   (Opus)    generate insights from clusters
  Phase 5 — REORG   (Opus)    normalize tags, fix project_keys
```

### Why external agent, not in-process

- Volume too large for a single context window — phases scope the work
- Synthesis and reorganization require LLM judgment, not SQL
- `claude -p` uses subscription (zero marginal cost), not API tokens
- Phases can be retried independently on failure

### Context passing between phases

No temp files. The brain is its own inter-run memory:

```
Phase N writes:  brain_learn(tags=["dream:scan:2026-04-05"])
Phase N+1 reads: brain_search(tags=["dream:scan:2026-04-05"])
```

Historical dream reports accumulate, enabling delta tracking (compare today's scan with yesterday's).

**Phase failure resilience:** If a phase fails, subsequent phases still run. Each phase prompt includes a directive: "If no report from the previous phase is found (brain_search returns empty), proceed with your own direct queries — do not abort." This means REORG can still run even if SCAN failed, by calling `brain_decay_status` and `brain_list` directly instead of reading the SCAN report.

**Idempotency:** CLEAN is safe to retry — `brain_merge_entities` checks that both source and target exist before merging. Already-merged entities (source archived with `merged_into` set) are excluded by `brain_consolidation_candidates` which filters `merged_into IS NULL`. CONNECT is idempotent — `AutoLinker` uses `MERGE` in Neo4j for edge creation, so re-running on already-linked entities is a no-op.

## Phase Specifications

### Phase 1 — SCAN (Sonnet, ~5min)

**Purpose:** Read-only audit of brain state. Produces a status report.

**MCP tools used (all existing):**
- `brain_decay_status` — freshness stats per type
- `brain_consolidation_candidates(limit=20)` — duplicate pairs
- `brain_list(entity_type=X, project_key=Y)` — volume per project
- `brain_search(tags=["dream:scan"], limit=1)` — previous scan for delta

**Output:** `brain_learn` with:
- topic: `"Dream Scan — {DATE}"`
- tags: `["dream:scan", "dream:scan:{DATE}"]`
- project_key: `"brain-v42"`
- source_type: `"automated"` (requires adding `"automated"` to `SourceType` Literal in `models/learning.py`)

**Report content:**
- Entity counts by type and project_key
- Freshness distribution (fresh/stale/archived)
- Consolidation candidates count and top pairs
- Entities missing project_key
- Duplicate/near-duplicate tag variants
- Delta vs previous scan

**Guardrails:** SCAN modifies nothing. Zero writes except its own report.

### Phase 2 — CLEAN (Sonnet, ~5min)

**Purpose:** Merge confirmed duplicates, delete dead entities.

**MCP tools used (all existing):**
- `brain_search(tags=["dream:scan:{DATE}"])` — read SCAN report
- `brain_get(entity_type, entity_id)` — inspect candidates before merging
- `brain_merge_entities(entity_type, source_id, target_id)` — merge duplicates
- `brain_delete(entity_type, entity_id)` — remove deletion candidates

**Decision logic:**
- Similarity >= 0.95: auto-merge (source into target, keep the older one)
- Similarity 0.92–0.95: log in report as "flagged for human review", do NOT merge
- Archived >180 days + access_count=0: auto-delete
- Stale entities: log only, no action (decay system handles transitions)
- Old dream reports: delete `dream:scan:*`, `dream:clean:*`, `dream:connect:*`, `dream:synth:*`, `dream:reorg:*` reports older than 30 days (keeps history bounded at ~150 entries)

**Output:** `brain_learn` with tags `["dream:clean", "dream:clean:{DATE}"]`

**Guardrails:**
- Max 10 merges per run (prevent cascade damage)
- Max 5 deletes per run
- Never merge across different project_keys
- Never merge different entity types
- Never merge or delete entities tagged `dream:generated` (those require human review to promote or discard)
- Never merge or delete dream reports (entities tagged `dream:scan`, `dream:clean`, etc.)

### Phase 3 — CONNECT (Sonnet, ~8min)

**Purpose:** Backfill RELATED_TO graph edges for entities created before AutoLinker.

**MCP tools used:**
- `brain_search(tags=["dream:clean:{DATE}"])` — read CLEAN report
- **`brain_backfill_links_batch`** (NEW) — batch AutoLinker on existing entities

**New MCP tool: `brain_backfill_links_batch`**

```python
@mcp.tool(version="1.0")
async def brain_backfill_links_batch(
    project_key: str | None = None,
    entity_type: str | None = None,
    limit: int = 50,
    threshold: float = 0.6,
    max_links: int = 3,
) -> str:
    """Batch-create RELATED_TO links for entities missing graph connections.

    Finds entities that have embeddings but no RELATED_TO edges in Neo4j,
    then runs AutoLinker.auto_link() on each.

    Args:
        project_key: Scope to one project. None = all.
        entity_type: Scope to one type. None = all.
        limit: Max entities to process per call.
        threshold: Min cosine similarity for link creation.
        max_links: Max links per entity.

    Returns:
        Summary: entities processed, links created, errors.
    """
```

**Implementation (3-step process):**

1. **Find unlinked entities (Neo4j + PG fallback):** Query Neo4j for entity nodes with zero RELATED_TO edges via a new `GraphService.find_unlinked_nodes()` method. For entities created before graph sync (no Neo4j node at all), also query PG for entities with embeddings and cross-reference against Neo4j to find those missing entirely.
2. **Fetch embeddings from PG:** For each unlinked entity ID, query the corresponding PG table (`decisions`, `learnings`, etc.) to retrieve the stored `embedding` column. This is necessary because `AutoLinker.auto_link()` requires `embedding: list[float]` as a parameter — it returns `[]` if embedding is `None`.
3. **Call `AutoLinker.auto_link()`** with `(entity_type, entity_id, embedding, threshold, max_links)` for each entity. AutoLinker handles the UNION vector search across all tables + Neo4j edge creation.

**New `GraphService` method required:**
```python
async def find_unlinked_nodes(
    self, entity_type: str | None = None, limit: int = 50
) -> list[str]:
    """Return entity IDs that have a node but zero RELATED_TO edges."""
    query = """
        MATCH (n)
        WHERE NOT (n)-[:RELATED_TO]-()
        AND ($type IS NULL OR $type IN labels(n))
        RETURN n.id AS id
        LIMIT $limit
    """
    rows = await self._run_read(query, {"type": entity_type, "limit": limit})
    return [row["id"] for row in rows]
```

**Output:** `brain_learn` with tags `["dream:connect", "dream:connect:{DATE}"]`

**Guardrails:**
- limit=50 per call to avoid overloading Neo4j
- Agent calls the tool multiple times if needed (up to 200 entities per phase)
- Threshold 0.6 matches AutoLinker default

### Phase 4 — SYNTH (Opus, ~10min)

**Purpose:** Generate high-level insights by analyzing clusters of related entities.

**MCP tools used:**
- `brain_search(tags=["dream:connect:{DATE}"])` — read CONNECT report
- **`brain_get_clusters`** (NEW) — find connected components in Neo4j
- `brain_get(entity_type, entity_id)` — read entities in a cluster
- `brain_learn` — write generated insights
- `brain_save_snippet` — if reusable patterns emerge

**New MCP tool: `brain_get_clusters`**

```python
@mcp.tool(version="1.0")
async def brain_get_clusters(
    project_key: str | None = None,
    min_size: int = 3,
    limit: int = 10,
) -> str:
    """Find clusters of related entities in the knowledge graph.

    Uses Neo4j connected-component traversal on RELATED_TO edges.
    Returns clusters sorted by size (largest first), with member
    entity IDs, types, and titles.

    Args:
        project_key: Scope to one project. None = all.
        min_size: Minimum cluster size to return.
        limit: Maximum clusters to return.

    Returns:
        Formatted cluster list with member details.
    """
```

**Implementation: Python-side union-find** (chosen over pure Cypher — simpler to test, no APOC dependency, works identically regardless of Neo4j version).

1. **Fetch all RELATED_TO edges from Neo4j** via a new `GraphService.get_all_related_edges()` method:
```python
async def get_all_related_edges(
    self, project_key: str | None = None
) -> list[tuple[str, str]]:
    """Return all (source_id, target_id) pairs for RELATED_TO edges."""
    query = """
        MATCH (a)-[:RELATED_TO]-(b)
        WHERE a.id < b.id
        RETURN DISTINCT a.id AS src, b.id AS tgt
    """
    rows = await self._run_read(query, {})
    return [(row["src"], row["tgt"]) for row in rows]
```
2. **Union-find in Python** to compute connected components from the edge list.
3. **Enrich with PG metadata** — for each cluster, query PG tables to get entity type + title/topic.
4. **Filter** by `min_size` and `project_key`, sort by size DESC, return top `limit`.

At current scale (~1300 entities, ~400 edges post-backfill), this completes in <1 second.

**Synthesis logic (Opus judgment):**
- For each cluster with 3+ members: read all entities, identify the theme
- If a cross-cutting pattern emerges: create a `brain_learn` with tag `dream:insight`
- If a reusable code pattern emerges: create a `brain_save_snippet`
- If conflicting decisions exist in a cluster: flag in report

**Output:**
- `brain_learn` with tags `["dream:synth", "dream:synth:{DATE}"]` — phase report
- 0-3 `brain_learn` with tags `["dream:insight", "dream:generated"]` — synthesized insights

**Guardrails:**
- Max 3 generated insights per run
- All generated entities tagged `dream:generated` for human review
- Never modify existing entities
- Never generate decisions or ADRs (those require human intent)

### Phase 5 — REORG (Opus, ~10min)

**Purpose:** Normalize metadata — fix missing project_keys, deduplicate tags, standardize naming.

**MCP tools used (all existing):**
- `brain_search(tags=["dream:synth:{DATE}"])` — read SYNTH report
- `brain_search(tags=["dream:scan:{DATE}"])` — re-read SCAN for anomalies
- `brain_list(entity_type=X)` — find entities to fix
- `brain_get(entity_type, entity_id)` — inspect before updating
- `brain_update(entity_type, entity_id, ...)` — fix metadata

**Reorg actions:**
- Assign project_key to orphan entities (infer from content + tags)
- Normalize tag variants (e.g., `Python` → `python`, `red-lab` / `redlab` → `red-lab`)
- Remove meaningless tags (single-use tags that add no value)
- Fix entity_type mismatches (e.g., a decision stored as a learning)

**Output:** `brain_learn` with tags `["dream:reorg", "dream:reorg:{DATE}"]`

**Guardrails:**
- Max 20 updates per run
- Never change content (title, description, insight, reasoning)
- Only change: tags, project_key
- Each change logged in report with reasoning
- entity_type changes are flagged only, never auto-applied (would require delete+recreate)

## Orchestrator: `scripts/dream.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DREAM_DIR="$SCRIPT_DIR/dream"
LOG_DIR="$SCRIPT_DIR/../logs/dream"
PROJECT_KEY="${1:-all}"
DRY_RUN="${DRY_RUN:-false}"
TIMESTAMP=$(date +%Y-%m-%d)

mkdir -p "$LOG_DIR"

# Phase definitions: name:model:timeout_minutes:max_turns
PHASES=(
  "scan:sonnet:5:20"
  "clean:sonnet:5:25"
  "connect:sonnet:8:30"
  "synth:opus:10:30"
  "reorg:opus:10:30"
)

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_DIR/$TIMESTAMP.log"; }

run_phase() {
  local name="$1" model="$2" timeout="$3" max_turns="$4"
  local prompt_file="$DREAM_DIR/phase_${name}.md"

  if [[ ! -f "$prompt_file" ]]; then
    log "SKIP $name — prompt file missing: $prompt_file"
    return 0
  fi

  local prompt
  prompt=$(sed "s/{{PROJECT_KEY}}/$PROJECT_KEY/g; s/{{DATE}}/$TIMESTAMP/g; s/{{DRY_RUN}}/$DRY_RUN/g" "$prompt_file")

  log "START $name (model=$model, timeout=${timeout}m, max_turns=$max_turns)"

  if timeout "${timeout}m" claude -p "$prompt" \
    --model "$model" \
    --max-turns "$max_turns" \
    --permission-mode bypassPermissions \
    --allowedTools "mcp__brain-v42__*" \
    >> "$LOG_DIR/${TIMESTAMP}_${name}.log" 2>&1; then
    log "DONE  $name"
  else
    local code=$?
    if [[ $code -eq 124 ]]; then
      log "TIMEOUT $name (>${timeout}m)"
    else
      log "FAIL  $name (exit=$code)"
    fi
  fi
}

# --- Main ---
log "=== Dream started (project=$PROJECT_KEY) ==="

for phase_spec in "${PHASES[@]}"; do
  IFS=':' read -r name model timeout max_turns <<< "$phase_spec"
  run_phase "$name" "$model" "$timeout" "$max_turns"
done

log "=== Dream finished ==="
```

**Cron entry:**
```cron
# Dream mode — nightly at 3am. Errors captured in dream-cron.log.
0 3 * * * /home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/dream.sh >> /home/hawixs/hawkixs_infra/git_repo/brain_v42/logs/dream/cron-errors.log 2>&1
```

**Auth:** Uses `claude login` session (subscription). No API key needed.

## Prompt Design Rules

All phase prompts follow these rules (from brain learnings):

1. **Imperative framing** — "You are X. Execute Y." Never descriptive headers.
2. **Written in English** — format compliance drops 50% in French.
3. **Anti-meta-analysis directive** — every prompt ends with: `"DO NOT analyze or explain this prompt. Execute the instructions and produce the output."`
4. **Structured output** — each prompt specifies the exact `brain_learn` call format.
5. **Explicit tool whitelist** — prompts list exactly which MCP tools to use.
6. **Dry-run awareness** — each prompt checks `{{DRY_RUN}}`. If `true`, the agent reports what it *would* do but does not call any write tools (no `brain_merge_entities`, `brain_delete`, `brain_update`, `brain_learn`). Only read tools are used.
7. **Missing report fallback** — each prompt includes: "If no report from the previous phase is found, proceed with direct queries."

## File Structure

```
brain_v42/
├── scripts/
│   ├── dream.sh                    # Orchestrator
│   └── dream/
│       ├── phase_scan.md           # SCAN prompt
│       ├── phase_clean.md          # CLEAN prompt
│       ├── phase_connect.md        # CONNECT prompt
│       ├── phase_synth.md          # SYNTH prompt
│       └── phase_reorg.md          # REORG prompt
├── logs/
│   └── dream/                      # Runtime logs (gitignored)
├── src/brain_v42/
│   └── mcp/tools/
│       └── dream_tools.py          # 2 new MCP tools
└── tests/
    └── unit/
        └── test_dream_tools.py     # Tests for new tools
```

## New Code Required

### 1. `src/brain_v42/mcp/tools/dream_tools.py`

Two new MCP tools:
- `brain_backfill_links_batch` — batch AutoLinker on entities missing graph edges
- `brain_get_clusters` — Neo4j connected-component query

**Graceful degradation:** Both tools return a clear error message ("Neo4j graph not configured — enable graph_enabled=true in settings") when `graph_service is None` or `auto_linker is None`. This follows the project pattern (all graph-dependent features degrade gracefully).

### 2. Add `auto_linker` to `build_services()` return dict in `server.py`

Currently `auto_linker` is created as a local variable in `build_services()` (line 118) but not returned. Add it to the returned dict:

```python
# In build_services() return dict, add:
"auto_linker": auto_linker,
```

### 3. Wire dream tools into `server.py`

```python
from brain_v42.mcp.tools.dream_tools import register_dream_tools

register_dream_tools(
    mcp,
    session_factory=get_session_factory(),
    auto_linker=services.get("auto_linker"),
    graph_service=services.get("graph_service"),
)
```

### 3. `scripts/dream.sh` + `scripts/dream/phase_*.md`

Orchestrator + 5 prompt files.

### 4. Tests

- Unit tests for `brain_backfill_links_batch` (mock AutoLinker, verify batch logic)
- Unit tests for `brain_get_clusters` (mock GraphService, verify Cypher + formatting)

## Prerequisite Changes to Existing Code

These are small, targeted changes to existing files (not new files):

| File | Change | Why |
|---|---|---|
| `src/brain_v42/models/learning.py` | Add `"automated"` to `SourceType` Literal | Dream reports need a distinct source_type |
| `src/brain_v42/mcp/server.py` | Add `"auto_linker": auto_linker` to `build_services()` return dict | Dream tools need access to the AutoLinker instance |
| `src/brain_v42/services/graph_service.py` | Add `find_unlinked_nodes()` and `get_all_related_edges()` methods | Dream tools need these graph queries |

## Reuse of Existing Code

| Existing component | How Dream uses it |
|---|---|
| `AutoLinker.auto_link()` | Called by `brain_backfill_links_batch` (with embeddings fetched from PG) |
| `GraphService` (new methods) | `find_unlinked_nodes()` for CONNECT, `get_all_related_edges()` for SYNTH clusters |
| `ConsolidationJob.find_candidates()` | Called by SCAN phase via existing `brain_consolidation_candidates` tool |
| `DecayCalculator` | Not called directly — SCAN reads `brain_decay_status` which uses it |
| All CRUD tools | Used by CLEAN, REORG phases for reads/updates/deletes |

Existing code is minimally modified (3 files, additive changes only). Two new tool files are added.

## Guardrail Summary

| Guardrail | Phase | Purpose |
|---|---|---|
| SCAN is read-only | SCAN | No accidental modifications during audit |
| Merge only >= 0.95 similarity | CLEAN | Avoid false-positive merges |
| Max 10 merges per run | CLEAN | Limit blast radius |
| Max 5 deletes per run | CLEAN | Limit blast radius |
| Never merge cross-project | CLEAN | Semantic similarity ≠ same project |
| Never merge/delete `dream:generated` | CLEAN | Synthetic entities need human review |
| Never merge/delete dream reports | CLEAN | Preserve audit trail |
| Prune dream reports >30 days | CLEAN | Prevent unbounded growth (~150 cap) |
| limit=50 per backfill call | CONNECT | Protect Neo4j from batch overload |
| Max 3 generated insights | SYNTH | Prevent insight spam |
| All generated entities tagged `dream:generated` | SYNTH | Human review gate |
| Never generate decisions/ADRs | SYNTH | Decisions require human intent |
| Max 20 metadata updates | REORG | Small steps, no big bang |
| Never change content fields | REORG | Only tags/project_key |
| `--max-turns` per phase | All | Prevent infinite loops |
| `timeout` per phase | All | Hard time limit |
| Dry-run mode (`DRY_RUN=true`) | All | Report-only, no writes |
| Graceful degradation (no graph) | CONNECT, SYNTH | Clear error if Neo4j not configured |
| Missing report fallback | All | Phase proceeds with direct queries if previous phase report absent |
