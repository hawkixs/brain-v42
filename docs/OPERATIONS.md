# Operations

Deep operational reference for the repository's deployment units, session lifecycle,
network boundary, migrations, graph projection, measured facts, and delivery observer.
[`README.md`](../README.md) gives the short operator overview.

## Service units and resource limits

The systemd templates in `deploy/systemd/` set explicit `MemoryMax` bounds. The
MCP HTTP service is limited to 4G; metrics and delivery observer to 1536M each;
automation to 1536M; graph reconciliation to 2G; embedding backfill to 4G; the
watchdog and log-retention service to 1G each; and Dream to 16G. These are unit
limits, not measurements of current memory use. The HTTP unit always restarts,
with backoff from 2 s to 60 s. The separate watchdog timer probes the HTTP service
and restarts it only when its health probe confirms failure.

The connection reaper is a maintenance command, not the HTTP watchdog. Run
`python -m brain_v42.maintenance.reap_stale_mcp --help` for its dry-run and explicit
execution options. It uses an age heuristic: within each agent group the newest
server is protected, except that the 48 h absolute cap reaps even that server.
Only stdio children are eligible; `--http-server` processes are excluded. Dry-run
is the default; `--execute` enables termination. Review its report before execution.
Log retention is dormant by default: the installer preserves timer state, and an
operator must explicitly enable the log-rotate timer to prune eligible files.

### MCP HTTP session bounds

`MCP_HTTP_MAX_SESSIONS` bounds the number of held stateful HTTP sessions (default
6000; values below 1 are rejected). `MCP_HTTP_SESSION_IDLE_SECONDS` retains its
eight-hour default. Size the cap and idle timeout together:

```text
memory ~= 175 MiB + 0.22 MiB x held sessions
held sessions ~= creation rate x idle timeout
```

The 0.7.1 measurement was about 225 KiB per held session: 6000 sessions add about
1.3 GiB above the base, leaving headroom under the 4 GiB container limit. This is
a sizing estimate, not a measurement of the live process.

At the cap, the least recently active idle session is evicted. Its next request
gets HTTP 404, and Claude Code reconnects automatically. Sessions with requests
in flight are protected; only when no session is idle does a new session get
HTTP 503. An eviction warning includes a shortened session ID and cumulative
eviction count. A capacity warning is logged on reaching 80% of the cap and is
rearmed after the count drops below 80%. If SDK private eviction attributes are
missing, an error is logged once and admission falls back to the SDK's refusal.
Eviction is decided at admission, before the opening request is validated, so at
the cap any authenticated request without a session ID evicts one eligible idle
session even if its initialize then fails; this is bounded by authentication.

## Measured facts

Use `brain_fact_list` to discover registered facts and `brain_fact_get` to read a
specific measured value. A fact definition names its source identity and the
measurement is taken from that source. Do not copy live schema heads, release
identities, Dream arming state, or projection lag into this document.

## Session lifecycle (full v4 contract)

Only an explicit user command may start, capture, heartbeat, list, resume, bind, relay, end, or abandon a session on the agent and client side. Hooks and agents never infer a boundary or close a stale session. **Amendment — slot relay (ADR #34).** A guard mod that the operator has explicitly enabled counts as a standing user command for one gesture only: `brain_session_relay` of an open operator session, onto its focus slot when it is bound to one and onto the project base when it is not — capture, end and start of its successor under a new `client_key`, in one transaction. The model makes the call and chooses its captures, summary and handover; the mod only triggers the turn and replays the result at compaction. The base form writes the handover as the project's whole base focus under a compare-and-swap on its revision, and refuses a handover under 70% of the current base (`base_focus_shrink`): only an operator relay with an active /brain-admin elevation may override that guard, never the mod. The standing command covers nothing else: not `brain_session_abandon`, not `brain_session_end` of any session, not the relay of a server-opened trace, not any write of the project base outside that relay, not opening, closing or changing a slot, and not `start`, `resume` or `bind` outside the relay. It is void while `BRAIN_SESSION_RELAY_GUARD_MOD_ENABLED` is false. Hooks still never capture, close or commit on their own. Cross-cutting work without a ticket needs no anchor: left unbound, it is relayed onto the project base, under the same guards. The only server-side exception is the Dream `sweep` phase, shipped disabled and dry, which abandons an open session with no heartbeat for seven days (`abandonment_reason = 'auto_stale_7d'`) without touching project focus. It ships behind `BRAIN_DREAM_SWEEP_ENABLED=false` and `BRAIN_DREAM_SWEEP_DRY_RUN=true`. Staleness is a list filter over open rows; it never changes the persisted `status` and never auto-closes a session. Do not confuse this 24-hour display flag with the separate seven-day server-side sweep, which is the only mechanism that moves an open session to `abandoned` without an explicit command (`abandonment_reason = 'auto_stale_7d'`).

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
heartbeat, a list, or an abandon. Abandoning a session keeps its ledger in place,
but makes its artifacts reclaimable by a later explicit capture or absorption
(decision 974f46f3). Open and ended operator holders remain protected. An exact retry
of `capture` stays idempotent while the abandoned session still owns the artifact.

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
Abandoned operator sessions are also donors, matched through their seen connections
because operator rows carry no `connection_id`. That start excludes artifacts produced
by a predecessor on the same connection. Decision 974f46f3 makes the connection stage
abstain when another open operator of the same project saw the same connection; a
coordinating session on a different connection still does not block the exact match.
A connection that saw no lifecycle call still falls back to the window stage.
The window stage then runs whenever ledger capacity
remains (100 artifacts per session, minus those already attributed), whether or not the
current connection still has a trace. Its donors are the project's `open` or
`closed_inactive` traces opened by a human actor and abandoned operator sessions,
never an abandoned trace and never a trace opened by a system actor such as the Dream.
The abandoned donor itself is not a rival. It takes an artifact only if no other
session of the project that is not an agent trace covered the instant of its creation,
whether that session is still open or has finished since; under that ambiguity the
artifact stays with its donor. Connection-stage abstentions also apply to the fallback,
and both stages report refused artifacts and rival session identifiers. Artifacts on
abandoned operator holders count as unattributed in the closing receipt.
The server never promotes a trace into an operator
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

**On red-base** (Compose, no Dream unit), the `session-sweep` service runs the same
CLI once at start and then every 24 hours, for the seven-day rule only: the net above
already covers the four-hour rule, so the service does not set
`BRAIN_SESSION_INACTIVE_SWEEP_ENABLED`. Whether it writes is declared on its command
line (`--wet` or not) in `deploy/compose.yaml` and changes only through a pull request
followed by a rail deploy. Each run prints its report to `docker logs brain-v42-session-sweep`
and records a `dream_runs` row (`phase = 'sweep'`, `phase_dry_run` set accordingly) on a
best-effort basis: a run that fails before reaching the database (invalid configuration,
PostgreSQL down) or whose INSERT fails leaves no row, so a missing row does not prove the
loop is dead. A failed run is retried the next day.
After the sweep, the same loop runs `python -m brain_v42.maintenance.trace_retention`
(see "Agent trace and graph outbox retention"), dry unless its command line carries
`--execute`; its two report lines go to the same container log.
Each of the two jobs touches its own heartbeat file when it succeeds
(`/tmp/brain-v42-session-sweep.heartbeat`, `/tmp/brain-v42-trace-retention.heartbeat`).
The healthcheck fails once either is missing or older than 26 hours (a day between runs
plus two hours for the runs), after a 15-minute start period that covers the first runs,
so `brain-v42-session-sweep` turns `unhealthy` when a job stops completing, not only
when the loop dies.

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

## Programme progress (`GET /programmes`)

Programmes and their gates are written by the `brain_programme_*` tools
([MCP tools](MCP_TOOLS.md#brain_programme_upsert)) and read by the cockpit through red-monitor.
The contract is `docs/contracts/programmes_v1.md`. Three things to know when operating it:

- **Where it is served.** `GET /programmes` on the metrics sidecar answers
  `{"programmes": <block>}`, or `{}` when the block's read failed. The failure is memoised
  for 5 s, and the next request retries. The block is not in `/metrics`. It shares the
  sidecar's listener and its read boundary: unauthenticated, behind the WireGuard perimeter
  (decision 94771690). Its exact fields are pinned with the other public reads by
  `tests/unit/metrics/test_read_exposure_pinned.py`. A release older than 065 answers `404`,
  which red-monitor reports as `no_publisher`.
- **How fresh it is.** The block is memoised for 30 s with the other slow blocks.
  `generated_at` is its computation time, so a block served 25 s after it was computed reads
  25 s old. That is not a stuck publisher.
- **Who may write.** Writes need the HTTP transport with a client credential: the writer is the
  project the credential resolves. A write under the shared token or stdio is refused with
  `caller_unidentified`. When a coordinating session ends, a programme stays readable and
  writable by its coordinating project. Mark a finished programme `done`: it stays published
  for seven days, then leaves the block but remains readable through `brain_programme_list`.

## Protected sidecar reads

Consumers can read `GET /api/session-slots/v1` (the slot inventory alone) and
`GET /api/brain-graph/v1` (the graph snapshot) over the sidecar's WireGuard listener.
The operator issues each consumer's client credential through the registry CLI;
issuing credentials is an operator gesture. Configure `METRICS_RECEIVER_AUTH=credentials`
and `METRICS_READ_CLIENTS=red-monitor,red-cockpit` with the client ids the operator
actually issued, in the metrics service's `environment:` list of `deploy/compose.yaml`
(that list is explicit, so a value set elsewhere never reaches the container; changing it
ships with a release). The allowlist is comma-separated with no spaces; each id must match
`[a-z0-9][a-z0-9.-]{0,63}`. Its default is empty, which admits no credentialed reader.
The credential must have the existing `telemetry` family as well as an allowlisted id.

Store the bearer token in a file with mode `0600` on the consumer's machine. The
consumer reads that file and sends exactly one `Authorization: Bearer <token>` header.
Keep the token out of the repository, environment and command line. This telemetry
credential exposes no MCP tools, but can also submit OTLP usage to `/v1/logs` and
`/v1/logs/claude`: the accepted residual risk is fabricated usage counters, so restrict
issuance to the operator's consumers (decision 11defbe0).

Headerless transport-loopback requests are admitted for local diagnostics. Any supplied
Authorization header is checked even on loopback; forwarded headers do not change the
transport peer. Without a verifier, non-loopback requests get
`403 {"error": "loopback peer required"}` before header inspection, and a loopback
request supplying any Authorization header gets `401`.

Credentialed read refusals return `{"reason": "<code>"}`:

| HTTP status | Reason |
|---|---|
| 401 | `malformed_authorization` (duplicate, non-Bearer or empty header/token), `missing_token`, `unknown_token`, `revoked_token`, `expired_token` |
| 403 | `client_not_allowed` (not allowlisted), `family_denied` (no telemetry family) |
| 429 | `rate_limited` (10 reads/s per client, burst 20, shared across both read routes) |
| 503 | `registry_unavailable` (cold snapshot or last successful refresh older than 90 s) |

`429` and credential `503` responses include `Retry-After: 1`. Malformed or missing
headers are refused locally with `401` before consulting even a cold or stale registry.
The registry refreshes every 30 s: an outage preserves valid cached credentials until
90 s after the last successful refresh. Expiry is checked on every request; revocation
is effective after the next successful refresh. A revoked credential still cached
during an outage can read until that 90 s boundary, after which reads fail closed.
Read quotas and refusal logs (`metrics.read_refused`, plus debug `metrics.read_admitted`)
are separate from telemetry receiver quotas and observation counters.

The slot response is `{"session_slots": <block>}`, matching the existing block in
`/metrics` and `docs/contracts/session_slots_block.json`. Both routes to that block
share one computation per cache TTL (30 s by default). A failed slot read returns `{}`
and retries after the error TTL (5 s by default); `/metrics.block_status.session_slots`
reports that health. The existing public reads `/metrics`, `/api/cockpit`, `/programmes`
and `/healthz` keep their admission and payloads, including `/metrics.session_slots`.

## Admin elevation hook (`/brain-admin`)

The operator elevates the current Brain session to admin by typing a command in the
conversation. Two scripts under `scripts/hooks/` implement the client side; the endpoint
is `POST /admin/elevations` on the MCP HTTP server.

- `brain_admin_elevate.py` judges a submitted prompt and posts the elevation.
- `brain_session_state.py` keeps the per-Claude-session state the first script reads
  (`claude_session_id`, `brain_session_id`, `prompt_count`) and calls it itself, so the
  order is deterministic: Claude Code runs the hooks of one event in parallel, and the
  prompt must be judged against the count of *preceding* prompts before it joins the count.

Install both files side by side in `~/.claude/hooks/brain-admin/` (directory mode `0700`),
then register **only** `brain_session_state.py`, for two events in the Claude Code settings:

- `UserPromptSubmit`, no matcher;
- `PostToolUse`, matcher `mcp__brain-v42__brain_session_(start|resume|relay|end|abandon)`.
  `start`, `resume` and `relay` record the session the tool returned (for `relay`, the
  successor, not the relayed one); `end` and `abandon` clear it only if it is that session.

Environment of the hook process:

| Variable | Meaning |
|---|---|
| `BRAIN_ADMIN_URL` | `http://<brain host>:8765/admin/elevations`; the path is checked exactly |
| `BRAIN_ADMIN_CREDENTIAL_FILE` | regular file, mode `0600`, holding the operator-elevate token |
| `BRAIN_SESSION_STATE_DIR` | optional; default `~/.local/state/brain-v42/sessions` |

The state directory is created `0700`; a symlink, a directory owned by someone else or one
accessible to group or others is refused and nothing is written. One file per Claude
session, written atomically with mode `0600`.

Usage: put `/brain-admin 1h <reason>` as the first line of a prompt (a duration of up to
four hours, `s`, `m` or `h`). It is never honoured as the first prompt of a Claude session,
nor inside a cross-session message wrapper, nor when embedded in other text; a Brain
session must have been started or resumed in that Claude session first. Every elevation
raises `credentials.elevated`, which red-watcher forwards as a DM.

Residual risks, accepted in decision `69128c54`: every process of the workstation user can
read the credential file and write the state files, so a malicious local agent with a shell
could elevate a session; the boundary against prompt injection holds (only text the human
submits reaches the hook), the boundary against a malicious local process is a convention
plus the DM alert. An agent that calls `brain_session_resume` on another open session makes
that session the one the next `/brain-admin` targets.

## Workstation Claude Code headers (`headersHelper`)

Claude Code reaches the MCP server with a user-scope `headersHelper`,
`deploy/clients/claude-brain-headers.sh`. It prints one JSON line: the bearer read from
the client's `0600` token file and, as `X-Brain-Agent`, what the server needs to attribute
the session to a project. The server keeps the basename of a path and accepts it only if it
is an existing kebab-case project; otherwise the call still runs, under the client actor,
and `mcp_auth_agent_unresolved` counts the miss (`not_kebab` or `unknown_project`).

What the helper sends, in order:

1. Nothing for the home directory itself or for the daemon's prewarmed
   `/tmp/cc-daemon-*/spare` processes: no project is claimed and nothing is counted.
2. The project key of the longest matching prefix in the operator's map file, when one
   matches on whole path components.
3. Otherwise, the working directory of the Claude Code process that runs the helper (never
   `${PWD}`: Desktop sessions inherit the app's directory, which is the home directory).

A path that JSON cannot carry unescaped (a quote, a backslash, a control character) claims
no project. A missing or empty token file makes the helper fail with nothing on stdout.

Install, from the canonical checkout:

```bash
install -m 0700 deploy/clients/claude-brain-headers.sh ~/.config/brain-v42/clients/claude-brain-headers.sh
```

The map file is the operator's, `~/.config/brain-v42/clients/agent-map`, mode `0600`, one
`<absolute path> <project_key>` per line, `#` for comments. The helper trusts it only when
its directory belongs to the user with no group or other permission, the map path is not a
symlink, and the file it actually opens is a regular file owned by the user with no group or
other permission; it ignores any line whose key the server would refuse. Directories above
the map's own are not checked: this residual is accepted (operator ruling, 2026-10-13),
because the default path sits in the user's private configuration tree next to the helper
and the token, which whoever controlled an ancestor could replace anyway. Never point
`BRAIN_AGENT_MAP_FILE` into a directory another user can write or redirect. Seed the map
for directories whose basename is not their project:

```
/path/to/ReD_v1/skills red-skills
/path/to/ReD_v1 red
```

Variables: `BRAIN_CLAUDE_TOKEN_FILE` (default
`~/.config/brain-v42/clients/workstation-claude.token`) and `BRAIN_AGENT_MAP_FILE`
(default `~/.config/brain-v42/clients/agent-map`). Sessions pick the new helper up when
they reconnect. Check the effect on the metrics sidecar: the `mcp_auth_agent_unresolved`
counters stop growing, and the per-agent view shows the mapped projects.

## Metrics sidecar health

`GET /healthz` on the metrics sidecar (`:9200`) runs `SELECT 1` on its own engine, bounded
by two seconds. PostgreSQL answering gives `200` with `"database": "reachable"`; an error
or a stall gives `503` with `"status": "degraded"` and `"database": "unreachable"`, and a
`metrics_healthz.database_unreachable` warning naming only the error type. The compose
healthcheck reads this endpoint, so a PostgreSQL outage now turns `brain-v42-metrics`
`unhealthy`, which red-agent reports in the container's health field. The answer still
carries `ingest_receivers`. Unlike the MCP server's `/healthz`, this one does touch the
database. The answer is decided at the two-second bound: a probe cancelled there finishes
tearing down its connection in the background, so the endpoint stays within the
healthcheck's three-second client timeout even when that teardown is slow.

## Network trust boundary (detailed)

MCP HTTP binds to loopback by default. A non-loopback `MCP_HTTP_HOST` requires
`MCP_HTTP_ALLOW_NON_LOOPBACK=true`, `BRAIN_MCP_AUTH_MODE=credentials`, and a non-empty
`MCP_HTTP_ALLOWED_HOSTS` comma-separated list of `host[:port]` authorities; enabling
the opt-in requires credentials mode even when the bind remains loopback. The
Host/Origin guard accepts listed authorities and the existing loopback hosts;
an explicit port limits an entry to that port. Unlisted Host headers receive
`421`, and unlisted Origins receive `403`. `GET /healthz` and `GET /version` are
unauthenticated exact paths in every authentication mode, while the Host/Origin
guard still applies. `/healthz` reports process liveness without a database call;
`/version` reports the package version, `BRAIN_GIT_SHA` and `BRAIN_IMAGE_DIGEST`
(null when absent), without echoing request headers.

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

### Hosted reranker settings

The reranker defaults to the local shim (`BRAIN_RERANK_BACKEND=shim`). Pointing it at
a hosted Cohere-style API (`BRAIN_RERANK_BACKEND=cohere`, for example OpenRouter with
`BRAIN_RERANKER_URL=https://openrouter.ai/api`) uses the settings below. None of
them changes the shim deployment.

For direct Voyage, set `BRAIN_RERANK_BACKEND=voyage`,
`BRAIN_RERANKER_URL=https://api.voyageai.com`,
`BRAIN_RERANK_MODEL=rerank-3-lite` and `BRAIN_RERANK_API_KEY_FILE` to the
operator's Voyage key file. The HTTPS origin is pinned; other hosts are refused.
The model must be non-empty and have no author prefix. An inline key,
`BRAIN_RERANK_PROVIDER` and an explicitly configured `BRAIN_RERANK_HEALTH_PATH`
are refused. Voyage has no free GET probe: availability sends a one-document
`POST /v1/rerank` with query and document `ping`, costing about 10 tokens per
300-second interval (plus the startup probe).

**Direct Voyage data policy:** training exclusion depends on the Voyage account's
training opt-out (operator decision `d4648d84`, Q4). OpenRouter's
`data_collection` routing setting does not govern a direct Voyage call; verify
the account opt-out before switching. The Cohere fallback through OpenRouter
keeps its enforced routing policy.

`BRAIN_RERANK_BACKEND=none` is the hosted reranker's configuration rollback:
no reranking, RRF order, `rerank_mode="disabled"`, no reranker client and no
background probe. This is an intended state, so it adds no degraded banner.
RRF scores render as ranks; the effective `min_score` is `0.0`, including when
a caller supplies an explicit threshold. No reranker key file is read.

For this rollback, clear non-empty `BRAIN_RERANK_MODEL` and `BRAIN_RERANK_API_KEY`,
unset `BRAIN_RERANK_API_KEY_FILE` and `BRAIN_RERANK_PROVIDER`, and unset explicitly
configured `BRAIN_RERANKER_URL` / `RERANKER_URL` and `BRAIN_RERANK_HEALTH_PATH`.
Also clear `BRAIN_RERANK_FALLBACK_MODEL` and `BRAIN_RERANK_FALLBACK_URL`, and
unset `BRAIN_RERANK_FALLBACK_PROVIDER`, `BRAIN_RERANK_FALLBACK_API_KEY_FILE` and
any explicitly configured `BRAIN_RERANK_PRIMARY_BUDGET_SECONDS`.
Startup refuses these fields, even when an explicit URL or health path equals
its default, and names the offending fields without their values.
The sidecar reports `reranker.backend="none"`, empty `by_identity` and null
`last_probe_*` fields. The configured backend (`shim`, `cohere`, `voyage` or `none`) is
persisted even without calls; the latest process report wins across processes.

| Setting | Default | Role |
|---|---|---|
| `BRAIN_RERANK_API_KEY_FILE` | unset | **Path** of the API key, read once at startup. Never a value: `systemctl show` and `docker inspect` print an environment verbatim. Mutually exclusive with `BRAIN_RERANK_API_KEY`, and refused with the shim backend. |
| `BRAIN_RERANK_HEALTH_PATH` | `/health` | GET path of the shim or Cohere availability probe. For OpenRouter set `/v1/key`, which is free and answers 200 only with a valid key (401 otherwise). Must stay unset for Voyage, which uses its POST probe. |
| `BRAIN_RERANK_PROVIDER` | unset | Provider routing sent with every request, as JSON: `{"only": ["voyageai"]}`. Keys: `only`, `allow_fallbacks` (default `false`), `data_collection` (`deny` by default, or `allow`), `zdr`. Unknown keys are refused. |
| `BRAIN_RERANK_PROBE_INTERVAL_SECONDS` | `300` | Period of the background probe the MCP server runs, once at startup and then on this interval. |
| `BRAIN_RERANK_BUDGET_SECONDS` | `1.5` | Positive elapsed-time budget for the whole hosted operation: connection, write, read, parsing and backoff sleeps. |
| `BRAIN_RERANK_FALLBACK_MODEL` | empty | Optional second Cohere model, such as `cohere/rerank-4-fast`; must differ from the primary and have a measured calibration. |
| `BRAIN_RERANK_FALLBACK_PROVIDER` | unset | JSON routing for the fallback, such as `{"only": ["cohere"]}`; required on OpenRouter and subject to the same author and privacy policy as the primary. Refused without `BRAIN_RERANK_FALLBACK_MODEL`. |
| `BRAIN_RERANK_FALLBACK_URL` | empty | Fallback Cohere endpoint, such as `https://openrouter.ai/api`; must be an http(s) base URL without credentials, query or fragment. Required with a Voyage primary and a configured fallback; otherwise defaults to the primary URL. Refused without `BRAIN_RERANK_FALLBACK_MODEL`. |
| `BRAIN_RERANK_FALLBACK_API_KEY_FILE` | unset | Fallback key file, with the same format and permissions as the primary key file. Required with a Voyage primary and a configured fallback; otherwise reuses the primary key. Refused without `BRAIN_RERANK_FALLBACK_MODEL`. |
| `BRAIN_RERANK_PRIMARY_BUDGET_SECONDS` | `1.0` | Positive primary-link budget; must be strictly below the overall budget when a fallback is configured. |

With a fallback configured, the primary gets one attempt, with no retry sleep,
bounded by its primary budget. Any HTTP, transport or response parsing failure
hands over to the fallback, which may retry within the overall budget measured
from the start of the primary call. Each configured key file is read once at
startup. A Cohere primary keeps the shared URL, probe path and key unless the
fallback transport is overridden. A Voyage primary requires a separate fallback
URL and key file; an OpenRouter fallback probes `/v1/key`, and another Cohere
fallback probes `/health`. Fallback logits are shifted by the difference between the
two calibrated floor logits so the primary's search floor also applies to fallback
scores after sigmoid. Search falls back to RRF only when both links fail; without
a configured fallback, the single-client behaviour below still applies. The chain
reports the primary's calibration and probe state, and neither hosted calibration
enables feature deduplication signalling.

The key file must be mode `0600` or stricter and have one of two shapes: the bare key value (one token: no whitespace, no `=`, at most
512 characters), or a dotenv-style file with exactly one `BRAIN_RERANK_API_KEY=<value>`
line (optional `export `, quotes, blank lines and `#` comments; every other line is
ignored and never sent anywhere). Anything else is refused, so an env fragment is
never forwarded;
anything readable beyond its owner, malformed, empty, or unreadable stops the runtime at construction with a `RerankKeyError` that
names the path, never the content. Create it the way the shim bearer is created, with
a redirection under a tight `umask`. The cohere and voyage backends never read
`BRAIN_EMBEDDING_TOKEN_FILE`: that file is the shim's bearer and must not reach a
hosted vendor.

When the cohere backend targets `openrouter.ai` (or a subdomain), the routing is
**policed at load time, not just required**: `BRAIN_RERANK_PROVIDER` must be set,
`only` must hold exactly one slug equal to the author prefix of `BRAIN_RERANK_MODEL`
(`voyageai` for `voyageai/rerank-3-lite`), `allow_fallbacks` must be `false`, and
`data_collection` must be `deny` unless `zdr` is `true`. A routing object supplied for any other cohere host must satisfy the same rules.
On OpenRouter the key comes from `BRAIN_RERANK_API_KEY_FILE` only: an inline
`BRAIN_RERANK_API_KEY` is refused there. The health path must be a plain path
starting with `/`, never a URL. A self-hosted Cohere-style server (TEI, Jina, vLLM)
needs no routing and may use an inline key.

The probe logs `reranker_client.unavailable` (WARNING, with the reason `http_<status>`
or `transport_<ExceptionName>`) when it starts failing and
`reranker_client.available_again` (INFO) on recovery, and nothing in between. A
rejected or revoked key shows up there as `http_401` rather than as search results
silently falling back to RRF ordering.

The Cohere and Voyage backends retry HTTP 429, 500, 502, 503 and 504, and connection errors,
at most twice. `Retry-After` accepts seconds or an HTTP-date and is never shortened;
without a usable header, full jitter chooses a delay between zero and
`0.1 * 2**attempt` seconds (the first attempt is zero). If the delay cannot fit in
the remaining budget, no further request is sent. A deadline or exhausted retries
raises `RerankBudgetExhausted`, an HTTP error that makes search fall back to RRF
ordering with `degraded={"rerank_mode": "rrf_fallback"}`. Other statuses and transport
errors fail immediately. The shim retains three busy retries on 503 **with**
`Retry-After`, with each delay capped at two seconds and each request using its own
HTTP timeout. It has no elapsed-time budget; a 503 without the header fails at once.

With MCP metrics enabled, the sidecar's `/metrics` `reranker.by_identity` reports operations,
`operations_by_outcome`, attempts, retries, `status_429`, `status_5xx` and
`budget_exhausted`. Identities are `shim`, `cohere:<model>` or `voyage:<model>` and carry `backend` and
`model` labels. `fallback_rate` is non-ok operations divided by operations (zero
before any operation), recomputed from counters summed across processes for each
identity. `operations_by_outcome` is also summed per outcome. `attempt_latency_ms`
and `operation_latency_ms` include p50/p95/p99 over each process's retained samples
from the last 24 hours; the sidecar takes the maximum per percentile across processes
as a conservative summary, not a percentile of pooled samples. Operation duration
includes backoff sleeps. Operation outcomes are `ok`, `budget_exhausted`, `http_error`,
`transport_error` and `parse_error`. The most recent probe across identities and
processes sets `reranker.last_probe_ok`, `last_probe_reason` and `last_probe_age_s`
(null before the first probe). The sidecar computes age from wall-clock `at`, clamps
it to zero and rounds to 0.1 seconds. Flush data keeps per-identity probe state under
`reranker.last_probe`; the `_reranker` pseudo-tool persists its latest probe as
`{ok, reason, at, identity}` even without operations, along with `by_identity` when
present. Existing legacy metrics retain their meanings; observer failures log once
and never fail a search.

`reranker.chain` counts chain operations separately from link attempts:
`operations`, `served_by` (counts keyed by the serving identity), `handovers`
(primary failures that pass to the fallback), and `failed_all`. A successful
fallback counts as a served operation; it does not count as RRF. Missing chain
observations appear as `{}` on `/metrics`.

`search_rerank` counts each search once, including grouped output:
`rerank_searches` counts all searches, `rerank_eligible` counts searches where at
least one shard actually passed candidates to the reranker, and `rerank_rrf`
counts eligible searches where at least one invoked shard returned `rrf_fallback`.
Empty candidate sets, FTS fallback, backend `none` (`disabled`) and `rrf_only`
are not eligible. Eligibility is measured before result limits and score
filtering, so an invoked search returning no hits can still be eligible. The
acceptance ratio is `rerank_rrf / rerank_eligible`; with a zero denominator it is
undefined, not evidence that reranking succeeded.

The MCP collector snapshot and flush store these search counters under `search`;
the flusher persists them as the `_search_rerank` pseudo-tool and chain counters
inside `_reranker`. The sidecar reads both from the single `_process` row:
`agent_name` is the primary key and each upsert replaces that row, so these
counters are never summed across agents. `search_rerank.emitter_started_at` is
the emitter's construction time (`time.time()`), stable for its process lifetime
and persisted even before its first search. Readers reset their baseline when
this generation changes, never merely because a counter drops. Missing search
observations appear as `{}`; the sidecar never substitutes its own generation.

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

## PostgreSQL timeouts

Every connection of the shared engine (`brain_v42.db.engine`) carries session budgets, sent
as asyncpg `server_settings` at connect time. All values are milliseconds; `0` disables, as
in PostgreSQL. Each one is read from the environment (`BRAIN_PG_*`, the bare name also
works), so retuning a unit needs a drop-in and a restart, not a release.

| Profile | Used by | statement | lock | idle in transaction | `application_name` |
|---|---|---|---|---|---|
| interactive | MCP server, codex gateway, automation runtime | 120000 | 30000 | 300000 | `brain-v42-interactive` |
| maintenance (default of `get_engine()`) | maintenance jobs, repair and backfill scripts | 1800000 | 300000 | 0 | `brain-v42-maintenance` |
| metrics | sidecar scrape path | 10000 | 5000 | 60000 | `brain-v42-metrics` |
| delivery observer | observer loop (constants) | 15000 | 5000 | 60000 | `brain-v42-delivery-observer` |

Environment names: `BRAIN_PG_STATEMENT_TIMEOUT_MS`, `BRAIN_PG_LOCK_TIMEOUT_MS`,
`BRAIN_PG_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS` (interactive);
`BRAIN_PG_MAINTENANCE_STATEMENT_TIMEOUT_MS`, `BRAIN_PG_MAINTENANCE_LOCK_TIMEOUT_MS`,
`BRAIN_PG_MAINTENANCE_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS` (maintenance);
`BRAIN_METRICS_PG_STATEMENT_TIMEOUT_MS` (metrics). The metrics lock and idle budgets and the
observer's budgets are constants.

- Only the long-lived processes opt into the tight profile, in their entry point
  (`python -m brain_v42.mcp.server`, the codex gateway launcher). Anything that calls
  `get_engine()` bare gets the generous maintenance profile; the maintenance profile has no
  idle limit because some jobs hold a transaction open on purpose (the embedding-backfill
  advisory-lock session).
- A non-zero idle-in-transaction budget is raised at engine build to the longest external
  client timeout (`BRAIN_EMBEDDING_TIMEOUT`, `RERANKER_TIMEOUT`, `NEO4J_TIMEOUT`) plus 30 s,
  because some jobs (the dedup job) hold a transaction across a rerank
  call. So `BRAIN_EMBEDDING_TIMEOUT=600` yields at least a 630000 ms idle budget on every
  profile; `0` stays `0`.
- `scripts/dream/*.py` build their own engines and stay unbounded on purpose: they are batch
  jobs.
- The sidecar's legacy dedup job, if ever enabled, shares the metrics budget (10 s).
- `alembic/env.py` forces `statement_timeout` and `idle_in_transaction_session_timeout` to 0
  for migrations, so they never inherit a short budget, even from a future
  `ALTER ROLE` / `ALTER DATABASE ... SET`.
- Neo4j: the driver gives up connecting after 5 s and waiting for a pooled connection after
  10 s (driver defaults: 30 s and 60 s). Per-query budgets are `NEO4J_TIMEOUT`.

A request cut by a budget fails with SQLSTATE `57014` (statement timeout), `55P03` (lock not
available) or `25P03` (idle-in-transaction session timeout). To retune, read the slowest
statements from `monitoring.pg_stat_statements` (`max_exec_time`, `calls`) after a week of
traffic; the view needs `shared_preload_libraries = pg_stat_statements` on the server and USAGE
on schema `monitoring` (`pg_read_all_stats` only to see other roles' query text).

## Migration history

The repository migration target is 068. No page in this repository proves a live
schema head; read `alembic_version` in the target database.

Migration 068 adds six partial `merged_into` indexes and six deferred constraint
triggers on `learnings`, `decisions`, `snippets`, `runbooks`, `adrs` and
`features`, sharing `public.refuse_merge_chain()`. The upgrade refuses existing
merge chains: run `scripts/repair_merge_chains.py` before migrating. Trigger
invocation requires no new runtime EXECUTE grant.

Migration 067 adds nullable `brain_client_credentials.allowed_projects` without a
default or backfill. `NULL` preserves unrestricted scope. Change exact project keys
in place with `brain-credentials scope <id> --projects brain-v42,red-shrik:agent-a
--reason "restrict projects"`; restore `NULL` with `brain-credentials scope <id>
--clear --reason "restore unrestricted scope"`. The CLI canonicalizes known aliases,
rejects empty or duplicate keys and keys longer than 50 characters, and never prints
a bearer for this operation. Both `list` and `list --json` show `allowed_projects`.
Each change commits a `credentials.scope_changed` outbox record with the old and
new scope, author and reason; repeat changes are separate audit rows. The existing
drainer includes `audit_id` on scope events so consumers can deduplicate retries
without dropping subsequent changes to the same credential. The
NOTIFY trigger reloads the verifier after commit, with periodic refresh as fallback.
The own-focus grant requires `write` and an exact allowed key; ordinary writes keep
their existing family authorization. Downgrade refuses while any credential is
scoped unless `-x allow_credential_scope_downgrade=yes` explicitly accepts widening
authority. The scope audit vocabulary and rows survive downgrade; an older drainer
that cannot render these events requires an operator upgrade before draining them.
Recovery contract v26 is the candidate for 068. Mint both SQL variants on disposable
databases, advance the manifest and ACL identity, then bind final assets through
`ops/recovery/current.json`. The restored twin starts from a custom-format dump at
067, restored with `pg_restore --no-owner --no-acl` and migrated to 068. Replay the
contract against the target before deploying this schema.

`brain-v42-migrate` uses the owner credentials from `POSTGRES_URL` and the
wheel's Alembic chain. It refuses an uninitialized database and any head newer
than or unknown to the image. An ancestor is upgraded to the image head and
recorded in `brain_schema_compat`, with the oldest compatible code head pinned
to that same head. Equal heads skip the upgrade. Every successful run proves
the image's own recovery binding; a rollback across a schema change is refused.

Migration 064 owns the runtime ACLs. After upgrading, the CLI requires the
existing `brain_app` role and configures LOGIN, restricted role attributes,
and the interactive pool's connection cap plus 20% headroom. Its three role
timeouts use `db/engine.py`'s interactive profile. Set `BRAIN_APP_PASSWORD_FILE`
to a readable, non-empty secret file; the password is never accepted on argv.
The CLI computes a fresh SCRAM-SHA-256 verifier locally (4096 iterations,
16-byte random salt) and sends only that verifier to PostgreSQL on each run,
so secret rotation never requires a schema migration.

R1 decides whether `brain_app` needs `pg_catalog.pg_control_system()`.
`BRAIN_APP_GRANT_PG_CONTROL_SYSTEM=true` enables that optional EXECUTE grant;
it is disabled by default and is the CLI's sole object-ACL exception, outside
the public schema contract. Exit codes are 0 for success, 1 for refusal/failure,
and 2 for invalid CLI usage.

### Rotate the Compose PostgreSQL passwords

On the Compose host, run as root from the repository:

```bash
sudo python3 scripts/rotate_postgres_role_passwords.py
```

This rotates `brain` and `brain_app` together, then atomically replaces the two
value-only password files, both PostgreSQL env files and the observer DSN under
`/etc/brain-v42`, preserving ownership, mode 0600 and unrelated observer lines.
Before changing PostgreSQL it saves the original five files in
`/root/pg-rotation-<UTC_TIMESTAMP>/` (mode 0700). Output contains only `ok`, the
rewritten paths and that backup directory. `--dry-run` validates without writing;
it also checks connectivity with the same `docker exec -i ... psql` command,
sending only `SELECT 1;` on stdin. A failed connection returns non-zero without
generating passwords or writing files. Each file path, `--container` and
`--backup-root` can be overridden.

For rotation and restore, the script computes SCRAM-SHA-256 verifiers locally
(RFC 5802 / RFC 7677, 4096 PBKDF2-HMAC-SHA-256 iterations, fresh 16-byte random
salt for each password). It sends only verifiers in `ALTER ROLE ... PASSWORD`;
plaintext passwords and their URL-encoded forms never reach the PostgreSQL server.
PostgreSQL stores these pre-hashed verifiers as supplied. Before `BEGIN`, the
same owner-superuser session also runs all four settings:

```sql
SET log_statement = 'none';
SET pg_stat_statements.track_utility = off;
SET log_min_duration_statement = -1;
SET log_min_error_statement = panic;
```

These suppress utility tracking and statement logging for the rotation session:
`pg_stat_statements.track=all` would otherwise retain `ALTER ROLE` literals.
Neither passwords nor verifiers are placed in process arguments or the child
environment, and echoed credentials are redacted from failure diagnostics.

Immediately restart consumers through `rail deploy`, explicitly recreating
`delivery-observer`: its mounted secret does not participate in Compose's recreate
hash. Verify authenticated connections as both roles using the installed files
without displaying their contents, MCP readiness, consumer/container health,
observer heartbeat freshness and absence of authentication failures in logs.
Confirm all five files still have their original uid/gid and mode 0600.

For rollback, run `sudo python3 scripts/rotate_postgres_role_passwords.py --restore
<UTC_TIMESTAMP>` with the timestamp from the backup directory (and the same path
overrides, if used), then repeat `rail deploy`, explicit observer recreation and
the verification list. Restore reapplies the backed-up passwords before restoring
the files. Backups remain after failures; use restore if a filesystem failure or
interruption left only some files replaced. File replacements are individually
atomic; the database transaction and five replacements are separate steps.
If a file write fails after the database commit, the error explicitly states
that PostgreSQL already holds the new passwords and prints the backup directory
and the exact `--restore <UTC_TIMESTAMP>` command to recover. A failed restore
file write similarly reports that the database already holds the restored passwords.

Recovery contract v20 is the recovery asset for the schema through 062. It includes
the session connection ledger from 061 and the 062 monitoring extension inventory
(`pg_stat_statements`, `plpgsql`, and `vector`), along with the expected database
objects and ACL shape. Before applying migrations to a production database, follow
the migration and restore gates in the recovery procedure; the migration number
alone is not proof that a usable restore exists.

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

Migration 062 adds two partial indexes on `delivery_confirmations`
(`idx_delivery_confirmations_binding_errors`, `idx_delivery_confirmations_context_errors`).
The observer's failure count reads them once the set-based queue change
(`feat/delivery-queue-summary-retention`, which merges first) is in: its binding branch
must repeat `subject_kind = 'artifact_binding'` for the planner to use the partial index.
062 also creates `pg_stat_statements` in a dedicated
schema, `monitoring`. It must not be created in `public`: the extension grants SELECT on its
view to PUBLIC, and recovery contract ACL v11 counts that as a mismatch. Facts to know before
applying it:

- **Lock.** Plain `CREATE INDEX` inside the migration transaction takes a SHARE lock, so the
  observer's inserts wait; on about a hundred thousand rows this is well under a second, and
  the revision sets `lock_timeout = 30s` so a stuck lock holder fails the migration atomically.
  The observer's own inserts carry a 5 s `lock_timeout` (SQLSTATE `55P03`): stop
  `brain-v42-delivery-observer` while `alembic upgrade 062` runs, or accept one lost pass.
- **Relocation.** If `pg_stat_statements` already exists in another schema (for instance
  `public`), the migration moves it with `ALTER EXTENSION ... SET SCHEMA monitoring` (it is
  relocatable, and this needs the extension owner) instead of leaving it where ACL v11
  refuses its PUBLIC grants.
- **Superuser.** `pg_stat_statements` is not a trusted extension. Under a least-privilege
  migration role, a superuser pre-creates it first:
  `CREATE SCHEMA monitoring; CREATE EXTENSION pg_stat_statements WITH SCHEMA monitoring;`.
  The migration then skips both steps (the schema step checks `pg_namespace` first, because
  `CREATE SCHEMA IF NOT EXISTS` demands the CREATE privilege on the database even when the
  schema exists). Reading `monitoring.pg_stat_statements` needs USAGE on `monitoring`;
  `pg_read_all_stats` is only needed to see other roles' query text.
- **Preload.** Creating the extension does not need `shared_preload_libraries`; reading the
  view does (SQLSTATE `55000` otherwise).
- **Contrib files.** The server image must ship the contrib extension files, and so must the
  image that restores a dump: a 062 dump carries `CREATE EXTENSION pg_stat_statements`. The
  red-backup restore sandbox must be PostgreSQL 16 with contrib (`pg_stat_statements 1.10` is
  pinned in the v20 extension inventory).
- **Downgrade.** Drops the extension, then the schema (RESTRICT): an object of your own in
  `monitoring` stops it with an error instead of being dropped. It drops both even when a
  superuser pre-created them and needs the extension owner, so a rollback to 0.6.7
  (downgrade 061) needs a superuser. The downgrade runs under a 30 s `lock_timeout` and rolls
  back entirely if a concurrent transaction holds `delivery_confirmations`. The statistics live in shared
  memory, so nothing durable is lost.

The 038→039 cutover was conducted with
[`docs/PLAN_INDEX_REPAIR_RUNBOOK.md`](PLAN_INDEX_REPAIR_RUNBOOK.md) (isolated restore,
migration, repair, restart-last gates).

## Graph ledger cutover evidence

Migration 033 adds the relational ledger and outbox. Migration 034 adds projector
fencing v2 with PostgreSQL generations and claims plus Neo4j fence and cursor checks.
Migration 035 adds a crash-safe, resumable interlock for offline projection recovery.
Configuration controls whether the canonical ledger path is enabled in an
environment. The [graph ledger runbook](GRAPH_LEDGER_RUNBOOK.md) describes the
required evidence and recovery procedure.

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

## Knowledge lifecycle CLI

Run `python -m brain_v42.lifecycle_cli`, the composition root above facts,
repositories and services. The spec's `python -m brain_v42.knowledge.lifecycle`
command is not provided because the pure knowledge package sits below
orchestration in the module layering. It must not depend on orchestration through
imports or a subprocess shim.

The code defaults are `BRAIN_KNOWLEDGE_LIFECYCLE_ENABLED=false` and
`BRAIN_KNOWLEDGE_LIFECYCLE_DRY_RUN=true`. The first refuses every command except
`report`. The second refuses apply runs, resuming apply runs, and all invalidations.
Read the invocation's actual environment to establish its arming state.

Commands are `run --mode {dry-run,apply} --scope {all|PROJECT_KEY}
--ceiling-usd AMOUNT`, `resume --run RUN_ID`, `invalidate --run RUN_ID --reason
TEXT`, and `report --run RUN_ID`. All accept `--json`; reports go to stdout,
structured logs to stderr, and entry text is never rendered. Exit codes are 0 for
completion, 2 for a refusal, 3 for a failed execution, 4 for an occupied run lock,
and 5 for a batch still processing; resume later. Exit 5 prints the durable report
and leaves the run submitted without result checkpoints. `run` and `resume` accept
`--max-wait-seconds` (positive integer, default 7200) to bound each batch wait.
Apply, resuming apply, and invalidate require `--backup-run-id ID` and
`--backup-sealed-at ISO_8601_UTC`. The seal must be timezone-aware UTC, no older
than one hour, and not in the future. The report states: "backup proof declared
by the coordination, not re-read". No backup is restored by this command.

The write target uses the existing `BRAIN_FACTS_PRODUCTION_IDENTITY` declaration:
cluster system identifier, database name and server port, with an optional pinned
server address. Missing, unreadable or mismatching identity refuses writes.
GitHub REST resolves main once per evaluation snapshot; one codeload archive of
that exact SHA provides both paths and Python identifier tokens. Archives are
also pinned to the recorded SHA when resuming an interrupted evaluation. They are
bounded, extracted into a temporary directory after validating every member,
and never executed. Pull requests are paginated with `state=all`. Tickets are
read in a read-only database transaction. Facts use the existing identity-bound
fact registry; a CLI outside a declared immutable release cannot measure
`live_release_sha`. Source failures make affected checks incomplete.

Optionally set `BRAIN_KNOWLEDGE_LIFECYCLE_GITHUB_TOKEN_ENV` to the **name** of an
environment variable containing a GitHub credential. It is sent only to GitHub
REST and never logged. The extraction SDK owns its standard environment handling;
the CLI never reads its API key. The operator-only `dream.nightly` decision has
no machine-readable binding in this adapter and remains unknown rather than
being inferred from prose or deployment flags.

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

## Delivery observer: observation cadence

A successful observation sets when the subject is looked at again:

- an open pull request (no `integration_sha` yet): every
  `BRAIN_DELIVERY_POLL_SECONDS` (default 60), so a merge is seen within a minute;
- an integrated pull request and a ticket's repository contexts: after
  `max(POLL_SECONDS, FRESHNESS_SECONDS // 2 - <duration of the collection>)`, 300 s at the
  defaults for a quick collection. Before its next confirmation lands, evidence ages by
  that delay, up to one poll interval before the loop picks the job, and the next
  collection, which is sequential requests plus admission waits and can last minutes
  for a large context set. Waiting the whole window would leave it stale at the end of
  every cycle and refuse a resolve in that gap; subtracting the collection keeps a
  uniformly slow one no worse than the poll cadence.

With `D_prev` and `D_next` the durations of two consecutive collections, the next
confirmation of a settled subject lands at age:

- `FRESHNESS_SECONDS / 2 + POLL_SECONDS + (D_next - D_prev)` while
  `FRESHNESS_SECONDS / 2 - D_prev >= POLL_SECONDS`: evidence stays fresh while a collection
  slows by at most `FRESHNESS_SECONDS / 2 - POLL_SECONDS` (240 s at the defaults) against the
  previous one, for example when GitHub's responses go from one to nine seconds across a
  large context set;
- `2 × POLL_SECONDS + D_next` once the delay is floored at the poll interval (a previous
  collection longer than 240 s at the defaults): the poll cadence's own bound, fresh while
  `D_next <= FRESHNESS_SECONDS - 2 × POLL_SECONDS` (480 s).

Past either bound evidence reads stale for at most one cycle: a resolve in that window is refused
with `delivery_requirements_unsatisfied` until the next confirmation lands, and nothing is
lost. This residual is accepted (coordination ruling, 2026-10-13): no cadence slower than
the poll interval could tolerate an arbitrary slowdown.

The observer paces itself with its own `BRAIN_DELIVERY_FRESHNESS_SECONDS`, while the MCP
server judges freshness with its own: give both processes the same value. An observer
value above the server's leaves evidence stale between two observations.

A failed observation retries on its own schedule. An outage, a rate limit or an invalid
response backs off up to 300 s, or longer when GitHub names a longer `Retry-After` (capped
at an hour). A repository or pull request the App cannot see (`provider_not_found`,
`provider_forbidden`) waits `300 × 4^n` seconds, `n` being the failures since its last
success (300 s, 20 min, 80 min, 5 h 20, 21 h 20, then a day), so a forgotten installation
case stops spending the shared request budget. From its fourth
consecutive failure the observer also writes an `observation_backoff` line on stderr
(`subject`, `error_code`, `prior_failures`, `delay_seconds`): that subject now waits on an
operator fix. Once it is fixed, `brain_delivery_refresh` on the ticket makes its
contexts and active bindings due at once instead of waiting out the backoff.

## Embedding backlog runner

Every knowledge write commits its row first, with a NULL vector, and embeds it after
the commit. When the embedding provider fails, the row stays in the durable backlog:
rows whose vector is NULL or has a near-zero norm. Full-text search still finds such a
row, semantic search does not, until something re-embeds it. That something is
`python -m brain_v42.maintenance.embedding_backfill`, and only that: the MCP server
runs no backlog loop of its own.

**On red-base** the `embedding-backfill` Compose service runs it once at start and then
every hour, over every project and every entity type. Whether it writes is declared on
its command line (`--execute` or not) in `deploy/compose.yaml` and changes only through
a pull request followed by a rail deploy. Dry, a run only counts the backlog and prints
its JSON report to `docker logs brain-v42-embedding-backfill`; it calls no provider and
writes nothing. Arming it is not a one-flag change: with `--execute` the command also
builds the feature and graph linkers, so the service then needs the graph environment
as well. A failed run is retried the next hour; the healthcheck proves only that the
loop process exists, not that a run completed. The sidecar's
`database.embedding_backlog` block is where the backlog's size is measured.

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

Observable delivery proves the independence of the source (GitHub) and of the
issuer identity, not the isolation of keys on the host: the host is the trust
boundary (operator decision of 2026-10-05, option (a) of ticket `fd0df7d6`). On
red-base (`deploy/compose.yaml`) the observer runs in its own container under the
uid every brain-v42 service uses (`1000:1000`), and its two file secrets, the
configuration (`/etc/brain-v42/delivery-observer.env`, mode 0600) and the GitHub
App key (`/etc/brain-v42/github-app-key.pem`), are mounted only in that container. That separation is a property of this deployment, not something the
proof depends on. A dedicated system user for the observer remains a possible
later hardening.

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

## Delivery observer: liveness

Two signals say whether the observer is still observing, not merely running.

**Heartbeat (container health).** With `BRAIN_DELIVERY_HEARTBEAT_PATH` set (Compose sets
`/tmp/brain-v42-delivery-observer.heartbeat`), the observer rewrites that file, atomically,
after every completed cycle, after every persisted observation attempt, and while the
release and deployed passes page through their candidates. A cycle cut short by ownership
loss or a shutdown does not refresh it at its end (a release pass may still refresh once
before it notices, and the process exits right after). The `delivery-observer` healthcheck fails when the
file is missing or older than 600 s (start period 120 s), so `brain-v42-delivery-observer`
turns `unhealthy` and red-agent reports it. A failed write never stops observation; it is
reported as one `observer_heartbeat_write_failed` JSON line on stderr with the error type.

**Fact `delivery_observer_liveness` (briefing).** Measured on the production database:
`lock_held` (the observer's session advisory lock is granted to a backend of this
database), `overdue` (subjects the observer queue considers due for more than 600 s),
`last_confirmation_age_seconds` (newest confirmation, any outcome; null when none) and
`healthy` = lock held and nothing overdue. A dead observer shows as `lock_held = false`; a
live but stalled one as `overdue > 0`. Read it with
`brain_fact_get("delivery_observer_liveness")`.

## Delivery confirmation retention

The maintenance CLI keeps every error confirmation (observer backoff depends on
them), every snapshot, and every success referenced by a binding or workflow's
latest attempt/success pointer. It also protects confirmation IDs named under
`success_confirmation_id` or `latest_attempt_confirmation_id` anywhere in receipt
proofs or event payloads/results, including nested JSON arrays.

Preview the eligible successes older than 14 days:

```bash
python -m brain_v42.maintenance.delivery_confirmation_retention
```

Arming is an operator gesture; take it after a red-backup dump. No timer ships.
After reviewing the dry report, explicitly arm deletion:

```bash
python -m brain_v42.maintenance.delivery_confirmation_retention --execute
```

The window is at least seven days (`--older-than-days`, default 14). Each transaction
deletes at most `--batch-size` rows (default 5000, range 1–50000); `--max-batches`
defaults to 100. Locked candidates are skipped. The report names the cutoff,
candidates and deleted rows: dry mode counts all eligible rows; execute mode counts
the rows selected within its batch budget. A later invocation can process remaining
rows. Exit codes are 0 for success, 1 for runtime failure, and 2 for invalid arguments.
Earlier batches remain committed if a later batch fails.

Receipt issuance freezes its pointer under `lock_workflows`, the observer never
moves pointers back to old confirmations, and receipts require evidence fresh
within ten minutes. This makes the old retention candidates safe without a workflow
lock. Measure the confirmation table's row count before and after retention on the
target database:

```sql
SELECT count(*) FROM delivery_confirmations;
```

Deleted successes remain recoverable from the preceding backup dump.

## Agent trace and graph outbox retention

`python -m brain_v42.maintenance.trace_retention` previews old, unreferenced closed agent traces and delivered graph outbox rows; deletion is dry by default and requires `--execute`. Trace retention has a 30-day minimum window, and outbox retention has a seven-day minimum (both default to 30 days). It keeps open and operator sessions, traces referenced by artifacts, checkpoints, slot history or relay children, and every undelivered outbox row. Batches default to 5000 rows, with at most 100 batches per purge. A dry run counts every candidate; an execute run is capped at `--max-batches` × `--batch-size` rows. Locked candidates are skipped, and later invocations can process the remainder.

## Historical merge chain repair

The repair must run before the release carrying migration 068 is deployed; coordination holds that deployment until the repair is complete.

`python scripts/repair_merge_chains.py` (or
`python -m brain_v42.scripts.repair_merge_chains`) inventories merge chains in
learnings, decisions, snippets, runbooks, ADRs and features. The default is a dry run in a
read-only transaction and emits one JSON report. After reviewing that report,
an explicit operator go is required to run `--execute` with repeated
`--expect TABLE=N` and `--expect-cross TABLE=N` approvals, for example
`--execute --expect learnings=3 --expect features=1 --expect-cross learnings=2`.
Each `N` must equal that table's measured `in_project_chained_count` or
`cross_project_chained_count`, respectively. Both options accept only ASCII digits.
An omitted table expects zero in that category; execution requires at least one
`--expect` or `--expect-cross`, and may use only `--expect-cross` when all in-project
counts are zero. Both counts on all six tables are checked before any write.
`expected_count_mismatch` identifies the table in the refusal's `table` field and
the category (`in_project` or `cross_project`) in its `count_kind` field.
The report retains totals and per-table counts for both categories, with each
row's ID, current `merged_into` and root in the two chain lists.
Execution flattens all approved pointers to their roots, writes a `repointed`
consolidation_log row per knowledge change and stages `MERGED_INTO` relations in
the PostgreSQL ledger/outbox in the same transaction; it never writes Neo4j
directly. Features write no consolidation_log rows: their audit is the JSON receipt
plus the graph ledger tombstones written by the registry trigger, as indicated in
the report's `audit` field. Save the execution JSON as the receipt. The features
self-reference has no ON DELETE action; repair only updates pointers and deletes
no feature rows. Cycles, dangling roots, depths beyond 64, a changed per-table
count or failed final verification refuse the repair. The final inventory must
contain zero chains of either kind; otherwise `chains_remain` rolls back all
changes. The expected result is `merge_chains = 0` on every one of the six tables.
The repair takes a SHARE ROW EXCLUSIVE lock on all six tables for its
duration and refuses after 5 s if it cannot acquire it. Reads proceed, but every
write to these tables waits, including the access-count flush (which updates
freshness counters), new knowledge writes and feature writes, to keep the approved
inventory stable.
Chains rooted in another project are flattened too, under their own `--expect-cross`
count: they already point into that project indirectly, so pointing directly at
the same root adds no new reachability. This applies only to the admin repair;
project-scoped consolidation still refuses merges that would strand foreign children.
`transits_other_project` counts rows whose path visits another project, including
chains returning to their own project. All changes commit together or
roll back together. The database URL comes only
from the existing settings (`POSTGRES_URL`), never from a CLI argument.

## Automation service

The `brain-v42-automation.service` unit is generated and verified, but stays dormant.
It listens on `AUTOMATION_PORT` (default 9201), loopback-only. The cutover without
dual-run, lease proof and rollback are described in the
[systemd runbook](../deploy/systemd/README.md). Deploying the code with
`METRICS_LEGACY_AUTOMATION_ENABLED=true` does not change the live automation owner.

On red-base the Compose file sets `METRICS_LEGACY_AUTOMATION_ENABLED=false` and no
automation service runs, so the legacy automation lease and the jobs it carried, among
them the six-hourly `feature_dedup`, run nowhere. This is accepted (operator ruling of
2026-10-13): the roadmap feature those jobs serve is being retired (ticket `0f008a1c`).
The legacy lease path stays in the code, dormant, until that retirement removes it; do
not re-enable it to bring `feature_dedup` back.

## Codex gateway

The Codex gateway is an optional companion service. Activation is blocked while the
PostgreSQL `codex_ro` and `brain` credentials use development defaults. Its `/ready`
check validates the SQL contract, including `security_barrier` on views scoped to the
red group; the gateway is not ready when that contract fails.

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
