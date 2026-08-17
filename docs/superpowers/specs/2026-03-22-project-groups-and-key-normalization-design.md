# Project Groups & Key Normalization

**Date:** 2026-03-22
**Status:** Draft
**Scope:** brain_v42 — project key management overhaul

## Problem

Project keys in brain are inconsistent and lack hierarchy:

- **Naming chaos**: `red_ml` vs `red-api` (underscores vs kebab-case)
- **No grouping**: Red ecosystem has ~12 sub-projects at the same flat level as unrelated projects
- **Noise**: test/orphan projects (`e2e_test_cleanup`, `e2e_test_project`, `test_deploy`) pollute listings and search
- **No cross-project search**: can't search "all of Red" without listing each project_key individually

## Solution

**Approach A — `project_group` column on `project_contexts`**

Add a nullable `project_group` column to enable ecosystem-level filtering, normalize all keys to kebab-case, and clean up orphans.

## Schema Changes

### New column

```sql
ALTER TABLE project_contexts ADD COLUMN project_group VARCHAR(50);
CREATE INDEX idx_project_contexts_group ON project_contexts (project_group);
```

### Kebab-case constraint

```sql
ALTER TABLE project_contexts ADD CONSTRAINT chk_project_key_format
CHECK (project_key ~ '^[a-z0-9]+(-[a-z0-9]+)*$');
```

## Data Migration

### Key renames

Cascade UPDATE across **10 tables**: project_contexts, decisions, learnings, snippets, runbooks, adrs, features, indexed_plans, gitlab_events, search_log.

Also update `related_projects` arrays in project_contexts that reference old-format keys.

| Old key | New key |
|---|---|
| `red_ml` | `red-ml` |
| `red_llm` | `red-llm` |
| `red_tsdb` | `red-tsdb` |
| `red_daemon` | `red-daemon` |
| `brain_v42` | `brain-v42` |

### Deletions

For orphan projects (`e2e_test_cleanup`, `e2e_test_project`, `test_deploy`):

- **Nullable columns** (decisions, learnings, snippets, search_log): SET project_key=NULL
- **NOT NULL columns** (runbooks, adrs, features, indexed_plans, gitlab_events): DELETE rows if any exist (unlikely for test projects, but must handle)
- **project_contexts**: DELETE row
- **Neo4j**: DELETE Project nodes and their BELONGS_TO relationships

### Neo4j graph sync

For each renamed key, update the Neo4j Project node:
```cypher
MATCH (p:Project {project_key: 'old_key'}) SET p.project_key = 'new-key'
```

For deleted projects:
```cypher
MATCH (p:Project {project_key: 'test_key'}) DETACH DELETE p
```

### Group assignment

| Group | Project keys |
|---|---|
| `red` | `red`, `red-api`, `red-ml`, `red-lab`, `red-alerts`, `red-orchestrator`, `red-monitor`, `red-llm`, `red-tsdb`, `red-daemon`, `brain-v42`, `auto-discord` |
| `null` | `mrc-rag`, `lyriks-v3`, `lab-secu`, `izi-track`, `hackathon-lyriks` |

### Migration order

1. ADD COLUMN `project_group`
2. Rename project_keys (UPDATE cascade on **10 tables** + `related_projects` arrays)
3. Sync Neo4j nodes (rename + delete)
4. Delete orphan project rows (entities first, then project_contexts)
5. Populate groups
6. ADD INDEX on `project_group`
7. ADD CHECK constraint kebab-case

**Note:** Migration 012 depends on 011 (schema_consistency_fixes) which must be finalized first.

## Model Changes

### Pydantic (`models/project_context.py`)

- Add `project_group: str | None = Field(None, max_length=50)` to `ProjectContextBase`
- Add `project_group` to `ProjectContextUpdate` as well
- Add validator on `project_key`: enforce `^[a-z0-9]+(-[a-z0-9]+)*$`

### SQLAlchemy (`db/tables.py`)

- Add column: `Column("project_group", String(50), nullable=True)`

## Repository Changes

### `pg_project_context.py`

- `list_all(project_group: str | None = None)` — filter by group when provided
- `get_or_create()` — include `project_group` in VALUES dict

### All 5 entity repos (decisions, learnings, snippets, runbooks, adrs)

- `semantic_search()` — accept `project_keys: list[str] | None` in addition to `project_key: str | None`
- When `project_keys` provided, filter with `WHERE project_key = ANY(:keys)` (uses PG array param)
- `project_key` (single) and `project_keys` (list) are mutually exclusive

### `HybridSearcher` (`services/search/hybrid.py`)

- `search()` — accept `project_keys: list[str] | None`, forward to both `fts_search_fn` and `vector_search_fn`

## Service Changes

### `project_context_service.py`

- `list_all(project_group=None)` — pass filter to repo

### `brain_service.py`

- `search()` and `_fan_out()` accept `project_group: str | None`
- When `project_group` provided: fetch project_keys for the group, pass as `project_keys` list to each service's search
- `project_key` and `project_group` are mutually exclusive (error if both provided)

## MCP Tool Changes

### Modified tools

| Tool | Change |
|---|---|
| `brain_search` | New optional param `project_group`, mutually exclusive with `project_key` |
| `brain_list_projects` | New optional param `project_group` to filter listing |
| `brain_set_project_context` | New optional param `project_group`, passed through to `ProjectContextCreate` |

### New tool

**`brain_list_project_groups`** — returns distinct groups with project count.

```sql
SELECT project_group, COUNT(*) FROM project_contexts
WHERE project_group IS NOT NULL
GROUP BY project_group ORDER BY project_group;
```

### Unchanged

- `brain_session_start` — takes a specific `project_key`, no code change needed. However, all callers (CLAUDE.md configs) must use the new kebab-case keys after migration.

## External References to Update

After migration, manually update:

- `CLAUDE.md` in brain-v42 repo (`brain_v42` → `brain-v42` in all project_key references, `brain_session_start` calls)
- `~/.claude/projects/*/memory/MEMORY.md` (project_key references)
- CLAUDE.md files in Red ecosystem repos that reference their project_keys
- `config.py` / `.env` `CLAUDE_MD_PATHS` JSON keys: rename `brain_v42` → `brain-v42` (runtime config, not just code)

**Critical:** Any `brain_session_start(project_key="brain_v42")` call will silently return no results after migration. All CLAUDE.md files must be updated to `brain-v42`.

## Deliverables

| # | Deliverable | Files |
|---|---|---|
| 1 | Alembic migration 012 | `alembic/versions/012_project_groups_and_cleanup.py` |
| 2 | Pydantic model | `models/project_context.py` |
| 3 | SQLAlchemy table | `db/tables.py` |
| 4 | Repository (project_context) | `repositories/pg_project_context.py` |
| 5 | Repositories (5 entity repos) | `pg_decision.py`, `pg_learning.py`, `pg_snippet.py`, `pg_runbook.py`, `pg_adr.py` — add `project_keys` list filter |
| 6 | Service | `services/project_context_service.py` |
| 7 | BrainService | `services/brain_service.py` |
| 8 | GraphService | `services/graph_service.py` — Group node upsert, BELONGS_TO_GROUP sync |
| 9 | MCP tools (modified) | `brain_search`, `brain_list_projects`, `brain_set_project_context` |
| 10 | MCP tool (new) | `brain_list_project_groups` |
| 11 | External refs | CLAUDE.md, MEMORY.md, config.py, Red repos CLAUDE.md |
| 12 | Tests | Unit tests for each modified layer |

## Design Decisions

- **Neo4j Group nodes as complement**: PG is source of truth for `project_group`. Neo4j gets a `(:Group {name: 'red'})` node with `(:Project)-[:BELONGS_TO_GROUP]->(:Group)` relationships, synced write-through like existing Project nodes. Enables future graph traversals (e.g., "all learnings in the Red ecosystem"). Graceful degradation: if Neo4j is down, grouping still works via PG column.
- **No FK between project_contexts and entities**: Maintained for flexibility (entities can exist without a pre-existing project). The string-based reference pattern is established and works.
- **`project_keys` list filter via `ANY(:keys)`**: More efficient than N parallel searches. Single query per repo, single embedding computation.

## Neo4j Graph Sync (complement)

PG is source of truth. Neo4j mirrors the group structure for traversal queries.

### New node type

```cypher
CREATE (g:Group {name: 'red'})
```

### New relationship

```cypher
MATCH (p:Project {project_key: 'red-ml'}), (g:Group {name: 'red'})
CREATE (p)-[:BELONGS_TO_GROUP]->(g)
```

### Write-through in `graph_service.py`

- On `set_project_context` with `project_group`: MERGE Group node, CREATE relationship
- On `update_project_focus` with `project_group` change: delete old relationship, create new
- On `project_group` set to NULL: delete BELONGS_TO_GROUP relationship
- Graceful degradation: if Neo4j down, PG column still works, log warning

### Migration sync

After PG migration completes, run Neo4j sync:
1. Create Group nodes for each distinct `project_group`
2. Create BELONGS_TO_GROUP relationships for all projects with a group
3. This runs in `upgrade()` with try/except (Neo4j optional)

## Data Safety

This migration touches 10 tables + Neo4j + external configs. **Zero data loss tolerance.**

### Pre-migration checklist

1. `pg_dump -Fc brain > brain_backup_$(date +%Y%m%d_%H%M).dump` — full PG backup
2. `neo4j-admin dump --to=/backup/neo4j_$(date +%Y%m%d).dump` — Neo4j snapshot
3. Copy all CLAUDE.md / .env files being modified
4. Verify backup integrity: `pg_restore --list brain_backup_*.dump | head`

### Migration safeguards

- Entire Alembic migration runs in a **single transaction** (default behavior)
- If any UPDATE/DELETE fails → full rollback, no partial state
- Neo4j sync in try/except — Neo4j failure does NOT abort PG migration
- Each key rename uses explicit WHERE clause (no wildcards, no LIKE)
- Orphan deletion: count rows BEFORE delete, log counts, assert expected

### Verification queries (post-migration)

```sql
-- No underscore keys remain
SELECT project_key FROM project_contexts WHERE project_key ~ '_';
-- Should return 0 rows

-- All groups assigned correctly
SELECT project_group, COUNT(*) FROM project_contexts GROUP BY project_group;
-- Should show: red=12, NULL=5

-- No orphan test projects
SELECT * FROM project_contexts WHERE project_key IN ('e2e_test_cleanup', 'e2e_test_project', 'test_deploy');
-- Should return 0 rows

-- Entity counts consistent
SELECT project_key, COUNT(*) FROM decisions WHERE project_key ~ '_' GROUP BY project_key;
-- Should return 0 rows (all renamed)
```

### Rollback

If something goes wrong: restore from PG dump + Neo4j dump + saved config files. The Alembic `downgrade()` will reverse the schema changes (drop column, drop constraint, drop index) but **will NOT reverse data renames** — use the backup for full rollback.
