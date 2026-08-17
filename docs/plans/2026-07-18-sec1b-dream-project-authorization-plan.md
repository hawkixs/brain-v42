---
title: "SEC1b — Dream project and object authorization"
status: completed
completed_at: "2026-07-18"
summary: "Turn SEC1a's authenticated project claim into a fail-closed authorization boundary for all 19 tools available to scoped Dream phases, without changing public tool schemas or enabling the dormant policy."
tags:
  - sol-ultra
  - sec1
  - dream
  - authorization
  - project-isolation
  - pattern-auto
---

# SEC1b — Dream project and object authorization

## Goal

Make the canonical `project_key` claim issued by SEC1a authoritative. A scoped Dream bearer
may read, aggregate, create, update, link, classify, or merge only resources owned by its
claimed project. Forged project arguments, project-group expansion, foreign UUIDs, and
cross-project graph traversal are denied before protected work runs and are constrained again
at the data operation.

This is the second bounded SEC1 slice in the
[Sol Ultra roadmap](2026-07-11-sol-ultra-audit-roadmap-plan.md). SEC1a already authenticates
the six phase identities and filters their exact tool catalogs. SEC1b closes the remaining
object/project gap. Server-bounded filesystem roots remain SEC1c.

## Starting evidence

- `DreamCapabilityTokenVerifier` authenticates immutable `phase` and `project_key` claims,
  but `DreamCapabilityMiddleware.on_call_tool` currently checks only the phase allowlist.
- The union of the six allowlists contains exactly **19** tools. They mix public project
  filters, ID-only resources, aggregate queries, graph traversals, and implicit writes.
- `brain_list`, `brain_search`, `brain_learn`, `brain_save_snippet`, `brain_propose_adr`,
  `brain_create_runbook`, and `brain_list_adrs` expose caller-controlled project arguments.
- `brain_get`, `brain_delete`, `brain_update`, `brain_merge_entities`,
  `brain_assign_domain`, `brain_get_neighbors`, and `brain_graph_path` identify resources by
  UUID but expose no project argument. Injecting a new argument into them would both change
  their MCP schemas and fail FastMCP validation.
- Dream aggregate tools currently query every project: decay status, consolidation
  candidates, unlinked nodes, clusters, and classification orphans.
- `BrainService.search` scopes PostgreSQL results but enriches them with unscoped Neo4j
  neighbors. Neo4j writes are intentionally degradable, so `BELONGS_TO` edges can be missing
  or stale and cannot be the ownership authority.
- `AutoLinker` searches all five knowledge tables globally. A scoped create or backfill can
  therefore create `RELATED_TO` edges into another project.
- `dream_runs` has no `project_key`. `dream_run_id` cannot be safely authorized in SEC1b
  without a schema/claim change.
- Core knowledge tables and `indexed_plans` already carry `project_key`; no schema migration
  is required. The capability policy is dormant by default.

## Acceptance criteria

1. Disabled capability mode, STDIO, and the admin bearer preserve their current tool names,
   public input schemas, catalogs, global behavior, and error contracts. No hidden scope
   argument is added to an MCP function signature.
2. In enabled mode, every scoped call is checked first against the phase allowlist and then
   against an exhaustive immutable project policy. A missing policy entry, missing resolver,
   resolver exception, or invalid request fails closed before the handler runs.
3. For tools that already expose `project_key`, a missing value is injected from the claim,
   an equal canonical value is accepted, and a different or non-canonical value is denied.
   Any non-null `project_group` is denied. ID-only and aggregate tools receive scope through
   a request-local context that is set/reset around `call_next`, invisible to `list_tools`.
4. Scoped references must be full UUIDs and resolve in PostgreSQL to exactly one resource in
   the claimed project. Typed references cover decision, learning, snippet, runbook, ADR,
   and indexed plan rows; generic graph references search the five graph-backed tables.
   Missing, malformed, ambiguous, null-project, and foreign IDs share one non-enumerating
   denial.
5. All embedded references are checked before the handler: merge source/target, graph path
   source/target, promotion source learning, `related_to[*].id`, update target, deletion
   target, and domain-assignment anchor. A scoped call with non-null `dream_run_id` is denied
   because Dream runs are not project-owned in the current schema.
6. The pre-handler check is defense in depth, not the sole object guard. SQL reads, updates,
   deletes, merges, and promotion-source loads add the claimed `project_key` predicate at
   point of use. Scoped `brain_update` rejects ownership fields and scoped relation writes
   revalidate both anchors immediately before a project-bounded Cypher write.
7. Project filters reach the data source for decay counts/deletion candidates,
   consolidation candidates, backfill candidates and embedding rows, cluster edges,
   classification orphans, graph neighbors, graph paths, search enrichment, and AutoLinker
   similarity candidates. Filtering only rendered output is insufficient.
8. PostgreSQL remains the ownership authority. Scoped Neo4j queries conservatively require
   `BELONGS_TO` for anchors and every returned/intermediate knowledge node, exclude
   `Project`/`Domain` nodes from traversals, and then batch-check every returned UUID against
   PostgreSQL before restitution or writes. Missing, stale, or multiple graph ownership fails
   closed; path search finds the shortest path inside the authorized subgraph rather than
   filtering the global shortest path afterward.
9. Scoped creation and backfill pass the authenticated scope separately from the entity's
   data `project_key` into AutoLinker. Foreign/null candidates are excluded and both graph
   anchors are project-bounded. Admin callers keep historical global AutoLinker behavior,
   even when creating a project-tagged object.
10. Authorization denials emit only principal, phase, project, safe tool name, and a bounded
    reason code. Arguments, entity IDs, content, bearer material, registry values, SQL text,
    exception reprs, and tracebacks never appear in logs or errors.
11. A real PostgreSQL integration test inserts two isolated projects and proves owned UUIDs
    are allowed while foreign, missing, null-project, ambiguous, and indexed-plan references
    fail closed. It requires `BRAIN_V42_TEST_DB_URL`, refuses the production DB name, ignores
    `POSTGRES_URL`, and cleans every inserted UUID in `finally` blocks.
12. An opt-in real Neo4j integration test requires dedicated `BRAIN_V42_TEST_NEO4J_*`
    variables, never falls back to localhost, and proves a project-1 path cannot traverse a
    project-2 node, including stale/multiple/missing `BELONGS_TO`. Cleanup targets only UUIDs
    created by the test. Without that isolated URL the graph test skips safely.
13. A real Uvicorn loopback test injects a deterministic fake authorizer and an unusable DSN,
    proves no real session factory is called, then covers project injection, forged scope,
    foreign IDs before handler execution, prompt-injection-shaped content, admin
    compatibility, and the existing Host/Origin/bearer/phase guarantees.
14. No migration, live configuration read, token creation, service/timer change, deployment,
    filesystem-root policy, OAuth, RLS, ownership-transfer feature, or cross-project Dream
    feature is part of this branch.

## Authorization contract

`DreamCapabilityMiddleware` remains the pre-handler enforcement point. It resolves the
principal, applies the phase allowlist, looks up the exhaustive tool policy, delegates SQL
ownership checks, then sets a request-local `DreamProjectScope` only for the duration of
`call_next`. The scope contains the canonical project and injected authorizer; a `finally`
block always resets it, including cancellation and handler failure.

The new `brain_v42.mcp.dream_project_authorization` bounded context owns:

- `PROJECT_TOOL_POLICIES`, an immutable map whose keys must equal the exact union of
  `DREAM_PHASE_TOOL_ALLOWLISTS`;
- copied FastMCP request validation and injection only for existing public `project_key`
  arguments;
- typed/generic full-UUID reference extraction, `project_group` and `dream_run_id` rejection;
- an injected async PostgreSQL ownership resolver and request-local scope helper;
- secret-safe, non-enumerating reason codes.

Enabled HTTP composition must inject a project authorizer built from the existing async
session factory. `_configure_http_security` exposes an explicit authorizer seam: `None` in
enabled mode is a secret-safe configuration error before Uvicorn starts. Disabled HTTP and
STDIO neither construct nor set project scope. Admin/unscoped calls bypass the project policy.

## Exhaustive 19-tool matrix

| Tool | Public binding | Request references | Point-of-use scope |
| --- | --- | --- | --- |
| `brain_decay_status` | request-local | none | both aggregate queries |
| `brain_consolidation_candidates` | request-local | none | pair SQL + handled-pair result |
| `brain_list` | inject existing `project_key` | none | existing repository filters |
| `brain_search` | inject `project_key`; reject `project_group` | none | fan-out + related graph/PG validation |
| `brain_get` | request-local | typed `entity_type/entity_id` | repository/plan read predicate |
| `brain_merge_entities` | request-local | typed source + target | one SQL transaction + scoped graph edge |
| `brain_delete` | request-local | typed target | repository/plan delete predicate |
| `brain_backfill_links_batch` | request-local | none | graph candidates + PG embeddings + AutoLinker |
| `brain_list_orphans_for_classification` | request-local | none | graph query + PG enrichment |
| `brain_assign_domain` | request-local | generic anchor | recheck + scoped Cypher anchor |
| `brain_get_clusters` | request-local | none | graph edges + batch PG ownership |
| `brain_learn` | inject existing `project_key` | `related_to[*].id` generic | create + relation/AutoLinker scope |
| `brain_save_snippet` | inject existing `project_key` | `related_to[*].id` generic | create + relation/AutoLinker scope |
| `brain_get_neighbors` | request-local | generic anchor | scoped Cypher + returned-ID PG check |
| `brain_graph_path` | request-local | generic source + target | authorized-subgraph path + all-ID PG check |
| `brain_propose_adr` | inject existing `project_key` | learning source; deny `dream_run_id` | scoped promotion load + AutoLinker |
| `brain_create_runbook` | inject existing `project_key` | learning source; deny `dream_run_id` | scoped promotion load + AutoLinker |
| `brain_list_adrs` | inject existing `project_key` | none | existing repository filter |
| `brain_update` | request-local | typed target + `related_to[*].id`; reject ownership field | scoped update + scoped relation writes |

## Task plan

Every behavioral task records a collecting test that fails on its intended assertion before
production code. Import/collection errors are not RED proof. Each task is one coherent commit
from its own task worktree.

### Task 1 — Fail-safe test infrastructure and project request policy

Files: create `src/brain_v42/mcp/dream_project_authorization.py`; create
`tests/unit/mcp/test_dream_project_authorization.py`; create
`tests/integration/mcp/test_dream_project_authorization.py`; modify
`tests/integration/conftest.py` and its focused unit tests.

RED: pin the 19-policy bijection and all matrix extraction/injection/rejection cases; assert
ContextVar reset on success/error/cancellation; parameterize exact secret-safe log/error
shape for every denial reason and a resolver exception. Add two-project PostgreSQL ownership
tests with explicit per-UUID teardown. Add a Neo4j fixture guard that refuses every implicit
localhost/default-credential fallback. GREEN: implement immutable pure policy, copied params,
request scope, and the injected SQL resolver. No middleware/server wiring yet.

### Task 2 — Middleware, HTTP composition, and schema compatibility

Files: modify `src/brain_v42/mcp/dream_capabilities.py` and
`src/brain_v42/mcp/server.py`; modify `tests/unit/mcp/test_dream_capabilities.py`,
`tests/unit/mcp/test_dream_capability_http.py`, `tests/unit/mcp/test_server.py`, and schema
catalog tests.

RED: prove policy lookup happens after phase validation, injected requests reach a synthetic
handler, every denial and missing dependency prevents handler execution, and scope resets.
Snapshot the exact affected `list_tools` input schemas for disabled, STDIO, and admin modes.
Extend real Uvicorn loopback with a fake resolver, unusable DSN, real handler signatures,
forged/prompt-shaped arguments, and an assertion that no DB factory is called. GREEN: compose
the authorizer only in enabled HTTP mode and expose the explicit dependency seam.

### Task 3 — Atomic SQL CRUD, merge, and promotion scope

Files: modify `src/brain_v42/mcp/tools/crud_tools.py`,
`src/brain_v42/mcp/tools/decay_tools.py`, `src/brain_v42/mcp/tools/brain_tools.py`,
`src/brain_v42/mcp/tools/runbook_tools.py`, repositories
`src/brain_v42/repositories/pg_decision.py`, `pg_learning.py`, `pg_snippet.py`,
`pg_runbook.py`, `pg_adr.py`, `pg_indexed_plan_repo.py`, and `promotion.py`, services
`src/brain_v42/services/{decision,learning,snippet,runbook,adr}_service.py`,
`src/brain_v42/services/consolidation.py`, and focused unit/integration tests. Do not widen
the change through generic `pg_base.py`, `pg_ticket.py`, or `pg_project_context.py`; scoped
predicates remain limited to the five knowledge tables, indexed plans, and promotions.

RED: prove scoped get/update/delete/plan operations include ownership in the executed SQL;
merge loads/updates both objects in one scoped transaction; promotion locks/loads its source
with the same project; forged ownership fields and non-null `dream_run_id` fail. Simulate an
ownership change after middleware validation and show the point-of-use operation returns no
row/no mutation. GREEN: thread optional internal scope through repository/service methods;
`None` retains admin behavior and no MCP signature changes.

### Task 4 — Project-scoped aggregates and search enrichment

Files: modify `src/brain_v42/mcp/tools/decay_tools.py`,
`src/brain_v42/services/consolidation.py`, `src/brain_v42/services/brain_service.py`, and
focused tests under `tests/unit/mcp/tools/` and `tests/unit/services/`, plus a two-project
PostgreSQL integration proof for decay or consolidation.

RED: prove decay counts/deletion candidates, consolidation pairs/handled results, all search
fan-out modes, and related enrichment carry the project predicate. GREEN: bind the
request-local scope at query construction; `None` preserves global operator behavior.

### Task 5 — Authorized-subgraph traversal and Dream graph maintenance

Files: modify `src/brain_v42/services/graph_service.py`,
`src/brain_v42/mcp/tools/dream_tools.py`, `src/brain_v42/mcp/tools/brain_tools.py`, and
`tests/unit/test_graph_service_dream.py`, `tests/unit/services/test_graph_service.py`,
`tests/unit/mcp/tools/test_dream_tools.py`, graph-tool tests, and
`tests/integration/test_graph_project_authorization.py`.

RED: pin project predicates and knowledge-label restrictions for unlinked nodes, edges,
orphans, domain assignment, neighbors, and paths. Cover a foreign intermediate, a longer
valid scoped path, and missing/stale/multiple `BELONGS_TO`; batch PG validation must deny any
divergence. GREEN: use bound Cypher parameters and search paths inside the authorized
subgraph; revalidate returned UUIDs through the request scope before output/write.

### Task 6 — Project-bounded implicit AutoLinker and relation writes

Files: modify `src/brain_v42/services/auto_linker.py`,
`src/brain_v42/services/graph_helpers.py`, `src/brain_v42/services/graph_service.py`,
`src/brain_v42/services/consolidation.py`, services
`src/brain_v42/services/learning_service.py`, `snippet_service.py`, `runbook_service.py`, and
`adr_service.py`, tools `src/brain_v42/mcp/tools/brain_tools.py`, `snippet_tools.py`,
`runbook_tools.py`, `dream_tools.py`, `crud_tools.py`, and `decay_tools.py`, plus focused
AutoLinker/service/tool, graph-service, update, merge, and isolated Neo4j tests.
`dream_tools.py` owns the scoped backfill path; every public MCP signature remains unchanged.

RED: prove all five AutoLinker UNION branches bind the authenticated project before ordering
and limiting, same-project candidates remain, and foreign/null candidates are excluded.
Prove scoped learn/snippet creation, plain and promoted ADR/runbook creation, and backfill
forward the same authenticated capability object independently of entity data. Immediately
before every implicit or explicit relation write, revalidate both PostgreSQL anchors and use
a project-bounded Cypher path restricted to knowledge labels and exactly one matching
`BELONGS_TO` owner per anchor. Cover `brain_update(..., related_to=...)`, an ownership change
between candidate selection and write, and the scoped `MERGED_INTO` edge after an atomic
merge; the historical admin merge remains unchanged. Authorization denials and resolver
failures must cross all degradation wrappers, perform no Cypher write, and emit no secondary
UUID, argument, exception, or traceback log. Admin/STDIO callers, including project-tagged
creation and maintenance backfill, retain the historical global SQL, Cypher, and degradation
contracts.

GREEN: thread an optional minimal authorization capability (`project_key` plus batch
revalidation) explicitly from tools through existing helpers and creation paths; never infer
it from `data.project_key` or reread the request ContextVar in services. Bind it in the UNION
query and a single relation-write primitive without duplicating linking logic. Keep every
internal scope parameter keyword-only with an admin-compatible default and preserve every
public MCP schema.

### Task 7 — Operator contract and delivery evidence

Files: update `README.md`, `docs/ARCHITECTURE.md`, `docs/MCP_TOOLS.md`, and this plan only
where SEC1b changes dormant behavior.

Document that SEC1a+SEC1b remain disabled until a separate operator-authorized rollout. That
later rollout must test two real profiles, owned/foreign read-write, aggregates, graph,
rotation overlap, and rollback by disabling enforcement. This delivery must not restart a
service, activate a timer, create a token, or read live credentials.

## Task gates

Use the base repository virtual environment from every isolated worktree:

```bash
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/python -m pytest <focused tests>
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/ruff check <changed Python files>
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/ruff format --check <changed Python files>
```

Before every production-symbol edit, run GitNexus upstream impact analysis and report direct
callers, affected processes, and risk. HIGH/CRITICAL results require an explicit warning
before editing. Before every commit, run `gitnexus_detect_changes(scope="all")` and inspect
the real diff.

## Required final gates

```bash
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/python -m pytest tests/unit \
  --cov=brain_v42 --cov-report=term-missing --cov-fail-under=60
env -u POSTGRES_URL -u NEO4J_URL -u NEO4J_USER -u NEO4J_PASSWORD \
  BRAIN_V42_TEST_DB_URL=<isolated-test-db> \
  /home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/python -m pytest tests/integration
bash tests/integration/test_dream_sh_tool_restriction.sh
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/ruff check src tests scripts
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/ruff format --check src tests scripts
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/mypy src
git diff --check main...HEAD
```

The PostgreSQL command is executed only with a verified non-production test URL. The opt-in
Neo4j test additionally requires dedicated `BRAIN_V42_TEST_NEO4J_*` values; absence is a safe,
explicit skip, never a localhost probe. The coordinator runs Uvicorn tests alone outside the
socket-restricted sandbox, scans the diff for credentials/argument logging/debug residue,
and verifies no pre-existing live service, timer, database, token file, or systemd unit was
modified. The unrelated systemd-install integration test is not a SEC1b gate because it
creates a transient unit.

## Rollback

Repository rollback is a normal revert of the SEC1b merge. Runtime rollback for a later,
separately authorized rollout is to disable `BRAIN_DREAM_CAPABILITY_ENFORCEMENT`, restart only
the HTTP MCP service in a quiescent Dream window, verify admin access, and handle the
persistent timer explicitly. This branch performs none of those actions.

## Delivery evidence

Repository delivery completed on 2026-07-18; operator rollout remains pending. The planning
commit is `6a40f28`. At the final gate, `git diff 6a40f28^..HEAD` covers the complete delivery,
including that planning commit.

- [x] Exhaustive immutable policy for all 19 scoped Dream tools and secret-safe denials.
- [x] Request-local scope plus project predicates at PostgreSQL points of use.
- [x] Project-bounded aggregates, search enrichment, graph traversal, and relation writes.
- [x] Scoped creation, update, backfill, promotion, consolidation, and merge paths with public
  MCP schemas and historical admin behavior preserved.
- [x] Operator contract for a separate two-project rollout, rotation overlap, and rollback.

The final unit gate completed with 3,686 passed and 48 skipped at 90.30% coverage. Both real
Uvicorn loopback tests ran outside the socket-restricted sandbox. The Dream shell restriction
contract, Ruff, formatting across 433 files, MyPy across 131 source files, and
`git diff --check` passed. Three independent post-repair reviews returned SHIP/ACCEPT with
no blocking finding.
The integration suite produced 132 explicit safe skips because no isolated
`BRAIN_V42_TEST_DB_URL` or `BRAIN_V42_TEST_NEO4J_*` values were available; this is not a live
two-project database or graph proof. No bearer was created, and no service, timer, database,
token file, live configuration, or systemd unit was changed.

## Non-goals and residual risks

- Filesystem path/root enforcement is SEC1c.
- Dynamic token issuance, OAuth, RLS, Neo4j native authorization, ownership transfer, and hot
  reload are out of scope.
- Cross-project Dream reasoning/linking remains a separate explicit feature.
- A trusted admin bearer remains intentionally global. Concurrent direct database mutation
  outside the supported application methods is not made transactionally atomic across
  PostgreSQL and Neo4j; scoped SQL predicates plus conservative graph checks fail closed at
  each supported point of use.
- No deployment, token generation, live denial drill, migration, or service restart occurs.
