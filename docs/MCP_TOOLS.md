# MCP Tools — brain_v42

**Updated:** 2026-07-24
**Repository registry:** 49 always-on + 2 graph-gated = 51 in the native profile; the gated tools are `brain_get_neighbors` and `brain_graph_path`.
**Default catalog:** Admin clients use `compact` while capability enforcement is disabled: the seven session lifecycle tools plus `brain_find_tool` and `brain_call_tool`; other registered tools remain discoverable through those gateways. `native` exposes every registered tool. An authenticated Dream phase always receives its exact native allowlist, independent of presentation headers, and cannot access either gateway. Experimental `brain_code_mode` takes precedence only while Dream capability enforcement is disabled.
**Transport:** HTTP loopback `http://127.0.0.1:8765/mcp` (production fleet). Tools are defined as closures capturing injected services — see `src/brain_v42/mcp/server.py` (`build_services()`) and the `register_*_tools()` functions in each module under `src/brain_v42/mcp/tools/`.

Most tools return formatted markdown strings. The seven v4 session lifecycle tools return structured Pydantic results. Their repository contract is documented below. Lifecycle v4 has run in production since 24 July 2026, after revision 036, explicit schema proof and a restart-last MCP cutover with authenticated E2E canaries.

Migration 046 is the repository target: it gives sessions their identity (`connection_id`,
`started_by_actor`, `intent`, `nature`) and the `closed_inactive` terminal state. Migration 045
widened `dream_runs.model` from `varchar(30)` to
`varchar(120)`, because two of the five configured phase models did not fit — and an overflow
loses the whole `dream_runs` row, not the column, the INSERT being best-effort. It follows
migration 044, itself after Dream revision 038, the timestamp-trigger isolation
of 039, the focus stamp of 040, the corpus provenance of 041 and `dream_runs.project_key` in 042.
Revision 043 dates the freshness STATUS — `freshness_status_updated_at` and `freshness_source` on
the six decay-tracked tables — because `updated_at` restarts on every counter write, so no honest
archive-residence clock existed. It was applied on 10 August 2026 and production measured `043`
with zero rows stamped. Both 042 and 043 are nullable and without backfill, so a reader that has
not migrated and a writer that has can coexist. Production was measured at head `042` on 8 August 2026, right after
the 041→042 cutover, with its 864 existing rows preserved and every one of them `NULL`;
`brain_session_start` now derives that revision at briefing time rather than restating it, because
this sentence claimed `037` for three days after the cutover.

## Catalog authorization boundary

SEC1a and SEC1b remain dormant while `BRAIN_DREAM_CAPABILITY_ENFORCEMENT=false`. In that state,
production HTTP and dev/fallback STDIO keep their historical global contracts, and the admin
principal stays global.
SEC1b changes no public tool names, catalogs, input schemas, or signatures.

When enforcement is active, FastMCP authenticates the admin bearer and a complete six-phase
`MCP_HTTP_DREAM_TOKENS` registry. Each Dream call is constrained first by phase, then by its
authoritative project claim across reads, writes, aggregates and search, graph traversal,
backfill and AutoLinker, promotions, update, and merge. Existing `project_key` arguments are
injected or checked; `project_group` and non-null `dream_run_id` are admin-only. ID-only and
aggregate tools receive request-local scope without new public parameters.

PostgreSQL remains the ownership authority and revalidates at the point of use. Missing,
foreign, ambiguous, and unowned references receive the same non-enumerating denial. Neo4j is
only an optional relationship index: scoped calls use a project-bounded knowledge subgraph,
require exactly one matching `BELONGS_TO` owner per anchor, and revalidate results in
PostgreSQL. Scoped authorization failures propagate and never degrade into success. Refusals
and logs omit UUIDs, arguments, content, bearer or registry material, SQL, and tracebacks.
Filesystem and root enforcement remain SEC1c.

The signatures, descriptions, examples, and legacy UUID errors below state the global
admin/dormant/STDIO contract; active Dream scope is narrower as described above. Configuration,
rollout, and rollback are documented in
[Architecture](ARCHITECTURE.md#future-capability-firewall-rollout).

## Tool-picking cheatsheet

| Intent | Tool |
|--------|------|
| Chose between options (A vs B, lib X vs Y) | `brain_log_decision` |
| Pure insight / gotcha (no code, no choice) | `brain_learn` |
| Reusable code pattern | `brain_save_snippet` |
| Reproducible step-by-step procedure | `brain_create_runbook` |
| Durable architectural decision | `brain_propose_adr` |
| Look something up across types | `brain_search` |
| Create an explicit roadmap feature | `brain_feature_create` |
| Start an explicit work session | `brain_session_start` |
| Attribute durable knowledge to a session | `brain_session_capture` |
| Refresh a long-running session's liveness | `brain_session_heartbeat` |
| Inspect open or historical sessions | `brain_session_list` |
| Resume an explicit open session | `brain_session_resume` |
| End a session and conditionally advance focus | `brain_session_end` |
| Discard a session without changing focus | `brain_session_abandon` |
| Refresh bounded workflow guidance | `brain_workflow_guide` |

`brain_learn` is the last-resort tool, never the default.

## Workflow guidance

### brain_workflow_guide (`workflow_guide_tools.py`)

```
brain_workflow_guide(project_key, workflow, phase="prepare",
                     known_guide_version=None, known_catalog_revision=None,
                     error_context=None)
```

Returns a bounded, read-only guide for the `session.lifecycle`, `project.context`,
`knowledge.decision`, `runbook.lifecycle`, or `ticket.lifecycle` workflow family. The response
reports `current`, `outdated`, or `unknown` freshness from the supplied guide and catalog
versions, with an explicit refresh action. `error_context` is never echoed.

Session lifecycle actions remain under exclusive user control on the agent and client side. Agents and hooks must not start, capture, heartbeat, resume, end, list, or abandon a session unless the user explicitly requests that command. The only server-side exception is the seven-day sweep documented under `brain_session_list`.

## Degraded search banners

`brain_search` prepends a banner line when the search path is degraded:

| `degraded` dict | Banner |
|-----------------|--------|
| `{"search_mode": "fts_fallback"}` | `degraded: embedding service indisponible — résultats FTS uniquement (ordre textuel)` |
| `{"rerank_mode": "rrf_fallback"}` | `degraded: reranker indisponible — ordre RRF (pas de re-scoring cross-encoder)` |
| `{"rerank_mode": "rrf_only"}` | `note: reranker non configuré — ordre RRF (pas de re-scoring cross-encoder)` |

`min_score` filtering is disabled in degraded mode to avoid silently returning zero results.

## UUID error contracts

The 13 legacy string-returning tools listed below normalize malformed UUIDs to:

```
✗ Invalid UUID: <value>
```

Two implementation paths produce this behaviour:

- **`parse_uuid()` from `parsing.py`** (10 call sites across `brain_tools.py`, `runbook_tools.py`, `snippet_tools.py`): `brain_supersede_decision`, `brain_get_supersession_chain`, `brain_validate_learning`, `brain_propose_adr` (source_learning_id path), `brain_accept_adr`, `brain_deprecate_adr`, `brain_create_runbook` (source_learning_id path), `brain_get_runbook` (runbook_id path), `brain_execute_runbook`, `brain_use_snippet`.
- **Inline `try/except UUID()` in `crud_tools.py`**: `brain_get`, `brain_update`, `brain_delete`.

All 13 tools return the same `✗ Invalid UUID: <value>` message on invalid input. The v4 session tools declare UUID parameters in their FastMCP schemas and therefore use MCP input validation instead of this formatted-string contract.

## Removed / deprecated (no longer exposed)

| Old tool | Replacement |
|----------|-------------|
| `brain_recall` | `brain_search(types=["learning"])` |
| `brain_find_snippet` | `brain_search(types=["snippet"])` |
| `brain_search_decisions` | `brain_search(types=["decision"])` |
| `brain_search_runbooks` | `brain_search(types=["runbook"])` |
| `brain_what_do_i_know_about` | `brain_search(group_by_type=True)` |
| `brain_get_project_context` | Aucun équivalent read-only exact. Utiliser `brain_list_projects` pour un aperçu, ou `brain_session_resume(session_id, expected_client_key)` après une reprise explicitement demandée. Réserver `brain_session_start` à une ouverture explicite. |

---

## Decisions — 3 tools (`brain_tools.py`)

### brain_log_decision
```
brain_log_decision(title, context, decision_made, reasoning,
                   alternatives=None, consequences=None,
                   project_key=None, tags=None, related_to=None)
```
Log a technical/architectural decision (the WHY, not just the WHAT). Embeds, writes to PG, fans out to Neo4j (`BELONGS_TO` + `related_to` edges).
Example: `brain_log_decision(title="Use pgvector over Chroma", context="...", decision_made="...", reasoning="...")`.

### brain_supersede_decision
```
brain_supersede_decision(old_decision_id, title, context, decision_made,
                         reasoning, alternatives=None, consequences=None,
                         project_key=None, tags=None)
```
Create a new decision and mark the old one `status=superseded`, `superseded_by=new_id`. Atomic (one transaction).

### brain_get_supersession_chain
```
brain_get_supersession_chain(decision_id)
```
Walk the supersession chain (recursive CTE) from any decision in the chain — returns oldest -> newest.

---

## Learnings — 2 tools (`brain_tools.py`)

### brain_learn
```
brain_learn(topic, insight, source=None, source_type="experience",
            confidence="medium", project_key=None, tags=None, related_to=None)
```
Record a pure insight/gotcha. `source_type` in {experience, documentation, code_review, bug, external, article, video, book, conversation, research, automated}. `confidence` in {low, medium, high}.

### brain_validate_learning
```
brain_validate_learning(learning_id)
```
Stamp `validated_at = now()` — signal that this insight has been confirmed in practice.

---

## Snippets — 2 tools (`snippet_tools.py`)

### brain_save_snippet
```
brain_save_snippet(title, intention, code, language,
                   dependencies=None, usage_example=None, gotchas=None,
                   project_key=None, tags=None, related_to=None)
```
Save a reusable snippet keyed by `intention` (the embedded field). Write intention as a sentence describing WHEN to use the code.

### brain_use_snippet
```
brain_use_snippet(snippet_id)
```
Increment `use_count`, set `last_used_at = now()`. Returns `✗ Invalid UUID: <value>` if `snippet_id` is malformed.

---

## ADRs — 4 tools (`brain_tools.py`)

### brain_propose_adr
```
brain_propose_adr(title, context, decision, consequences, project_key,
                  alternatives_considered=None, tags=None,
                  source_learning_id=None, auto_accept=False, dream_run_id=None)
```
Propose an Architecture Decision Record (`status=proposed`). Dream-agent path: pass `source_learning_id` + `auto_accept=True` together to graduate a mature learning straight to `accepted` in one transaction — writes a `dream_promotions` row for audit. Both kwargs must be set together.

### brain_accept_adr
```
brain_accept_adr(adr_id)
```
Flip `status=proposed` -> `status=accepted`, stamp `decided_at`.

### brain_deprecate_adr
```
brain_deprecate_adr(adr_id, reason=None)
```
Set `status=deprecated`. Optional reason appended to consequences. Returns `✗ Invalid UUID: <value>` if `adr_id` is malformed.

### brain_list_adrs
```
brain_list_adrs(project_key=None, status=None, limit=20, offset=0)
```
Filter ADRs by project and status in {proposed, accepted, deprecated, superseded}.
**Limit**: clamped server-side to [1, 100].

Alias de compatibilité temporaire : pendant cette fenêtre de migration,
préférer `brain_list(entity_type="adr")`. Les deux noms partagent le même
adaptateur de liste ADR et excluent les éléments archivés (`include_archived=False`).
Un retrait éventuel de `brain_list_adrs` exige un ticket ultérieur fondé sur une
preuve d'usage et une décision explicite ; cet alias reste enregistré jusque-là.

---

## Runbooks — 3 tools (`runbook_tools.py`)

### brain_create_runbook
```
brain_create_runbook(title, description, project_key, trigger, steps,
                     prerequisites=None, rollback_steps=None,
                     estimated_duration=None, tags=None,
                     source_learning_id=None, dream_run_id=None)
```
`steps` is a list of `{order?, description, command?, verification?}`. Dream-agent path: `source_learning_id` graduates a learning into a runbook atomically (no accept state machine, so no `auto_accept`).

### brain_get_runbook
```
brain_get_runbook(runbook_id=None, title=None, project_key=None, limit=10)
```
Three dispatch modes:
- `runbook_id` — fetch one runbook by UUID (returns `✗ Invalid UUID` if malformed)
- `(title, project_key)` — fetch by exact title match within project
- `project_key` alone — list all runbooks for project (**limit default 10, max 50**; trailing notice if more exist)

### brain_execute_runbook
```
brain_execute_runbook(runbook_id, status="success")
```
Increment `execution_count`, stamp `last_executed_at`, set `last_execution_status` in {success, failed, partial}. Returns `✗ Invalid UUID` if `runbook_id` is malformed.

---

## Global search — 1 tool (`brain_tools.py`)

### brain_search
```
brain_search(query, types=None, project_key=None, project_group=None,
             limit=20, min_score=0.2, include_archived=False,
             group_by_type=False, tags=None, include_related=False)
```
Hybrid semantic search: pgvector fan-out across services + `BatchingRerankerClient` rerank (20 ms coalescing window). `types` subset of {decision, learning, snippet, runbook, adr, plan}. `project_key` XOR `project_group` scope. `tags` filter by overlap. Results render with `[s:score]` prefix sorted by score desc. `group_by_type=True` groups output into sections (former `brain_what_do_i_know_about`). `include_related=True` appends a `### Related` graph-neighbour block.

**Limit**: clamped server-side to [1, 100]. Degraded banners: see top of document.

Examples:
- `brain_search("pgvector migration", types=["decision"])`
- `brain_search("neo4j setup", group_by_type=True, project_key="brain-v42")`
- `brain_search("webhook ingestion", project_group="red-stack")` (admin/dormant/STDIO only)

---

## Graph traversal — 2 tools (`brain_tools.py`), conditional on `graph_enabled=true`

### brain_get_neighbors *(graph-gated)*
```
brain_get_neighbors(entity_id, rel_types=None, depth=1)
```
Return the local neighbourhood (1-3 hops, clamped) around an entity. `rel_types` subset of {SUPERSEDES, MOTIVATED_BY, IMPLEMENTS, DOCUMENTS, USES, RELATED_TO}. Use for targeted traversal; prefer `brain_search(include_related=True)` for search-driven discovery.

### brain_graph_path *(graph-gated)*
```
brain_graph_path(source_id, target_id, max_depth=3, rel_types=None)
```
Return the shortest graph path between two entities (1-6 hops, clamped). Discovers how two seemingly unrelated entities are connected. By default excludes `BELONGS_TO_DOMAIN` edges to avoid tautological paths via shared Domain nodes. Returns a markdown path string or a "no path found" message.

---

## Session lifecycle — 7 tools (`session_lifecycle_tools.py`, v4.0)

These tools implement explicit, persistent session actions. Only an explicit user command may invoke them; hooks and agents must never infer a start, capture, heartbeat, resume, end, list, or abandon action. Migration 037 extends the schema created by migration 032 and depends on revision 036. It is active on the production database since 24 July 2026; fresh or restored environments must still prove their own Alembic head before enabling this runtime.

Every structured result that contains `session` uses the same `BrainSession` shape:

- identity: `id` (persistent UUID), `project_key`, `client_key`, `status`;
- start snapshot: `started_focus`, `started_focus_revision`;
- terminal snapshot: `summary`, `next_focus`, `captured_knowledge_ids`, `nothing_to_capture_reason`, `abandonment_reason`, `end_expected_focus_revision`, `focus_outcome`, `focus_at_end`, `focus_revision_at_end`;
- ledger view: `attributed_knowledge_ids`, rehydrated for open, ended, and abandoned sessions;
- liveness: `last_heartbeat_at`, plus derived `is_stale`;
- timestamps: `started_at`, `ended_at`, `updated_at`.

`expected_client_key` is required together with `session_id` by capture, heartbeat, resume, end, and abandon. The pair must match before the requested action proceeds. This second key prevents an accidentally supplied peer-session UUID from being acted on, but it is an isolation guard, not authentication or authorization.

Public input bounds are enforced by the MCP schema: `project_key` is 1–50 characters,
`client_key` and `expected_client_key` 1–128, `summary` and `next_focus` 1–10,000,
and either reason field 1–2,000. `expected_focus_revision` and `offset` must be
non-negative; `limit` is between 1 and 100. Each capture batch contains 1–100 unique UUIDs,
and one session may own at most 100 captured artifacts in total.

### brain_session_start
```
brain_session_start(project_key, client_key)
-> {session, replayed, open_session_count, briefing}
```
Start a session for an existing project and return its UUID plus the action-forward project briefing. `client_key` identifies one intended session: reuse exactly the same stable key for every retry of that session, and assign a distinct stable key to each parallel session. Retrying the same open `(project_key, client_key)` returns the same session with `replayed=true`; reusing the key after a terminal state is a conflict.

Different client keys may create concurrent open sessions for the same project. `open_session_count` is informational and never blocks the start.

The session is committed before the optional briefing is assembled. A total briefing
failure is logged and returned as an unavailable briefing marker, while the persisted
session UUID remains visible to the caller.

### brain_session_resume
```
brain_session_resume(session_id, expected_client_key)
-> {session, open_session_count, current_focus,
    current_focus_revision, briefing}
```
Attach to an existing `open` session after the UUID/client-key guard passes, without mutating it. Ended and abandoned sessions cannot be resumed. Resume does not refresh liveness; issue an explicit heartbeat for a long-running session. Use the returned current focus revision before attempting `brain_session_end`.
The nested session also restores every existing ledger attribution in
`attributed_knowledge_ids`, so a client can recover safely after losing local state.

### brain_session_capture
```
brain_session_capture(session_id, expected_client_key, knowledge_ids)
-> {session, captured_knowledge_ids, newly_captured_knowledge_ids,
    replayed_knowledge_ids, replayed}
```
Persist client-declared provenance in the `brain_session_artifacts` ledger and refresh the session heartbeat. This is an explicit attribution by the caller, not automatic discovery or proof that the same client created the artifact.

Each UUID must resolve unambiguously to a decision, learning, snippet, runbook, ADR, or indexed plan in the same project, created at or after the session start. One knowledge UUID can belong to only one session globally. Replaying IDs already owned by the same session is safe; an ID owned by another session conflicts, and mixed batches report newly captured and replayed IDs separately.
An exact retry remains read-only and successful after that owning session has ended or been
abandoned; it never reopens the terminal session.

### brain_session_heartbeat
```
brain_session_heartbeat(session_id, expected_client_key)
-> {session}
```
Refresh `last_heartbeat_at` and `updated_at` for an `open` session without changing its lifecycle status or project focus. Terminal sessions reject heartbeats.

### brain_session_list
```
brain_session_list(project_key=None, status="open", limit=20, offset=0)
-> {sessions, total, limit, offset}
```
List sessions in reverse start order. `status` is one of `open | stale | ended | abandoned | all`; `limit` is bounded to 1–100.
Every listed session includes its current ledger view in `attributed_knowledge_ids`, including
abandoned sessions whose terminal `captured_knowledge_ids` snapshot must remain empty.

An open session becomes `is_stale=true` when its last heartbeat is at least 24 hours old. `status="stale"` selects that subset of open sessions; this derived flag never changes the persisted `status` and never auto-closes a session. The regular `open` filter therefore includes both fresh and stale open sessions. Do not confuse this 24-hour display flag with the separate seven-day server-side sweep, which is the only mechanism that moves an open session to `abandoned` without an explicit command (`abandonment_reason = 'auto_stale_7d'`).

### brain_session_end
```
brain_session_end(session_id, expected_client_key, summary, next_focus,
                  expected_focus_revision,
                  nothing_to_capture_reason=None)
-> {session, replayed, remaining_open_session_count,
    current_focus, current_focus_revision, focus_outcome,
    focus_at_end, focus_revision_at_end}
```
End a session and attempt the shared project-focus update in one transaction. `summary` and `next_focus` must be non-blank. End reads and revalidates the persistent capture ledger; it no longer accepts captured UUIDs in its own payload.

The capture outcome is an exclusive choice:

- the session ledger already contains 1–100 valid artifacts and `nothing_to_capture_reason` is omitted; or
- the ledger is empty and a non-blank `nothing_to_capture_reason` is provided.

Invalid or missing capture evidence rolls back the transaction and leaves the session open. A focus revision mismatch is instead a normal terminal outcome: focus remains unchanged, the session still becomes `ended`, and `focus_outcome="conflict"` is persisted with the observed `focus_at_end` and `focus_revision_at_end`. A matching revision applies `next_focus`, increments the revision even when the text is unchanged, and persists `focus_outcome="applied"` with the resulting focus snapshot.

Replaying the exact terminal payload returns `replayed=true` and the original persisted focus outcome/snapshot; a different payload conflicts. `current_focus` and `current_focus_revision` report the project state at response time and may therefore differ from the persisted end snapshot on a later replay.

### brain_session_abandon
```
brain_session_abandon(session_id, expected_client_key, reason)
-> {session, replayed, remaining_open_session_count}
```
After the UUID/client-key guard passes, explicitly mark an open session `abandoned` without updating project focus. `reason` must be non-blank. Retrying the same abandonment reason returns `replayed=true`; a different terminal request conflicts.
Abandon does not delete or release ledger ownership. The returned session therefore retains
those UUIDs in `attributed_knowledge_ids`.

Downgrading migration 037 is intentionally fail-closed when v3 would lose an unsnapshotted
ledger attribution or a `focus_outcome="conflict"`. Operators must resolve or export those
states before an offline 037→036 rollback.

No lifecycle tool infers a session from project, list order, or recency.

The briefing returned by start and resume is assembled in `session_tools.py`.

---

## Project context — 4 tools

### brain_set_project_context (`project_context_tools.py`)
```
brain_set_project_context(project_key, name, description,
                          languages=None, frameworks=None, databases=None,
                          code_style=None, git_workflow=None, test_strategy=None,
                          current_phase=None, current_focus=None, blockers=None,
                          related_projects=None, plan_scan_paths=None,
                          gitlab_project_path=None, project_group=None)
```
Upsert by `project_key`. `plan_scan_paths` drive `PlanIndexer`; `gitlab_project_path` drives webhook ingestion; `project_group` allows cross-project search.

### brain_update_project_focus (`project_context_tools.py`)
```
brain_update_project_focus(project_key, current_focus, expected_focus_revision,
                           blockers=None, feature_status=None, unpin=None)
```
Compare `expected_focus_revision` to the current project revision, then apply focus, blockers, feature statuses, and pins in one PostgreSQL transaction. `feature_status` uses exact feature names and the canonical statuses `planned | research | design | building | deployed | done | archived`. An invalid status, missing or ambiguous feature, merged-feature reactivation, overlap with `unpin`, or revision conflict rolls back the complete batch. Every successful composite mutation consumes the revision, even when the focus text is unchanged. The project's dynamic CLAUDE.md section is updated afterward on a best-effort basis and is not part of the transaction.

### brain_list_projects (`project_context_tools.py`)
```
brain_list_projects(project_group=None)
```
List all known projects with focus and phase.

### brain_list_project_groups (`project_context_tools.py`)
```
brain_list_project_groups()
```
List groups with project count. Pair with `brain_search(project_group=...)`.

---

## Roadmap — 3 tools (`roadmap_tools.py`)

### brain_get_roadmap
```
brain_get_roadmap(project_key=None, full=False)
```
Features grouped by project with status (`planned|research|design|building|deployed|done|archived`), artifact counts, and last-activity timestamps. Features may be created explicitly with `brain_feature_create`; automatic artifact, plan, and GitLab signals still pass through `ClusterGuard`. `archived` is accepted by status-update paths only, rejected on explicit creation, and hidden from the live roadmap view.

**Anti-token-bomb**: the all-projects view carries TWO caps, because two different things grow. Each project's feature list is capped at **20 features**, and only the **10 most recently active projects** are rendered. Each cap has its own trailing notice naming what it dropped, and its own escape hatch — they are not interchangeable: `project_key=...` returns every feature of one project, `full=True` returns every project. `full=True` lifts the PROJECT cap only; the per-project feature cap still applies, otherwise the hatch would emit roughly twice the output the cap exists to prevent.

The project cut is by **recency, not input order**: rows arrive ordered by `project_key`, so slicing the head would keep whatever sorts alphabetically first and silently drop active work — measured, a naive slice dropped 14 projects touched within 30 days.

### brain_feature_create
```
brain_feature_create(name: str, description: str, project_key: str,
                     status: str = "planned", pinned: bool = True) -> str
```
Crée explicitement une feature dans un `project_context` existant. Inspecter d'abord
`brain_get_roadmap(project_key=...)` : ce chemin contourne `ClusterGuard` et ne déduplique pas
sémantiquement. `name` et `description` sont trimés, doivent être non vides et sont limités
respectivement à 200 et 10 000 caractères.
`project_key` est trimé, limité à 50 caractères et doit contenir des segments alphanumériques
minuscules séparés par `-` ou `:`; les alias `brain` et `brain_v42` sont canonisés en `brain-v42`.

`status` accepte `planned | research | design | building | deployed | done` et vaut `planned`
par défaut; `archived` est refusé à la création. `pinned` vaut `true` par défaut et peut être
passé à `false`. Un nom déjà présent dans le même projet, après trim et comparaison exacte
insensible à la casse, est refusé. Une validation invalide, un projet absent, un doublon ou
un embedding indisponible, non numérique, non fini ou de dimension différente de
`EMBEDDING_DIMENSION` (1536 par défaut) renvoie `✗ ...` sans créer de feature. La portée de
l'unicité et le choix des deux writers sont documentés dans la
[décision de création explicite](superpowers/specs/2026-07-23-explicit-roadmap-feature-creation-design.md).

**Exemple** : `brain_feature_create("Recherche hybride", "Ajouter FTS + vecteurs.", "brain-v42")`

### brain_feature_update

Write-back session → roadmap : met à jour le statut d'une feature (spec 2026-07-04 §6).

**Signature** : `brain_feature_update(feature: str, status: str, project_key: str) -> str`

- `feature` : nom exact → préfixe d'id git-style (≥8 hex, tirets ignorés) → fragment unique du nom (ILIKE). Ambiguïté → erreur listant les candidats (id + nom).
- `status` : `planned | research | design | building | deployed | done | archived`.
- Side-effects : `status_updated_at=now()`, `pinned=true`.
- `brain_update_project_focus(..., expected_focus_revision=..., feature_status=...)` sert aux mutations composites atomiques; `brain_feature_update` sert à une mise à jour autonome.

**Exemple** : `brain_feature_update("Recherche hybride", "deployed", "brain-v42")`

---

## Plans — 1 tool (`plan_tools.py`)

### brain_reindex_plans
```
brain_reindex_plans(project_key=None)
```
Rescan markdown specs/plans in `plan_scan_paths`, chunk + embed, link to features via `ClusterGuard`. Skips unchanged files by content hash. Also consolidates pre-existing mirror-path duplicates (`dedupe_plans`). Results land in `indexed_plans` + `indexed_plan_chunks`.

---

## Generic CRUD — 4 tools (`crud_tools.py`)

Work across `entity_type` in {decision, learning, snippet, runbook, adr, plan}.

### brain_get
```
brain_get(entity_type, entity_id, max_chars=8000)
```
Fetch one entity by type + UUID. Returns `✗ Invalid UUID: <value>` if `entity_id` is malformed.

For `entity_type="plan"`: renders the plan header + chunk list bounded by `max_chars` (default **8000 chars**). A trailing notice identifies omitted chunks — increase `max_chars` to retrieve more content.

### brain_list
```
brain_list(entity_type, project_key=None, limit=20, offset=0,
           status=None, confidence=None, language=None,
           tags=None, include_archived=False, summary_only=False)
```
List with per-type filters. `runbook` requires `project_key`. `include_archived=False` (default) hides merged / archived entities.

**Limit**: clamped server-side to [1, 100].

`summary_only=True` (decision/learning only): drops body content and surfaces `project_key`, `tags`, `access_count`, `freshness_status` in compact rows. Used by Dream REORG Part 1 for full-corpus pagination without token cost.

### brain_update
```
brain_update(entity_type, entity_id, fields, related_to=None)
```
Partial update validated through the per-type `<Entity>Update` Pydantic model. `related_to` adds graph edges when Neo4j is enabled. Returns `✗ Invalid UUID` if `entity_id` is malformed. `plan` is immutable — rerun `brain_reindex_plans`.

### brain_delete
```
brain_delete(entity_type, entity_id)
```
Hard delete; no soft-delete here — use `brain_merge_entities` if you want audit + archive. Returns `✗ Invalid UUID` if `entity_id` is malformed.

---

## Decay & consolidation — 4 tools (`decay_tools.py`)

### brain_decay_status
```
brain_decay_status()
```
Freshness distribution (fresh/stale/archived) per type + deletion candidates (`archived >= 180 days, access_count = 0`).

### brain_refresh_entity
```
brain_refresh_entity(entity_type, entity_id)
```
Reset `freshness_status='fresh'`, stamp `last_accessed_at=now()`.

### brain_consolidation_candidates
```
brain_consolidation_candidates(entity_type=None, limit=20)
```
List quasi-duplicate pairs detected by embedding similarity (`ConsolidationJob`). Filters out already-merged rows.

### brain_merge_entities
```
brain_merge_entities(entity_type, source_id, target_id)
```
Keep `target`, archive `source` with `merged_into=target_id`, union `tags`. Writes a row to `consolidation_log` for audit.

---

## Dream mode — 5 tools (`dream_tools.py`)

`brain_backfill_links_batch`, `brain_get_clusters`, `brain_list_orphans_for_classification`, and `brain_assign_domain` all require `graph_enabled=true` — they return an error string otherwise.

### brain_backfill_links_batch
```
brain_backfill_links_batch(entity_type=None, limit=50,
                           threshold=0.6, max_links=3)
```
Find entities in Neo4j with zero `RELATED_TO` edges, fetch their PG embeddings, and call `AutoLinker` to create missing semantic links. Used by the CONNECT phase of the nightly dream orchestrator.

**Contrat `max_links`** (décision opérateur 2026-08-18, ticket fb62624f) : le plafond borne les liens **réussis** (`created` + `matched`). Les erreurs ne le consomment pas ; les tentatives sont bornées de fait par les `2×max_links` candidats sélectionnés, donc une entité dont toutes les écritures échouent peut rapporter jusqu'à `2×max_links` erreurs. Pour estimer un nombre d'entités fautives depuis `errors`, diviser par `2×max_links`, jamais par `max_links`.

### brain_get_clusters
```
brain_get_clusters(min_size=2, limit=20,
                   max_members_per_cluster=30)
```
Run union-find over all `RELATED_TO` edges, return connected components sorted by size. Each cluster member is enriched with PG metadata (type + title).

**Anti-token-bomb**: members per cluster are capped at `max_members_per_cluster` (default **30**). A trailing notice identifies the number of omitted members.

### brain_list_orphans_for_classification
```
brain_list_orphans_for_classification(limit=20)
```
List cross-domain orphans (entities with zero `RELATED_TO` edges AND no `BELONGS_TO_DOMAIN`) ready for Domain-node assignment by the Dream CONNECT phase. Returns a JSON array of `{id, type, topic, tags, project_key}`.

`limit` is clamped to [1, 50]. Returns `"[]"` when the graph is at domain-equilibrium.

Allowed domain names (closed set): `infra`, `ml`, `backend`, `memory`, `tooling`, `data`, `ops`, `frontend`, `security`.

### brain_list_curation_proposals
```
brain_list_curation_proposals(project_key, status="proposed", limit=20, offset=0)
```
Read back the roadmap curation proposals the nightly ROADMAP phase writes. That phase is
proposer-only by design and never applies anything, so the rows accumulate: **499 measured
2026-08-11**, 43 of them for `brain-v42`. Nothing in the catalogue could show them, and the
only apply/reject surface lives behind the Codex gateway — a reviewer had to open a read-only
psql transaction to decide item by item.

`project_key` is **required**: this table carries no project key of its own, so scoping goes
through a join on `features`, and an unscoped read would return every project's proposals
under a scoped request. `status` defaults to `proposed` because the applied and rejected rows
outnumber the ones left to decide.

Unlike its neighbours in this file, it needs no graph — it reads PostgreSQL directly.

**Anti-token-bomb**: `limit` is capped at **100** and the cap is announced when it bites; each
proposal's free-form JSONB `payload` is truncated at 200 characters.

### brain_assign_domain
```
brain_assign_domain(entity_id, domain_name)
```
Write a `BELONGS_TO_DOMAIN` edge from an entity to a Domain node. Called by the Dream CONNECT phase after local classification. Upserts the Domain node first, then creates the edge. Returns `"created"`, `"matched"`, `"missing_node"`, `"invalid_domain"`, `"invalid_entity_id"` or `"error"` — `"error"` covers both a failed write and an entity that is no longer an active graph endpoint (archived between the orphan listing and the call; logged as `mcp.brain_assign_domain.unknown_graph_endpoint` instead of escaping as an opaque exception).

---

---

## Tickets (coordination adressée) — 5 tools (`ticket_tools.py`)

Famille **coordination** — orthogonale à la famille mémoire. Les tickets sont adressés (un émetteur `from_project`, un destinataire `to_project`), stateful (machine à états), et **exclus** de `brain_search`, embeddings, decay, classification domaines et sync Neo4j.

> Note: ces 5 tools ne participent PAS à `brain_search` / embeddings — famille coordination, pas mémoire.

| Tool | Signature | Rôle |
|------|-----------|------|
| `brain_ticket_create` | `(from_project, to_project, kind, title, body, extraction=None)` | Ouvre un ticket adressé. `kind` in `{'request', 'fyi'}`. Les deux projets doivent exister (`brain_set_project_context`). |
| `brain_ticket_reply` | `(ticket_id, author_project, body)` | Poste un message dans le fil — tout statut, participants uniquement. |
| `brain_ticket_transition` | `(ticket_id, author_project, action, message=None)` | Change le statut via la machine à états. `message` optionnel est ajouté au fil. |
| `brain_ticket_list` | `(project_key)` | Liste les tickets groupés par action requise : à traiter / à confirmer / en attente. |
| `brain_ticket_get` | `(ticket_id)` | Vue complète : header, body, fil de messages, actions possibles. |

`from_project == to_project` est valide : le projet assume alors les rôles demandeur et
exécutant. Un `request` de ce type sert de note-to-self et réapparaît dans son briefing.

### Actions `brain_ticket_transition` par rôle

| Action | Rôle requis | Transitions |
|--------|-------------|-------------|
| `start` | executor (`to_project`) | `open → in_progress` |
| `resolve` | executor | `open → resolved`, `in_progress → resolved` |
| `wontfix` | executor | `open → wontfix`, `in_progress → wontfix` |
| `ack` | executor | `open → acked` (fyi uniquement) |
| `confirm` | requester (`from_project`) | `resolved → closed`, `wontfix → closed` |
| `reopen` | requester | `resolved → open`, `wontfix → open` |
| `cancel` | requester | `open/in_progress/resolved/wontfix → closed` |

Statuts terminaux : `closed`, `acked` — aucune transition possible depuis ces statuts. La discussion (`brain_ticket_reply`) reste autorisée quel que soit le statut.

### Exemple — cycle request complet (create → resolve → confirm)

```
# 1. red-shrik ouvre une demande vers red-data
brain_ticket_create(from_project="red-shrik", to_project="red-data",
                    kind="request", title="Exposer /api/signals en ndjson",
                    body="Besoin d'un stream ndjson pour le dashboard.")
# → ticket #a1b2c3 créé, status=open

# 2. red-data prend en charge
brain_ticket_transition(ticket_id="<uuid>", author_project="red-data",
                        action="resolve", message="Déployé en v2.4.1")
# → status=resolved

# 3. red-shrik confirme (→ closed, extraction_status=pending)
brain_ticket_transition(ticket_id="<uuid>", author_project="red-shrik",
                        action="confirm", message="Validé en staging")
# → status=closed · extraction_status=pending
```

### Exemple — fyi (create → ack)

```
# red-data notifie red-shrik d'un breaking change
brain_ticket_create(from_project="red-data", to_project="red-shrik",
                    kind="fyi", title="Breaking: champ 'price' renommé 'close'",
                    body="Déployé en v3.0 — mettre à jour les consommateurs.")
# → ticket créé, status=open

# red-shrik accuse réception (→ acked, extraction_status=pending)
brain_ticket_transition(ticket_id="<uuid>", author_project="red-shrik", action="ack")
# → status=acked · extraction_status=pending
```

À la clôture (`closed` ou `acked`), `extraction_status` passe à `pending` — le job nocturne `scripts/ticket_extract.py` (step EXTRACT de `dream.sh`) propose des learnings/decisions extraits du fil dans `ticket_extraction_proposals`, reviewables puis applicables manuellement ou en WET run.

Avant l'INSERT, une gate vectorielle exacte et limitée au projet cible élimine les doublons `>= 0,85` contre les learnings/décisions actifs et entre drafts du même run. Elle est fail-closed : ligne active avec embedding absent ou non comparable (norme `<= 1e-6`), nouveau vecteur invalide ou lecture corpus impossible → aucun draft du run n'est persisté et les tickets restent `pending`. Le seuil n'est pas encore calibré ; EXTRACT reste en DRY pendant le soak.

`--apply-ids` est un override opérateur pour des proposals déjà relues : il ne rejoue pas la gate corpus automatique.

---

## Tool registration map

| File | Group | Tools |
|------|-------|-------|
| `brain_tools.py` | decisions / learnings / ADRs / search / graph | 10 + 2 conditional |
| `crud_tools.py` | generic CRUD | 4 |
| `decay_tools.py` | decay + consolidation | 4 |
| `dream_tools.py` | dream-phase maintenance | 5 |
| `plan_tools.py` | plan indexing | 1 |
| `project_context_tools.py` | project + groups | 4 |
| `roadmap_tools.py` | roadmap | 3 |
| `runbook_tools.py` | runbooks | 3 |
| `session_lifecycle_tools.py` | persistent session lifecycle | 7 |
| `snippet_tools.py` | snippets | 2 |
| `ticket_tools.py` | tickets cross-projet (coordination) | 5 |
| `workflow_guide_tools.py` | bounded workflow guidance | 1 |
| **Total** | | **49 always-on + 2 graph-gated = 51** |
