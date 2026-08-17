# Roadmap V2 — Event-Driven Feature Tracking

## Problem

The current roadmap tracking system requires manual feature creation (seed script) and manual status updates (`brain_update_project_focus`). Features cannot be created dynamically during sessions, and status changes depend entirely on the LLM remembering to call the right tool. This creates overhead and guarantees stale data.

## Solution

Transform the roadmap into an **event-driven projection** fed by 3 signal sources: MCP artifacts, superpowers plan files, and GitLab webhooks. Features are created implicitly when signals don't match existing features. Statuses update automatically via a monotonic heuristic. A cross-encoder reranker prevents duplicate features.

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │         brain_v42 MCP Server         │
                    │                                     │
  Artifacts ───────>│  FeatureLinker (enhanced)            │
  (learn, log_dec)  │    └─> ClusterGuard.resolve()       │
                    │           └─> StatusEngine.update()  │
                    │                                     │
  Plans .md ───────>│  PlanIndexer                         │
  (specs + plans)   │    └─> ClusterGuard.resolve()       │
                    │           └─> StatusEngine.update()  │
                    │                                     │
  GitLab events ──>│  GitLabIngestor                      │
  (webhooks)        │    └─> ClusterGuard.resolve()       │
                    │           └─> StatusEngine.update()  │
                    └──────────┬──────────┬───────────────┘
                               │          │
                               v          v
                          PostgreSQL   Reranker :8004
                          + pgvector   (MiniLM, CPU)
```

All three signal sources share the same pipeline: generate embedding, resolve via ClusterGuard (link/merge/create feature), then update status via StatusEngine.

### Infrastructure

```
PC Serveur (192.168.1.12)
├── brain_v42 MCP Server (stdio)
├── Qodo-Embed (port 8003, GPU) — existing
├── Cross-Encoder Reranker (port 8004, CPU) — new
├── Metrics sidecar (port 9200) + GitLab webhook endpoint — extended
└── PostgreSQL + pgvector (port 5433) — existing
```

The reranker runs on CPU (MiniLM ~80MB, <10ms/pair). No GPU needed.

## Component 1: ClusterGuard

Central anti-duplication layer. Every signal passes through it before creating or linking a feature.

### Interface

```python
class ClusterGuard:
    def __init__(self, session_factory, embedding_svc, reranker_client):
        ...

    async def resolve(
        self, text: str, embedding: list[float], project_key: str
    ) -> tuple[Feature, Literal["linked", "merged", "created"]]:
        """Resolve a signal to a feature: link existing, merge similar, or create new."""
```

### Flow

```
1. Cosine pgvector → top 5 features (same project_key)
2. best_score >= 0.70 → return (feature, "linked")
3. best_score 0.50-0.70 → cross-encoder reranker on top 5
   ├─ reranker_score >= 0.75 → return (feature, "linked")
   ├─ reranker_score 0.50-0.75 → "merged"
   │    └─ Enrich feature description with new context
   │    └─ Re-generate embedding
   │    └─ return (feature, "merged")
   └─ reranker_score < 0.50 → "created"
4. best_score < 0.50 → "created" (skip reranker)
```

### Merge behavior

When merging, `resolve()` reads the feature's current description from the DB, appends the new signal context, updates the row, and regenerates the embedding from the updated description. Example: feature "Memory Decay" + signal about "Active Forgetting" → description updated to cover both concepts, embedding refreshed from the full updated text.

### Batch deduplication

A periodic job (reusing ConsolidationJob pattern) scans all features:
- Pre-filter: for each feature, find top-3 nearest neighbors via cosine similarity (pgvector) within same project_key
- Only run cross-encoder on pairs where cosine score >= 0.50 (avoids O(n^2) explosion)
- Cross-encoder score >= 0.80 → auto-merge (oldest absorbs newest, all feature_artifacts transferred)
- Exposed via existing `brain_consolidation_candidates` tool (extended to include features)

### Reranker fallback

If the reranker service (port 8004) is down, ClusterGuard falls back to cosine scores only: >= 0.65 links to existing feature, < 0.65 creates new feature. Merge zone is skipped entirely. Degraded but functional.

## Component 2: PlanIndexer

Scans superpowers spec and plan files, indexes them, and links to features.

### Scan paths

Configured per-project in `project_contexts` table (new `plan_scan_paths` JSONB column). Set via `brain_set_project_context`:

```python
brain_set_project_context(
    project_key="brain_v42",
    plan_scan_paths=[
        "/home/hawixs/hawkixs_infra/git_repo/brain_v42/docs",
        "/home/hawixs/hawkixs_infra/git_repo/ReD_v1/projects/brain-v42/docs"
    ]
)
```

### Parsing

Superpowers specs and plans follow a predictable structure:

```python
class IndexedPlan:
    file_path: str          # absolute path
    title: str              # from frontmatter or # H1
    plan_type: str          # "spec" or "plan"
    project_key: str        # from project_context association
    content_hash: str       # SHA256 of file content
    embedding: list[float]  # generated via GPU embedding service
```

Glob patterns: `**/*-design.md`, `**/*-plan.md`

Title extraction priority:
1. YAML frontmatter `title:` or `name:` field
2. First `# H1` heading
3. Filename without date prefix and suffix

### When to index

- **MCP server startup**: scan all configured paths, compare `content_hash`, re-index only changed files
- **On-demand**: `brain_reindex_plans` MCP tool for forced re-scan
- No file watcher (MCP is stdio, not a daemon)

### Linking

Each indexed plan is passed through ClusterGuard:
- Embedding generated from plan title + first 500 chars of content
- ClusterGuard resolves to existing feature or creates new one
- Link stored in `feature_artifacts` with `artifact_type="plan"`

Note: `content_hash` is computed from full file content (to detect any change), while the embedding is from title + first 500 chars (for semantic matching). If the body changes but the intro stays the same, the plan is re-indexed (hash changed) but may link to the same feature. This is expected behavior.

### Storage

New `indexed_plans` table for tracking indexed files and detecting changes. The `project_key` column has no FK constraint — it is always derived from `project_contexts.plan_scan_paths`, so validity is enforced at the application layer.

## Component 3: GitLabIngestor

Receives GitLab webhooks, extracts context, links to features, updates statuses.

### Endpoint

Added to the existing metrics sidecar (port 9200) — no new service:

```
POST /gitlab/webhook
Header: X-Gitlab-Token: <secret>
```

### Events processed

| GitLab Event | Context Extracted | Status Signal |
|-------------|-------------------|---------------|
| Push (commits) | Commit messages + branch name | linking only |
| MR opened | MR title + description | `building` |
| MR merged | MR title + description | `deployed` |
| Pipeline success | Branch + associated MR | reinforces `deployed` |
| Pipeline failure | Branch name | no status change |

### Project mapping

GitLab `project.path_with_namespace` → `project_key` via new `gitlab_project_path` field in `project_contexts`:

```python
brain_set_project_context(
    project_key="brain_v42",
    gitlab_project_path="hawkixs_project/brain_v42"
)
```

The ingestor looks up `project_key` from the incoming webhook's project path.

### Processing flow

```
Webhook received → validate X-Gitlab-Token
  → parse event type + extract title/description/branch
  → lookup project_key from gitlab_project_path
  → generate embedding from extracted text
  → ClusterGuard.resolve(text, embedding, project_key)
  → StatusEngine.update(feature, signal_type)
  → store in gitlab_events table
```

### Security

Webhook secret token configured in `.env` (`GITLAB_WEBHOOK_SECRET`). Requests without valid `X-Gitlab-Token` header are rejected with 401.

### Reliability

GitLab retries failed webhook deliveries automatically. If the sidecar is temporarily down, events are not lost.

### Idempotency

GitLab may retry webhooks. To prevent duplicate processing, `gitlab_events` includes a `gitlab_event_id` column (VARCHAR, unique) populated from the webhook's `X-Gitlab-Event-UUID` header. Duplicate events are silently ignored via `ON CONFLICT DO NOTHING`.

### Storage

New `gitlab_events` table — append-only (write-once, never updated). Serves as audit trail. The `MetricsServer.__init__` constructor is extended to receive `ClusterGuard`, `StatusEngine`, and `embedding_svc` references for webhook processing.

## Component 4: StatusEngine

Centralized heuristic that computes feature status from accumulated signals.

### Status progression (monotonic — never goes backward automatically)

```
planned → research → design → building → deployed → done
```

### Heuristic

```python
STATUS_ORDER = ["planned", "research", "design", "building", "deployed", "done"]

def compute_status(feature: Feature, signal_type: str) -> str:
    if feature.pinned:
        return feature.status  # manual override, don't touch

    signal_status_map = {
        "learning": "research",
        "decision": "research",
        "snippet": "research",
        "runbook": "design",
        "adr": "design",
        "plan": "design",
        "mr_opened": "building",
        "push": None,  # linking only, no status change
        "mr_merged": "deployed",
        "pipeline_success": "deployed",
        "pipeline_failure": None,  # no status change
    }

    proposed = signal_status_map.get(signal_type)
    if proposed is None:
        return feature.status

    # Never go backward
    current_idx = STATUS_ORDER.index(feature.status)
    proposed_idx = STATUS_ORDER.index(proposed)
    if proposed_idx > current_idx:
        return proposed
    return feature.status
```

### Pinning

- `pinned` boolean column on `features` table (default: `false`)
- Set to `true` when `brain_update_project_focus` is called with explicit `feature_status`
- StatusEngine skips pinned features
- Unpinning: new `unpin` parameter in `brain_update_project_focus`

### When to compute

Status is updated at signal arrival time — no batch recalculation. Each signal source (FeatureLinker, PlanIndexer, GitLabIngestor) calls `StatusEngine.update(feature_id, signal_type)` after linking.

## Component 5: Reranker Service

Lightweight cross-encoder service running on CPU.

### Specification

- **Model**: `cross-encoder/ms-marco-MiniLM-L-6-v2` (~80MB)
- **Port**: 8004
- **Host**: localhost (PC Serveur)
- **Runtime**: CPU only, no GPU required

### API

```
POST /rerank
{
    "query": "Memory decay and active forgetting system",
    "candidates": [
        "Memory Decay / Active Forgetting",
        "Hybrid Search + Context Optimization",
        "Knowledge Graph / Entity Extraction"
    ]
}
→ {"scores": [0.92, 0.15, 0.08]}
```

```
GET /health
→ {"status": "ok", "model": "cross-encoder/ms-marco-MiniLM-L-6-v2"}
```

### Deployment

Docker container or standalone Python process. FastAPI + sentence-transformers (CrossEncoder class).

### Fallback

If the reranker is unreachable, ClusterGuard falls back to cosine-only scoring with tighter thresholds.

## Feature creation (dynamic)

Features are no longer seeded manually. They are created exclusively by ClusterGuard when `resolve()` returns `"created"`.

### Name extraction by signal source

| Signal Source | Name Extracted From |
|--------------|-------------------|
| `brain_learn` / `brain_log_decision` | Artifact `title` field |
| Plan spec `.md` | Frontmatter `title:` or `# H1` heading |
| GitLab MR | MR title (stripped of `feat/fix/chore` prefixes) |
| GitLab push | Branch name parsed (`feat/decay-system` → "Decay System") |

### Initial status

Determined by StatusEngine based on the signal type that triggered creation.

### Seed script migration

The existing `seed_features.py` script is converted to a one-time migration for features already in the database. After migration, the script is removed. All future feature creation is dynamic.

## Database changes

### Migration 009: roadmap_v2

(Migrations 007 and 008 are already taken by the decay system.)

```sql
-- 1. Features: add pinned flag
ALTER TABLE features ADD COLUMN pinned BOOLEAN DEFAULT FALSE;

-- 2. Project contexts: add config fields
ALTER TABLE project_contexts ADD COLUMN plan_scan_paths JSONB DEFAULT '[]';
ALTER TABLE project_contexts ADD COLUMN gitlab_project_path VARCHAR(200);

-- 3. New table: indexed_plans
CREATE TABLE indexed_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_path VARCHAR(500) UNIQUE,
    title VARCHAR(200),
    plan_type VARCHAR(20) CHECK (plan_type IN ('spec', 'plan')),
    project_key VARCHAR(50) NOT NULL,
    content_hash VARCHAR(64),
    embedding VECTOR(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_indexed_plans_project ON indexed_plans(project_key);
CREATE TRIGGER set_indexed_plans_updated_at
    BEFORE UPDATE ON indexed_plans
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- 4. New table: gitlab_events (append-only, never updated)
CREATE TABLE gitlab_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gitlab_event_id VARCHAR(100) UNIQUE,
    event_type VARCHAR(30) NOT NULL,
    project_key VARCHAR(50) NOT NULL,
    gitlab_project_id INTEGER,
    ref VARCHAR(200),
    title VARCHAR(500),
    embedding VECTOR(1536),
    feature_id UUID REFERENCES features(id),
    processed_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_gitlab_events_project ON gitlab_events(project_key);
CREATE INDEX idx_gitlab_events_feature ON gitlab_events(feature_id);

-- 5. Feature artifacts: add new types
ALTER TABLE feature_artifacts
    DROP CONSTRAINT feature_artifacts_artifact_type_check;
ALTER TABLE feature_artifacts
    ADD CONSTRAINT feature_artifacts_artifact_type_check
    CHECK (artifact_type IN ('learning', 'decision', 'snippet', 'runbook', 'adr', 'plan', 'gitlab_event'));
```

### Pydantic model updates

The following models must be updated to reflect the new columns:

- **`Feature`**: add `pinned: bool = False`
- **`ProjectContextCreate`**: add `plan_scan_paths: list[str] = []`, `gitlab_project_path: str | None = None`
- **`ProjectContext`**: add same fields
- **`RoadmapFeature`**: add `pinned: bool` for display

### RoadmapService updates

`RoadmapService._ARTIFACT_TYPES` must be extended to include `"plan"` and `"gitlab_event"` so that artifact counts in `brain_get_roadmap` include these new types. The `_ROADMAP_SQL` query and `_pivot_rows` logic must handle the new types accordingly.

## MCP tool changes

### Modified tools

**`brain_set_project_context`** — change to upsert semantics (currently get-or-create which does NOT update existing records). Must update existing project_contexts when called with new field values. Add parameters:
- `plan_scan_paths: list[str] | None` — directories to scan for plan files
- `gitlab_project_path: str | None` — GitLab project path for webhook mapping

**`brain_update_project_focus`** — add parameter:
- `unpin: list[str] | None` — list of feature names to unpin (re-enable auto status)

When `feature_status` dict is provided, matched features are set to `pinned=true`.

**`brain_get_roadmap`** — output enriched with pinned indicator.

### New tools

**`brain_reindex_plans`** — force re-scan of plan files:
```python
@mcp.tool(version="1.0")
async def brain_reindex_plans(project_key: str | None = None) -> str:
    """Re-scan and index plan files for a project or all projects.

    Compares file content hashes to skip unchanged files.
    Links new/updated plans to features via ClusterGuard.
    """
```

## Testing strategy

- Unit tests for ClusterGuard (mock reranker + DB)
- Unit tests for StatusEngine (pure logic, no I/O)
- Unit tests for PlanIndexer (mock filesystem + embedding service)
- Unit tests for GitLabIngestor (mock webhook payloads)
- Integration test: full signal → feature creation → status update cycle
- Reranker service: health check + accuracy test on known pairs

## Out of scope

- Feature deletion (features accumulate, old ones decay naturally via the existing decay system)
- Feature hierarchy (epics, milestones) — keep it flat, one level under project
- Feature search tool (brain_get_roadmap handles display, ClusterGuard handles matching)
- UI/dashboard changes (brain_get_roadmap output format stays compatible)
