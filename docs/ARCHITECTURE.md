# Architecture — brain_v42

**Repository schema:** the repository carries migrations through revision 062. This is a code target, not a statement about any deployed database. Migration 062 adds two partial observer-error indexes and installs `pg_stat_statements` in the `monitoring` schema. Earlier revisions add measured-fact definitions, delivery attestations, focus slots, ticket release planning, and session connection provenance.

**MCP catalog:** 78 always-on + 2 graph-gated = 80.

brain-v42 is a persistent FastMCP application backed by PostgreSQL and pgvector.
Neo4j is a derived relationship projection; HTTP is used for persistent connections
and stdio remains available for development and fallback deployments.

## Overview

The current runtime has distinct MCP, metrics, delivery-observer, graph-projector,
and embedding components. The MCP server composes tool services with HTTP middleware
for host/origin checks, fail-closed bearer authentication, request-size enforcement,
and request provenance. Bounded input models constrain tool arguments. HTTP
connections can own server-created agent traces; transport termination closes
those traces. Operator sessions can absorb eligible artifacts from traces and
from the connections on which the operator session was observed.

PostgreSQL is the source of truth for knowledge, projects, sessions, delivery data,
and graph facts. The transactional graph ledger/outbox records graph changes;
the separate graph projector consumes it and updates Neo4j, a rebuildable
relationship projection. Search uses PostgreSQL FTS and pgvector, with embedding
and reranking supplied by the configured endpoint. The metrics sidecar reads
operational state and exposes metrics and health. The delivery observer polls
providers and records confirmations for ticket and release views. The facts
registry exposes definitions and measured values through `brain_fact_get` and
`brain_fact_list`. Dream provides separately gated consolidation and maintenance.

```
                            Claude Code
                                 |
                  MCP HTTP (127.0.0.1:8765/mcp)
                  X-Brain-Agent: brain-v42
                                 |
                                 v
 +-------------------------------+--+  +----------------------+  +----------------------+
 | FastMCP server (Python 3.12+)     |  | Metrics runtime      |  | Automation runtime   |
 | 78 always-on + 2 graph-gated = 80 |  | 127.0.0.1:9200       |  | 127.0.0.1:9201       |
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
4. **Graph projection is explicit.** PostgreSQL stores graph facts and the transactional outbox; the graph projector delivers them to Neo4j. Configuration controls whether graph integration and ledger writes are enabled. Durable ledger failures propagate instead of degrading into success.
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
  fail-closed `BearerTokenGuard`. Empty or blank `MCP_HTTP_TOKEN` refuses HTTP startup
  unless `MCP_HTTP_ALLOW_UNAUTHENTICATED=true` explicitly opts out for development.
  The opt-out is refused with a token or under capability enforcement and never belongs
  in the production systemd unit, which requires the private, non-empty admin bearer.
  Every non-`/health` request must carry the bearer when configured.
- **Enabled capability mode** — FastMCP's token-verifier boundary authenticates the distinct
  admin bearer and phase-scoped Dream bearers. Application middleware then filters tool lists
  and authorizes calls. `HostOriginGuard` remains enforced; FastMCP authentication runs before
  the supplied ASGI middleware. Real loopback HTTP tests pin the resulting behavior.

`/health` is always exempt from auth — used by systemd watchdog and red-monitor.

### Dev/fallback: stdio

Activated by `BRAIN_MCP_TRANSPORT=stdio` (default) or omitting the env var. Used for local dev (`fastmcp dev`), pytest fixtures, and non-fleet contexts. Signal handlers (SIGTERM/SIGINT) and `prctl(PR_SET_PDEATHSIG, SIGTERM)` prevent zombie MCP children.

### Network trust boundary

**Tracked network boundary** (replayed 2026-08-23): MCP, PostgreSQL and Neo4j bind to loopback; metrics and automation default to loopback. The versioned Compose target binds the embedding host publish to loopback and the live runtime matches it — measured `127.0.0.1:8003`, with the host's own LAN address refusing the connection. Application bearer authentication is armed and enforcing: `MCP_HTTP_TOKEN` is set and non-empty in the live server process, and `POST /mcp` answers `401` both without a bearer and with a wrong one. The dedicated Docker client network exists and carries the clients: `brain-net` holds the embedding shim and both `auto-discord` containers. Repository-managed WAN isolation remains unproven — the repository manages no firewall rule at all. What would make this paragraph false again, and is watched by no test: a host-publish override reopening `:8003`, or `MCP_HTTP_TOKEN` cleared. `METRICS_HOST` has LEFT that list: since 2026-09-03 (`6c61b63`) a fail-closed validator refuses a non-loopback bind unless `METRICS_ALLOW_NON_LOOPBACK` names the decision, and under that opt-in the three POST receivers stay unregistered and say so on `/healthz`. Re-measure with `ss -ltnp`, `docker port` and an unauthenticated `POST /mcp` — do not copy this line forward.

**Embedding topology**: production/default = local unified endpoint `http://localhost:8003`; the personal `dev-pc` deployment is a superseded rollback/reference path, now private.

**Embedding shim limits (ROLLED OUT 2026-08-21, temps 1)**: 8 MiB body, 5 s body-read timeout, 8 concurrent ingress reads, 100 embed texts, 128 rerank candidates, maximum JSON depth 64, one embedding calculation and one rerank calculation per worker. Saturation returns short `503` JSON with `Retry-After: 1`.

**SEC2 residuals** (replayed 2026-08-23): bearer authentication and the dedicated Docker client network are done — the coordinated `auto-discord` cutover happened, and both `auto-discord` containers sit on `brain-net`. One residual stands, and it is wider than previously written: the versioned legacy PyTorch profile remains unbounded — `services/embedding/main.py` carries no body cap, no read deadline, no concurrency semaphore and no `413`/`503` — and it preserves neither of the two DNS names its clients use. A `--profile legacy` rollback publishes `embedding` and `brain_v42_embedding` on `brain-net`, while the compose sets `EMBEDDING_URL=http://embedding-shim:8003` and the running bot, carrying no `EMBEDDING_URL` of its own, falls back to the code default `http://brain_v42_embedding_shim:8003`. Two names break, not one.

The deployment model is personal agents on a trusted LAN. Remote database administration goes
through an SSH tunnel. The loopback publish is code-ready but not live proof: until an authorized
rollout verifies the effective bind and listeners, treat `:8003` as LAN-exposed. Do not expose it
to the Internet; independently verify the router/network boundary.

### Reranker: GPU with CPU fallback

`OnnxRerankBackend` (`services/embedding_shim/shim_backends.py`) prefers
`CUDAExecutionProvider` (env `RERANK_DEVICE=auto|cuda|cpu`, default `auto`), falling back to
`CPUExecutionProvider` when CUDA is unavailable or a CUDA run raises (retried once on CPU for
that request). Candidates are sorted by real token length and scored in micro-batches (env
`RERANK_BATCH_SIZE`, default 32), so each batch pads to its own local max instead of the whole
request's. Measured 2026-09-26 (85/128 real knowledge-base candidates, same model + tokenizer):
CPU single batch ~3.4 s; CPU sorted micro-batches of 32 ~1.9 s; CUDA sorted micro-batches of 32
~0.3 s (85 candidates) / ~0.4 s (128) — a single CUDA batch of 128x512 OOMs the shared 6 GB GPU.
CPU vs CUDA scores: max |diff| 0.00012, identical top-10.

### Qodo retired from the default stack (2026-09-26)

brain-v42 embeds through the Mistral codestral endpoint since 2026-09-22; the local
Qodo-Embed-1-1.5B GGUF served by `embedding-llama` (llama.cpp) is no longer a brain
dependency. `auto-discord`, the last `/embed` client, is migrating off it in its own
change. Consequently `embedding-llama` sits behind the `qodo` Compose profile: a plain
`docker compose up` never starts it, and `embedding-shim` carries no `depends_on` on it
— the shim starts on its own and serves `/rerank` (used by brain's hybrid search and
ClusterGuard) regardless of whether qodo is running.

`POST /embed`, `/embed/query` and `/embed/single` on the shim depend on
`embedding-llama` and answer with an upstream error while it is stopped — this is
expected, not a regression, for any caller that has not migrated off `/embed` yet.

**The `qodo` Compose profile change alone does not stop a container already running
from the old default stack.** `docker compose up` only skips starting
`embedding-llama` on a fresh bring-up; it does not touch a container that a previous
`docker compose up` (before this change) already started and left running. Stop it
explicitly on any host that ran the old default stack:

```bash
docker stop brain_v42_embedding_llama
```

Verify with `docker ps -a`: the container should show as `Exited`, not `Up`, once
stopped (it stays present, just not running, unless also `docker rm`d). This was done
by the operator on the production host on 2026-09-26, alongside this change.

To bring qodo back deliberately (e.g. to serve a caller that still needs `/embed`):

```bash
docker compose --profile qodo up -d embedding-llama
```

This does not change the reranker: `/rerank` is served locally by the ONNX
cross-encoder (see above) and never depended on `embedding-llama`.

## Storage layout

| Store | Port | Role | Source files |
|-------|------|------|--------------|
| Postgres 16 + pgvector | 5433 | CRUD + FTS + 1536-d vectors + audit + graph ledger/outbox + time series | `src/brain_v42/db/tables.py`, `alembic/versions/` |
| Neo4j 5 Community | 7687 (bolt) / 7474 (browser) | Relationship traversal, clusters, neighbourhood, domain classification | `src/brain_v42/db/neo4j.py`, `src/brain_v42/services/graph_service.py` |
| GPU embedding service | 8003 | Cross-encoder reranker (always on); `/embed*` only when the `qodo` profile runs `embedding-llama` (Qodo-Embed-1-1.5B, 1536 dims) — the shipped configuration (`BRAIN_EMBEDDING_BACKEND=shim`, `BRAIN_EMBEDDING_MODEL=qodo`) embeds through it, so it needs `--profile qodo`; the production deployment instead sets the OpenAI-compatible backend to Mistral codestral | `src/brain_v42/services/gpu_embedding_service.py` |
| Reranker | 8003 | Cross-encoder rerank for hybrid search + ClusterGuard grey zone (same unified endpoint as embed) | `src/brain_v42/services/reranker_client.py` |

### 43 PG tables (`src/brain_v42/db/tables.py`)

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
Delivery: `delivery_workflows`, `delivery_contract_revisions`, `delivery_dependencies`,
`delivery_artifact_bindings`, `delivery_snapshots`, `delivery_confirmations`,
`delivery_receipts`, `delivery_events`, `delivery_attestations`.
The delivery observer derives two kinds of attestations from GitHub, issued as
`brain-v42-delivery-observer`: `released` (the first `v*` tag that contains a merged
deliverable) and `deployed` (the live brain-v42 release contains it, brain-v42 only).
The ticket view renders them beside `tickets.target_release` without reconciling the
two: a plan is a declaration, a release is a measurement (see `docs/OPERATIONS.md`,
"Delivery observer: releases and deployments").

See `docs/SCHEMA.md` for the maintained schema reference. Migration files and
`src/brain_v42/db/tables.py` remain authoritative.

### Canonical graph ledger, fencing, and recovery (migrations 033–035)

Historical cutover record:

**Production graph state:** cutover validated at head 035 on 22 July 2026

To measure the live Alembic head, run `docker exec brain_v42_postgres psql -U brain -d brain -Atc "select version_num from alembic_version"`. The repository migration head is not evidence of the deployed database head.

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

Ledger proofs belong to one instance and do not authorize another. A fresh or otherwise
unproved environment must keep `GRAPH_LEDGER_WRITE_ENABLED=false` until the graph ledger
runbook's gates have been repeated there.

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

Historical evidence, kept for reference: the earlier head-035 restore proves the graph cutover
state. DR-v5 run `20260724_150315` renewed the PostgreSQL restore evidence at head 037 with
24/24 checks and an independent SQL attestation. A future recovery must revalidate the evidence
for its own instance and deployed head.

The relation row and its outbox instruction are atomic with each other, not necessarily with
the earlier business-row commit. When a service commits an entity before staging a related
edge, a ledger failure propagates but may leave that entity committed without the relation.
Clients must treat the operation as a retryable saga. The cutover drill must exercise this
window and prove its retry/reconciliation contract.

The code supports enabling graph ledger writes and projection through configuration.
Those settings and recovery proofs are environment-specific and are not asserted by this
architecture reference. Follow the [graph ledger runbook](GRAPH_LEDGER_RUNBOOK.md) for
the required preconditions and recovery procedure.

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

The session schema adds persistent operator sessions, exclusive capture attribution,
server-owned HTTP agent traces, focus history, slots, checkpoints, and recorded
connections. Session rows distinguish `open`, `ended`, `abandoned`, and
`closed_inactive`; database constraints validate terminal fields and slot bindings.
This describes the repository schema, not a deployed database or a completed
disaster-recovery proof.

- **User-controlled boundaries.** Only an explicit user command may start, capture, heartbeat, list, resume, bind, relay, end, or abandon a session on the agent and client side. Hooks and agents never infer a boundary or close a stale session. **Amendment — slot relay (ADR #34).** A guard mod that the operator has explicitly enabled counts as a standing user command for one gesture only: `brain_session_relay` of an open operator session, onto its focus slot when it is bound to one and onto the project base when it is not — capture, end and start of its successor under a new `client_key`, in one transaction. The model makes the call and chooses its captures, summary and handover; the mod only triggers the turn and replays the result at compaction. The base form writes the handover as the project's whole base focus under a compare-and-swap on its revision, and refuses a handover under 70% of the current base (`base_focus_shrink`): only an operator relay may override that guard, never the mod. The standing command covers nothing else: not `brain_session_abandon`, not `brain_session_end` of any session, not the relay of a server-opened trace, not any write of the project base outside that relay, not opening, closing or changing a slot, and not `start`, `resume` or `bind` outside the relay. It is void while `BRAIN_SESSION_RELAY_GUARD_MOD_ENABLED` is false. Hooks still never capture, close or commit on their own. Cross-cutting work without a ticket needs no anchor: left unbound, it is relayed onto the project base, under the same guards. The only server-side exception is the Dream `sweep` phase, shipped disabled and dry, which abandons an open session with no heartbeat for seven days (`abandonment_reason = 'auto_stale_7d'`) without touching project focus.
- **Concurrent starts.** Multiple open sessions may share a project. Idempotence is enforced by unique `(project_key, client_key)`; retrying the same key while its session is open returns the same UUID.
- **Two-part isolation.** Resume, capture, heartbeat, end, and abandon require both `session_id` and its `expected_client_key`. A mismatch changes nothing. This comparison is an isolation guard, not authentication.
- **Durable, exclusive provenance.** `brain_session_capture` records client-declared knowledge UUIDs in `brain_session_artifacts`, up to 100 per session. Each artifact must exist in a supported knowledge table, belong to the project, and have been created after the session started. Re-capture by its owner is idempotent; another session conflicts. This proves the caller's persisted attribution, not which process created the artifact.
- **Recoverable ledger view.** Session results expose `attributed_knowledge_ids`, loaded from the ledger for retries, list, resume, capture, heartbeat, end, and abandon. Abandon preserves ownership, and an exact capture retry remains idempotent without reopening the session.
- **Legible absorption.** When derived capture is armed, `brain_session_capture` and `brain_session_heartbeat` return an `absorption` object saying what the window stage did: `absorbed`, `abstained` (today only under ambiguity — another non-`agent` session covered the artifact's creation instant), or `nothing`. It names the rival sessions, capped at ten, and reports how many eligible artifacts a tracer still held. The field is absent (`None`) when no absorption was attempted, which is not the same statement as `nothing`. It is carried by these two tools alone: they publish no output schema, so the field costs nothing against the session tools' frozen schema budget, whereas it does not fit on `end`, `start`, or `resume`.
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

## Claim reads (lot B4)

`brain_claim_list` and `brain_claim_history` (spec 2026-09-19, section 6.6) are bounded,
SELECT-only reads layered strictly downward, with no dependency from
services/repositories/models to `facts`:

- `repositories/pg_claim_reads.py` reads immutable claim and verdict rows in one batched
  SQL query per caller-supplied entry set — never one query per entry, never one per claim.
- `models/claim_read.py`'s `evaluate_claim(claim, now)` is a pure leaf function that computes
  validity at ONE supplied UTC instant; it never reads the clock itself and never mutates a
  stored row.
- `services/claim_read_service.py`'s `ClaimReadService` batches entry lookups, enforces the
  caller's trusted project scope before revealing an occurrence or its history, and masks
  every unexpected fault behind a closed `ClaimReadError` code
  (`invalid_argument`, `claim_not_found`, `read_unavailable`) — never a raw exception or a DB
  message.
- `mcp/tools/claim_tools.py` registers the two tools unconditionally (see "MCP tool census"
  below) and `mcp/tools/claim_rendering.py` turns a batch of `ClaimState` rows into the
  compact suffix `brain_get`, `brain_search` and the session briefing recap append.

**Freshness is computed, never stored.** `valid_until = conclusive.emitted_at +
validity_seconds`, ageing from the conclusive verdict's `emitted_at` (never `recorded_at`,
so an observation measured five minutes ago and inserted a second ago is five minutes
stale). The claim is fresh on the half-open window `[emitted_at, valid_until)` and already
`stale` at the boundary instant itself (`now >= valid_until`, not `>`); a small allowed
clock skew never renders a negative age. The **latest attempt** (greatest-`seq` verdict,
whatever its outcome) and the **last conclusive** result (greatest-`seq` among
`holds`/`falsified` only) are independent: a newer `unreadable` attempt after a fresh or
stale conclusive result keeps both visible, never overwriting the older one.

**Ranking and selection are unchanged.** `brain_search`'s scoring, ordering and filtering
never read claim state; a `falsified` claim changes the rendered suffix of an entry, never
its score, its position, or whether it is returned at all. The suffix is appended strictly
after selection and ranking are already final, from one batch fetch per result set.

**MCP tool census constraint.** `register_claim_tools`'s `read_service` parameter is
optional (existing standalone tests exercising only `brain_claim_verify` omit it), but both
read tools are always REGISTERED regardless — only their behaviour is gated, raising
`read_unavailable` without one. `tests/unit/test_documentation_contract.py` walks
`mcp/server.py`'s composition root statically to census every registered tool and cannot
trace a runtime-conditional registration; a `if read_service is None: return` guard placed
before a tool's `@mcp.tool`/`@claims.tool` definition made the census fail with "dynamic
control flow can bypass registration calls" (ticket-less regression from `3f8ed541`, fixed
in the same lot). The same pattern already used by `brain_get`/`brain_search`'s optional
`claim_read_svc` — register unconditionally, gate only the runtime behaviour — is the one
constraint every future optional-dependency tool in this repository must follow.

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

`GET /metrics` itself is polled by `red-monitor` roughly every 5s. The `dream`, `nightly`, `tickets` and graph-inventory (`nodes_total`/`edges_total`/`orphans_total`) sections it assembles together run on the order of fifteen PostgreSQL queries plus a Neo4j round trip on every call. `SlowBlockCache` (`src/brain_v42/metrics/slow_block_cache.py`) memoizes those blocks behind a TTL with single-flight coalescing: concurrent pollers landing during a refresh await the SAME in-flight computation rather than each starting their own query set. Each cached block carries its own `generated_at` (ISO-8601 UTC), additive — no existing key is renamed or removed. `database` and the embedding/graph healthchecks are deliberately excluded and stay live, uncached, on every poll. TTLs are `METRICS_SLOW_BLOCK_CACHE_TTL_SECONDS` (default 30s) and the shorter `METRICS_SLOW_BLOCK_CACHE_ERROR_TTL_SECONDS` (default 5s) for a failed or partial computation — a raised exception is never cached as a success for the full TTL (decision 1669d429 item 2). Two signalling shapes reach that short TTL: `tickets` raises its SQL exception plainly (see below), while `dream`, `nightly` and graph-inventory each catch their own SQL/Neo4j failures internally and, since they already have a degraded value ready (`{}`, or a partial dict), raise it wrapped in `CollectorDegraded(payload)` instead of returning it — a plain return would have been indistinguishable from a legitimate empty result (e.g. "no dream runs yet") and was memoized for the full TTL (a MAJOR fix, PR #201). `SlowBlockCache` recognises `CollectorDegraded` ahead of its generic `except` clause and stores/returns `.payload` unchanged — red-monitor sees the exact same shape either way, only the retry cadence differs.

`tickets` (ticket 0fb857ef) is the top-level per-project ticket-counter block that replaces red-monitor's abandoned roadmap tab — brain owns the counter semantics so red-monitor holds no brain DB credential and runs no SQL of its own: `{"generated_at": ..., "categories": [{"key": "todo", "label": "à traiter"}, {"key": "to_confirm", "label": "à confirmer"}, {"key": "waiting", "label": "en attente"}], "projects": [{"project": "brain-v42", "counts": {"todo": 53, "to_confirm": 2, "waiting": 9}}]}`. `count_grouped_by_project()` (`src/brain_v42/repositories/pg_ticket.py`) computes it by reusing — never re-deriving — the SAME `_ACTIONABLE`/`_CONFIRMABLE` status tuples as `PgTicketRepo.list_grouped`'s `a_traiter`/`a_confirmer`/`en_attente` groups, so the two can never drift apart: `todo` mirrors `a_traiter` (`to_project`, no self-ticket exclusion), `to_confirm` mirrors `a_confirmer` (`from_project`, so a resolved ticket counts at the REQUESTER, not the executor), `waiting` mirrors `en_attente` (`from_project`, excluding self-tickets — a note-to-self is never "waiting on someone"). `projects` arrives pre-sorted most-urgent-first (`todo` desc, then `to_confirm` desc, then `waiting` desc); a project with nothing pending is simply absent from the list. `collect_ticket_counts()` (`src/brain_v42/metrics/collector_tickets.py`) deliberately does not catch its own exceptions at all, unlike `collect_nightly_ops`/`collect_dream_metrics`/`collect_graph_inventory` (which catch internally and re-signal via `CollectorDegraded`, above) — it relies on `SlowBlockCache`'s own generic `except` clause to apply the short error TTL, so `projects: []` (nothing pending anywhere, still published) stays distinguishable from the whole `tickets` key being absent from the payload (a failed computation, "not measured").

Instrumentation is opt-in: `InstrumentedEmbeddingService`, `InstrumentedGraphService`, `InstrumentedReranker`, and `instrument_tool()` wrap the real services when `metrics_enabled=true`.

The `embedding_service.usage` block carries only provider-reported totals, split into
the fixed `read` and `write` intents. Each intent has `total_tokens` and
`reported_requests`; an absent provider usage value contributes neither field, while a
reported zero counts as one request. The wrapper captures these integer aggregates for
one embedding call, and the flusher persists them in the `_process` row. The sidecar
aggregates only those process rows, so agent rows cannot double-count usage. These are
observability counters, not a billing ledger: request text, provider payloads and prices
are not retained, and the totals reset with the process that holds them.

Only a backend whose responses carry a `usage` object feeds these counters. The default
`shim` backend reports none, so on it both intents stay at zero for good:
`reported_requests = 0` means *unknown*, never *free*. Which backend a process runs is
set by `EMBEDDING_BACKEND`; read it in the live process environment rather than inferring
it from this paragraph.

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

`scripts/dream.sh` orchestrates six headless agent phases: **SCAN / CLEAN / CONNECT / SYNTH / PROMOTE / REORG**. Codex is the default provider and authenticates through the active ChatGPT login. The fast tier (phase 1: SCAN, CLEAN, CONNECT) uses `gpt-5.6-luna` with high reasoning; the deep tier (phase 2: SYNTH, PROMOTE, REORG) uses `gpt-6-astra` with max reasoning. Both defaults were canaried through the live Codex binary on 2026-09-12; `max` requires the runner that accepts it (same day).

The Codex adapter exposes only the Brain MCP tools required by each phase:

| Phase | Default tier | Exact Brain MCP allowlist |
|-------|--------------|--------------------------|
| SCAN | `gpt-5.6-luna` / high | `brain_decay_status`, `brain_consolidation_candidates`, `brain_list`, `brain_search` |
| CLEAN | `gpt-5.6-luna` / high | `brain_search`, `brain_get`, `brain_consolidation_candidates`, `brain_decay_status`, `brain_merge_entities`, `brain_delete`, `brain_list` |
| CONNECT | `gpt-5.6-luna` / high | `brain_backfill_links_batch`, `brain_list_orphans_for_classification`, `brain_assign_domain` |
| SYNTH | `gpt-6-astra` / max | `brain_get_clusters`, `brain_get`, `brain_learn`, `brain_save_snippet`, `brain_search`, `brain_list`, `brain_get_neighbors`, `brain_graph_path` |
| PROMOTE | `gpt-6-astra` / max | `brain_get`, `brain_search`, `brain_promote_adr`, `brain_promote_runbook`, `brain_list`, `brain_get_neighbors`, `brain_graph_path` |
| REORG | `gpt-6-astra` / max | `brain_search`, `brain_list`, `brain_get`, `brain_update` |

`scripts/dream/codex_runner.py` starts each turn in an ephemeral, read-only workspace. It ignores ambient Codex configuration, requires the loopback Brain MCP server, and disables shell, web search, apps, and subagents. The orchestrator checks both ChatGPT authentication and `MCP_HTTP_TOKEN` before maintenance begins. With capability enforcement enabled, it also validates a complete six-phase profile before phase one, passes only that phase's `active` bearer through an allowlisted child environment, removes the full registry, and adds the loopback MCP hosts to `NO_PROXY`. It never falls back automatically on a failure it cannot read: a failed WET phase may already have committed a mutation, so switching providers mid-run would risk replaying it. The chain (`BRAIN_DREAM_AGENT_PROVIDERS`) advances on two codes only, both a proof that no Brain tool call succeeded — `3` (failed, no completed call) and `4` (the runner's own deadline on a stream where no call ever started; the link is then retired for the rest of the night, `LINK DOWN` in the log — from the first immutable release that ships headless-agents 0.3.0). Claude remains an explicit operator rollback only after capability enforcement is disabled:

```bash
BRAIN_DREAM_AGENT_PROVIDER=claude scripts/dream.sh brain-v42
```

Model and reasoning defaults can be overridden without changing the phase policy:

```bash
BRAIN_DREAM_CODEX_FAST_MODEL=gpt-5.6-luna \
BRAIN_DREAM_CODEX_FAST_REASONING=high \
BRAIN_DREAM_CODEX_DEEP_MODEL=gpt-6-astra \
BRAIN_DREAM_CODEX_DEEP_REASONING=max \
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
the live unit directory; `--render-dir` publishes the verified unit files and, on restricted
hosts, their AppArmor user namespace compatibility drop-ins to a new private directory outside
systemd. The historical `--dry-run` still writes all eight live fragments and
is not a side-effect-free preview. On 24 July 2026 only `brain-mcp-http.service`,
`brain-mcp-http-watchdog.service` and `brain-mcp-http-watchdog.timer` were published and canaried
live, including kernel enforcement and authenticated E2E. The five Dream, graph-recon and
automation fragments have not yet been rolled out.

### Shared agent runtime (`headless_agents`) and the Dream's adapters (`brain_v42.agents`)

Before lot 1 of the agent runtime extraction (Brain ticket c31bad72, 2026-09), the codex, agy and
Claude adapters above lived only in `scripts/dream/{codex,agy,claude}_runner.py` and
`scripts/dream/_agent_capability.py` -- private to Dream and outside the wheel. When the extract
rescue link (`src/brain_v42/scripts/agy_completion.py`) needed the same ephemeral-HOME pattern for
its own agy fallback, it had to reimplement it a third time rather than depend on `scripts/`,
which `src/brain_v42/` must never import from. Lot 1 moved that code into `src/brain_v42/agents/`;
lot 2 (ticket afd56820) moved the phase chain after it.

Brain ticket b2a2d1a5 (decision 3c5c56e1, 2026-09-14) then split the package in two, because
the same mechanics were being rewritten in every ReD project that runs an agent CLI and a
consumer whose invariant is "no database" cannot import `brain_v42` (a dry import loads
sqlalchemy, neo4j, pgvector, fastmcp and uvicorn):

```
headless-agents                    # Git dependency from hawkixs/red-ha, tag v0.5.4
    headless_agents/ package
        profile.py       # CapabilityProfile: McpServer, ToolGuard, Credentials
        capability.py    # exit codes 3/4/124, child-env allowlist, NO_PROXY, loopback, killpg
        sandbox.py       # ephemeral HOMEs from a profile, credentials (symlink | 0600 copy)
        spec.py, result.py, protocol.py   # RunSpec / RunResult / AgentProvider
        chain.py         # the fallback state machine, reporting through callbacks
        envelope.py      # pure unwrap of claude/codex/agy JSON envelopes (model, tokens, cost)
        providers/codex.py, agy.py, claude.py, opencode.py   # build_*_command / run_* over an McpServer|None

src/brain_v42/agents/                # the Dream's POLICY over the runtime
    capability.py        # BRAIN_DREAM_* variables, registry, (project, phase) bearer, brain_mcp_server()
    sandbox.py           # the Dream HOMEs (agy phase, opencode phase, tool-less extract) and their credential lists
    providers/*.py       # the historical run_*/build_* signatures and the CLIs dream.sh invokes
    lines.py, prompt.py, phase.py, chain.py, run_phase_chain.py   # phase orchestration (below)
```

The runtime executes; it never decides. Everything the Dream used to resolve for itself from
`brain_v42.mcp.dream_capabilities` -- which MCP server, with which bearer and which exact tool
allowlist, which `PreToolUse` guard, which credential files -- arrives as one
`CapabilityProfile` value. `McpServer` carries the server name the CLI declares, its URL
validated against `allowed_networks` (loopback by default; a private network is admitted by
listing it, `None` lifts the restriction by name -- the Dream's `brain_mcp_server` uses `None`,
its URL being validated under enforcement instead), the bearer as a value (for
the rail that writes it literally, agy) and/or the variable name it travels under (codex and
claude read it from their environment), the extra headers and the tools. `mcp=None` is a run that
may reach no server at all: codex declares no `mcp_servers`, claude gets `{"mcpServers": {}}`
under `--strict-mcp-config` and no `--allowedTools`, agy an empty `mcp_config.json`. A profile
without a guard is refused by the agy rail: the guard is the only wall between agy and a shell.

The upstream package test suite enforces its import boundary and dependency set. And the
Dream's behaviour did not move: `brain_v42.agents` keeps every public name, signature, CLI
argument, log line and exit code of lots 1 and 2, builds the profile from the phase allowlists
and the `MCP_HTTP_DREAM_TOKENS` registry, and hands it down. `tests/unit/agents/
test_golden_commands.py` still holds the argv, the child environment and the agy HOME's files to
the fixtures captured from the pre-extraction runners, byte for byte, for every provider and
every phase; the fixtures were not touched by the split.

The package is installed as a Git dependency from its own repository:

```sh
uv add "headless-agents @ git+https://github.com/hawkixs/red-ha.git@v0.5.4"
```

Inside this repository `uv sync` installs the pinned Git dependency alongside `brain_v42`.
Since the move to red-ha, `headless-agents` is a third-party dependency pinned by commit in
`uv.lock`, like every other dependency; `scripts/check_delivery_deployment.py` no longer
attests its files individually. The release builds only the `brain_v42` wheel and sdist.
Deployment validation still checks any `packages/<dir>` workspace member carried by a release
archive; brain-v42 carries none.

What the runtime offers beyond what the Dream uses today, for the red-arena pilot (ticket
e9087e13): `sandbox.sandbox_environment()` (a rebuilt `HOME`/`TMPDIR`/`PATH`/`LANG`/`LC_ALL`
environment that inherits nothing else), `Credentials(mode="copy")` (a `0600` copy for a sandbox
that must not point back at the real HOME), `RunSpec.deadline` (a monotonic instant every link of
a chain can share), and `envelope.unwrap()` (the model the CLI REPORTED, its token counts and its
cost, never raising -- an unreadable envelope yields the raw text). agy still takes its prompt in
argv, not on stdin: measured 2026-08-11, it ignores stdin.

The fourth adapter, `providers/opencode.py` (2026-09-15, headless-agents 0.2.0), runs
`opencode run --format json` under the OpenCode Go subscription and is the rail that writes no
secret to disk: its configuration travels inline in `OPENCODE_CONFIG_CONTENT` and references the
bearer as `{env:<var>}`, which opencode substitutes from the child environment (measured on
1.18.30, headers included). Its tool wall is not a guard script but the config's `tools` map, a
fail-closed ALLOWLIST -- `{"*": false, "<server>_<tool>": true}` -- that removes every built-in
tool before the model sees it; asked to list its tools, the model names the scoped MCP tools and
reports `bash` as absent, not denied. It still needs an ephemeral HOME, because opencode persists
every session into `~/.local/share/opencode/opencode.db` and reads its credential from
`auth.json` there. The cost nobody documents: a fresh HOME makes opencode `bun install` 150 MiB
from npm on every run, even under `--pure`; the adapter symlinks the operator's
`~/.config/opencode/node_modules` into the HOME (footprint 936 KiB, no network) and refuses to
start when the real HOME has none. `step_finish` events are summed into `RunResult.tokens` and
`cost_usd` -- this rail is the only one whose CLI states a per-run cost.

### The provider chain (`brain_v42.agents.{lines,prompt,phase,chain,run_phase_chain}`)

Since 2026-09-15 the chain has four links and the intended order is
`BRAIN_DREAM_AGENT_PROVIDERS=codex,opencode,agy,claude`: opencode sits between codex and agy so a
night that loses the ChatGPT quota falls onto the Go subscription before the Google one. The
opencode link reads `BRAIN_DREAM_OPENCODE_{FAST,DEEP}_MODEL` and `_{FAST,DEEP}_VARIANT` (the
`--variant`, opencode's reasoning effort) with `dream.sh` owning the defaults -- one 60 $-cap Go
model for both tiers, by weekly-quota arithmetic -- and `BRAIN_DREAM_OPENCODE_BIN`, which the
drop-in must name because `~/.opencode/bin` is not on the unit's `PATH`. Its preflight drops the
link before the night when the host lacks an `opencode-go` credential or
`~/.config/opencode/node_modules`; unlike agy it does not require capability enforcement, since
its tool wall is rendered on every run. `brain_v42.metrics.opencode_dream_parser` persists its
`dream_runs` row, `cost_usd` included.

Lot 2 of the agent runtime extraction (Brain ticket `afd56820`, 2026-09) moves `scripts/dream.sh`'s
`run_phase` and `run_phase_chain` -- one phase against one provider, and the loop that falls back
across the configured provider chain -- to Python, byte-identical to what the bash produced (see
`tests/unit/agents/test_chain_golden.py` and its fixtures under
`tests/fixtures/agents_chain_golden/`, captured from `scripts/dream.sh` before deletion).

```
src/brain_v42/agents/
    lines.py             # every log-line template (START/DONE/FAIL/FALLBACK/...), byte-identical
    prompt.py            # render()/render_file() -- moved from scripts/dream/_render_prompt.py
    phase.py             # PHASE_DEPS, PhasePaths, runner/parser/otel argv, run_phase
    chain.py             # run_chain -- the Dream's FALLBACK lines over headless_agents.chain
    run_phase_chain.py   # `python -m brain_v42.agents.run_phase_chain` CLI
```

`phase.run_phase` reproduces bash's `run_phase` exactly: model/reasoning-tier selection from the
`BRAIN_DREAM_{CODEX,AGY}_{FAST,DEEP}_{MODEL,REASONING}` environment variables (no defaults --
dream.sh keeps those, exporting them before it calls the CLI) and the `BRAIN_DREAM_{CODEX,AGY,
CLAUDE}_BIN` executables; the SKIP-on-missing-prompt and unsupported-tier short circuits;
`effective_dry_run` (the `dream_wants_wet` semantics for the REORG override, including its
KILLSWITCH log line); dependency-report injection with the same header text; and the runner,
`otel_split` and parser subprocesses, run as `[interpreter, "-m", module, *argv]` with the prompt
on stdin -- no shell. `phase.spawn` is the single seam that launches all three subprocess kinds;
`phase._python_executable()` resolves the interpreter from `BRAIN_AGENTS_SUBPROCESS_PYTHON` (a
test-only seam, unset in production) falling back to `sys.executable`. Every log line is written
through a `log: Callable[[str], None]` the caller supplies -- `phase.make_logger()` builds the
production one (`[HH:MM:SS]` prefix, prints, appends to the night's main log), matching bash's
`log() { echo ... | tee -a ...; }`.

`chain.run_chain` keeps the signature `run_phase_chain` calls and the `FALLBACK`/`FALLBACK-END`
lines exactly as bash wrote them; the state machine itself is `headless_agents.chain.run_chain`,
which advances to the next provider only on the fallback exit code
(`PROVIDER_FALLBACK_EXIT_CODE`, "failed and proved no tool call succeeded"), reports through
`on_fallback`/`on_exhausted` callbacks, and returns a frozen `ChainResult(provider, rc,
fallbacks)`.

`run_phase_chain.py`'s CLI is the seam `dream.sh` now calls once per phase (the normal call and its
RETRY, unchanged): it builds `PhasePaths` and a logger, runs the configured provider chain via
`run_chain(providers, run_one=lambda p: phase.run_phase(p, ...), ...)`, and writes
`{"provider", "rc", "status", "fallbacks"}` to `--result-json`. `dream.sh`'s
`_run_phase_chain_python` helper exports the model/executable/PROMOTE JSON environment variables,
runs the CLI, takes its exit code as `phase_rc` (identical contract to the old bash `run_phase_chain`
return value), and reads `fallbacks` out of the result JSON with `jq` to append
`$PROJECT_KEY/$name` to `FALLBACK_PHASES` -- the one piece of state the shell still aggregates for
its closing summary.

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
   `--dry-run`, because both publish the complete managed set. Normal install keeps the state of a
   timer that already exists, but arms a timer it installs for the first time, and every timer under
   `--enable-timers`.
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

`StatusEngine` advances feature status monotonically
(planned → research → design → building → deployed → done) based on artifact types.

## GitNexus integration

Side-by-side, not merged. GitNexus is a second MCP stdio server exposing `gitnexus_*` tools (code graph: AST + call chains + impact analysis). Isolation invariants: disjoint MCP surfaces (`brain_*` vs `gitnexus_*`), disjoint data (`.gitnexus/` vs PG+Neo4j), disjoint compute (transformers.js CPU vs GPU embed :8003). Nightly reindex at 04:30 finishes before the Dream timer's 06:00 window (plus up to 120 seconds of jitter).

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
METRICS_SLOW_BLOCK_CACHE_TTL_SECONDS=30                               # dream/nightly/graph-inventory memo
METRICS_SLOW_BLOCK_CACHE_ERROR_TTL_SECONDS=5                          # short TTL for a raised exception
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

`MCP_HTTP_TOKEN` and `MCP_HTTP_DREAM_TOKENS` never belong in the shared `.env`.
HTTP startup refuses an empty or blank token unless the development-only
`MCP_HTTP_ALLOW_UNAUTHENTICATED=true` opt-out is set. The opt-out is refused with a
non-empty token or under capability enforcement. The production systemd path requires a
non-empty `MCP_HTTP_TOKEN` in `~/.config/brain-v42/mcp-token.env` in mode `0600`.
`MCP_HTTP_MAX_BODY_BYTES` defaults to 2,097,152 bytes; middleware refuses oversized
bodies before the MCP request handler. Tool input bounds count characters.

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
│       └── tools/                # 78 always-on + 2 graph-gated = 80
├── alembic/versions/             # migrations 001–062 defined in the repository
├── scripts/                      # legacy import + projection inventory/recovery CLIs
├── tests/                        # unit/ + integration/
├── docs/
│   ├── ARCHITECTURE.md           # this file
│   ├── GRAPH_LEDGER_RUNBOOK.md   # gated import, rebuild, observability, rollback
│   ├── MCP_TOOLS.md              # tool catalog
│   └── SCHEMA.md                 # PG schema reference
└── scripts/dream.sh              # nightly dream orchestrator
```

## datalake_v2 -> brain_v42 (what changed)

| Aspect | datalake_v2 (pre-2026-02) | brain_v42 (current repository) |
|--------|---------------------------|------------------------|
| MCP application topology | API + MCP bridge inside a 7-container chain | 1 shared FastMCP HTTP process; stdio dev/fallback |
| Network hops per tool call | 4 (HTTP chain) | 0 for tool logic; 1 HTTP hop for MCP protocol (loopback) |
| Source of truth | Neo4j (CRUD + graph + vectors) | Postgres + pgvector |
| Graph | Neo4j Cypher reduce() for similarity | Neo4j relationship index only, pgvector for similarity |
| Embeddings | sentence-transformers / PyTorch in-process | Configurable (`BRAIN_EMBEDDING_BACKEND`): shipped default `shim` → local :8003 (Qodo-Embed-1-1.5B, 1536d, `qodo` profile); production deployment: OpenAI-compatible backend on Mistral codestral |
| Reranker | none | Cross-encoder :8003 unified endpoint, BatchingRerankerClient (20 ms window) |
| MCP transport | stdio | HTTP loopback 127.0.0.1:8765 + HostOriginGuard + bearer obligatoire sous systemd (optionnel en HTTP dev direct) |
| MCP tools | 21 | 78 always-on + 2 graph-gated = 80 |
| Tables | 6 | 31 (knowledge, audit, plans, dream, webhook, coordination, sessions, graph ledger) |
| Maintenance | manual | Dream mode nightly + DecayFlusher + ConsolidationJob |
| Observability | none | /metrics :9200 + /api/cockpit + process_metrics |

## Planned deployment

The planned production direction is a dedicated server operated by the red-rail
release rail as one Compose project: PostgreSQL, Neo4j, the MCP server, metrics
sidecar, delivery observer, a one-shot migration service that proves the recovery
contract, and Compose-scheduled work. Clients will reach MCP over a private
WireGuard network with bearer authentication. Each client service is planned to
have its own credential with scoped read, write, delivery, or admin rights; admin
access is intended to require time-boxed operator elevation. Delivery attestation
issuers are planned to be proven against the credential in attestation API v1.1.
After deployment work, planned priorities are search and embedding quality,
disaster-recovery proof, and metrics. Dream can be suspended by the operator and
re-armed later.

## References

- `docs/SCHEMA.md` — PG schema column-level
- `docs/MCP_TOOLS.md` — tool catalog
- `docs/GRAPH_LEDGER_RUNBOOK.md` — import, cutover, rebuild, observability, and rollback gates
