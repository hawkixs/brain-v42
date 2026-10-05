# Operations

Deep operational reference for running brain-v42 in production: the full session
lifecycle contract, the detailed network trust boundary, migration history, the graph
ledger cutover evidence, and the private secret files an operator manages outside the
shared `.env`. [`README.md`](../README.md) covers the short version of most of this;
this document exists so the short version doesn't have to carry everything.

## Session lifecycle (full v4 contract)

Only an explicit user command may start, capture, heartbeat, list, resume, bind, relay, end, or abandon a session on the agent and client side. Hooks and agents never infer a boundary or close a stale session. **Amendment — slot relay (ADR #34).** A guard mod that the operator has explicitly enabled counts as a standing user command for one gesture only: `brain_session_relay` of an open operator session, onto its focus slot when it is bound to one and onto the project base when it is not — capture, end and start of its successor under a new `client_key`, in one transaction. The model makes the call and chooses its captures, summary and handover; the mod only triggers the turn and replays the result at compaction. The base form writes the handover as the project's whole base focus under a compare-and-swap on its revision, and refuses a handover under 70% of the current base (`base_focus_shrink`): only an operator relay may override that guard, never the mod. The standing command covers nothing else: not `brain_session_abandon`, not `brain_session_end` of any session, not the relay of a server-opened trace, not any write of the project base outside that relay, not opening, closing or changing a slot, and not `start`, `resume` or `bind` outside the relay. It is void while `BRAIN_SESSION_RELAY_GUARD_MOD_ENABLED` is false. Hooks still never capture, close or commit on their own. Cross-cutting work without a ticket needs no anchor: left unbound, it is relayed onto the project base, under the same guards. The only server-side exception is the Dream `sweep` phase, shipped disabled and dry, which abandons an open session with no heartbeat for seven days (`abandonment_reason = 'auto_stale_7d'`) without touching project focus. It ships behind `BRAIN_DREAM_SWEEP_ENABLED=false` and `BRAIN_DREAM_SWEEP_DRY_RUN=true`. Staleness is a list filter over open rows; it never changes the persisted `status` and never auto-closes a session. Do not confuse this 24-hour display flag with the separate seven-day server-side sweep, which is the only mechanism that moves an open session to `abandoned` without an explicit command (`abandonment_reason = 'auto_stale_7d'`).

**Two natures.** Since migration 046, `brain_sessions.nature` separates two kinds of
row. An *operator* session is the one a user opens with `brain_session_start` and
drives with the explicit commands below. An *agent* trace is a row the server
opens for itself, one per HTTP MCP connection, so that the artifacts created on that
connection have somewhere to be attributed. The explicit-command rule governs the operator nature; the agent nature is a server-owned trace that grants no right to an agent, a hook or a client.
No code path writes `nature = 'operator'` today: explicit sessions are persisted with
`nature IS NULL`, and every rule that separates the two natures reads "not `agent`"
(`nature IS NULL OR nature <> 'agent'`) as operator. Agent traces, their arming flags
and the inactivity sweep are described in
[Agent traces and the inactivity sweep](#agent-traces-and-the-inactivity-sweep).

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
ledger. Since migration 047 a closure no longer needs a non-empty ledger or a
`nothing_to_capture_reason` to the exclusion of the other: the ledger may be empty or
filled, by explicit or derived capture, with or without a reason, and a reason, once
given, must not be blank. An identity, capture, or provenance error still leaves the
session open.

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

### Agent traces and the inactivity sweep

Three server paths act on the agent nature. Each ships closed in code
(`src/brain_v42/config.py`) and is armed by an operator gesture, outside the
repository.

**Auto-opening** (`src/brain_v42/mcp/session_autoopen.py`, flag
`BRAIN_SESSION_AUTO_OPEN_ENABLED`). Over HTTP, the outermost tool call of a
connection opens an agent trace keyed by `(project_key, connection_id)`, where
`connection_id` is the `Mcp-Session-Id` the server minted for that connection: the
only identifier of the call that the client does not declare itself. The project
comes from the caller's actor, and the row gets a generated `client_key`
(`auto:<hex>`). A call with no connection identifier, no resolvable actor or no
project context opens nothing. Under stdio there is no connection identifier, so no
trace is ever opened: this is the contract, not a degraded mode, and falling back on
the declared actor was rejected. Stateless HTTP (`mcp_http_stateless=true`) mints no
identifier either. The opening is synchronous and runs before the tool, so the
artifact the call creates falls inside the trace's capture window; it is also
fail-open, so a failed opening is logged as `session_autoopen.failed` and the call
proceeds. The partial unique index `uq_brain_sessions_connection`
(`WHERE status = 'open'`) keeps at most one open trace per connection, and each later
call stamps `last_observed_at`, the server's observation clock. That clock is
distinct from `last_heartbeat_at`, which only the explicit commands refresh on an
operator session. An agent trace never carries a `summary`, a `next_focus` or a focus
outcome, and `connection_id` is never set on an operator row.

**Derived capture** (`src/brain_v42/db/session_derived_capture.py`, flag
`BRAIN_SESSION_DERIVED_CAPTURE_ENABLED`, migrations 047, 048 and 061). An artifact created on
a connection is deposited in that connection's trace at creation time. An operator
session absorbs what traces hold on `start`, `resume`, `capture`, `heartbeat`, `end` and `relay`, in two
stages, and only artifacts of the same project created at or after the session started,
which is what an explicit capture would accept. Absorption is attempted only on a call
that carries a connection identifier, so never under stdio or in stateless HTTP mode.
The connection stage records each connection seen by one of those lifecycle calls
in `brain_session_connections` and takes artifacts from the project's `open` or
`closed_inactive` traces on every recorded connection, bounded by the session's start.
That start excludes artifacts produced by a predecessor on the same connection; the
exact match works even when a coordinating session makes every instant ambiguous.
A connection that saw no lifecycle call still falls back to the window stage.
The window stage then runs whenever ledger capacity
remains (100 artifacts per session, minus those already attributed), whether or not the
current connection still has a trace. Its donors are the project's `open` or
`closed_inactive` traces opened by a human actor, never an abandoned trace and never one
opened by a system actor such as the Dream. It takes an artifact only if no other
session of the project that is not an agent trace covered the instant of its creation,
whether that session is still open or has finished since; under that ambiguity the
artifact stays with the trace. The server never promotes a trace into an operator
session. Migration 047 removed the "non-empty ledger XOR `nothing_to_capture_reason`"
constraint on `ended`, because a ledger can now be filled without an explicit capture,
and migration 048 records in `attribution_mode` which key attributed each artifact
(`explicit`, `derived_deposit`, `derived_connection` or `derived_window`).

**Inactivity sweep** (`src/brain_v42/maintenance/session_sweep.py`, flag
`BRAIN_SESSION_INACTIVE_SWEEP_ENABLED`). The same Dream `sweep` phase carries a second, narrower rule: its predicate selects only open `nature = 'agent'` traces whose `last_observed_at` is more than four hours old and moves them to `closed_inactive`.
The CHECK on `brain_sessions` fixes the terminal fields of `closed_inactive` (`next_focus IS NULL`, no summary, no abandonment reason) and, since migration 060, refuses it for every operator row, whether its `nature` is `operator` or `NULL` (ticket `16314b31`, closed).
Four hours is an eligibility threshold evaluated once a night, not a closing delay:
a trace that goes idle just after a pass waits for the next one, about 28 hours in
the worst case. Both rules run in one statement, and the seven-day rule wins when
both match, so a trace silent for more than seven days is abandoned with
`auto_stale_7d` rather than marked inactive. A trace never observed
(`last_observed_at IS NULL`) is never eligible, and an operator row is out of reach of
the predicate, whether its `nature` is `NULL` or `operator`. Both outcomes leave the
trace's capture ledger in place and the phase touches neither `project_contexts` nor
the focus. What differs is donor eligibility for derived capture: an abandoned trace
is no longer a donor, while a `closed_inactive` one stays absorbable, so artifacts
left in it can still reach an operator session. `closed_inactive` carries no
`abandonment_reason`.
Each run writes its count to `dream_runs.closed_inactive_count` (migration 049),
apart from abandonments. The four-hour rule writes only when the phase itself runs
wet (`BRAIN_DREAM_SWEEP_ENABLED=true` and `BRAIN_DREAM_SWEEP_DRY_RUN=false`).

**Closing by the server** (`src/brain_v42/mcp/server.py`,
`src/brain_v42/services/agent_trace_net.py`, ticket `09d2b56e`). The server that opened an agent trace also closes it: when the transport of its connection terminates (a client `DELETE`, the idle eviction, the server shutdown), and through a net that moves to `closed_inactive` every open `nature = 'agent'` trace whose `last_observed_at` is more than four hours old, every fifteen minutes.
The transport path reaches every connection that ends cleanly; the net catches the
ones that never do (a crashed client, a killed server), with its first pass at
startup. Both write exactly what the four-hour rule writes, `closed_inactive` with no
reason and the capture ledger kept, so a closed trace stays a donor for derived
capture. Neither has a seven-day branch and neither can reach an operator row. Both
run wherever auto-opening runs (stateful HTTP, `BRAIN_SESSION_AUTO_OPEN_ENABLED`) and
have no flag of their own; the Dream rule above then only meets what they have not
closed yet. A failed close is logged (`session_autoopen.close_failed`,
`agent_trace_net.sweep_failed`) and never holds the connection or the shutdown.

**Arming state is measured, not documented.** This document deliberately does not
state whether the four flags are armed: drop-ins change, and a copied value cannot
tell it has aged. Measure each flag in the process that reads it. Auto-opening,
derived capture and the `guard_mod` relay flag (`BRAIN_SESSION_RELAY_GUARD_MOD_ENABLED`)
are read by the live MCP server; the inactivity sweep is read by the
Dream unit (`python -m brain_v42.maintenance.session_sweep`), and a copy of
`BRAIN_SESSION_INACTIVE_SWEEP_ENABLED` in the MCP server's environment arms nothing.
These commands print only the exact flag keys listed below, and only when the value
is `true` or `false`: whatever the other variables hold, nothing else can come out.
Never dump the full environment, which carries `MCP_HTTP_TOKEN`, and do not replace
these commands with a `tr ... | grep` pipeline. Splitting an environment on NUL or on
spaces loses the record boundaries, so a variable whose value embeds a newline, or a
space followed by a flag-shaped token, forges a matching line and prints material
around it.

```bash
# Run in bash. Whole-record match against an exact KEY=(true|false) whitelist.
shopt -s extglob
flags_of() {  # $1 = pid, $2 = extglob pattern of the exact records to print
  local record
  while IFS= read -r -d '' record; do
    case $record in $2) printf '%s\n' "$record" ;; esac
  done < "/proc/$1/environ"
}

# MCP server (auto-open, derived capture, the `guard_mod` relay flag): the live process environment
flags_of "$(systemctl --user show brain-mcp-http.service -p MainPID --value)" \
  'BRAIN_SESSION_@(AUTO_OPEN|DERIVED_CAPTURE|INACTIVE_SWEEP|RELAY_GUARD_MOD)_ENABLED=@(true|false)'

# Dream unit, what the unit declares: Environment= lines, drop-ins included.
# One grep per key, with token boundaries: only these exact tokens can come out.
for key in BRAIN_SESSION_INACTIVE_SWEEP_ENABLED BRAIN_DREAM_SWEEP_ENABLED BRAIN_DREAM_SWEEP_DRY_RUN; do
  systemctl --user show brain-v42-dream.service -p Environment --value |
    grep -oE "(^| )$key=(true|false)( |\$)"
done | tr -d ' '

# Dream unit, what the process sees: only while a night is running
# (otherwise it prints nothing and exits 1)
pid=$(systemctl --user show brain-v42-dream.service -p MainPID --value)
[ "${pid:-0}" -gt 0 ] && flags_of "$pid" \
  '@(BRAIN_SESSION_INACTIVE_SWEEP_ENABLED|BRAIN_DREAM_SWEEP_ENABLED|BRAIN_DREAM_SWEEP_DRY_RUN)=@(true|false)'
```

A flag that is set but not printed has a non-boolean value, or is absent: inspect that
one by hand, without echoing the rest of the environment. The unit-level command
works on one text line, so it can repeat a flag-shaped token that sits inside another
variable's quoted value; it can print a flag value that way, never anything else. The
process-environment form matches whole records and has no such case.

`systemctl show -p Environment` lists the `Environment=` lines only. A value set
through `EnvironmentFile=` does not appear there (`-p EnvironmentFiles` lists the
files by path, not their content), so that command shows what the unit declares, not
what the process receives. The process environment is the measurement, and the Dream
unit has a process only while a night runs. Do not open the private environment files
to settle the question.

pydantic-settings also reads `.env` from the working directory, below the process
environment. If a flag is absent from the environment, check that file by key name
only. Volume is measured the same way: per night in
`dream_runs.closed_inactive_count` (`phase = 'sweep'`), and by grouping
`brain_sessions` on `nature` and `status`.

For context: on 2026-09-06 brain learning `823c686d` measured all three flags armed
in the live server, while the code comments and the project prose still described
them as shipped dormant. ADR #29 (`12f17bd2`) made reading the live process
environment the method for any flag state, and confirmed the covenant of ADR #24
(`f24ba872`): the server never writes a `summary`, never chooses a `next_focus`, and
closes an operator session only through the seven-day rule that opens this section.
Ticket `09d2b56e` measured the trace volume on 2026-09-22, over the six days before
it: about 1,400 agent traces opened per day, almost none closed. These are dated
observations, not the current state.

## Focus slots

Focus slots (ADR #34) carry the topics in flight; the tools are described in
[MCP tools](MCP_TOOLS.md#brain_slot_open). Two operator-visible rules:

- **A slot's anchors must belong to the slot's project (spec D4).** Opening a slot of project
  `red` on a ticket addressed to `brain-v42` is refused with `anchor_ticket_foreign`, and so is
  an unknown ticket id. Anchor on a ticket addressed to the slot's own project, on a lot planned
  by one of its tickets, or on a PR bound to one of them. When a cross-project ticket is the real
  subject, open the slot in the project that owns the ticket.
- **A receipt and an open cannot cross.** `brain_slot_open` takes a key-share lock on the tickets
  whose receipts could complete its anchors before it reads those receipts, and the receipt
  writers lock the same rows exclusively first. An open that meets a receipt being written waits
  for it, then refuses with `anchor_already_received`; it never leaves an open slot with
  `receipt_pending=true`. A slot found in that state came from a missed hook call: close it with
  `brain_slot_close`.

## Network trust boundary (detailed)

**Tracked network boundary** (replayed 2026-08-23): MCP, PostgreSQL and Neo4j bind to loopback; metrics and automation default to loopback. The versioned Compose target binds the embedding host publish to loopback and the live runtime matches it — measured `127.0.0.1:8003`, with the host's own LAN address refusing the connection. Application bearer authentication is armed and enforcing: `MCP_HTTP_TOKEN` is set and non-empty in the live server process, and `POST /mcp` answers `401` both without a bearer and with a wrong one. The dedicated Docker client network exists and carries the clients: `brain-net` holds the embedding shim and both `auto-discord` containers. Repository-managed WAN isolation remains unproven — the repository manages no firewall rule at all. What would make this paragraph false again, and is watched by no test: a host-publish override reopening `:8003`, or `MCP_HTTP_TOKEN` cleared. `METRICS_HOST` has LEFT that list: since 2026-09-03 (`6c61b63`) a fail-closed validator refuses a non-loopback bind unless `METRICS_ALLOW_NON_LOOPBACK` names the decision, and under that opt-in the three POST receivers stay unregistered and say so on `/healthz`. Re-measure with `ss -ltnp`, `docker port` and an unauthenticated `POST /mcp` — do not copy this line forward.

**Embedding shim limits (ROLLED OUT 2026-08-21, temps 1)**: 8 MiB body, 5 s body-read timeout, 8 concurrent ingress reads, 100 embed texts, 128 rerank candidates, maximum JSON depth 64, one embedding calculation and one rerank calculation per worker. Saturation returns short `503` JSON with `Retry-After: 1`.

**SEC2 residuals** (replayed 2026-08-23): bearer authentication and the dedicated Docker client network are done — the coordinated `auto-discord` cutover happened, and both `auto-discord` containers sit on `brain-net`. One residual stands, and it is wider than previously written: the versioned legacy PyTorch profile remains unbounded — `services/embedding/main.py` carries no body cap, no read deadline, no concurrency semaphore and no `413`/`503` — and it preserves neither of the two DNS names its clients use. A `--profile legacy` rollback publishes `embedding` and `brain_v42_embedding` on `brain-net`, while the compose sets `EMBEDDING_URL=http://embedding-shim:8003` and the running bot, carrying no `EMBEDDING_URL` of its own, falls back to the code default `http://brain_v42_embedding_shim:8003`. Two names break, not one.

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

### Embedding shim static bearer

The shim reads its bearer from a file, never from a variable: `docker inspect`
prints `Config.Env` verbatim, so a token wired as a value would be readable by
anyone who can reach the daemon. Compose passes only the path.

```dotenv
BRAIN_SHIM_BEARER_FILE=~/.config/brain-v42/embedding-shim-bearer
```

Generate it without letting the value reach a terminal, an argument list or the
shell history — a redirection under a tight `umask`, never `echo`:

```bash
( umask 077; openssl rand -hex 32 > ~/.config/brain-v42/embedding-shim-bearer )
```

Mode `0600` is a hard precondition, not hygiene. Compose bind-mounts a file
secret **as-is**: the mode the container sees is the mode on the host, and
`load_bearer_token` refuses anything readable beyond its owner, anything under
32 bytes, and anything still carrying a `REPLACE_` placeholder. A loose secret
does not degrade the guard, it stops the container from starting. Unlike the
Neo4j secret, whose override is exported ad hoc at cutover time, this path lives
in the shared `.env` so an ordinary `docker compose up -d` resolves it; the
versioned default (`./.secrets/embedding-shim-bearer`) is a fallback that does
not exist on this host.

`SHIM_BEARER_MODE=optional` is a **census**, not an authentication: every caller
is served, and the ones arriving without a valid token are logged with their
address and user agent, never with the value they presented. That is the point —
six `auto-discord` containers reach `:8003` on `brain-net` carrying no bearer at
all, and `required` would 401 all of them. Arming is a separate operator gesture
that waits on the client-side ticket; it is not a config tweak.

The token is read **once, at startup**. Rotating the file changes nothing until
the container restarts:

```bash
docker compose build embedding-shim
docker compose up -d --no-deps embedding-shim   # --no-deps: never recreate embedding-llama
```

Never widen that to a bare `docker compose up -d`. One trap still makes the
global form unsafe on a running host, and it is silent until it is not: every
secret source whose override variable is unset falls back to a versioned
default under `./.secrets/`, a directory this host does not have, so the `up`
fails on the first service that needs one. Always name the service and pass
`--no-deps`.

A second trap depends on the configuration: since 2026-09-26
`embedding-llama` sits behind the `qodo` Compose profile
(docs/ARCHITECTURE.md), so a plain `docker compose up -d` never starts it.
The production deployment embeds through the OpenAI-compatible backend
(Mistral codestral) and does not need it; the shipped configuration
(`BRAIN_EMBEDDING_BACKEND=shim`, `BRAIN_EMBEDDING_MODEL=qodo`, `config.py` and
`.env.example`) does, and must run with `--profile qodo`. Without
`QODO_GGUF_DIR` set, Compose then mounts an empty model directory and puts
`embedding-llama` into a crash-loop (incident 2026-08-21) whenever the profile
is included: `docker compose --profile qodo up -d`.

The override variables are documented in `deploy/compose-secrets.env.example`
and belong in the repository's `.env`, which Compose reads on its own — no
ad-hoc export before the command, which is what made the Neo4j one invisible
until 2026-09-02. `tests/unit/test_docker_compose.py::TestComposeSecretSources`
refuses a hard-coded secret source and an undocumented override variable; it
cannot tell whether the file exists on any given host, so
`docker compose config` stays the measurement before any `up`.

### The MCP process is a client of that shim, and it carries no token yet

`SHIM_BEARER_MODE=optional` was armed to answer one question, and it answered it
on the first day: the census names `python-httpx/0.28.1` on `/embed/query` and
`/rerank`, which is this repository's own MCP process. Arming `required` before
that client carries a bearer would cut `brain_search` off from its own
embeddings. The six `auto-discord` containers are the other half, tracked in
ticket `9ef5c69d` and living in another repository.

The client half is wired and ships CLOSED. `brain_embedding_token_file`
(`BRAIN_EMBEDDING_TOKEN_FILE`) defaults to `None`, which keeps today's contract:
no `Authorization` header at all. Point it at the same 0600 file the shim reads
and both clients — embedding and reranker — send `Authorization: Bearer` on
every route they use (`/embed`, `/embed/query`, `/rerank`, `/healthz`,
`/health`; the two health routes are exempt server-side and the header is simply
ignored there).

A PATH, never a value: `systemctl show` prints a unit's environment verbatim.
The file is read once, at construction, which is startup for every runtime that
goes through `build_embedding_service` / `build_reranker_client` — so rotating
it needs a restart, exactly like the shim's own read. Configured but absent,
empty or unreadable is a **named startup failure**, never a silent call without
the header: while the shim answers `optional`, such a call still succeeds, and
the misconfiguration would stay invisible until the day someone arms `required`.

Arming the client is an operator gesture — a drop-in and a restart, not a config
tweak — and it must land BEFORE `SHIM_BEARER_MODE=required` is even considered.

Rollback, once the previous image is tagged before the build:

```bash
docker tag <previous-image-id> brain_v42_embedding_shim:pre-bearer-<date>
```

## MCP HTTP input bounds and residuals

`MCP_HTTP_MAX_BODY_BYTES` defaults to 2,097,152 bytes (2 MiB), with allowed values
from 65,536 to 67,108,864. Declared oversize requests get 413 without reading the
body; streamed bodies are buffered up to the cap before reaching MCP and get the
same `{"detail": "Request body too large"}` response if they exceed it. Invalid or
negative `Content-Length` returns 400 with `{"detail": "Invalid Content-Length"}`.
Tool fields separately have character/list bounds documented in
[MCP_TOOLS.md](MCP_TOOLS.md#input-bounds); values are refused, never truncated.

HTTP startup raises `HttpAuthConfigurationError` when `MCP_HTTP_TOKEN` is empty or
blank unless `MCP_HTTP_ALLOW_UNAUTHENTICATED=true`. This opt-out is **development
only**, refused alongside a non-empty token and under Dream capability enforcement;
it must never appear in the production systemd unit. Clearing the production token
now causes an outage through startup refusal rather than unauthenticated access.
The measured network-boundary paragraph above still describes that configuration
drift literally; its measurements have not been changed. There is no minimum token
length and the systemd preflight is unchanged in this release.

`/health` remains auth-exempt and retains status, version, alembic_head and pool.
Concurrent probes share one database round trip, with no TTL cache; sequential probes
always check the database again.

Residuals retained for follow-up:

- `X-Brain-Agent` and `X-Brain-Session` are attribution, not authorisation.
- One shared admin bearer remains; per-client scoped admin bearers need a future ticket.
- `SHIM_BEARER_MODE=optional` remains pending ticket `9ef5c69d`.
- There is no body-read deadline or ingress concurrency cap. In capability enforcement
  mode, authentication rejects at the route inside user middleware, so unauthenticated
  clients can cause buffering up to the cap before receiving 401. Host checks still run.
  In ordinary bearer mode, authentication precedes buffering. The transport is loopback.
- FastMCP may log the full rejected argument at WARNING, bounded by the HTTP body cap.
- ADR deprecation builds an `ADRUpdate`: existing consequences within about 2,000
  characters of the 50,000-character cap can refuse the appended reason. This is accepted
  headroom risk; the measured maximum was 2,279 characters.
- Backfill similarity thresholds retain their existing semantics and are not clamped.

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

## Nightly claim verification

`dream.sh` runs `verify` once per night, before provider preflight and the project
loop. It selects active claims across all projects, capped at 200 by default
(`BRAIN_DREAM_VERIFY_MAX_CLAIMS`, range 1–5000). This in-process step uses no LLM,
MCP call, or Dream bearer. It ships disabled and dry:
`BRAIN_DREAM_VERIFY_ENABLED=false` skips it, while
`BRAIN_DREAM_VERIFY_DRY_RUN=true` runs it without writing claim verdicts. Only the
literal `false` for the dry-run key enables wet verification. A dry run still
records its own `dream_runs` row and measures each distinct eligible fact once.
Eligible claims have no conclusive verdict, have an expired conclusive verdict,
or have a later unreadable verdict. Dry mode classifies historical definitions,
release skips and measurable claims. Both modes refuse to verify anything if
fact definitions are unregistered or an eligible fact is refused or disabled.

The step appends a structured report to `logs/dream/<date>_verify.json` and a
one-line summary to `logs/dream/<date>_verify.log`. A second invocation on the
same date appends another line; read the last matching report for the run ID and
mode you are investigating. The dated Dream log records `SKIP`, `DONE`, `TIMEOUT`,
`BUSY`, or `FAIL verify`. In the morning, check the JSON report's `status`, `rc`,
`selected`, `eligible_at_start`, `unreadable`, `errors`, and skipped counts, then
compare it with the dated Dream log. A successful dry run proves selection and
measurement, not that verdicts were written. An argument-parser failure or the
outer timeout may leave no JSON line; inspect the CLI log in that case.

For the morning reading of the latest wet run, call
`brain_fact_get("claims_verification_last_night")`. The fact reports the run status,
verdicts written by that run, and claims eligible now. An unreadable fact is not
by itself a missing run: read its error code first. No wet verify row is an
expected empty state, reported as `no_observation` (the briefing says "aucune
observation enregistrée", not "illisible"); a failed database read stays a
`probe_error`, so the two are no longer confused. `timeout`,
`queue_timeout`, `capacity_timeout`, `target_mismatch` and `identity_unreadable`
mean the measurement itself failed, not the run. Use the JSON report and dated
log for errors, skips, and dry-run details.

| Return code | Meaning | Dream handling |
| --- | --- | --- |
| `0` | Done | Records `DONE` and continues |
| `3` | Controlled deadline; terminal run row recorded | Records a controlled timeout and continues |
| `5` | Partial, with per-claim errors | Records failure and continues |
| `1` | Failed, including a precondition refusal | Records failure and continues |
| `6` | Busy: another wet verifier holds the run lock | Records failure and continues; no run row or verdict is written by this attempt |
| `7` | Wet run lost ownership | Records failure and continues; no further verdict dispatch or run-row update |
| `2` | Invalid configuration or run date | Records failure and continues |
| `124` | Outer five-minute guard killed the CLI | Records an uncontrolled timeout and continues; inspect the log and database before assuming a terminal run status |

For a manual dry run, first inspect `systemctl --user show
brain-v42-dream.service -p Environment -p EnvironmentFiles -p WorkingDirectory`.
Confirm the release environment, working directory and facts identity file on
the live unit without displaying private file contents. Then use its release
venv and the same facts identity file in an isolated transient unit:

```bash
unit=brain-v42-dream.service
environment=$(systemctl --user show "$unit" -p Environment --value)
repo=$(systemctl --user show "$unit" -p WorkingDirectory --value)
rel=$(printf '%s\n' "$environment" | tr ' ' '\n' |
  sed -n 's#^UV_PROJECT_ENVIRONMENT=\(.*\)/venv$#\1#p')
live_identity=$(printf '%s\n' "$environment" | tr ' ' '\n' |
  sed -n 's#^BRAIN_FACTS_LIVE_RELEASE_IDENTITY=##p')
test -n "$repo" && test -n "$rel" && test -n "$live_identity" || exit 1
systemd-run --user --wait --pipe --collect \
  --working-directory="$repo" \
  -p EnvironmentFile=-"$HOME"/.config/brain-v42/facts-identity.env \
  -p NoNewPrivileges=true -p UMask=0077 \
  -E PYTHONSAFEPATH=1 -E UV_NO_SYNC=1 -E PYTHONHOME= \
  -E PYTHONPATH="$rel/brain-v42" \
  -E BRAIN_FACTS_LIVE_RELEASE_IDENTITY="$live_identity" \
  "$rel/venv/bin/python" -m brain_v42.maintenance.claim_verify \
    --run-date "$(date +%F)" --report-dir "$rel/brain-v42/logs/dream"
```

There is no `--wet` in this invocation. Run it only after confirming that the
live release contains `claim_verify`; this hand-run procedure still needs a
live-unit check before it can be treated as proven. The nightly `verify` step
skips claims on `dream_last_night` and `claims_verification_last_night` in both
wet and dry modes and reports them as `skipped_self_referential`. Its own
`dream_runs` row could otherwise make either fact describe the current,
incomplete run instead of the previous complete one.

## Reading a Dream night: the file, not journald

`dream.sh` tees every `log()` line to `logs/dream/<date>.log` AND to the unit's
stdout. Only the file is complete. Measured line for line over the seven nights
2026-08-27 → 2026-09-02, journald holds 25-32 % fewer lines every single night —
252 in the file against 181 in the journal for 2026-09-02, to the point of showing
six `START clean` for eight `DONE clean`, which cannot happen. The cause is not
established: it is neither a `stdout` capture upstream (`run_phase_chain` is called
directly) nor a declared journald rate limit. Until it is, a morning check greps
`logs/dream/<date>.log`; `journalctl --user -u brain-v42-dream` is a convenience,
not the record. Note the `--user`: the unit is a user unit, and `journalctl -u
brain-v42-dream` answers `-- No entries --`.

The morning report itself is replayable read-only against the night's own manifest:

```bash
uv run python -m scripts.dream.post_run_alert --date 2026-09-02
```

It prints the coverage block, the freshness provenance block, and — since the seven
nights above, where roadmap ran clean once and three degraded nights printed
`no failures` — a `### DÉGRADÉ (secours)` rubric naming the mute primary model and
how many batches the standby served. A degraded phase does NOT change the exit code:
escalation to `2` belongs to coverage alone, because `dream.sh` reads that `2` as
"expected rows are missing" and writes a `coverage` row saying so.

It also prints a `### REORG` block, read from the trailer each project's report
ends with — not from `dream_runs`, which carries no REORG counter. The block keeps
two kinds of number APART, and a reader who merges them draws the wrong conclusion:

- **Mesuré** — archives and tag updates, derived from the ids the report names.
  `scripts.dream.reorg_validate` confronts those same ids with the event stream and
  with PostgreSQL, so they are evidence.
- **Déclaré** — candidates examined, refusals by reason, deferrals. This is the
  phase's own account of entities it looked at and did NOT touch. No call exists
  that could confirm it. It is checked only against itself, and the block says so.

The block is never mute, because the ways a REORG night can look empty are
different failures: no report file at all, reports carrying no machine-readable
trailer, a trailer from a prompt older than the declared tally, and a tally that
genuinely examined zero candidates. Each gets its own sentence. The second and
third are rails succeeding without producing, which is the reading this block
exists to prevent.

## Plan index refresh

Two ways exist to index a plan file written after the server started, and only one
of them is armed.

`brain_reindex_plans(project_key=None)` is the explicit one. It is registered
unconditionally and answers under the production `compact` profile, so an agent can
always say "now" without waiting for anything. It also consolidates mirror-path
duplicates before scanning, which the periodic sweep deliberately does not do.

`PLAN_INDEX_REFRESH_ENABLED` arms the passive one: a loop that re-scans every project
with `plan_scan_paths` configured, every `PLAN_INDEX_REFRESH_INTERVAL_SECONDS`
(default 900). It ships **closed**, and that is not caution for its own sake. The
sweep writes: an indexed plan reaches `ClusterGuard.resolve()`, `plan` is in
`CREATING_SIGNALS`, so link-only mode does not stop it and a sweep can create
features. The roadmap tap is under observation and the purge of pseudo-features is
waiting for the dry-up to be judged established — arming a periodic creator by
default would change the very thing being measured. Arm it when that measurement is
closed, not before.

An unchanged corpus costs one read and one lookup per plan file and no embedding at
all: the content hash decides before anything reaches the GPU. The loop sleeps its
interval before its first sweep, so it never walks the same files as the one-shot
pass the lifecycle already runs at startup.

A scan path that fails — missing, unreadable, not a directory, or relative — is
warned once per `(project, path, reason)` per process and drops to debug on every
identical repeat. On a period the alternative is 96 identical lines a day, per path,
forever. The suppression is a LOG volume decision only: `brain_reindex_plans` still
names every rejected path in its `Fichiers non indexés` list on every single run, and
so does each sweep's `plan_index_refresher.sweep_done` error count.

## Delivery observer: releases and deployments

Besides confirming pull requests, the delivery observer
(`brain-v42-delivery-observer.service`) derives two facts per merged deliverable,
once per cycle, after its confirmation queue. It records them as delivery
attestations; it never transitions a ticket, and it never judges what a fact means
(red-rail does).

**`released` — which tag first shipped a merge.** For every active artifact binding
with an `integration_sha` on its workflow's current contract revision, and no
`released` row yet for that `integration_sha` (issued by the observer as the
executor project), the pass lists the repository's `v*` tags (only
`vMAJOR.MINOR.PATCH` names count), dates each tag and the merge by commit date,
and walks the tags from the oldest one not older than the merge. Containment is
asked tag-first, `GET .../compare/{tag}...{merge}`: `behind` or `identical`
means the tag carries the merge. The first containing tag is recorded and the walk
stops; a conflict on that tag's key also stops it, so a later tag is never claimed
in its place. A failed tag list or tag-date fetch leaves the repository empty for
this cycle and is retried on the next one. A failed fetch of the merge's own date
skips that candidate for this cycle; if GitHub answers that the merge commit does
not exist, the candidate is skipped for the life of the process, any other failure
is retried on the next cycle.

**`deployed` — whether the live brain-v42 release carries a merge.** brain-v42 only:
the observer reads the release it runs from (`releases/<40-hex sha>/` of its own
import path) and asks GitHub whether that SHA contains each brain-v42 merge not yet
attested for it. A process running from a checkout measures no release and records
nothing. The pass is the observer's own measurement of its own release, so it
covers the brain-v42 repository only, and only while that repository is registered
for `brain-v42` in the observer's repository registry.

| Kind | Idempotency key | Payload |
|---|---|---|
| `released` | `released:<ticket>:<deliverable>:<tag>` | `repository_id`, `tag`, `tag_sha`, `integration_sha` |
| `deployed` | `deployed:<ticket>:<deliverable>:<live release sha>` | `repository_id`, `live_release_sha`, `package_version`, `integration_sha` |

Both are issued as `brain-v42-delivery-observer` by the ticket's executor project,
with the binding's contract revision; `released` is dated by the tag's commit,
`deployed` by the observation. Readers count only rows carrying that issuer
identity and issued by the ticket's executor project: a caller's declaration of the
same kind is stored but is not a measurement. The identity is a declared label,
like every attestation issuer, not a proof of origin: it holds inside the same
trust boundary as the MCP bearer.

**Reopened tickets.** A reopen deactivates the bindings; the next attempt binds a
new merge under the same deliverable. Both passes then treat that merge as new work:
a row counts as "already recorded" only when it carries the binding's
`integration_sha`, so the new merge is derived against the later tag or live
release. The ticket view reads only observer rows whose `integration_sha` belongs
to an active binding of the ticket, so the first merge's tag and live release are no
longer shown as the reopened ticket's own. The idempotency key does not contain the
merge: when the new merge lands in the same tag or the same live release as the
first one, the key is already taken, the write is refused, and the key stays
blocked for the life of the process with one `idempotency_key_reused` diagnostic.
Nothing is claimed for the new merge in that case, and it is never attributed to a
later tag in place of the taken one.

Both passes run only in a repository-wide cycle: a run scoped to one project
(`run_once(project_key=...)`) leaves them out. They check the shutdown signal, like
the loss of ownership, before each request, since admission can wait up to a
minute for one.

**Budget.** Both passes go through the observer's GitHub transport and share its
admission budget, `BRAIN_DELIVERY_REQUEST_BUDGET_PER_MINUTE` (default and ceiling
40). On top of it, a cycle performs at most ten containment checks across the two
passes; the rest wait for the next cycle. A pair found not contained, and a merge
GitHub does not know, are remembered for the life of the process and not asked again.

**Rollback.** Switching the live release back writes nothing and deletes nothing:
the `deployed` rows stay, and the ticket view compares them with the release the
MCP server itself runs from. A ticket whose deployed releases do not include the
running one reads "deployed once (<sha>), not in the live release"; a server
running from a checkout says "live release unmeasured" instead of guessing.

**Check it.** On a merged ticket, `brain_delivery_get` shows the binding's
`integration_sha`; after the next cycle that follows a tag (or a cutover),
`brain_ticket_get` shows the measurement on its `release:` line, for example
`release: planned 0.6.3 · shipped v0.6.3 · deployed`.
`brain_ticket_list(project_key, target_release="0.6.3")` opens with the lot header:
whether `v0.6.3` was observed, the tickets planned but not shipped in it, and those
shipped under another tag.

## Automation service

The `brain-v42-automation.service` unit is generated and verified, but stays dormant.
It listens on `AUTOMATION_PORT` (default 9201), loopback-only. The cutover without
dual-run, lease proof and rollback are described in the
[systemd runbook](../deploy/systemd/README.md). Deploying the code with
`METRICS_LEGACY_AUTOMATION_ENABLED=true` does not change the live automation owner.

## Codex gateway

The `red-codex` administration gateway deploys only on the private Docker network,
with no host port and no systemd unit; follow the Codex gateway runbook
(`deploy/CODEX_GATEWAY.md` in the private brain-v42-internal repository — it couples
this service to private sibling infrastructure). Its live activation stays
blocked while the PostgreSQL `codex_ro` and `brain` credentials use their development
defaults, or while `/ready` doesn't validate the SQL contract, including the
`security_barrier` of the seven views scoped to the `red` group.

## Release: recovery binding

`ops/recovery/current.json` answers which recovery contract covers the schema
the WORKING CHECKOUT ships (`tests/unit/test_recovery_current_binding.py`).
`red-backup` needs the operational half of that answer: which recovery contract
covers the schema of the LIVE release, found WITHOUT brain running and WITHOUT
reading brain's working checkout — a checkout moves under a release the moment
`main` advances. `brain_v42.release_recovery` (`src/brain_v42/release_recovery.py`)
is that bridge, and the out-of-repo release script calls it at three points:

- **Build** — once `current.json`'s source tree and `delivery-release.json` exist
  in the new release directory:

  ```bash
  python -m brain_v42.release_recovery publish "$RELEASE"
  ```

  Copies the three files `<release>/brain-v42/ops/recovery/current.json` names
  into `<release>/recovery/` (sha256-verified before AND after the copy, and
  `schema_head` checked against the release's own shipped Alembic head), writes
  `<release>/recovery/recovery-binding.json` atomically, and records that
  binding's own sha256 into `<release>/delivery-release.json` under
  `recovery_binding`. `scripts/check_delivery_deployment.py` refuses, fail-closed,
  any manifest missing this key or disagreeing with it — with no
  backward-compatibility grace period, unlike `pyvenv_cfg`.

- **Cutover** — once the new release's units are active and the dormant/canary
  preflight is green:

  ```bash
  python -m brain_v42.release_recovery live "$RELEASES_ROOT" "$SHA"
  ```

  Atomically replaces `<releases_root>/live` (a temporary symlink created next
  to it, then `os.replace`) so no reader ever observes a missing or half-written
  link. Refuses a `SHA` with no release directory or no published recovery
  binding.

- **Rollback** — identical swap, pointed at the predecessor:

  ```bash
  python -m brain_v42.release_recovery live "$RELEASES_ROOT" "$PREDECESSOR_SHA"
  ```

`red-backup` reads `<releases_root>/live/recovery/recovery-binding.json` — never
brain's working checkout, and never a running brain process. Show the current
target at any time with:

```bash
python -m brain_v42.release_recovery show-live "$RELEASES_ROOT"
```
