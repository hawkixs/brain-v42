# MCP Tools Completeness — Design Spec

**Date:** 2026-03-15
**Status:** Approved
**Context:** Audit revealed 21 service methods not exposed as MCP tools. Currently 27 tools, docstrings say 21.

## Problem

The brain_v42 MCP server exposes 27 tools but the underlying services have 21 additional public methods with no MCP exposure. Key gaps:

- **No get-by-id** for decisions, learnings, snippets, ADRs
- **No delete** for any entity type
- **No update** for any entity type (only supersede for decisions, validate for learnings)
- **No semantic search** for runbooks and ADRs
- **No list_projects** — cannot discover available project contexts
- **No deprecate_adr** — ADR lifecycle incomplete (propose → accept → ???)
- **No pagination offset** on search/listing tools
- **Stale docstrings** claim "21 tools" when there are 27

## Constraints

- LLM tool selection accuracy degrades beyond ~30 tools (Anthropic research)
- MCP Resources/Prompts not consumed by Claude Code yet — stay on Tools
- Must follow existing patterns (decay_tools.py generic dispatch)
- TDD workflow required
- Total tool count target: ~35

## Approach: Hybrid (Generic CRUD + Specific Tools)

Generic tools for uniform CRUD operations (pattern from `brain_refresh_entity` / `brain_merge_entities`). Specific tools for operations with unique semantics.

## New Tools

### 1. `brain_get` — Generic get-by-id

**File:** `src/brain_v42/mcp/tools/crud_tools.py` (new)

```python
async def brain_get(entity_type: str, entity_id: str) -> str
```

- `entity_type`: one of `decision`, `learning`, `snippet`, `runbook`, `adr`
- Dispatches to `{service}.get_by_id(UUID(entity_id))`
- Returns formatted markdown (reuse existing formatters per type)
- Returns error if not found or invalid type

### 2. `brain_delete` — Generic delete

**File:** `crud_tools.py`

```python
async def brain_delete(entity_type: str, entity_id: str) -> str
```

- Dispatches to `{service}.delete(UUID(entity_id))`
- Returns confirmation with short_id or error if not found
- Hard delete (not archive — archive is handled by decay/merge)

### 3. `brain_update` — Generic field update

**File:** `crud_tools.py`

```python
async def brain_update(
    entity_type: str,
    entity_id: str,
    fields: dict,  # keys = field names, values = new values
) -> str
```

- Dispatches to `{service}.update(UUID(entity_id), *Update(**fields))`
- Validates via Pydantic `*Update` model per type (DecisionUpdate, LearningUpdate, etc.)
- Returns updated entity formatted or validation error
- Re-embeds if text fields changed (title, description, insight, intention, etc.)

Valid fields per type (from existing `*Update` Pydantic models):
| Type | Updatable fields | Notes |
|------|-----------------|-------|
| decision | title, description, reasoning, alternatives, consequences, status, tags, project_key | See `DecisionUpdate` for full field list |
| learning | topic, insight, source, source_type, confidence, tags, project_key | See `LearningUpdate` |
| snippet | title, intention, code, language, dependencies, usage_example, gotchas, tags, project_key | See `SnippetUpdate` |
| runbook | title, description, trigger, steps, prerequisites, rollback_steps, estimated_duration, tags | `steps`/`rollback_steps` accept `list[dict]` — Pydantic coerces to `RunbookStep` |
| adr | title, context, decision, consequences, alternatives_considered, status, tags | See `ADRUpdate` |

**Validation path:** `fields` dict → `*Update(**fields)` → Pydantic validates and rejects unknown keys. On validation error, return formatted error message (no crash). Use keyword args for all service dispatch calls.

### 4. `brain_list` — Generic listing with filters

**File:** `crud_tools.py`

```python
async def brain_list(
    entity_type: str,
    project_key: str | None = None,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,       # decision, adr
    confidence: str | None = None,    # learning
    language: str | None = None,      # snippet
    tags: list[str] | None = None,    # all types
) -> str
```

- `status` filter applies to decisions (active/superseded) and ADRs (proposed/accepted/deprecated/superseded)
- `confidence` filter applies to learnings only
- `language` filter applies to snippets only
- `tags` filter applies to decision, learning, adr (snippet `list_snippets()` has no tags param)

**Dispatch map** (method names differ per type):
| entity_type | Method called | Notes |
|-------------|---------------|-------|
| decision | `decision_svc.list_all(project_key, status, tags, limit, offset)` | |
| learning | `learning_svc.list_all(project_key, confidence, tags, limit, offset)` | |
| snippet | `snippet_svc.list_snippets(project_key, language, limit, offset)` | Method is `list_snippets`, not `list_all` |
| runbook | `runbook_svc.list_by_project(project_key, limit, offset)` | `project_key` **required** — return error if None |
| adr | `adr_svc.list_all(project_key, status, limit, offset)` | |

### 5. `brain_deprecate_adr` — ADR lifecycle completion

**File:** `brain_tools.py`

```python
async def brain_deprecate_adr(adr_id: str, reason: str | None = None) -> str
```

- Sets ADR status to `deprecated` and stores reason in `consequences` field (no schema change needed — reuse existing column)
- Requires `ADRService.deprecate(adr_id, reason)` method (to add)

### 6. `brain_list_projects` — Project discoverability

**File:** `project_context_tools.py`

```python
async def brain_list_projects() -> str
```

- Returns all project contexts: project_key, name, current_focus, current_phase
- Requires `ProjectContextService.list_all()` method (to add)

### 7. `brain_search_runbooks` — Semantic search for runbooks

**File:** `runbook_tools.py`

```python
async def brain_search_runbooks(
    query: str,
    project_key: str | None = None,
    limit: int = 10,
    min_score: float = 0.2,
) -> str
```

- Uses hybrid search if available, falls back to semantic_search
- Service methods `search()` and `semantic_search()` already exist

### 8. `brain_get_supersession_chain` — Decision history

**File:** `brain_tools.py`

```python
async def brain_get_supersession_chain(decision_id: str) -> str
```

- Returns the full supersession chain for a decision (old → new → newer)
- Service method `get_supersession_chain()` already exists
- Formats as markdown timeline

## Modifications to Existing Tools

### Add `offset` parameter (default=0)

Rétro-compatible addition. **Only on tools where the service layer already supports `offset`:**
- `brain_search_decisions` — pass to `decision_svc.search(offset=)` / `decision_svc.list_all(offset=)` — **supported**
- `brain_list_adrs` — pass to `adr_svc.list_all(offset=)` — **supported**

**Deferred** (service/repo layer lacks `offset` on semantic_search, would require cascading changes):
- `brain_recall` — `learning_svc.semantic_search()` has no offset
- `brain_find_snippet` — `snippet_svc.semantic_search()` has no offset
- `brain_search` — `brain_svc.search()` has no offset, fans out to 5 services
- `brain_what_do_i_know_about` — same cascading issue

Semantic search pagination is architecturally different from CRUD pagination (vector similarity doesn't paginate cleanly with offset). These are deferred to a future PR.

### Update stale docstrings

- `server.py:6,12` — "21 brain_* tools" → "35 brain_* tools"
- `brain_tools.py:1` — "21 brain_* tool registrations" → update header

## Service Layer Changes

### ADRService — add `deprecate()`, `delete()`, `update()`

```python
async def deprecate(self, adr_id: UUID, reason: str | None = None) -> ADR | None:
    """Set ADR status to deprecated. Appends reason to consequences field."""

async def delete(self, adr_id: UUID) -> bool:
    """Hard delete an ADR. Repo method already exists (PgADRRepo.delete)."""

async def update(self, adr_id: UUID, data: ADRUpdate) -> ADR | None:
    """Update ADR fields. Repo method already exists (PgADRRepo.update).
    Re-embed if title/context/decision changed."""
```

Note: `PgADRRepo` already has `delete()` and `update()` — the service just needs to wrap them with embedding logic.

### ProjectContextService — add `list_all()`

```python
async def list_all(self) -> list[ProjectContext]:
    """List all project contexts. Repo method already exists (PgProjectContextRepo.list_all)."""
```

## New Formatter Functions

Add to `formatters.py`:
- `format_decision(d)` — single decision detail view (promote existing `_format_decision_item` to full detail)
- `format_learning(lr)` — single learning detail view (promote existing `_format_learning_item`)
- `format_snippet_detail(s)` — single snippet with full code (existing `_format_snippet_item` shows truncated code)
- `format_adr_detail(adr)` — single ADR full view with alternatives_considered
- `format_projects_list(contexts)` — project list summary (project_key, name, focus, phase)
- `format_supersession_chain(chain)` — timeline view (decision1 → decision2 → ...)

Note: `format_runbook(rb)` already exists as public function — reuse for `brain_get(entity_type="runbook")`.

## File Changes Summary

| File | Change |
|------|--------|
| `crud_tools.py` | **NEW** — brain_get, brain_delete, brain_update, brain_list |
| `brain_tools.py` | +brain_deprecate_adr, +brain_get_supersession_chain, +offset on brain_search_decisions, docstrings |
| `snippet_tools.py` | (no changes — offset on semantic search deferred) |
| `runbook_tools.py` | +brain_search_runbooks |
| `project_context_tools.py` | +brain_list_projects |
| `server.py` | +register_crud_tools(), docstrings |
| `tools/__init__.py` | +register_crud_tools export |
| `formatters.py` | +6 new formatter functions |
| `adr_service.py` | +deprecate(), +delete(), +update() methods |
| `project_context_service.py` | +list_all() method |

## Testing Strategy

TDD workflow — tests first for each new tool:
- Unit tests for each new tool function (mock services)
- Unit tests for new service methods (mock repos)
- Unit tests for new formatters
- Integration tests for crud_tools dispatch logic

## Tool Count

- Before: 27 tools
- New: +8 tools
- Total: 35 tools (under ~37 threshold)

## Future Considerations

- Migrate read-only tools to MCP Resources when Claude Code supports them
- Migrate brain_session_start to MCP Prompt
- Consider structured JSON return format alongside markdown for programmatic clients
