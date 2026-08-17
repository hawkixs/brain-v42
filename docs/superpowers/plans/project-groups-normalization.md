# Implementation Plan: Project Groups & Key Normalization

**Tech stack:** Python 3.12+, SQLAlchemy 2.0 async, Pydantic 2, FastMCP, Alembic, pytest
**Test command:** `cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && .venv/bin/python -m pytest tests/unit -x -q`
**Working directory:** `/home/hawixs/hawkixs_infra/git_repo/brain_v42`
**TDD:** Red-Green-Refactor obligatoire
**No checkpoints — run all batches end-to-end.**

---

## Batch 1: Foundation (parallel)

### Task 1.1: Alembic migration 012

Create `alembic/versions/012_project_groups_and_normalization.py`.

Previous migration is `011_schema_consistency_fixes.py` — use its revision as `down_revision`.

**Steps:**
1. Read `alembic/versions/011_schema_consistency_fixes.py` to get its revision ID for `down_revision`.
2. Write migration with:

```python
def upgrade():
    # 1. ADD COLUMN
    op.add_column('project_contexts', sa.Column('project_group', sa.String(50), nullable=True))
    op.create_index('idx_project_contexts_group', 'project_contexts', ['project_group'])

    # 2. RENAME KEYS — cascade UPDATE on all tables with project_key
    # Tables: decisions(nullable), learnings(nullable), snippets(nullable),
    #         runbooks(NOT NULL), adrs(NOT NULL), project_contexts(NOT NULL),
    #         features(NOT NULL), indexed_plans(NOT NULL), gitlab_events(NOT NULL),
    #         search_log(nullable)
    renames = {
        'red_ml': 'red-ml', 'red_data': 'red-data', 'red_story': 'red-story',
        'red_daemon': 'red-daemon', 'red_llm': 'red-llm', 'red_tsdb': 'red-tsdb',
        'brain_v42': 'brain-v42', 'auto_discord': 'auto-discord',
        'poc_lyriks_v2': 'poc-lyriks-v2', 'datalake_v2': 'datalake-v2',
        'mrc_rag': 'mrc-rag', 'hk_infofeed': 'hk-infofeed',
        'hk_anime_list': 'hk-anime-list', 'test_pock': 'test-pock',
        'test_deploy': 'test-deploy', 'red_data': 'red-data',
    }
    tables_with_pk = [
        'decisions', 'learnings', 'snippets', 'runbooks', 'adrs',
        'project_contexts', 'features', 'indexed_plans', 'gitlab_events', 'search_log',
    ]
    for old, new in renames.items():
        for table in tables_with_pk:
            op.execute(f"UPDATE {table} SET project_key = '{new}' WHERE project_key = '{old}'")

    # Also update related_projects arrays in project_contexts
    for old, new in renames.items():
        op.execute(
            f"UPDATE project_contexts SET related_projects = "
            f"array_replace(related_projects, '{old}', '{new}') "
            f"WHERE '{old}' = ANY(related_projects)"
        )

    # 3. MERGE DUPLICATES — poc_lyriks_v2 into poc-lyriks-v2, datalake_v2 into datalake-v2
    # (Already handled by renames — both old keys map to same new key)
    # Delete the duplicate project_contexts row (keep the one with newer updated_at)
    for pk in ['poc-lyriks-v2', 'datalake-v2']:
        op.execute(
            f"DELETE FROM project_contexts WHERE project_key = '{pk}' "
            f"AND id NOT IN (SELECT id FROM project_contexts WHERE project_key = '{pk}' ORDER BY updated_at DESC LIMIT 1)"
        )

    # 4. DELETE ORPHANS — SET NULL on nullable, DELETE on NOT NULL tables
    orphans = ['e2e-test-cleanup', 'e2e-test-project', 'test-deploy', 'test-pock']
    nullable_tables = ['decisions', 'learnings', 'snippets', 'search_log']
    not_null_tables = ['runbooks', 'adrs', 'features', 'indexed_plans', 'gitlab_events']
    for orphan in orphans:
        for table in nullable_tables:
            op.execute(f"UPDATE {table} SET project_key = NULL WHERE project_key = '{orphan}'")
        for table in not_null_tables:
            op.execute(f"DELETE FROM {table} WHERE project_key = '{orphan}'")
        op.execute(f"DELETE FROM project_contexts WHERE project_key = '{orphan}'")

    # 5. POPULATE GROUPS
    red_projects = [
        'red', 'red-api', 'red-ml', 'red-lab', 'red-alerts',
        'red-orchestrator', 'red-monitor', 'red-llm', 'red-tsdb',
        'red-daemon', 'brain-v42', 'auto-discord',
        'red-lab:architect', 'red-lab:developer', 'red-lab:developer-gemini',
        'red-lab:developer-opus', 'red-lab:orchestrator', 'red-lab:reviewer',
        'red-lab:sentinel', 'red-lab:shared', 'red-lab:overseer',
    ]
    for pk in red_projects:
        op.execute(f"UPDATE project_contexts SET project_group = 'red' WHERE project_key = '{pk}'")

    lyriks = ['lyriks', 'lyriks-v3', 'poc-lyriks-v2', 'lyriks-backend-v2', 'hackathon-lyriks']
    for pk in lyriks:
        op.execute(f"UPDATE project_contexts SET project_group = 'lyriks' WHERE project_key = '{pk}'")

    watchk = ['watchk', 'watchk-claude', 'watchk-memory', 'watchk-gitk']
    for pk in watchk:
        op.execute(f"UPDATE project_contexts SET project_group = 'watchk' WHERE project_key = '{pk}'")

    infra = ['brain-v42', 'hawkixs-infra']
    for pk in infra:
        op.execute(f"UPDATE project_contexts SET project_group = 'infra' WHERE project_key = '{pk}'")

    # 6. ADD CHECK CONSTRAINT — kebab-case only
    # NOTE: must allow colons for red-lab:architect style keys
    op.execute(
        "ALTER TABLE project_contexts ADD CONSTRAINT chk_project_key_format "
        "CHECK (project_key ~ '^[a-z0-9]+([:-][a-z0-9]+)*$')"
    )


def downgrade():
    op.execute("ALTER TABLE project_contexts DROP CONSTRAINT IF EXISTS chk_project_key_format")
    op.drop_index('idx_project_contexts_group', table_name='project_contexts')
    op.drop_column('project_contexts', 'project_group')
    # NOTE: key renames and group assignments are NOT reversed
```

3. Run `ruff format` on the file.
4. Run tests: `pytest tests/unit -x -q` (migration file itself doesn't need unit tests — it's SQL).

**Files:** `alembic/versions/012_project_groups_and_normalization.py`

---

### Task 1.2: Pydantic models + SQLAlchemy table

**Files:**
- `src/brain_v42/models/project_context.py`
- `src/brain_v42/db/tables.py`
- `tests/unit/test_models_project_context.py` (NEW)

**Steps:**

1. Read existing files first.

2. **`db/tables.py`** — Add to `project_contexts` table definition:
```python
Column("project_group", String(50), nullable=True),
```
After the `gitlab_project_path` column, before `created_at`.

3. **`models/project_context.py`** — Changes:

a. Add import: `from pydantic import BaseModel, Field, field_validator`
b. Add to `ProjectContextBase`:
```python
project_group: str | None = Field(None, max_length=50)
```
c. Add to `ProjectContextUpdate`:
```python
project_group: str | None = None
```
d. Add kebab-case validator on `ProjectContextBase`:
```python
@field_validator("project_key")
@classmethod
def validate_project_key_format(cls, v: str) -> str:
    import re
    if not re.match(r'^[a-z0-9]+([:-][a-z0-9]+)*$', v):
        raise ValueError(
            f"project_key must be kebab-case (lowercase, hyphens/colons only): {v!r}"
        )
    return v
```

4. **Tests** — Write `tests/unit/test_models_project_context.py`:
- Test valid keys: `"brain-v42"`, `"red-lab:architect"`, `"red"`, `"a1-b2-c3"`
- Test invalid keys: `"brain_v42"` (underscore), `"UPPER"`, `"spaces here"`, `"trailing-"`
- Test `project_group` field exists and is optional
- Test `ProjectContextUpdate` has `project_group`

5. Run `ruff format` + `pytest tests/unit -x -q`.

---

### Task 1.3: Entity repos — `project_keys` list filter

**Files:**
- `src/brain_v42/repositories/pg_decision.py`
- `src/brain_v42/repositories/pg_learning.py`
- `src/brain_v42/repositories/pg_snippet.py`
- `src/brain_v42/repositories/pg_runbook.py`
- `src/brain_v42/repositories/pg_adr.py`
- `tests/unit/test_repo_decision_project_keys.py` (NEW)

**Steps:**

1. Read all 5 repo files first.

2. For EACH of the 5 repos, add `project_keys: list[str] | None = None` parameter to:
   - `search_fts()` method
   - `search_vector()` method
   - `list_all()` method (if it has project_key filter)

3. Logic: When `project_keys` is provided, replace `WHERE project_key = :pk` with `WHERE project_key = ANY(:keys)`:
```python
# In search_fts and search_vector:
if project_keys is not None:
    stmt = stmt.where(TABLE.c.project_key == sa.any_(sa.literal(project_keys)))
elif project_key is not None:
    stmt = stmt.where(TABLE.c.project_key == project_key)
```

**IMPORTANT:** Use `sa.any_(sa.literal(project_keys))` for SQLAlchemy array parameter. The pattern is:
```python
from sqlalchemy import any_ as sa_any
# ...
stmt = stmt.where(decisions.c.project_key == sa.any_(sa.literal(project_keys)))
```
Or alternatively: `stmt.where(decisions.c.project_key.in_(project_keys))` which also works fine for small lists.

Actually, simplest and most readable: use `.in_()`:
```python
if project_keys is not None:
    stmt = stmt.where(TABLE.c.project_key.in_(project_keys))
elif project_key is not None:
    stmt = stmt.where(TABLE.c.project_key == project_key)
```

4. Apply to ALL 5 repos identically. The pattern is the same for each: add param, add filter.

5. **Tests** — Write `tests/unit/test_repo_decision_project_keys.py`:
- Mock session, test that `project_keys=["a","b"]` produces `.in_()` filter
- Test that `project_key="a"` still works (backward compat)
- Test that `project_keys=None` produces no filter

6. Run `ruff format` + `pytest tests/unit -x -q`.

---

## Batch 2: Services (parallel)

### Task 2.1: ProjectContext repo + service — group filter

**Files:**
- `src/brain_v42/repositories/pg_project_context.py`
- `src/brain_v42/services/project_context_service.py`
- `tests/unit/test_project_context_group.py` (NEW)

**Steps:**

1. Read existing files first.

2. **`pg_project_context.py`** — Changes:

a. Add `project_group` filter to `list_all()`:
```python
async def list_all(
    self,
    limit: int = 20,
    offset: int = 0,
    project_group: str | None = None,
) -> list[ProjectContext]:
    async with self.get_session() as session:
        stmt = sa.select(project_contexts)
        if project_group is not None:
            stmt = stmt.where(project_contexts.c.project_group == project_group)
        stmt = stmt.order_by(project_contexts.c.created_at.desc()).limit(limit).offset(offset)
        ...
```

b. Add `get_keys_by_group()`:
```python
async def get_keys_by_group(self, project_group: str) -> list[str]:
    """Return all project_keys that belong to the given group."""
    async with self.get_session() as session:
        stmt = (
            sa.select(project_contexts.c.project_key)
            .where(project_contexts.c.project_group == project_group)
            .order_by(project_contexts.c.project_key)
        )
        result = await session.execute(stmt)
        return [row[0] for row in result.fetchall()]
```

c. Add `project_group` to `create()` and `get_or_create()` VALUES dicts.

3. **`project_context_service.py`** — Read the file first, then:
a. Add `project_group` param to `list_all()`, forward to repo
b. Add `get_keys_by_group()` method, forward to repo

4. **Tests** — `tests/unit/test_project_context_group.py`:
- Test `list_all(project_group="red")` filters correctly
- Test `get_keys_by_group("red")` returns list of keys
- Test `get_keys_by_group("nonexistent")` returns empty list

5. Run `ruff format` + tests.

---

### Task 2.2: BrainService + HybridSearcher — project_group support

**Files:**
- `src/brain_v42/services/brain_service.py`
- `src/brain_v42/services/search/hybrid.py`
- `tests/unit/test_brain_service_group.py` (NEW)

**Steps:**

1. Read BOTH files fully first.

2. **`services/search/hybrid.py`** — `HybridSearcher.search()`:
Add `project_keys: list[str] | None = None` param. When provided, forward to both `fts_search_fn` and `vector_search_fn` as `project_keys=project_keys`.

Read the full `search()` method first. It calls `fts_search_fn(query, project_key=project_key, ...)` and `vector_search_fn(query, project_key=project_key, ...)`. Add `project_keys` alongside `project_key` to both calls.

3. **`services/brain_service.py`** — Changes:

a. Add `project_context_svc` to `__init__()`:
```python
def __init__(self, ..., project_context_svc: Any | None = None):
    ...
    self._project_context_svc = project_context_svc
```

b. Modify `_fan_out()` — add `project_keys: list[str] | None = None` param:
```python
async def _fan_out(
    self,
    types: list[KnowledgeType],
    query: str,
    project_key: str | None,
    limit: int,
    project_keys: list[str] | None = None,
) -> dict[KnowledgeType, list[tuple[Any, float]]]:
```
Pass `project_keys=project_keys` to `self._hybrid_searcher.search()` and `svc.semantic_search()`.

c. Modify `search()` — add `project_group: str | None = None` param:
```python
async def search(self, query, types=None, project_key=None, project_group=None, ...):
    # Resolve group to keys
    project_keys = None
    if project_group and self._project_context_svc:
        project_keys = await self._project_context_svc.get_keys_by_group(project_group)
        if not project_keys:
            return SearchResponse(results=[], total=0, query=query)

    results_by_type = await self._fan_out(types, query, project_key, limit, project_keys=project_keys)
```

4. **Tests** — `tests/unit/test_brain_service_group.py`:
- Test `search(project_group="red")` calls `get_keys_by_group` and forwards keys
- Test `search(project_key="x")` still works unchanged
- Test `search(project_group="empty")` returns empty results

5. Run `ruff format` + tests.

**IMPORTANT:** Also update `server.py` `build_services()` to pass `project_context_svc` to `BrainService`:
```python
brain_svc = BrainService(
    ...,
    project_context_svc=project_context_svc,
)
```

---

## Batch 3: MCP Tools (parallel)

### Task 3.1: Modified MCP tools — brain_search, brain_list_projects, brain_set_project_context

**Files:**
- `src/brain_v42/mcp/tools/brain_tools.py`
- `src/brain_v42/mcp/tools/project_context_tools.py`
- Tests for modified tools

**Steps:**

1. Read BOTH tool files fully first.

2. **`brain_tools.py`** — `brain_search` tool:
Add `project_group: str | None = None` parameter. Pass to `brain_svc.search(project_group=project_group)`.
Update docstring to mention `project_group` is mutually exclusive with `project_key`.

3. **`project_context_tools.py`** — `brain_list_projects`:
Add `project_group: str | None = None` parameter. Pass to `project_context_svc.list_all(project_group=project_group)`.
Update the formatting to show group info.

4. **`project_context_tools.py`** — `brain_set_project_context`:
Add `project_group: str | None = None` parameter. Include in `ProjectContextCreate(project_group=project_group)`.

5. Run `ruff format` + tests.

---

### Task 3.2: New `brain_list_project_groups` MCP tool

**Files:**
- `src/brain_v42/mcp/tools/project_context_tools.py` (add new tool)
- `tests/unit/test_project_group_tool.py` (NEW)

**Steps:**

1. Read `project_context_tools.py` fully first.

2. Add `list_groups()` method to `PgProjectContextRepo`:
```python
async def list_groups(self) -> list[dict]:
    """Return distinct groups with project count."""
    async with self.get_session() as session:
        stmt = (
            sa.select(
                project_contexts.c.project_group,
                sa.func.count().label('project_count'),
            )
            .where(project_contexts.c.project_group.is_not(None))
            .group_by(project_contexts.c.project_group)
            .order_by(project_contexts.c.project_group)
        )
        result = await session.execute(stmt)
        return [{"group": row[0], "count": row[1]} for row in result.fetchall()]
```

3. Expose in `ProjectContextService` as `list_groups()`.

4. Add MCP tool in `project_context_tools.py`:
```python
@mcp.tool()
async def brain_list_project_groups() -> str:
    """List all project groups with their project count."""
    groups = await project_context_svc.list_groups()
    if not groups:
        return "No project groups defined."
    lines = ["## Project Groups\n"]
    for g in groups:
        lines.append(f"- **{g['group']}**: {g['count']} projects")
    return "\n".join(lines)
```

5. Register in `register_tools()` or ensure auto-registration.

6. **Tests** — `tests/unit/test_project_group_tool.py`:
- Test `list_groups()` returns correct format
- Test empty case

7. Run `ruff format` + tests.

---

## Batch 4: Data Migration + Neo4j + External Refs (sequential)

### Task 4.1: Apply migration + sync Neo4j + update configs

**Steps:**

1. Backup PG:
```bash
docker exec brain_v42_postgres pg_dump -U brain brain > /tmp/brain_backup_$(date +%Y%m%d).sql
```

2. Run Alembic migration:
```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
.venv/bin/alembic upgrade head
```

3. Neo4j sync — rename Project nodes:
```cypher
MATCH (p:Project) WHERE p.project_key CONTAINS '_'
// For each renamed key, update the node
MATCH (p:Project {project_key: 'red_ml'}) SET p.project_key = 'red-ml'
MATCH (p:Project {project_key: 'brain_v42'}) SET p.project_key = 'brain-v42'
// etc for all renames
// Delete orphan projects
MATCH (p:Project) WHERE p.project_key IN ['e2e_test_cleanup', 'e2e_test_project', 'test_deploy', 'test_pock'] DETACH DELETE p
```

4. Also rename entity nodes that have project_key as a property.

5. Update external refs:
- `CLAUDE.md` — change `brain_v42` to `brain-v42` in brain_session_start references
- `MEMORY.md` — update project_key references
- `~/.claude.json` — no change needed (MCP config doesn't use project_key)
- CLAUDE.md files in Red repos that reference project_keys

6. Verify:
```bash
.venv/bin/python -m pytest tests/unit -x -q
docker exec brain_v42_postgres psql -U brain -d brain -c "SELECT project_key, project_group FROM project_contexts ORDER BY project_group, project_key"
```
