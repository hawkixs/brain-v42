# Memory Decay, Lifecycle & Consolidation — Design Spec

**Date:** 2026-03-13
**Project:** brain_v42
**Status:** Approved

## Overview

Add self-organizing memory capabilities to brain_v42: time-aware search scoring (decay), lifecycle management (stale/archived), and consolidation (duplicate detection + merge). The system currently ranks results purely by semantic relevance — this adds temporal and usage signals.

## Motivation

With ~500 entities and growing, the brain returns old/irrelevant results alongside fresh ones. Core operational knowledge (ADRs, runbooks) should age slowly while context-specific learnings and snippets should decay faster. No cleanup mechanism exists today.

**Research basis:** EverMemOS (2026) — decay score (recency + frequency + relevance), consolidation (merge redundant learnings), active forgetting (distill low-utility into semantic memory). Core operational knowledge decays slowly, context-specific decays faster.

## Architecture — Approach 1: Decay Integrated at Search Time

Decay score is computed **at search time** using aggregated access statistics. A flusher periodically aggregates raw access logs into per-entity counters.

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  MCP Tools   │────▶│  access_log  │     │  DecayFlusher   │
│ (log access) │     │ (insert-only)│◀────│ (every 5 min)   │
└─────────────┘     └──────────────┘     │                 │
                                          │ 1. aggregate    │
┌─────────────┐     ┌──────────────┐     │ 2. update status│
│HybridSearcher│◀───│ decay_score  │◀────│ 3. purge old    │
│ (search time)│    │  (computed)  │     └─────────────────┘
└─────────────┘     └──────────────┘
                                          ┌─────────────────┐
                                          │ ConsolidationJob │
                                          │ (every 6 hours) │
                                          │ detect dupes    │
                                          └─────────────────┘
```

## Section 1: Access Log & Aggregation

### New table: `access_log`

```sql
CREATE TABLE access_log (
    id          BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(20) NOT NULL,  -- 'decision', 'learning', 'snippet', 'runbook', 'adr'
    entity_id   UUID NOT NULL,
    access_type VARCHAR(20) NOT NULL,  -- 'search_hit', 'get_by_id', 'use', 'execute'
    accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_access_log_entity ON access_log(entity_type, entity_id);
CREATE INDEX idx_access_log_time ON access_log(accessed_at);
```

### New columns on all 5 entity tables

```sql
ALTER TABLE decisions ADD COLUMN last_accessed_at TIMESTAMPTZ;
ALTER TABLE decisions ADD COLUMN access_count INTEGER DEFAULT 0;
ALTER TABLE decisions ADD COLUMN freshness_status VARCHAR(10) DEFAULT 'fresh';
-- CHECK (freshness_status IN ('fresh', 'stale', 'archived'))
-- Same for: learnings, snippets, runbooks, adrs
```

Note: snippets already have `last_used_at` and `use_count`. These are preserved as-is. `last_accessed_at` and `access_count` track a broader set of access patterns (search hits, get_by_id, etc.) while `last_used_at`/`use_count` remain specific to `brain_use_snippet`.

### DecayFlusher (every ~5 minutes)

Follows the existing MetricsFlusher pattern:

1. **Aggregate**: GROUP BY entity_type, entity_id from access_log → update `last_accessed_at = MAX(accessed_at)`, `access_count += COUNT(*)`
2. **Recalculate freshness_status** for each touched entity (see Section 2)
3. **Purge** access_log entries older than 30 days
4. **Log** transitions via structlog (e.g., "decision X: fresh → stale")

### When to log accesses

| Event | access_type | Where |
|-------|-------------|-------|
| Entity appears in search results | `search_hit` | BrainService / HybridSearcher |
| Entity fetched via get_by_id | `get_by_id` | Repository layer |
| `brain_use_snippet` called | `use` | SnippetService |
| `brain_execute_runbook` called | `execute` | RunbookService |

Access logging uses a **bounded in-memory queue** (`asyncio.Queue(maxsize=1000)`) drained by a background consumer in batches (matching the MetricsFlusher pattern). On queue full, new events are silently dropped. On DB errors, the consumer logs and discards the batch. Search latency must not be affected.

## Section 2: Decay Score — Formula & Profiles

### Composite formula

```
decay_multiplier = w_age * age_factor
                 + w_access * access_factor
                 + w_freq * frequency_factor
                 + w_valid * validation_factor
```

Each factor is normalized to [0.0, 1.0]:

| Factor | Calculation | Meaning |
|--------|-------------|---------|
| `age_factor` | `exp(-λ_age * days_since_created)` | Exponential decay from creation |
| `access_factor` | `exp(-λ_access * days_since_last_access)` | Decay from last access. Fallback: `last_accessed_at = created_at` when NULL (never-accessed entities decay from creation, not boosted) |
| `frequency_factor` | `min(access_count / baseline, 1.0)` | Normalized access frequency |
| `validation_factor` | `1.0` if validated/accepted, `0.7` otherwise | Boost for validated entities |

Where `λ = ln(2) / half_life_days` (standard exponential decay).

### Decay profiles by entity type

| Type | age half-life | access half-life | w_age | w_access | w_freq | w_valid | freq baseline |
|------|--------------|-----------------|-------|----------|--------|---------|---------------|
| decision | 180d | 90d | 0.3 | 0.3 | 0.2 | 0.2 | 10 |
| learning | 90d | 60d | 0.3 | 0.3 | 0.2 | 0.2 | 10 |
| snippet | 60d | 30d | 0.2 | 0.3 | 0.3 | 0.2 | 20 |
| runbook | 365d | 180d | 0.2 | 0.3 | 0.3 | 0.2 | 5 |
| adr | 730d | 365d | 0.1 | 0.2 | 0.2 | 0.5 | 3 |

**Rationale:** ADRs and runbooks are core operational knowledge — they age very slowly. Snippets are volatile (code evolves). Learnings are mid-range. Profiles are configurable via a dict in `config.py`.

### Integration with HybridSearcher

The raw `SearchResult.score` is **preserved as-is** (semantic relevance). The decay multiplier is applied as a separate field and used for **re-ranking only**:

```python
# Re-ranking with decay (score field unchanged for min_score compatibility)
effective_score = semantic_score * (decay_floor + (1 - decay_floor) * decay_multiplier)
```

Results are **sorted by `effective_score`** but `SearchResult.score` remains the raw semantic score. This avoids breaking existing `min_score` thresholds (e.g., 0.24 recommended). The `decay_multiplier` and `freshness_status` are added to the result item metadata.

`decay_floor = 0.3` — even a fully decayed entity retains 30% of its effective ranking. No silent disappearance.

When `decay_enabled = False` (config toggle), the HybridSearcher skips decay entirely: `effective_score = semantic_score`.

### Implementation location

New module: `src/brain_v42/services/decay.py`
- `DecayCalculator` class with `compute_multiplier(entity_type, created_at, last_accessed_at, access_count, validated_at)`
- `DecayProfiles` dataclass loaded from config
- Used by HybridSearcher at search time and by DecayFlusher for freshness_status

## Section 3: Lifecycle Management

### Freshness states

| State | Condition | Effect |
|-------|-----------|--------|
| `fresh` | decay_multiplier >= 0.5 | Normal behavior |
| `stale` | 0.2 <= decay_multiplier < 0.5 | Visible in results with stale tag, scoring penalty already applied |
| `archived` | decay_multiplier < 0.2 | Excluded from results by default |

**No automatic deletion.** Archival is a soft-filter at search time.

### Search behavior changes

- Default: `archived` entities excluded from results
- `stale` entities visible but with `"freshness": "stale"` in result metadata
- New optional parameter on `brain_search` and `brain_what_do_i_know_about`: `include_archived: bool = False`

### New MCP tools

| Tool | Action |
|------|--------|
| `brain_decay_status` | Returns stats: count of fresh/stale/archived per entity type |
| `brain_refresh_entity` | Forces `freshness_status = 'fresh'` + sets `last_accessed_at = NOW()` |

### DecayFlusher freshness update

After aggregating access_log, the flusher:
1. Computes `decay_multiplier` for each entity with changed access stats
2. Maps to freshness_status via thresholds
3. Updates `freshness_status` only if changed (avoids unnecessary writes)
4. Logs transitions: `structlog.info("freshness_transition", entity_type=..., entity_id=..., old=..., new=...)`

## Section 4: Consolidation & Active Forgetting

### Duplicate detection

The `ConsolidationJob` runs every ~6 hours (configurable):
1. For each entity type, use a SQL self-join with pgvector `<=>` operator: `SELECT a.id, b.id, 1 - (a.embedding <=> b.embedding) AS similarity FROM table a, table b WHERE a.id < b.id AND a.project_key = b.project_key AND 1 - (a.embedding <=> b.embedding) > 0.92`
2. Exclude pairs already in `consolidation_log` (previously merged or dismissed)
3. Store candidates for retrieval via MCP tool

**Scale note:** At ~500 entities per type, the self-join is fast (<1s). At >2000 entities, optimize by partitioning on `project_key` and only comparing entities created in the last N days against the full set. The job runs via `asyncio.to_thread` to avoid blocking the event loop.

### No automatic merge

Merge is a human/Claude decision. The system exposes candidates, it doesn't act.

### New MCP tools

| Tool | Action |
|------|--------|
| `brain_consolidation_candidates` | List quasi-duplicate pairs (similarity > 0.92) with preview. Params: `entity_type` (optional filter), `limit` |
| `brain_merge_entities` | Merge two entities of the same type. Params: `entity_type`, `source_id` (absorbed), `target_id` (kept). Keeps the target, absorbs tags/metadata from source, marks source `archived` with `merged_into = target_id` |

### New table: `consolidation_log`

```sql
CREATE TABLE consolidation_log (
    id            BIGSERIAL PRIMARY KEY,
    source_id     UUID NOT NULL,
    target_id     UUID NOT NULL,
    entity_type   VARCHAR(20) NOT NULL,
    similarity    FLOAT NOT NULL,
    action        VARCHAR(20) NOT NULL,  -- 'merged', 'dismissed'
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
```

Prevents re-proposing previously handled pairs.

### Active forgetting

Entities `archived` for more than 180 days with `access_count = 0` since archival are flagged as **deletion candidates** in `brain_decay_status` output. No auto-delete — signal only.

### Merge column

```sql
ALTER TABLE decisions ADD COLUMN merged_into UUID;
-- Same for: learnings, snippets, runbooks, adrs
-- No FK constraint: simplicity, and the target entity may itself be archived/merged later.
```

Entities with `merged_into IS NOT NULL` are treated as archived and excluded from search.

## Section 5: Sidecar Metrics

Essential metrics exposed by the metrics sidecar:

| Metric | Type | Description |
|--------|------|-------------|
| `brain_stale_entities_count` | gauge | Count of stale entities per type |
| `brain_archived_entities_count` | gauge | Count of archived entities per type |
| `brain_access_log_size` | gauge | Row count of access_log table |

Additional metrics (add later if needed for tuning):

| Metric | Type | Description |
|--------|------|-------------|
| `brain_decay_score_histogram` | histogram | Distribution of decay scores by entity type |
| `brain_consolidation_candidates_count` | gauge | Number of detected duplicate pairs |
| `brain_consolidation_merges_total` | counter | Total merges performed |

Note: `brain_decay_status` MCP tool already exposes full stats interactively. Prometheus metrics are for passive monitoring/alerting only.

## Database Migrations (Alembic)

**Migration 006:** Create `access_log` table
**Migration 007:** Add `last_accessed_at`, `access_count`, `freshness_status`, `merged_into` to all 5 entity tables
**Migration 008:** Create `consolidation_log` table

## New Files

| File | Purpose |
|------|---------|
| `src/brain_v42/services/decay.py` | DecayCalculator, DecayProfiles |
| `src/brain_v42/services/decay_flusher.py` | DecayFlusher (access_log aggregation + freshness updates) |
| `src/brain_v42/services/consolidation.py` | ConsolidationJob (duplicate detection) |
| `src/brain_v42/repositories/pg_access_log.py` | AccessLog repository |
| `src/brain_v42/repositories/pg_consolidation_log.py` | ConsolidationLog repository |
| `src/brain_v42/mcp/tools/decay_tools.py` | brain_decay_status, brain_refresh_entity, brain_consolidation_candidates, brain_merge_entities |
| `alembic/versions/006_access_log.py` | access_log table |
| `alembic/versions/007_decay_columns.py` | decay columns on entity tables |
| `alembic/versions/008_consolidation_log.py` | consolidation_log table |
| `tests/unit/services/test_decay.py` | DecayCalculator unit tests |
| `tests/unit/services/test_decay_flusher.py` | DecayFlusher unit tests |
| `tests/unit/services/test_consolidation.py` | ConsolidationJob unit tests |
| `tests/unit/repositories/test_pg_access_log.py` | AccessLog repo tests |
| `tests/unit/mcp/tools/test_decay_tools.py` | Decay MCP tools tests |

## Testing Strategy

- TDD obligatoire (Red-Green-Refactor)
- Unit tests for DecayCalculator with known inputs/outputs (verify exponential decay math)
- Unit tests for DecayFlusher aggregation logic (mock repos)
- Unit tests for ConsolidationJob (mock embedding similarity)
- Unit tests for all new MCP tools
- Integration test: full cycle access_log → aggregation → freshness_status change
- Integration test: search with decay scoring vs without

## Configuration

```python
# config.py additions
decay_enabled: bool = True
decay_floor: float = 0.3
decay_flush_interval_seconds: int = 300  # 5 minutes
consolidation_interval_seconds: int = 21600  # 6 hours
consolidation_similarity_threshold: float = 0.92
stale_threshold: float = 0.5
archive_threshold: float = 0.2
forgetting_archive_days: int = 180
```

## Out of Scope

- Automatic deletion of entities (always manual)
- Automatic merge of duplicates (always manual)
- Per-entity custom decay overrides (use `brain_refresh_entity` instead)
- UI/dashboard for decay visualization (metrics sidecar + Grafana is sufficient)
