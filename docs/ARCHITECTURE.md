# Architecture — brain_v42

**Updated:** 2026-07-24
**Repository and production state:** migrations 001–048 defined, 31 PG tables modeled; MCP catalog: 49 always-on + 2 graph-gated = 51. Production runs lifecycle v4 since 24 July 2026: revision 036 was applied and validated first, then 037 was proved before the restart-last MCP cutover and authenticated lifecycle-v4 E2E. The deployed Alembic head has since advanced and is not asserted here — measure it with `select version_num from alembic_version`. Last measurement: `045` on 16 August 2026, right after the 044→045 cutover.

**Repository target: 048.** Revision 048 adds `brain_session_artifacts.attribution_mode`,
which records BY WHICH KEY a row was attributed: `explicit` (a human named the UUID),
`derived_deposit` (the server parked it in a tracer), `derived_connection` (the exact match)
and `derived_window` (deduced by temporal exclusivity). Nullable, no backfill — `NULL` means
"written before 048". Only the deduced mode carries a partial index: undoing a guess must be a
query, not a scan, and its downgrade is fail-closed because dropping the column makes a
deduction indistinguishable from a human's explicit capture. Revision 047 removes the closing
XOR — non-empty ledger XOR
`nothing_to_capture_reason` — from the `ended` branch of `brain_sessions_terminal_state_valid`.
That check measured whether the CLIENT had declared; derived capture would now feed its signal
from the server, and a check is hollow the moment the thing it checks can influence its own
signal. It also made any session whose ledger the server had filled impossible to close. Its
downgrade is fail-closed and names the closures it would destroy. Revision 046 gives sessions
their identity (`connection_id`,
`started_by_actor`, `intent`, `nature`) and the `closed_inactive` terminal state, behind flags
that ship closed. It follows 045, which widened `dream_runs.model`, itself after the focus stamp,
provenance, freshness and decay work of 040 through 044. Revisions 038 and 039 — Dream
ticket-extraction attempts, and the project-context timestamp-trigger isolation that lets repair
transactions preserve signed timestamps — went to production during the 2026-08-03 cutover.
This line states what the REPOSITORY carries, never what runs live: it claims no deployed head,
and a test derives the number above from `alembic/versions/` instead of trusting this sentence.

**Production graph state:** cutover validated at head 035 on 22 July 2026. The production
instance uses the private projector credential and `GRAPH_LEDGER_WRITE_ENABLED=true`; the safe
default remains `false` for every fresh, restored, or otherwise unproved environment. The
[graph ledger runbook](GRAPH_LEDGER_RUNBOOK.md) is authoritative for the renewable evidence.

Successor of `datalake_v2` (7-container HTTP chain). brain_v42 uses one shared FastMCP
application process backed by Postgres + pgvector, with Neo4j as an optional relationship index
and a local unified GPU embedding/reranker service. The production fleet runs HTTP transport (see below); stdio
is the dev/fallback mode.

## Overview

```
                            Claude Code
                                 |
                  MCP HTTP (127.0.0.1:8765/mcp)
                  X-Brain-Agent: brain-v42
                                 |
                                 v
 +-------------------------------+--+  +----------------------+  +----------------------+
 | FastMCP server (Python 3.12+)     |  | Metrics runtime      |  | Automation runtime   |
 | 49 always-on + 2 graph-gated = 51 |  | 127.0.0.1:9200       |  | 127.0.0.1:9201       |
 | service layer + search fan-out    |  | /metrics / cockpit   |  | health / webhook     |
 | MCP background flushers/indexer   |  | optional legacy owner|  | dedup scheduler      |
 +----------------+------------------+  +----------+-----------+  +-----------+----------+
                  |                                |                          |
                  +--------------------------------+--------------------------+
                                                   |
         +----------------+------------------------+----------------+
         |                |                        |                |
         v                v                        v                v
   +---------+      +-------------+          +----------+    +-------------+
   | Postgres|      | Neo4j 5     |          | GPU      |    | Reranker    |
   | 16+pgv  |      | Community   |          | embed svc|    | cross-enc   |
   | :5433   |      | bolt :7687  |          | :8003    |    | :8003      |
   | source  |      | relations   |          | Qodo-    |    | same unified|
   | of truth|      | only        |          | Embed-1.5|    | endpoint    |
   +---------+      +-------------+          +----------+    +-------------+
```

## Core principles

1. **Single MCP process principle.** Production clients share one persistent FastMCP HTTP
   process; dev/fallback clients may run the stdio server directly. Postgres, Neo4j and the
   unified GPU embedding/reranker service are separate and independently restartable.
2. **PG is the source of truth.** All CRUD, FTS (tsvector), vector search (pgvector HNSW, 1536 dims), supersession chains (recursive CTE), and audit tables live in PG.
3. **Neo4j is a relationship index.** Canonical entity data stays in PostgreSQL. Neo4j stores bounded identity/display fields, graph edges (`SUPERSEDES`, `MOTIVATED_BY`, `IMPLEMENTS`, `DOCUMENTS`, `USES`, `RELATED_TO`, `CONTAINS`, `DEPENDS_ON`, `BELONGS_TO`, `MERGED_INTO`, `BELONGS_TO_DOMAIN`), and internal projection fence/cursor nodes. It enriches search results through a "Related" section or explicit `brain_get_neighbors`.
4. **Additive graph cutover.** With `graph_ledger_write_enabled=false`, services keep the historical PG-then-Neo4j write-through path. Migrations 033–035 can be installed and backfilled without changing that owner. Production completed the separately authorized cutover on 22 July 2026: PostgreSQL owns graph facts and an at-least-once outbox projects them to Neo4j. Fresh or unproved environments stay at the safe `false` default until their own gates close. Durable ledger failures propagate instead of degrading into success; `graph_enabled=false` disables both graph paths.
5. **Stdout is sacred.** All logs go to stderr via `_configure_stdio_logging()` in `src/brain_v42/mcp/server.py` — any stray `print` corrupts JSON-RPC and silently drops the tool list.

## Transport

### Production: HTTP (loopback only)

brain_v42 runs as a persistent HTTP MCP server on `127.0.0.1:8765`. Claude Code connects via:

```json
{
  "mcpServers": {
    "brain-v42": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {
        "X-Brain-Agent": "brain-v42",
        "Authorization": "Bearer ${MCP_HTTP_TOKEN}"
      }
    }
  }
}
```

The `X-Brain-Agent` header is normalised by `_normalize_agent()` in `metrics/instrument.py` to produce clean per-project Prometheus labels (e.g. path-like values are reduced to their basename).

The server binds only to `127.0.0.1` (validated by `Settings._loopback_only`).
`HostOriginGuard` protects every HTTP mode. Authentication then follows one of two paths:

- **`HostOriginGuard`** — DNS-rebinding protection. Rejects any `Host` not in `{127.0.0.1, localhost, ::1}` (421) and any `Origin` whose host is non-loopback (403). Duplicate-Host protection comes from h11 (uvicorn's HTTP/1.1 parser) upstream.
- **Dormant capability mode** — `BRAIN_DREAM_CAPABILITY_ENFORCEMENT=false` keeps the
  historical `BearerTokenGuard`. Direct dev HTTP can leave `MCP_HTTP_TOKEN=""`; the production
  systemd unit instead requires the private, non-empty admin bearer. When present, every
  non-`/health` request must carry it.
- **Enabled capability mode** — FastMCP's token-verifier boundary authenticates the distinct
  admin bearer and phase-scoped Dream bearers. Application middleware then filters tool lists
  and authorizes calls. `HostOriginGuard` remains enforced; FastMCP authentication runs before
  the supplied ASGI middleware. Real loopback HTTP tests pin the resulting behavior.

`/health` is always exempt from auth — used by systemd watchdog and red-monitor.

### Dev/fallback: stdio

Activated by `BRAIN_MCP_TRANSPORT=stdio` (default) or omitting the env var. Used for local dev (`fastmcp dev`), pytest fixtures, and non-fleet contexts. Signal handlers (SIGTERM/SIGINT) and `prctl(PR_SET_PDEATHSIG, SIGTERM)` prevent zombie MCP children.

### Network trust boundary

**Tracked network boundary** (replayed 2026-08-23): MCP, PostgreSQL and Neo4j bind to loopback; metrics and automation default to loopback. The versioned Compose target binds the embedding host publish to loopback and the live runtime matches it — measured `127.0.0.1:8003`, with the host's own LAN address refusing the connection. Application bearer authentication is armed and enforcing: `MCP_HTTP_TOKEN` is set and non-empty in the live server process, and `POST /mcp` answers `401` both without a bearer and with a wrong one. The dedicated Docker client network exists and carries the clients: `brain-net` holds the embedding shim and both `auto-discord` containers. Repository-managed WAN isolation remains unproven — the repository manages no firewall rule at all. What would make this paragraph false again, and is watched by no test: a host-publish override reopening `:8003`, `METRICS_HOST` set off-loopback (no validator guards it), or `MCP_HTTP_TOKEN` cleared. Re-measure with `ss -ltnp`, `docker port` and an unauthenticated `POST /mcp` — do not copy this line forward.

**Embedding topology**: production/default = local unified endpoint `http://localhost:8003`; `deploy/dev-pc` is a superseded rollback/reference path.

**Embedding shim limits (ROLLED OUT 2026-08-21, temps 1)**: 8 MiB body, 5 s body-read timeout, 8 concurrent ingress reads, 100 embed texts, 128 rerank candidates, maximum JSON depth 64, one embedding calculation and one rerank calculation per worker. Saturation returns short `503` JSON with `Retry-After: 1`.

**SEC2 residuals** (replayed 2026-08-23): bearer authentication and the dedicated Docker client network are done — the coordinated `auto-discord` cutover happened, and both `auto-discord` containers sit on `brain-net`. One residual stands, and it is wider than previously written: the versioned legacy PyTorch profile remains unbounded — `services/embedding/main.py` carries no body cap, no read deadline, no concurrency semaphore and no `413`/`503` — and it preserves neither of the two DNS names its clients use. A `--profile legacy` rollback publishes `embedding` and `brain_v42_embedding` on `brain-net`, while the compose sets `EMBEDDING_URL=http://embedding-shim:8003` and the running bot, carrying no `EMBEDDING_URL` of its own, falls back to the code default `http://brain_v42_embedding_shim:8003`. Two names break, not one.

The deployment model is personal agents on a trusted LAN. Remote database administration goes
through an SSH tunnel. The loopback publish is code-ready but not live proof: until an authorized
rollout verifies the effective bind and listeners, treat `:8003` as LAN-exposed. Do not expose it
to the Internet; independently verify the router/network boundary.

## Storage layout

| Store | Port | Role | Source files |
|-------|------|------|--------------|
| Postgres 16 + pgvector | 5433 | CRUD + FTS + 1536-d vectors + audit + graph ledger/outbox + time series | `src/brain_v42/db/tables.py`, `alembic/versions/` |
| Neo4j 5 Community | 7687 (bolt) / 7474 (browser) | Relationship traversal, clusters, neighbourhood, domain classification | `src/brain_v42/db/neo4j.py`, `src/brain_v42/services/graph_service.py` |
| GPU embedding service | 8003 | Qodo-Embed-1-1.5B, 1536 dims, local HTTP; also serves cross-encoder reranker | `src/brain_v42/services/gpu_embedding_service.py` |
| Reranker | 8003 | Cross-encoder rerank for hybrid search + ClusterGuard grey zone (same unified endpoint as embed) | `src/brain_v42/services/reranker_client.py` |

### 31 PG tables (`src/brain_v42/db/tables.py`)

Knowledge: `decisions`, `learnings`, `snippets`, `runbooks`, `adrs`, `project_contexts`.
Plans: `indexed_plans` (extended by migration 014), `indexed_plan_chunks` (created by migration 014).
Roadmap: `features`, `feature_artifacts`, `roadmap_curation_proposals`.
Audit / ops: `search_log`, `process_metrics`, `access_log`, `consolidation_log`, `metrics_timeseries`.
Webhook & dream: `gitlab_events`, `dream_runs`, `dream_promotions`.
Coordination: `tickets`, `ticket_messages`, `ticket_extraction_proposals`,
`ticket_extraction_attempts`.
Tickets may address another project or the same project; equal endpoints implement a
note-to-self that resurfaces in that project's briefing.
Sessions: `brain_sessions`, `brain_session_artifacts`.
Graph foundation and projection control: `projects`, `project_aliases`, `brain_entities`,
`entity_relations`, `graph_outbox`, `graph_projection_leases`.

See `docs/SCHEMA.md` for the maintained schema reference. Migration files and
`src/brain_v42/db/tables.py` remain authoritative.

### Canonical graph ledger, fencing, and recovery (migrations 033–035, active in production)

Migration 033 installs and backfills the canonical graph ledger. Migration 034 adds durable
projector coordination and a cross-store fence. Migration 035 adds a resumable recovery
interlock to the same singleton row. Production uses all three; other environments keep this
path dormant while the runtime flag is closed:

```text
business tables / durable relation facade
                 |
                 v
 projects -> brain_entities -> entity_relations
                 |                   |
                 +------ triggers ---+
                            |
                            v
                       graph_outbox
                            |
                  GraphOutboxProjector
                            |
                            v
                    Neo4j projection
```

Five canonical tables and one fencing table separate responsibilities:

- `projects` owns canonical project keys; `project_aliases` maps known legacy spellings.
- `brain_entities` assigns one graph identity and lifecycle to each Project, Domain,
  Decision, Learning, Snippet, Runbook, ADR, Feature, and Plan.
- `entity_relations` owns the relation type, endpoints, origin, safe properties, lifecycle,
  and monotonic revision.
- `graph_outbox` stores one idempotent projection instruction per aggregate revision.
- `graph_projection_leases` owns the singleton protocol-v2 generation, owner lease, durable
  Neo4j arm marker, active recovery UUID and phase, and most recently completed recovery UUID.

Migration triggers mirror source-table changes, project membership, supersession, merges,
and lifecycle changes into the ledger. A source entity in `archived` state remains
projectable so lineage such as `MERGED_INTO` stays traversable; only `deleted` removes its
Neo4j node. Project keys become immutable on `project_contexts`, and known aliases normalize
at migration time and on later writes.

When `graph_ledger_write_enabled=true`, the durable facade stages explicit relations and their
outbox instructions in one PostgreSQL transaction. Registry triggers stage node changes. The
projector acquires a singleton PostgreSQL `(owner, generation)` lease, activates the same
monotonic Neo4j fence, then arms that generation in PostgreSQL before claiming work. Each
claim carries `lease_generation` and a monotonic `claim_version`; renewal extends the leader
and exact claim atomically. A healthy worker retains its armed generation across polls and
releases it on shutdown or after a failed fence/CAS check. The lease duration is always at
least twice the configured poll interval.

The projector leases bounded batches with `FOR UPDATE SKIP LOCKED` and blocks a newer eligible
revision behind an earlier pending revision. In one explicit Neo4j transaction, it checks the
fence, locks the aggregate cursor, applies the mutation, and advances the cursor. PostgreSQL
delivery and failure updates lock the live leader and compare-and-set the exact claim. A
successor waits for a predecessor mutation already in flight; after its barrier commits, an
older generation cannot mutate or acknowledge. Retries use bounded backoff and secret-safe
error codes. Lowering the configured attempt limit atomically normalizes already-over-limit
events to `max_attempts`. Relation projection replaces the complete allowlisted property set,
so deleting a canonical property cannot leave stale metadata in Neo4j.

Normal Neo4j activation requires an existing protocol-v2 fence with no recovery marker. It
accepts either the exact armed `(generation, owner)` or, for an unarmed PostgreSQL leader,
exactly the immediately preceding Neo4j generation. Runtime activation never creates a
missing fence and never crosses an active recovery marker; both cases fail closed. This is
only a lineage check: a Neo4j PITR inside the same generation can still lose data and cursors
without being detected automatically.

Startup requires all six graph tables, the singleton protocol-v2 row, and the
`graph_outbox.lease_generation` and `claim_version` columns. It also requires the three
migration-035 recovery columns and the validated recovery-state constraint. Startup then
installs the Neo4j identity, fence, and cursor constraints. This readiness check does not
attest the exact Alembic head, trigger definitions, or complete schema shape.

Migration 035 gives an offline operator one crash-resumable state machine:

```text
idle -> prepared -> neo_ready -> idle
```

The PostgreSQL `prepared` transition locks the singleton, increments the generation once,
records an explicit recovery UUID, clears the arm marker, and requeues every current canonical
revision in the same transaction. Every normal acquire, claim, renew, ACK, fail, and release
path requires `recovery_id IS NULL`. A Neo4j transaction then deletes only the allowlisted
Brain projection labels and `BrainProjectionCursor`, preserves the fence, leaves nodes without
those labels untouched, and installs the exact recovery marker. PostgreSQL enters `neo_ready`
only after that commit. Neo4j removes the marker before PostgreSQL records
`last_completed_recovery_id` and returns to `idle`. Reusing the same UUID resumes the recorded
phase without another generation bump or requeue; a different recovery UUID is refused.

The narrower Neo4j delete is still destructive. Under Option A, "PostgreSQL canonical +
rebuild-on-doubt", recovery requires a dedicated disposable Brain database, stopped writers,
zero active Neo4j sessions, revoked legacy credentials, and a tested PostgreSQL restore at the
exact deployed head — measured at restore time, never quoted; this file does not assert it, see
the state line at the top of this document. That restore must also carry the graph invariants
introduced through 035. The CLI checks those five explicit confirmations; their presence records operator
assertions rather than discovering external state. A Neo4j backup or correlated restore is not a
gate because Neo4j never supplies canonical recovery state.

The relation row and its outbox instruction are atomic with each other, not necessarily with
the earlier business-row commit. When a service commits an entity before staging a related
edge, a ledger failure propagates but may leave that entity committed without the relation.
Clients must treat the operation as a retryable saga. The cutover drill must exercise this
window and prove its retry/reconciliation contract.

The production activation was explicitly authorized and completed on 22 July 2026 with
`GRAPH_LEDGER_WRITE_ENABLED=true`. Its instance-specific evidence includes:

- a PostgreSQL sandbox restore at head 035 followed by the DR-v3 contract at 24/24;
- recovery UUID `776fd1b9-dbd0-4a1c-b7e3-cd3398ebf93a` completed before projector generation 3;
- stopped legacy writers, credential rotation, zero legacy sessions and authenticated refusal
  of the revoked credential;
- a rebuild into an empty dedicated Neo4j database, followed by matching counts of 4,678
  entities, 11,888 relations and 16,566 cursors, an empty outbox and an MCP smoke test.

These proofs do not authorize another instance or a future rebuild. Such an environment must
remain at `GRAPH_LEDGER_WRITE_ENABLED=false` until the runbook's four gates are repeated and
reviewed. Repository migrations 036–037 were deployed separately on 24 July 2026. The earlier
head-035 restore still proves the graph cutover state historically. DR-v5 run
`20260724_150315` renewed the current PostgreSQL restore evidence at head 037 with 24/24 checks
and an independent SQL attestation; a future recovery must revalidate the evidence for its own
instance and deployed head.

The runtime cannot detect an arbitrary PostgreSQL PITR within an already armed generation.
The restored ledger may be older than Neo4j cursors while both retain a valid fence lineage.
After a restore, operators must keep projection stopped and rebuild Neo4j from restored
PostgreSQL. The migration-035 protocol can bootstrap a missing fence or resume its exact
recovery marker after any recorded cross-store commit boundary. When PostgreSQL is in
`neo_ready`, it always repeats the bounded reset with the same active UUID; surviving fence and
cursor metadata is not treated as proof that projection content is intact. A missing, older, or
exact compatible disposable projection is accepted, while an incompatible marker or a newer
finalized generation is refused. Once
PostgreSQL is `idle/completed`, loss of Neo4j requires a new recovery UUID because replaying the
completed UUID is a no-op. Operators may run it only from a PostgreSQL state validated by the
tested-restore gate. See the
[graph ledger runbook](GRAPH_LEDGER_RUNBOOK.md).

### Persistent session lifecycle (repository migrations 032 and 037)

Migration 032 adds `brain_sessions`, persistent session UUIDs, and `project_contexts.focus_revision`. The v4 lifecycle delivered with migration 037 adds the durable capture ledger, heartbeat, identity checks, and persisted focus outcomes. It follows migration 036 in the single Alembic chain. Both are active in production since 24 July 2026, with schema, health and authenticated E2E proofs. DR-v5 now certifies an isolated PostgreSQL restore at head 037. Full disaster recovery remains open until roles, owners and ACLs are replayed, a dedicated Neo4j rebuild is proved, DR-v5 runs from the production timer, and an encrypted off-host copy plus alert delivery are verified.

The lifecycle has three database-constrained states: `open`, `ended`, and `abandoned`.

- **User-controlled boundaries.** Only an explicit user command may start, capture, heartbeat, list, resume, end, or abandon a session on the agent and client side. Hooks and agents never infer a boundary or close a stale session. The only server-side exception is the Dream `sweep` phase, shipped disabled and dry, which abandons an open session with no heartbeat for seven days (`abandonment_reason = 'auto_stale_7d'`) without touching project focus.
- **Concurrent starts.** Multiple open sessions may share a project. Idempotence is enforced by unique `(project_key, client_key)`; retrying the same key while its session is open returns the same UUID.
- **Two-part isolation.** Resume, capture, heartbeat, end, and abandon require both `session_id` and its `expected_client_key`. A mismatch changes nothing. This comparison is an isolation guard, not authentication.
- **Durable, exclusive provenance.** `brain_session_capture` records client-declared knowledge UUIDs in `brain_session_artifacts`, up to 100 per session. Each artifact must exist in a supported knowledge table, belong to the project, and have been created after the session started. Re-capture by its owner is idempotent; another session conflicts. This proves the caller's persisted attribution, not which process created the artifact.
- **Recoverable ledger view.** Session results expose `attributed_knowledge_ids`, loaded from the ledger for retries, list, resume, capture, heartbeat, end, and abandon. Abandon preserves ownership, and an exact capture retry remains idempotent without reopening the session.
- **Derived staleness.** An open session becomes `is_stale=true` after 24 hours without a heartbeat. `brain_session_heartbeat` refreshes presence, and capture also refreshes it. Staleness is a list filter over open rows; it never changes the persisted `status` and never auto-closes a session. Do not confuse this 24-hour display flag with the separate seven-day server-side sweep, which is the only mechanism that moves an open session to `abandoned` without an explicit command (`abandonment_reason = 'auto_stale_7d'`).
- **Focus-independent end.** End locks the addressed session and project, revalidates its capture ledger, and marks the session `ended` in one transaction. A matching `expected_focus_revision` updates focus and persists `focus_outcome=applied`; a mismatch leaves shared focus untouched but still ends the session with `focus_outcome=conflict`.
- **Fail-closed validation.** An identity mismatch or invalid, cross-project, pre-session, or ambiguous capture writes nothing and leaves an open session open.
- **Capture outcome.** End requires either at least one ledger artifact or a non-blank `nothing_to_capture_reason`, exclusively. It copies the ledger UUIDs into `captured_knowledge_ids` as the terminal snapshot.
- **Briefing degradation.** Start commits the session before assembling its optional briefing. A total briefing failure is logged and returned as unavailable without hiding the persisted UUID.
- **Stable replay.** End persists the requested revision, focus outcome, focus value, and focus revision observed at closure. An exact retry returns that terminal outcome even if shared focus later changes; a different terminal payload conflicts.
- **Explicit abandon.** `brain_session_abandon` records a reason and marks the session `abandoned` without changing project focus. An exact retry is idempotent.
- **Fail-closed downgrade.** Revision 037 refuses to downgrade while an open/abandoned or otherwise unsnapshotted ledger attribution exists, or while an ended session carries `focus_outcome=conflict`; revision 036 cannot represent those facts without data loss.

`brain_update_project_focus` row-locks the project context and requested feature rows,
validates the complete batch, checks `expected_focus_revision`, and commits focus, blockers,
statuses, and pins together. Every successful batch consumes the revision. Validation or CAS
failure produces no partial write. The optional CLAUDE.md update occurs after commit and is
not part of the database transaction.

### Decay columns

The 6 knowledge tables (`decisions`, `learnings`, `snippets`, `runbooks`, `adrs`) and `indexed_plans` all carry decay columns added by migration 007:

- `last_accessed_at TIMESTAMPTZ` — stamped on every read (via `AccessLogger`)
- `access_count INTEGER DEFAULT 0` — cumulative read count
- `freshness_status VARCHAR` — `fresh` | `stale` | `archived` (driven by `DecayCalculator`)
- `merged_into UUID` — set by `brain_merge_entities`; source rows are archived, not deleted

## Repository pattern

All 6 knowledge repositories subclass `BasePgRepository` (`src/brain_v42/repositories/pg_base.py`), which provides:

- `get(id)`, `list(...)`, `create(...)`, `update(...)`, `delete(id)`
- `search_fts(query, ...)` — full-text search via `search_vector @@ plainto_tsquery(...)`
- `search_vector(embedding, ...)` — semantic search via `op("<=>", return_type=sa.Float)` (ADR #8). The explicit `return_type=Float` cast is mandatory: without it, pgvector returns `bytea` and SQLAlchemy cannot compare or sort the result.

Constructor takes `session_factory: async_sessionmaker[AsyncSession]` for consistent DI and testability.

## Background workers

All started from `server.py`'s `app_lifecycle()` context manager (owns lifecycle for both stdio and http transports):

- **MetricsFlusher** — writes `process_metrics` snapshots every 30s. Calls `snapshot_counters()` at the start of each flush cycle so delta-based RPS metrics reflect the actual interval.
- **TimeseriesFlusher** — writes `metrics_timeseries` rolling window (30-min buckets for rps/p95/err_rate, 1-hour buckets for cost). Retention: 7 days; the cockpit read window is 24h.
- **DecayFlusher** — drains `access_log` queue, advances `freshness_status` fresh → stale → archived based on `last_accessed_at` and `DecayCalculator` thresholds.
- **AccessLogger** — records reads to `access_log` so decay has evidence.
- **PlanIndexer** — fire-and-forget on boot, scans `plan_scan_paths` per project and indexes markdown specs/plans into `indexed_plans` + `indexed_plan_chunks` with embeddings, links to features via `ClusterGuard`. Skips unchanged files by content hash.
- **GraphOutboxProjector** — drains canonical graph revisions toward Neo4j only when
  `graph_ledger_write_enabled=true`; startup checks all six graph tables, protocol-v2 state,
  both claim-fencing columns, the three recovery columns, and the validated recovery-state
  constraint before installing the Neo4j projection constraints. It does not attest the
  Alembic head, trigger definitions, or complete schema shape.

All are optional via settings (`metrics_enabled`, `decay_enabled`, `graph_enabled`,
`graph_ledger_write_enabled`, `brain_code_mode`).

## Hybrid search

`BrainService._fan_out()` sends a semantic query to each domain service in parallel, collects candidates, then rerankss via `BatchingRerankerClient` → `HybridReranker`.

**Degraded modes** — surfaced in the formatted output banner:

| Marker | Condition | Banner text |
|--------|-----------|-------------|
| `fts_fallback` | GPU embedding service down — only FTS results | "degraded: embedding service indisponible — résultats FTS uniquement (ordre textuel)" |
| `rrf_fallback` | Reranker down — RRF rank-based scores instead of cross-encoder | "degraded: reranker indisponible — ordre RRF (pas de re-scoring cross-encoder)" |
| `rrf_only` | No reranker configured | "note: reranker non configuré — ordre RRF (pas de re-scoring cross-encoder)" |

`min_score` filtering is disabled in degraded mode to avoid silently dropping all results when scores are RRF-based rather than semantic.

**`BatchingRerankerClient`** (`src/brain_v42/services/search/batching_reranker.py`) — wraps `RerankerClient` with a 20 ms coalescing window. All parallel fan-out shards that arrive within the window are batched into a single HTTP request to the reranker, reducing round trips 3–6x. `ClusterGuard` and `FeatureDedupJob` use the raw `RerankerClient` directly (single-query paths with no fan-out).

## Metrics sidecar (port 9200)

`src/brain_v42/metrics/server.py` starts an aiohttp app exposing:

- `GET /metrics` — Prometheus format, read from `MetricsCollector` in-memory counters and PG aggregates.
- `GET /api/cockpit` — single JSON snapshot (`CockpitCollector`) consumed by `red-monitor` with ~2s poll. The `transport` field reads `settings.brain_mcp_transport` (so it correctly shows `"http"` post-cutover). `cache_hit_ratio` is `null` — the GPU embedding service does not expose a cache-hit counter.

Instrumentation is opt-in: `InstrumentedEmbeddingService`, `InstrumentedGraphService`, `InstrumentedReranker`, and `instrument_tool()` wrap the real services when `metrics_enabled=true`.

`record_search_latency` is called before every DB INSERT in `record_search_log`, so the in-memory p50/p95 histogram is always populated regardless of DB availability.

When legacy automation is enabled, metrics gives PostgreSQL lease acquisition two seconds.
On timeout it cancels that attempt and binds metrics-only, without the webhook or dedup
scheduler, so a database outage cannot hold `:9200` behind asyncpg's connection timeout.

## Automation runtime (port 9201)

`src/brain_v42/automation/` is an independently managed bounded context. It serves only
`GET /health` and `POST /gitlab/webhook`, and owns the periodic feature-dedup scheduler.
It deliberately exposes neither `/metrics` nor `/api/cockpit`. `brain-metrics` retains
those two observability routes on `127.0.0.1:9200`.

`AutomationServer` configures `aiohttp.web.AppRunner` with a 10-second maximum drain,
supported by the project's minimum `aiohttp>=3.9`. The systemd unit grants 30 seconds for
the complete stop, leaving the remaining budget to close embedding and reranker clients,
release the lease and dispose the engine. Tests hold the default at 10 seconds and exercise
an in-flight webhook with a short injected timeout.

Before cutover, metrics keeps the legacy webhook and scheduler while
`METRICS_LEGACY_AUTOMATION_ENABLED=true`. During cutover, a late host
`EnvironmentFile=` sets that flag to `false`, metrics releases the advisory lease, then
automation acquires it before metrics restarts. The committed automation unit remains
dormant; [the systemd runbook](../deploy/systemd/README.md) is the only operator procedure.

The automation and legacy metrics builders inject the lease's synchronous ownership check
as an optional mutation guard into `GitLabIngestor`, `ClusterGuard`, and `FeatureDedupJob`.
The scheduler checks ownership after candidate discovery and merge, around commit, and
before advancing or logging. `FeatureDedupJob` re-embeds before DML and checks ownership
outside the best-effort embedding handler and around every SQL await. Non-automation
consumers retain the default `None` guard.

The PostgreSQL advisory lease remains non-fencing. The guards close the observed handover
window, including losses during embedding or reranking, but cannot revoke a transaction
that PostgreSQL has already committed. ClusterGuard resolution, `gitlab_events` insertion
and `feature_artifacts` insertion use independent transactions. A loss detected after a
commit can therefore replay a merge or leave the artifact row absent;
`gitlab_events.feature_id` still records the feature association. Eliminating this Medium
recovery risk requires cross-step atomicity or durable reconciliation, outside this lot.

A dedup `commit()` already entered in PostgreSQL remains non-fencing: its post-commit guard
can stop the pass and later logs, but cannot restore prior state. The two feature rows also
remain locked from `SELECT ... FOR UPDATE` until re-embedding returns or the transaction is
rolled back. Runtime cancellation bounds the normal lease-loss path, while a blocked
embedding can prolong those locks. `feature_dedup.merge_staged` is pre-commit; only
`dedup_loop.merged` reports guarded post-commit progress.

After the split, automation events no longer feed the in-process metrics snapshot, so
`cockpit.recent` intentionally loses those event entries; health, Prometheus metrics and
the cockpit endpoint themselves remain available on `:9200`.

## Dream Mode (nightly maintenance)

`scripts/dream.sh` orchestrates six headless agent phases: **SCAN / CLEAN / CONNECT / SYNTH / PROMOTE / REORG**. Codex is the default provider and authenticates through the active ChatGPT login. The fast tier uses `gpt-5.6-terra` with medium reasoning; the deep tier uses `gpt-5.6-sol` with high reasoning.

The Codex adapter exposes only the Brain MCP tools required by each phase:

| Phase | Default tier | Exact Brain MCP allowlist |
|-------|--------------|--------------------------|
| SCAN | `gpt-5.6-terra` / medium | `brain_decay_status`, `brain_consolidation_candidates`, `brain_list`, `brain_search` |
| CLEAN | `gpt-5.6-terra` / medium | `brain_search`, `brain_get`, `brain_consolidation_candidates`, `brain_decay_status`, `brain_merge_entities`, `brain_delete`, `brain_list` |
| CONNECT | `gpt-5.6-terra` / medium | `brain_backfill_links_batch`, `brain_list_orphans_for_classification`, `brain_assign_domain` |
| SYNTH | `gpt-5.6-sol` / high | `brain_get_clusters`, `brain_get`, `brain_learn`, `brain_save_snippet`, `brain_search`, `brain_list`, `brain_get_neighbors`, `brain_graph_path` |
| PROMOTE | `gpt-5.6-sol` / high | `brain_get`, `brain_search`, `brain_propose_adr`, `brain_create_runbook`, `brain_list_adrs`, `brain_list`, `brain_get_neighbors`, `brain_graph_path` |
| REORG | `gpt-5.6-sol` / high | `brain_search`, `brain_list`, `brain_get`, `brain_update` |

`scripts/dream/codex_runner.py` starts each turn in an ephemeral, read-only workspace. It ignores ambient Codex configuration, requires the loopback Brain MCP server, and disables shell, web search, apps, and subagents. The orchestrator checks both ChatGPT authentication and `MCP_HTTP_TOKEN` before maintenance begins. With capability enforcement enabled, it also validates a complete six-phase profile before phase one, passes only that phase's `active` bearer through an allowlisted child environment, removes the full registry, and adds the loopback MCP hosts to `NO_PROXY`. It never falls back to Claude automatically: a failed WET phase may already have committed a mutation, so switching providers mid-run would risk replaying it. Claude remains an explicit operator rollback only after capability enforcement is disabled:

```bash
BRAIN_DREAM_AGENT_PROVIDER=claude scripts/dream.sh brain-v42
```

Model and reasoning defaults can be overridden without changing the phase policy:

```bash
BRAIN_DREAM_CODEX_FAST_MODEL=gpt-5.6-terra \
BRAIN_DREAM_CODEX_FAST_REASONING=medium \
BRAIN_DREAM_CODEX_DEEP_MODEL=gpt-5.6-sol \
BRAIN_DREAM_CODEX_DEEP_REASONING=high \
scripts/dream.sh brain-v42
```

Codex keeps the final report, JSONL events, and stderr in separate logs. `codex_dream_parser.py` records fresh input, cached input, output tokens, and MCP tool calls in `dream_runs`; ChatGPT subscription runs record `cost_usd=NULL` because the event stream has no trustworthy per-run dollar cost. The Claude rollback retains its historical OTEL parser.

ROADMAP and EXTRACT are separate Python jobs after the six agent phases. They continue to call the NVIDIA API with strict JSON and no MCP tools, retain their own killswitches and timeouts, and are outside the Codex migration.

The repository systemd timer targets 06:00 local time with `RandomizedDelaySec=120` and persistent catch-up. Dream and HTTP both require `%h/.config/brain-v42/mcp-token.env`; the HTTP unit refuses a missing, symlinked, wrongly owned, non-`0600`, empty or effectively overridden admin token. That private file accepts only `MCP_HTTP_TOKEN`, `MCP_HTTP_DREAM_TOKENS`, and `BRAIN_DREAM_CAPABILITY_ENFORCEMENT`. The repository `.env` is attested separately and must contain no MCP bearer. The Dream unit applies `UMask=0077`, waits up to 30 seconds for the auth-exempt Brain MCP `/health` route, and has a three-hour worst-case cap. The independently managed `brain-mcp-http.service` must therefore already be active; the installer generates and validates it but leaves its lifecycle operator-managed. The orchestrator itself defaults to Codex.

The versioned user-unit profiles are workload-specific. Automation and the graph ledger inventory
use the strong integrity profile with `PrivateUsers=true`, read-only HOME/system paths, empty
capability sets, and a bounded socket-family allowlist. MCP HTTP keeps HOME writable for its
documented file operations while protecting repository and Brain credentials read-only. Dream
and the watchdog use reduced profiles so nested agent sandboxes, caches, logs, and the user bus
remain compatible. `install.sh --check-only` renders and verifies all eight units without touching
the live unit directory; `--render-dir` publishes the same verified bytes to a new private
directory outside systemd. The historical `--dry-run` still writes all eight live fragments and
is not a side-effect-free preview. On 24 July 2026 only `brain-mcp-http.service`,
`brain-mcp-http-watchdog.service` and `brain-mcp-http-watchdog.timer` were published and canaried
live, including kernel enforcement and authenticated E2E. The five Dream, graph-recon and
automation fragments have not yet been rolled out.

### Future capability firewall rollout

The repository ships SEC1a and SEC1b dormant. While
`BRAIN_DREAM_CAPABILITY_ENFORCEMENT=false`, production HTTP and dev/fallback STDIO keep their
historical capability contracts, and the admin principal remains global. A separate, operator-authorized rollout
must enable the boundary. Enabled mode parses
`MCP_HTTP_DREAM_TOKENS` as a `SecretStr` JSON registry keyed by canonical
`<project_key>:<phase>`. Every configured project must define all six phases. Each profile
has one `active` bearer and an `accepted` list for rotation overlap; all values, including
the admin bearer, must be distinct. Use placeholders only in shared documentation:

```dotenv
MCP_HTTP_TOKEN='<ADMIN_BEARER_PLACEHOLDER>'
BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true
MCP_HTTP_DREAM_TOKENS='{"brain-v42:scan":{"active":"<SCAN_ACTIVE_PLACEHOLDER>","accepted":["<SCAN_PREVIOUS_PLACEHOLDER>"]},"brain-v42:clean":{"active":"<CLEAN_ACTIVE_PLACEHOLDER>","accepted":[]},"brain-v42:connect":{"active":"<CONNECT_ACTIVE_PLACEHOLDER>","accepted":[]},"brain-v42:synth":{"active":"<SYNTH_ACTIVE_PLACEHOLDER>","accepted":[]},"brain-v42:promote":{"active":"<PROMOTE_ACTIVE_PLACEHOLDER>","accepted":[]},"brain-v42:reorg":{"active":"<REORG_ACTIVE_PLACEHOLDER>","accepted":[]}}'
```

Enabled Dream principals first receive their exact native phase catalog, independent of
`X-Brain-Tool-Profile`, then the authoritative project claim constrains reads, writes,
aggregates, search, graph traversal, backfill, AutoLinker, promotions, update, and merge. The
call middleware rejects cross-phase tools, compact gateways, and foreign references before
handler execution. Admin principals keep the current global compact/native behavior.
`BRAIN_CODE_MODE=true` is incompatible because Code Mode can introduce another gateway.

PostgreSQL is the ownership authority. Reads and mutations add the project predicate at the
point of use; promotions, merges, and relation writes revalidate ownership before protected
work. Missing, foreign, ambiguous, and unowned references receive the same non-enumerating
denial. Neo4j remains an optional relationship index: scoped operations use only the
project-bounded knowledge subgraph, require exactly one matching `BELONGS_TO` owner per
anchor, and revalidate returned UUIDs in PostgreSQL. Traversals exclude `Project` and `Domain`
nodes, and path search runs inside the authorized subgraph instead of filtering a global
shortest path afterward. AutoLinker selects candidates within the project and revalidates both
anchors before every link. Scoped authorization failures never degrade into graph success.

Denials and logs may identify only the principal, phase, project, safe tool name, and bounded
reason code. They never include UUIDs, arguments, content, bearer or registry material, SQL,
or tracebacks. Filesystem and root enforcement remain SEC1c.

An authorized operator can activate the dormant boundary with this quiescent sequence:

1. Run `systemctl --user disable --now brain-v42-dream.timer`. Wait until
   `systemctl --user show brain-v42-dream.service -p ActiveState --value` reports `inactive`.
   Record whether `Persistent=true` will trigger a catch-up when the timer starts again.
2. Run `deploy/systemd/install.sh --check-only`, then generate a private artifact with
   `--render-dir`. Inspect it and back up the live fragments/drop-ins. Stop
   `brain-mcp-http-watchdog.timer` then `brain-mcp-http-watchdog.service`, disable the timer with
   `systemctl --user disable --no-reload brain-mcp-http-watchdog.timer`, atomically publish only
   the required basenames from that artifact, and only then run `systemctl --user daemon-reload`.
   Follow the repository systemd runbooks; do not use normal install or the historical
   `--dry-run`, because both publish the complete managed set and normal install starts the Dream
   timer.
3. Edit `~/.config/brain-v42/mcp-token.env` privately with the enabled flag and a complete
   registry that defines all six phases for each of two distinct real projects. Set its mode
   to `0600`. Never print, log, or commit its values.
4. Run `systemctl --user restart brain-mcp-http.service` only. Through a loopback MCP client,
   prove `/health`, invalid-bearer `401`, Host/Origin rejection, admin compact/native access,
   every scoped catalog, one cross-phase denial, and one gateway denial. Use one real profile
   from each project; in both project directions, prove owned reads and writes succeed while
   foreign reads and writes return the same denial. Prove aggregates and search stay inside
   the claimed project. With isolated graph fixtures, prove neighbors, paths, clusters,
   orphans, backfill, and linking stay inside its project-bounded subgraph.
5. Run `systemctl --user enable --now brain-v42-dream.timer` only after every drill passes.
   Treat any persistent catch-up as an explicit Dream run and observe it; never restart the
   Dream oneshot as a configuration action.

Rotation uses two separate quiescent windows. First, disable the timer, wait for the oneshot
to become inactive, and record the persistent catch-up risk. Set `accepted=old` and
`active=new`, restart only HTTP, then prove that the `accepted=old` and `active=new` bearers
expose the same scoped catalog. With each bearer, repeat the project-isolation checks from
rollout step 4; only then prove the cross-phase and gateway denials. Re-enable the timer and
observe any explicit catch-up or Dream run using `new`. To revoke, open a new window: disable the
timer, wait for the oneshot to become inactive, remove `old`, restart only HTTP, and prove `old`
returns `401` while `new` works. Re-enable the timer and handle any persistent catch-up. Never
restart the Dream oneshot for rotation or revocation.

Rollback uses the same quiescence boundary: disable the timer, wait for the oneshot to become
inactive, set `BRAIN_DREAM_CAPABILITY_ENFORCEMENT=false` in the shared file, restart only the
HTTP MCP service, prove the historical global admin contract, then re-enable the timer and
handle any persistent catch-up. This delivery performed no deployment, service restart, timer
change or activation, token creation, live-credential access, or enforcement activation.

Designs: [provider migration](../.specs/plans/dream-codex-agent-migration.design.md), [original Dream mode](superpowers/specs/2026-04-05-dream-mode-design.md), and [v3 actionability](superpowers/specs/2026-04-17-dream-v3-actionability-design.md).

## GitLab webhook ingestion

`src/brain_v42/services/gitlab_ingestor.py` receives webhook payloads and:

1. Deduplicates on `gitlab_event_id` (unique index, `ON CONFLICT DO NOTHING`).
2. Extracts text from merge requests, issues, commits, comments.
3. Embeds and asks `ClusterGuard` to resolve the signal against existing `features` — link, merge, or create.
4. Writes the raw event to `gitlab_events` for audit, and a typed row to `feature_artifacts`.

Feature creation has two deliberate paths:

- **Explicit MCP creation.** `brain_feature_create` delegates to `FeatureCreationService`. It
  requires an existing `project_contexts` row, generates an embedding with the configured
  `EMBEDDING_DIMENSION` (1536 by default), then locks that row and repeats the project and exact
  trimmed, case-insensitive name checks before inserting in the same transaction. It defaults to
  `status=planned` and `pinned=true`; accepted initial statuses are `planned`, `research`,
  `design`, `building`, `deployed`, and `done`, while `archived` is rejected. This path bypasses
  `ClusterGuard` and provides neither semantic nor global uniqueness.
- **Signal-driven resolution.** Eligible artifact, plan, and GitLab paths continue through
  `ClusterGuard`, which may link, merge, or create within the project using semantic similarity.

Decision and concurrency boundary:
[explicit roadmap feature creation](superpowers/specs/2026-07-23-explicit-roadmap-feature-creation-design.md).

`StatusEngine` advances feature status monotonically
(planned → research → design → building → deployed → done) based on artifact types.

## GitNexus integration

Side-by-side, not merged. GitNexus is a second MCP stdio server exposing `gitnexus_*` tools (code graph: AST + call chains + impact analysis). Isolation invariants: disjoint MCP surfaces (`brain_*` vs `gitnexus_*`), disjoint data (`.gitnexus/` vs PG+Neo4j), disjoint compute (transformers.js CPU vs GPU embed :8003). Nightly reindex at 04:30 finishes before the Dream timer's 06:00 window (plus up to 120 seconds of jitter).

Design: `docs/superpowers/specs/2026-04-20-gitnexus-integration-design.md`.

## Configuration

Environment variables (`src/brain_v42/config.py`):

```
POSTGRES_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain   # required
EMBEDDING_SERVICE_URL=http://localhost:8003                           # local unified GPU service
EMBEDDING_DIMENSION=1536
RERANKER_URL=http://localhost:8003                                    # same unified endpoint
NEO4J_URL=bolt://localhost:7687                                      # legacy path only
NEO4J_USER=neo4j                                                     # legacy path only
NEO4J_PASSWORD=...                                                   # legacy path only
GRAPH_ENABLED=false                                                   # opt-in
GRAPH_LEDGER_WRITE_ENABLED=false                                      # safe default; production is true
GRAPH_OUTBOX_INTERVAL_SECONDS=5                                       # > 0
GRAPH_OUTBOX_BATCH_SIZE=100                                           # 1 .. 1000
GRAPH_OUTBOX_MAX_ATTEMPTS=10                                          # 1 .. 100
GRAPH_PROJECTOR_ENABLED=false                                         # private MCP role at cutover
GRAPH_PROJECTOR_NEO4J_URL=bolt://127.0.0.1:7687                        # credential-free URI
GRAPH_PROJECTOR_NEO4J_USER=neo4j
GRAPH_PROJECTOR_NEO4J_PASSWORD=...                                    # SecretStr
METRICS_ENABLED=false                                                 # opt-in
METRICS_PORT=9200
AUTOMATION_HOST=127.0.0.1                                            # loopback only
AUTOMATION_PORT=9201
AUTOMATION_DEDUP_INTERVAL_SECONDS=21600
METRICS_LEGACY_AUTOMATION_ENABLED=true                               # safe default
DECAY_ENABLED=true
BRAIN_MCP_TRANSPORT=http                                              # or stdio (default)
MCP_HTTP_HOST=127.0.0.1                                              # loopback only
MCP_HTTP_PORT=8765
BRAIN_MCP_PROFILE=compact                                             # compact or native
BRAIN_CODE_MODE=false                                                 # experimental; overrides profile
BRAIN_DREAM_CAPABILITY_ENFORCEMENT=false                              # dormant by default
```

`MCP_HTTP_TOKEN` et `MCP_HTTP_DREAM_TOKENS` n'appartiennent jamais au `.env` partagé. Le
transport HTTP direct de développement peut omettre le bearer ; le chemin systemd de production
exige un `MCP_HTTP_TOKEN` non vide dans `~/.config/brain-v42/mcp-token.env` en mode `0600`.

The default `compact` profile always exposes `brain_session_start`,
`brain_session_capture`, `brain_session_heartbeat`, `brain_session_end`,
`brain_session_list`, `brain_session_resume`, and `brain_session_abandon`, plus
`brain_find_tool` and `brain_call_tool`. The remaining
registered tools stay discoverable and callable through those two catalog tools. The
`native` profile lists every registered Brain tool directly. Experimental
`brain_code_mode` takes precedence and bypasses the catalog profile.
Capability enforcement rejects Code Mode. Scoped Dream principals always receive their exact
native phase catalog; only admin principals use the compact/native presentation setting.
The shared environment keeps `GRAPH_ENABLED` and `GRAPH_LEDGER_WRITE_ENABLED` visible to all
legacy-writer guards. At cutover, remove the legacy `NEO4J_URL` and `NEO4J_PASSWORD`, rotate
the Neo4j credential, and place only `GRAPH_PROJECTOR_*` in
`~/.config/brain-v42/graph-projector.env`. The file must be a regular non-symlink owned by the
service user with exact mode `0600`; its URL must contain no credentials, query, fragment, or
path. The MCP runtime requires this private credential whenever the ledger flag is active,
while the metrics runtime refuses the projector role. The systemd preflight checks the file
shape but cannot prove writer quiescence or credential revocation. On Neo4j Community this is
a secret-distribution boundary, not a reduced-privilege database role.

Enabling `GRAPH_LEDGER_WRITE_ENABLED` also requires `GRAPH_ENABLED=true`. The MCP runtime then
fails startup unless the private projector role, migration-035 recovery shape, and protocol-v2
state pass readiness checks.

## Project structure

```
brain_v42/
├── src/brain_v42/
│   ├── config.py                 # Settings (pydantic-settings)
│   ├── db/                       # engine.py, tables.py (30 tables), neo4j.py
│   ├── models/                   # Pydantic models
│   ├── repositories/             # BasePgRepository + Pg<Entity>Repo + graph/audit repos
│   ├── services/                 # application services (search/, session, decay, dream…)
│   ├── metrics/                  # collector, flusher, server (:9200), cockpit
│   ├── automation/               # webhook + dedup owner, server (:9201)
│   └── mcp/
│       ├── server.py             # entry point (stdio+http), build_services(), app_lifecycle()
│       ├── http_security.py      # HostOriginGuard + BearerTokenGuard ASGI middleware
│       └── tools/                # 49 always-on + 2 graph-gated = 51
├── alembic/versions/             # migrations 001 .. 048 defined in the repository
├── scripts/                      # legacy import + projection inventory/recovery CLIs
├── tests/                        # unit/ + integration/
├── docs/
│   ├── ARCHITECTURE.md           # this file
│   ├── GRAPH_LEDGER_RUNBOOK.md   # gated import, rebuild, observability, rollback
│   ├── MCP_TOOLS.md              # tool catalog
│   ├── SCHEMA.md                 # PG schema reference
│   └── superpowers/specs/        # design specs per feature
└── scripts/dream.sh              # nightly dream orchestrator
```

## datalake_v2 -> brain_v42 (what changed)

| Aspect | datalake_v2 (pre-2026-02) | brain_v42 (current repository) |
|--------|---------------------------|------------------------|
| MCP application topology | API + MCP bridge inside a 7-container chain | 1 shared FastMCP HTTP process; stdio dev/fallback |
| Network hops per tool call | 4 (HTTP chain) | 0 for tool logic; 1 HTTP hop for MCP protocol (loopback) |
| Source of truth | Neo4j (CRUD + graph + vectors) | Postgres + pgvector |
| Graph | Neo4j Cypher reduce() for similarity | Neo4j relationship index only, pgvector for similarity |
| Embeddings | sentence-transformers / PyTorch in-process | Local GPU service :8003 (Qodo-Embed-1-1.5B, 1536d) |
| Reranker | none | Cross-encoder :8003 unified endpoint, BatchingRerankerClient (20 ms window) |
| MCP transport | stdio | HTTP loopback 127.0.0.1:8765 + HostOriginGuard + bearer obligatoire sous systemd (optionnel en HTTP dev direct) |
| MCP tools | 21 | 49 always-on + 2 graph-gated = 51 |
| Tables | 6 | 31 (knowledge, audit, plans, dream, webhook, coordination, sessions, graph ledger) |
| Maintenance | manual | Dream mode nightly + DecayFlusher + ConsolidationJob |
| Observability | none | /metrics :9200 + /api/cockpit + process_metrics |

## References

- `docs/SCHEMA.md` — PG schema column-level
- `docs/MCP_TOOLS.md` — tool catalog
- `docs/GRAPH_LEDGER_RUNBOOK.md` — import, cutover, rebuild, observability, and rollback gates
- `docs/superpowers/specs/2026-03-13-memory-decay-design.md`
- `docs/superpowers/specs/2026-03-14-roadmap-v2-design.md`
- `docs/superpowers/specs/2026-03-16-neo4j-knowledge-graph-design.md`
- `docs/superpowers/specs/2026-03-22-project-groups-and-key-normalization-design.md`
- `docs/superpowers/specs/2026-04-05-dream-mode-design.md`
- `docs/superpowers/specs/2026-04-07-plans-chunking-indexing-design.md`
- `docs/superpowers/specs/2026-04-17-dream-v3-actionability-design.md`
- `docs/superpowers/specs/2026-04-20-gitnexus-integration-design.md`
