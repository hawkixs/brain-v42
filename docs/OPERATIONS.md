# Operations

Deep operational reference for running brain-v42 in production: the full session
lifecycle contract, the detailed network trust boundary, migration history, the graph
ledger cutover evidence, and the private secret files an operator manages outside the
shared `.env`. [`README.md`](../README.md) covers the short version of most of this;
this document exists so the short version doesn't have to carry everything.

## Session lifecycle (full v4 contract)

Only an explicit user command may start, capture, heartbeat, list, resume, end, or abandon a session on the agent and client side. Hooks and agents never infer a boundary or close a stale session. The only server-side exception is the Dream `sweep` phase, shipped disabled and dry, which abandons an open session with no heartbeat for seven days (`abandonment_reason = 'auto_stale_7d'`) without touching project focus. It ships behind `BRAIN_DREAM_SWEEP_ENABLED=false` and `BRAIN_DREAM_SWEEP_DRY_RUN=true`. Staleness is a list filter over open rows; it never changes the persisted `status` and never auto-closes a session. Do not confuse this 24-hour display flag with the separate seven-day server-side sweep, which is the only mechanism that moves an open session to `abandoned` without an explicit command (`abandonment_reason = 'auto_stale_7d'`).

`brain_session_start(project_key, client_key)` creates a persistent session identified
by a UUID. `client_key` names a session the client wants: reuse the exact same key for
every retry of that session, and give a distinct, stable key to every parallel
session. The same `(project_key, client_key)` pair replays the opening of a still-open
session, while a new key creates a concurrent one. The presence of other open sessions
never refuses a start; the result exposes `open_session_count`.

`resume`, `capture`, `heartbeat`, `end` and `abandon` all require the
`(session_id, expected_client_key)` pair. The server refuses any pair that doesn't
match the session before mutating it: keeping both values together prevents a valid
but wrong UUID from acting on a different parallel session. This guard isolates
targeting mistakes; it is not an authentication mechanism.

`brain_session_capture` records the durable artifacts a session produced before it
ends. Each call accepts 1 to 100 unique UUIDs, capped at 100 artifacts per session.
The server checks they exist, belong to the same project, and were created after the
session started. The ledger is exclusive: a UUID already attributed to another session
is refused. This provenance is client-declared, not cryptographic proof of authorship.

Every result that exposes a session also carries `attributed_knowledge_ids`, a view
rehydrated from the ledger. Captures stay visible after a start retry, a resume, a
heartbeat, a list, or an abandon. Abandoning a session does not release its
attributions: their provenance stays exclusive, and an exact retry of `capture` stays
idempotent even after abandonment.

`brain_session_end` no longer accepts capture identifiers directly: it reads this
ledger and stays fail-closed. The session must have either at least one captured
artifact or a non-empty `nothing_to_capture_reason`, never both. An identity, capture,
or provenance error leaves the session open.

Closing then attempts a compare-and-swap of the focus with
`expected_focus_revision`. If the revision matches, the focus updates and the
persisted result is `focus_outcome="applied"`. Under concurrency, the shared focus
stays unchanged but the session still closes with `focus_outcome="conflict"`;
`next_focus` stays that session's proposal, and `focus_at_end` / `focus_revision_at_end`
freeze what was actually observed. A focus conflict therefore never needs replaying an
already-valid close.

Focus and its revision are shared per project. Any successful composite mutation via
`brain_update_project_focus` consumes the revision, and a close that applies its focus
advances it too. Other open sessions' snapshots then go stale without preventing their
own closure or closing them.

`brain_session_heartbeat` refreshes `last_heartbeat_at` without changing focus or
state. After 24 hours without a heartbeat, an open session exposes `is_stale=true`;
this marker is derived, its persisted status stays `open`, and only the 7-day
server-side sweep ever abandons a session without an explicit command.
`brain_session_list` accepts `open` (default), `stale`, `ended`, `abandoned` and
`all`, with `limit` between 1 and 100 and a non-negative `offset`. The `stale` filter
returns only open sessions marked stale. `brain_session_resume` only resumes an open
session and returns the briefing, current focus and its revision.
`brain_session_abandon` requires a reason and abandons the session without modifying
project focus.

If assembling the full briefing fails after a start, the tool still returns the
persisted session's UUID with a briefing marked unavailable; a briefing failure
therefore never creates an open session invisible to the client.

This v4 contract has been live on the production Brain since 24 July 2026, after
sequential application of migrations 036 then 037, explicit proof of
`alembic current=037`, and restarting the MCP service last. The 037→036 downgrade
refuses any capture not reflected by a terminal snapshot and any focus-conflict close,
because v3 cannot represent either without loss.

## Network trust boundary (detailed)

**Tracked network boundary**: MCP, PostgreSQL and Neo4j bind to loopback; metrics and automation default to loopback. The versioned Compose target now binds the embedding host publish to loopback, but the live runtime remains LAN-wide until an authorized rollout. Application bearer authentication, a dedicated Docker client network and repository-managed WAN isolation are not yet proven.

**Embedding shim limits (versioned, not yet rolled out)**: 8 MiB body, 5 s body-read timeout, 8 concurrent ingress reads, 100 embed texts, 128 rerank candidates, maximum JSON depth 64, one embedding calculation and one rerank calculation per worker. Saturation returns short `503` JSON with `Retry-After: 1`.

**SEC2 residuals**: bearer authentication and a dedicated Docker client network require the coordinated `auto-discord` cutover; the versioned legacy PyTorch profile remains unbounded and does not preserve the `auto-discord` DNS alias.

## Private secret files

Never place `MCP_HTTP_TOKEN` or `MCP_HTTP_DREAM_TOKENS` in the shared `.env`. The
production systemd path requires `~/.config/brain-v42/mcp-token.env`, a regular file
owned by the service user, mode `0600`, with a non-empty `MCP_HTTP_TOKEN`. Phase
bearers stay optional while the Dream firewall is disabled. That private file accepts
only `MCP_HTTP_TOKEN`, `MCP_HTTP_DREAM_TOKENS` and
`BRAIN_DREAM_CAPABILITY_ENFORCEMENT`.

The `NEO4J_*` keys in the shared `.env` example belong to the legacy path and are
absent from the active production runtime. For any other canonical cutover, remove
them from the shared `.env`, rotate the Neo4j credential, and install the new secret
only in `~/.config/brain-v42/graph-projector.env`, from
`deploy/systemd/graph-projector.env.example`:

```dotenv
GRAPH_PROJECTOR_ENABLED=true
GRAPH_PROJECTOR_NEO4J_URL=bolt://127.0.0.1:7687
GRAPH_PROJECTOR_NEO4J_USER=neo4j
GRAPH_PROJECTOR_NEO4J_PASSWORD=REPLACE_WITH_ROTATED_PASSWORD
```

Reserve this private file for the four `GRAPH_PROJECTOR_*` variables. It must be a
regular file, not a symlink, owned by the service user, mode exactly `0600`. Its URI
must be a Bolt/Neo4j URI without credentials, query, fragment or path. Do not preload
this example live while `GRAPH_LEDGER_WRITE_ENABLED=false`:
`GRAPH_PROJECTOR_ENABLED=true` requires the ledger active. When the shared ledger flag
is true, MCP systemd startup runs a preflight that checks the private file's shape and
rejects legacy keys in it; it proves neither the revocation of the previous
credential, nor writer quiescence, nor the absence of Neo4j sessions.

## Migration history

The repository migration target is 046. No page in this repository proves a live
schema head — measure it, never read it here.

Migration 038 adds the terminal audit trail for Dream EXTRACT attempts. Migration 039
isolates the `project_contexts` timestamp trigger. Migration 040 adds
`project_contexts.focus_updated_at`, written by application code, never by a trigger,
deliberately not backfilled. Migration 041 separates corpus provenance from content:
it adds `access_log.actor`, `access_count_human` on the six decay-tracked tables, and
`content_updated_at` on the five knowledge tables, the latter written by a
value-conditional trigger, also not backfilled. Migration 042 adds
`dream_runs.project_key` — nullable, no backfill, so the column can land in production
before any reader of it does. Migration 043 dates the freshness status
(`freshness_status_updated_at` + `freshness_source`) on the six decay-tracked tables:
the hard prerequisite of any purge, since without it `updated_at` restarts on every
counter write and no honest archive-residence clock exists; it is written by a
conditional trigger, because `freshness_status` has four writers, one of them a prompt
going through the generic `brain_update`. Migration 044 adds
`last_accessed_at_human`: migration 041 had given the six decay-tracked tables a human
access counter, fixing a 0.2-weight term, but left the 0.3-weight recency term reading
a counter contaminated by machine reads. Both signals now switch together, behind
`decay_human_signal_enabled`, which ships closed. Migration 046 gives sessions their
identity — `connection_id` with a PARTIAL unique index (`WHERE status = 'open'`),
`started_by_actor`, `intent`, `nature` — and declares a fourth terminal state,
`closed_inactive`, in the two CHECK constraints. **Migration 046 changes no behaviour:**
it adds the schema those columns need and nothing writes them yet. All five columns are
nullable and none is backfilled: `NULL` means "before 046". Migration 045 widens
`dream_runs.model` to `varchar(120)`: two of the five configured phase models did not
fit in 30 characters, and an overflow loses the whole row rather than the column
(the INSERT being best-effort).

The 038→039 cutover was conducted with
[`docs/PLAN_INDEX_REPAIR_RUNBOOK.md`](PLAN_INDEX_REPAIR_RUNBOOK.md) (isolated restore,
migration, repair, restart-last gates).

## Graph ledger cutover evidence

Migration 033 adds the relational ledger and outbox. Migration 034 adds projector
fencing v2 with PostgreSQL generations and claims plus Neo4j fence and cursor checks.
Migration 035 adds a crash-safe, resumable interlock for offline projection recovery.
The canonical path has been active in production since 22 July 2026 with
`GRAPH_LEDGER_WRITE_ENABLED=true`; fresh or unproved environments stay fail-closed at
`false`. The [graph ledger runbook](GRAPH_LEDGER_RUNBOOK.md) carries the live
evidence.

Migrations 033-035 deliver the canonical ledger, normal-runtime fencing and the
projection recovery interlock. A stale worker cannot mutate Neo4j or acknowledge its
claim after a successor barrier commits. Recovery 035 can resume the same explicit
recovery UUID after a crash at a PostgreSQL or Neo4j commit boundary.

The recovery CLI implements Option A, "PostgreSQL canonical + rebuild-on-doubt". It
requires five explicit offline confirmations: stopped writers, revoked legacy
credentials, zero Neo4j sessions, a dedicated Neo4j database, and a tested PostgreSQL
restore at the exact deployed head. Its reset is bounded to the Brain projection
labels and `BrainProjectionCursor`; Neo4j is a disposable projection, so no Neo4j
backup or correlated restore is a cutover gate.

Repository code alone does not authorize writer activation. Keep
`GRAPH_LEDGER_WRITE_ENABLED=false` outside an explicitly authorized offline window.
During that window, follow the rotation and recovery sequence with all application
writers and normal projectors stopped; reopen no writer until the runbook's gates are
closed and reviewed.

## Dream capability firewall

The capability firewall's protections are shipped but stay inactive with
`BRAIN_DREAM_CAPABILITY_ENFORCEMENT=false`. The production HTTP transport and the
STDIO fallback keep their historical contracts while this boundary stays inactive.
The administrator bearer `MCP_HTTP_TOKEN` stays global. Activation requires a separate
operator rollout and a complete `MCP_HTTP_DREAM_TOKENS` registry for the six phases of
each project. Each profile carries one `active` bearer sent to the runner, and zero or
more `accepted` bearers reserved for rotation.

When the firewall is active, the server confines each Dream principal to its phase
then its project for reads, writes, aggregates, search and graph operations. It
refuses compact gateways and out-of-scope calls before the handler runs. The
administrator stays global. The runner forwards only the `active` bearer, in a
allow-listed child environment, and strips the full registry. `BRAIN_CODE_MODE=true`
is incompatible with it.

## Automation service

The `brain-v42-automation.service` unit is generated and verified, but stays dormant.
It listens on `AUTOMATION_PORT` (default 9201), loopback-only. The cutover without
dual-run, lease proof and rollback are described in the
[systemd runbook](../deploy/systemd/README.md). Deploying the code with
`METRICS_LEGACY_AUTOMATION_ENABLED=true` does not change the live automation owner.

## Codex gateway

The `red-codex` administration gateway deploys only on the private Docker network,
with no host port and no systemd unit; follow the
[Codex gateway runbook](../deploy/CODEX_GATEWAY.md). Its live activation stays
blocked while the PostgreSQL `codex_ro` and `brain` credentials use their development
defaults, or while `/ready` doesn't validate the SQL contract, including the
`security_barrier` of the seven views scoped to the `red` group.
