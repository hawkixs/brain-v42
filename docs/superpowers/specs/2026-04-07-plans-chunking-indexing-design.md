# Plans Chunking & Search Integration — Design

**Date:** 2026-04-07
**Status:** Draft
**Author:** Brain Session (Hawixs + Claude)

## Context

Brain-v42 has an existing plan indexing system (`IndexedPlan` model, `PlanIndexer`
service, `brain_reindex_plans` MCP tool) that scans `*-design.md` and `*-plan.md`
files from configured `plan_scan_paths`, computes a single embedding per file
(title + first 500 chars), and links each plan to a feature via `ClusterGuard`.

This existing system serves only feature linking. Indexed plans are **not**
exposed to `brain_search`, are **not** chunked, and cannot be retrieved as
context by Claude during other sessions. This means the richest documents in
the project (specs and implementation plans) are invisible to semantic search.

## Goals

1. Make project plans and specs discoverable via `brain_search` alongside
   decisions, learnings, snippets, runbooks, and ADRs.
2. Preserve semantic granularity by chunking markdown on section headers
   (embeddings per section rather than per file).
3. Reuse existing infrastructure (`PlanIndexer`, `brain_reindex_plans`,
   `HybridSearcher`) without creating new MCP tools.
4. Keep total MCP tool count stable (currently 32, target under 30 long-term).

## Non-Goals

- Hook-based automatic indexing on file write. Manual invocation of
  `brain_reindex_plans` is sufficient for this iteration.
- Ingesting plan content via an MCP parameter. Plans remain file-based.
- A dedicated `brain_search_plans` tool. Global `brain_search` with
  `types=["plan"]` filter is the only search surface.
- Managing chunks individually via MCP. The parent plan is the unit of
  interaction (reindex, get, delete).
- Rich lifecycle tracking beyond `draft`/`active`/`archived`.

## Design

### Data Model

#### Modify `indexed_plans`

Add the following columns to the existing table:

| Column | Type | Notes |
|--------|------|-------|
| `content` | TEXT NOT NULL | Full markdown **with frontmatter stripped**. DB becomes source of truth. Filesystem is an input. |
| `summary` | TEXT nullable | Short summary (≤ 300 words). Populated from frontmatter `summary:` field if present, otherwise left NULL. LLM generation is deferred. |
| `search_vector` | TSVECTOR | For FTS via `HybridSearcher`. GIN indexed. |
| `tags` | ARRAY(VARCHAR) NOT NULL default `{}` | Coherent with other entities. GIN indexed. |
| `metadata` | JSONB NOT NULL default `{}` | Extensibility (author, source_commit, related_issue). |
| `status` | ENUM(`draft`,`active`,`archived`) NOT NULL default `active` | Lifecycle. Extracted from frontmatter `status:` field. |
| `chunk_count` | INTEGER NOT NULL default 0 | Denormalized count for display. |
| `word_count` | INTEGER NOT NULL default 0 | Stats; detects anomalously small/large plans. |
| `access_count` | INTEGER NOT NULL default 0 | Standard access tracking. |
| `last_accessed_at` | TIMESTAMP nullable | Standard access tracking. |
| `freshness_status` | ENUM(`fresh`,`stale`,`archived`) NOT NULL default `fresh` | Integrates with decay system. |
| `indexed_at` | TIMESTAMP NOT NULL | Last time file was (re)chunked and embedded. Distinct from `updated_at` which tracks any row change. |

Existing columns preserved as-is: `id`, `file_path` (unique), `title`, `plan_type`,
`project_key`, `content_hash`, `embedding`, `created_at`, `updated_at`.

#### New table `indexed_plan_chunks`

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | |
| `plan_id` | UUID NOT NULL | FK to `indexed_plans.id` ON DELETE CASCADE. |
| `section_title` | VARCHAR(500) NOT NULL | H2/H3 header text. |
| `section_path` | VARCHAR(1000) NOT NULL | Breadcrumb (e.g. `"Design > Data Model > Table indexed_plans"`). |
| `content` | TEXT NOT NULL | Section markdown (including subsections). |
| `section_order` | INTEGER NOT NULL | 0-based order inside parent. |
| `word_count` | INTEGER NOT NULL | Stats. |
| `embedding` | Vector(1536) NOT NULL | HNSW indexed. |
| `search_vector` | TSVECTOR | GIN indexed. |
| `tags` | ARRAY(VARCHAR) NOT NULL default `{}` | Denormalized from parent. |
| `project_key` | VARCHAR(50) NOT NULL | Denormalized from parent. |
| `plan_type` | ENUM(`spec`,`plan`) NOT NULL | Denormalized from parent. |
| `status` | ENUM(`draft`,`active`,`archived`) NOT NULL | Denormalized from parent. |
| `access_count` | INTEGER NOT NULL default 0 | Per-chunk access stats. |
| `last_accessed_at` | TIMESTAMP nullable | |
| `created_at` | TIMESTAMP NOT NULL | |

#### Indexes

**`indexed_plans`:**
- HNSW on `embedding` (m=16, ef_construction=64)
- GIN on `tags`
- GIN on `search_vector`
- Composite `(project_key, status, freshness_status)`

**`indexed_plan_chunks`:**
- HNSW on `embedding` (m=16, ef_construction=64)
- GIN on `tags`
- GIN on `search_vector`
- B-tree on `plan_id`
- Composite `(project_key, plan_type)`

### Markdown Chunker

New module: `src/brain_v42/services/plan_chunker.py`.

Signature:

```python
def chunk_markdown(content: str) -> tuple[PlanParentData, list[ChunkData]]:
    ...
```

Where `PlanParentData` carries the extracted `title`, `preamble`, `summary`,
`status`, `word_count`, and the full `content`, and each `ChunkData` carries
`section_title`, `section_path`, `content`, `section_order`, and `word_count`.

#### Rules

1. H1 (`#`) → parent title (first H1 wins).
2. H2 (`##`) → chunk boundary.
3. H3 (`###`) → stays inside its parent H2 chunk; contributes to `section_path`
   of any nested content but does not create a new chunk.
4. Content before the first H2 → parent `preamble` (not a chunk).
5. Chunk `< 50` words → merged with the next chunk.
6. Chunk `> 1500` words → logged warning, stored as-is.
7. Headers inside fenced code blocks (```` ``` ````) are ignored.
8. Frontmatter (`---` YAML at top) extracted for `title`, `status`, `summary`,
   `tags`. Stripped from `content` before chunking.

#### Embeddings and FTS

- **Parent embedding:** `title + summary` if summary present, else
  `title + preamble`.
- **Parent search_vector:** generated from `title + content` (full document
  FTS) so that keyword searches can still match the parent even when the
  embedding input is only a summary.
- **Chunk embedding:** the full chunk content (including subsection headers).
- **Chunk search_vector:** generated from `section_title + content`.
- **Batching:** parent + all chunks embedded in a single HTTP call to the GPU
  embedding service.

### PlanIndexer Changes

`src/brain_v42/services/plan_indexer.py` is modified to:

1. After reading a file and computing hash, call `chunk_markdown(content)`.
2. If hash unchanged → skip (existing behaviour).
3. If hash changed or plan is new → delete existing chunks for the plan
   (cascade via FK), then upsert the parent and insert all new chunks in a
   single transaction.
4. Populate new columns (`content`, `word_count`, `chunk_count`, `status`,
   `summary`, `indexed_at`, etc.).
5. ClusterGuard feature linking continues to run on the parent only.

### Search Integration

`HybridSearcher` is extended to:

1. Add `indexed_plan_chunks` as a searchable source when `"plan"` is in
   `types`.
2. Run pgvector cosine similarity and FTS in parallel on the chunks table.
3. Apply RRF fusion across the results.
4. Return each matching chunk with its `parent_id` included in the payload so
   the caller can fetch the full plan via `brain_get` if needed.
5. Filter by `tags`, `project_key`, `status='active'` by default (drafts and
   archived plans excluded from search unless explicitly requested).

`brain_search` accepts `"plan"` in its `types` parameter. No new MCP tool.

### Retrieval

`brain_get` (already generic) dispatches `entity_type="plan"` to a new
`IndexedPlanService.get_with_chunks(plan_id)` method that returns the parent
plus its chunks ordered by `section_order`.

`brain_delete` dispatches `entity_type="plan"` to a service method that relies
on `ON DELETE CASCADE` to remove the chunks.

No new MCP tool for update. `brain_reindex_plans` is the only write path.

### Migration

Single Alembic migration `add_plan_chunking_support`:

1. `ALTER TABLE indexed_plans` to add all new columns with appropriate
   defaults so existing rows remain valid.
2. `CREATE TABLE indexed_plan_chunks` with all indexes.
3. **Backfill** existing `indexed_plans` rows:
   - `content` ← read the file from `file_path`, or NULL if missing
     (mark `freshness_status='archived'` in that case).
   - `status` ← `'active'`.
   - `indexed_at` ← `updated_at`.
   - `word_count` ← computed from content.
   - `chunk_count` ← `0` (chunks created on next `brain_reindex_plans` run).
4. Log: "X plans migrated, run brain_reindex_plans to generate chunks".

Migration is non-destructive: an abort leaves `indexed_plans` usable under the
old schema.

### MCP Tool Surface

| Tool | Change |
|------|--------|
| `brain_reindex_plans` | Extended to chunk. Signature unchanged. |
| `brain_search` | Accepts `"plan"` in `types`. Signature unchanged. |
| `brain_get` | Dispatches `entity_type="plan"`. Signature unchanged. |
| `brain_delete` | Dispatches `entity_type="plan"`. Signature unchanged. |

**Net new tools: 0.** Total MCP surface remains at 32.

## Testing Strategy

TDD is mandatory. Tests are written first, must fail, then minimal
implementation follows.

### Unit tests (no DB)

1. `test_plan_chunker.py` — H2/H3 splitting, preamble, min/max rules,
   frontmatter extraction, embedding-input selection.
2. `test_plan_chunker_edge_cases.py` — no H2, empty file, emoji headers,
   fenced code blocks containing `##`, CRLF line endings, unicode titles.

### Integration tests (Postgres + pgvector)

3. `test_plan_indexer_chunking.py` — indexation produces parent + chunks, skip
   via hash, reindex replaces chunks cleanly.
4. `test_plan_repo.py` — CRUD on `IndexedPlan` and `IndexedPlanChunk`,
   `get_with_chunks` returns ordered chunks.
5. `test_plan_search_integration.py` — `brain_search(types=["plan"])` returns
   chunks with `parent_id`, honours `tags` and `project_key` filters.
6. `test_plan_get_integration.py` — `brain_get(entity_type="plan", id)`
   returns parent + ordered chunks.
7. `test_plan_delete_cascade.py` — deleting a parent removes all its chunks.
8. `test_plan_decay.py` — decay updates `freshness_status` on both parents
   and chunks.

### Non-regression

9. `test_existing_plan_indexer_still_works.py` — `brain_reindex_plans`
   continues to hash-skip unchanged files, still links to features via
   ClusterGuard, still returns the same stats shape.

### Coverage target

≥ 60% (project-wide CI gate). New modules should reach ≥ 80%.

## Implementation Risks

1. **Code block header bleed** — a naïve regex on `^##` will split inside
   fenced code blocks. The chunker must track fence state.
2. **Embedding throughput** — a single large project with 50 plans and an
   average of 10 chunks each = 500+ embeddings per reindex. The existing
   `PlanIndexer` semaphore (max 5 concurrent files) must be verified to handle
   the batched embedding calls without rate-limiting the GPU service.
3. **Transaction atomicity** — partial failure during chunking must roll back
   the entire plan. No half-indexed plans.
4. **File-on-disk drift** — if a file is deleted between the scan and the
   backfill migration, mark it `archived` rather than failing the migration.
5. **Search recall regression** — adding chunk results to the HybridSearcher
   may crowd out other types. Verify RRF fusion weights remain balanced.

## Deferred / Future Work

- Hook-based automatic reindex on file write.
- Status transitions beyond `draft`/`active`/`archived` (e.g., `in_progress`,
  `completed`) — only if real need emerges.
- MCP tool count reduction from 32 to under 30 — separate clean-up effort.
- Explicit plan-to-decision / plan-to-learning links in Neo4j.
- LLM-generated summaries when no frontmatter summary is provided.
