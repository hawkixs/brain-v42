---
title: "ARC1 lot 1 — separate metrics and automation"
status: completed
plan_kind: implementation
summary: "Extract GitLabIngestor and FeatureDedupJob into a dormant brain_v42.automation runtime, keep an explicit legacy rollback, and prove lifecycle independence without touching BrainService or the MCP surface."
tags:
  - arc1
  - architecture
  - automation
  - metrics
  - pattern-auto
  - reversible-cutover
---

# ARC1 lot 1 — separate metrics and automation

## Purpose and scope

This plan executes the first bounded lot of ARC1 in the Sol Ultra roadmap. It deals only
with the availability coupling between the metrics sidecar, the GitLab webhook, and the
periodic feature deduplication.

The lot does not close ARC1 globally. Typing `build_services()`, grouping `BrainService`'s
fifteen dependencies, and pulling SQL out of the MCP handlers remain deferred.

Starting point: `main` at `8c36fec912f5fc31c375f03096ca5b184cb28f89`.
Branch: `feat/arc1-automation-runtime`.
Merge/push base and target: `main` to `origin/main`, no force.

### pattern-auto authorization gate

The `ok go focus` command authorizes focus preparation, but does not by itself constitute
explicit authorization for a direct merge and push to `main`. Before Task 1, the user must
confirm `pattern-auto` or end-to-end autonomy including merge and push. Without this
confirmation, the deliverable stops at the isolated branch and the uncommitted plan: no
task commit, merge, or push is performed.

**Gate lifted on 2026-07-15:** the user explicitly confirmed `pattern-auto`.

After confirmation and before the first RED:

1. attach a first durable artifact to the stable feature
   `ARC1 lot 1 — separate metrics and automation`;
2. verify that ClusterGuard created a dedicated feature, with no ambiguous attachment to
   the global ARC1;
3. apply `brain_feature_update(..., status="building")` and verify the roadmap;
4. keep the global ARC1 open regardless of this lot's final status.

## Proven initial state

`python -m brain_v42.metrics` currently holds three responsibilities:

1. serve `GET /metrics` and `GET /api/cockpit`;
2. conditionally serve `POST /gitlab/webhook`;
3. run `FeatureDedupJob` every six hours.

The success of the `:9200` bind gates the start of deduplication, and the same signal
stops all three responsibilities. No `brain_v42.automation` package nor versioned systemd
unit exists. The `brain-metrics.service` unit is host-local and is not a reproducible
source in the repository.

The external webhook is currently decommissioned. The lot does not recreate it, does not
change the loopback-by-default bind, and performs no deployment.

## Contracts to preserve

### Metrics and cockpit

- `GET /metrics` and its JSON stay unchanged.
- `GET /api/cockpit` and its JSON stay unchanged.
- The embedding healthcheck and the Neo4j healthcheck stay owned by the metrics runtime.
- The `process_metrics` / `search_log` cleanup stays owned by the metrics runtime.
- No public constructor change of `MetricsServer` is required.

### GitLab webhook

The same handler must serve both the legacy path and the new runtime:

- route absent without an ingestor;
- empty secret: `401 Webhook authentication not configured`;
- missing or incorrect token: `401 Invalid token`;
- missing `X-Gitlab-Event-UUID`: `400`;
- unknown project: `200 {"status":"unknown_project","path":"..."}`;
- success: exact call `process_event(payload, event_uuid, project_key)` and unchanged JSON;
- no new exception capture or payload rewrite.

The resolver keeps reading `project_contexts.gitlab_project_path`. The MCP surface that
feeds this column and the one that reads the roadmap do not change.

### Deduplication

- first pass after the delay, not at startup;
- default interval of 21,600 seconds;
- traversal of all project keys;
- one transaction and one commit per merge;
- `consumed_ids` protection unchanged;
- `CancelledError` propagation and logging of any other exception;
- same `dedup_loop.*` event names;
- ownership barriers after discovery, after merge, around the commit, and before
  advancement or durable logging.

The automation and legacy metrics builders inject `lease.ensure_owned` into
`FeatureDedupJob`. The job re-embeds after `SELECT ... FOR UPDATE` and before any DML,
checks ownership outside the best-effort embedding handler, then before and after each
SQL await. `feature_dedup.merge_staged` denotes an uncommitted transaction; only
`dedup_loop.merged` signals guarded post-commit progress.

### Compatibility and single ownership

- `METRICS_LEGACY_AUTOMATION_ENABLED=true` by default: deploying the code alone does not
  change the live owner.
- A non-blocking PostgreSQL advisory lease, with no migration, protects the effective
  owner. It uses a signed, stable `bigint` key, and a dedicated `AsyncConnection` in
  `AUTOCOMMIT`. Acquisition, heartbeat, and release use this same session and the same
  `pg_backend_pid()`. Metrics keeps running without automation if the lease is held; the
  new runtime fails explicitly.
- A watcher bounded by interval and timeout checks the connection and its PID without
  recalling `pg_try_advisory_lock`. Any loss or invalidation fires `ownership_lost` exactly
  once: the scheduler cancels itself and the webhook becomes fail-closed. No runtime
  attempts to reconnect or reacquire before its restart. The new runtime stops its server
  and exits non-zero; the legacy metrics path stays metrics-only.
- Release cancels then waits for the watcher, calls `pg_advisory_unlock` on the original
  connection, and requires `true`. A `false` response, an error, or a cancellation
  invalidates the physical connection before it closes. Cleanup is idempotent and protects
  this release from external cancellation.
- `brain-v42-automation.service` is generated and verified, but never enabled nor started
  by the installer.
- The documented cutover forbids dual-run: the legacy owner is stopped before the new
  runtime starts.
- The rollback restores the legacy path before any external reactivation of the webhook.

## Chosen architecture

### Bounded context `brain_v42.automation`

The new package contains:

- `webhook.py`: single HTTP handler, independent of the server hosting it;
- `server.py`: automation aiohttp server, limited to `GET /health` and
  `POST /gitlab/webhook`;
- `ownership.py`: single-owner PostgreSQL advisory lease;
- `dedup.py`: the moved periodic loop, with ownership barriers around mutation
  boundaries and the commit;
- `runtime.py`: typed composition and explicit lifecycle ownership;
- `__main__.py`: `python -m brain_v42.automation` entrypoint and SIGINT/SIGTERM handling;
- `__init__.py`: minimal package surface.

A composition dataclass carries the concrete types needed by the bounded context:
`AsyncEngine`, session factory, lease, embedding, reranker, ingestor, job, and server. It
replaces the local bundle of variables and enables deterministic shutdown. The shutdown
order is: cancel/await dedup, stop/drain aiohttp, close embedding and reranker, release the
lease, then dispose the engine.

`AutomationServer` bounds the aiohttp drain to 10 seconds with
`AppRunner(shutdown_timeout=10.0)`, a contract available from the minimum supported floor
`aiohttp>=3.9`. The unit keeps `TimeoutStopSec=30`, a nominal margin of 20 seconds to close
embedding, reranker, release the lease, and dispose the engine.

The lease exclusively uses its dedicated `AsyncConnection` for acquisition, heartbeat, and
release. It exposes a typed `ownership_lost` state and event; no other pool checkout can be
used to claim the lock is still held. Wrappers check this state after authentication and
immediately before `process_event`. The handler translates an authenticated ownership loss
into `503 {"status":"ownership_lost"}` without calling the business logic. An
unauthenticated request stays `401`.

The advisory lock is not a fencing token. It guarantees an owner as long as the session
stays healthy, and a fail-closed after bounded detection; it cannot interrupt a mutation
already committed to PostgreSQL at the exact moment of a network outage.

### Legacy facade

`MetricsServer` keeps its signature and its conditional route. Its implementation delegates
processing to `GitLabWebhookEndpoint`, which guarantees a single HTTP contract without
duplicating the logic. `metrics.__main__` only builds the business composition and starts
the loop if `metrics_legacy_automation_enabled` is `true`.

When this flag is `false`, the metrics process builds only the collector, health embedding,
health graph, metrics server, and cleanup. The webhook route is then `404`. When the flag
is `true` but the lease is already owned by the new runtime, metrics logs the conflict and
continues with only these infrastructure responsibilities. Legacy lease acquisition is
bounded to two seconds: an unavailable database cancels the attempt and also starts
metrics-only, with no legacy webhook or scheduler.

The metrics lifecycle becomes controllable through a `stop_event` in
`brain_v42.metrics.runtime`, called by `metrics.__main__`. This seam remains the real
production path: it owns the server, cleanup, legacy lease, dedup, and shutdown. On legacy
lease loss, it cancels dedup and makes the webhook handler fail-closed without stopping
`/metrics` or `/api/cockpit`.

The historical fallback stays characterized: an `ImportError` on the webhook component
disables the webhook without disabling deduplication. The composition therefore separates
the mandatory job from the optional webhook instead of merging their two failure modes.

### New runtime

The automation runtime:

1. builds the typed business dependencies;
2. acquires the lease; a conflict produces a non-zero configuration exit;
3. starts the automation server on `127.0.0.1:9201` by default;
4. fails if the bind fails, so systemd sees a red startup;
5. starts the deduplication loop only after a successful bind;
6. waits for an explicit signal;
7. always runs the full cleanup in a `try/finally`, including after a red bind.

`GET /health` is a process liveness signal. It is not added to the MCP surface and does not
claim to validate the DB, embedding, reranker, or scheduler progress.

Exact configuration, loaded by `Settings`:

- `automation_host` / `AUTOMATION_HOST`, default `127.0.0.1`, strictly validated as
  loopback;
- `automation_port` / `AUTOMATION_PORT`, default `9201`, bounded to `1..65535`;
- `automation_dedup_interval_seconds` / `AUTOMATION_DEDUP_INTERVAL_SECONDS`, default
  `21600`, strictly positive;
- `metrics_legacy_automation_enabled` / `METRICS_LEGACY_AUTOMATION_ENABLED`, default `true`.

### Dormant unit

`deploy/systemd/brain-v42-automation.service.tmpl`:

- launches `.venv/bin/python -m brain_v42.automation`;
- loads the existing `.env`;
- logs under `brain-v42-automation`;
- has crash-loop limits, restart, and stop timeout;
- declares no `Requires=`, `Wants=`, `PartOf=`, `BindsTo=`, or `Conflicts=` relation
  toward `brain-metrics`;
- is generated and verified by `install.sh`, but never `enable --now`.

## Lot acceptance criteria

1. The `/metrics`, `/api/cockpit`, legacy webhook, and MCP contracts pass unchanged.
2. `python -m brain_v42.automation` has a typed composition and a graceful shutdown.
3. The new server exposes neither `/metrics` nor `/api/cockpit`.
4. Metrics with the legacy flag disabled builds neither `GitLabIngestor` nor
   `FeatureDedupJob`, starts no business loop, and answers `404` to the webhook.
5. The enabled legacy path keeps webhook and deduplication with no observable drift,
   except when a held lease forces it to stay metrics-only.
6. Stopping the metrics runtime leaves the automation runtime healthy; stopping automation
   leaves `/metrics` healthy, proven with the real `AutomationRuntime`, real start/stop
   cycles, and ephemeral TCP sockets.
7. A double-start test proves that only one lease and one scheduler exist.
8. A dedicated connection loss cancels the scheduler, closes/refuses the webhook, and
   releases all resources in `finally`; the new runtime exits non-zero.
9. The systemd template does not link the lifecycles, and the installer never enables nor
   starts the unit. An already-active unit remains an operator state, not an installer
   guarantee.
10. The runbook contains preflight, non-overlapping cutover, immediate abort, smoke,
   rollback, and a separate condition for any future re-activation of the GitLab hook.
11. No `BrainService` file, MCP handler, tool registry, or DB schema is modified.

## Non-goals

- Deploying or enabling the unit on a host.
- Recreating the external GitLab webhook or opening a LAN bind.
- Modifying `BrainService`, `build_services()`, or any MCP signature/tool.
- Reworking the business policies of `GitLabIngestor` or `FeatureDedupJob` beyond the
  ownership guards and the embedding-before-DML ordering needed for fail-closed.
- Adding a migration or a new distributed lease table.
- Closing ARC1 globally.
## Task 1 — Extract the webhook contract and create the automation server

Mandatory pre-impact on the GitNexus index aligned with `HEAD`:

- `MetricsServer._handle_webhook` (resolve by file context before impact);
- `ProjectKeyResolver` if its alias is moved.

Report risk, direct calls, and flows before editing; warn again if GitNexus returns HIGH or
CRITICAL. Run `gitnexus_detect_changes` before the commit.

Files:

- new `src/brain_v42/automation/__init__.py`;
- new `src/brain_v42/automation/webhook.py`;
- new `src/brain_v42/automation/server.py`;
- modify `src/brain_v42/metrics/server.py` without changing its signature;
- new `tests/unit/automation/test_webhook.py`;
- new `tests/unit/automation/test_server.py`;
- keep `tests/unit/test_metrics_webhook.py` as the legacy facade test.

Mandatory RED: start with a bounded import/existence test, then write the webhook,
`/health`, and metrics-routes-absent behavioral assertions separately. A collection error,
an untargeted `ModuleNotFoundError`, or a broken fixture never counts as RED proof. Each
microcycle records the command, the non-zero exit code, the assertion name, and an excerpt
showing the expected behavioral defect.

GREEN: minimal implementation of the shared handler and legacy delegation.

Targeted gates:

```bash
pytest tests/unit/automation/test_webhook.py tests/unit/automation/test_server.py \
  tests/unit/test_metrics_webhook.py tests/unit/test_metrics_server.py \
  tests/unit/test_cockpit_endpoint.py tests/unit/metrics/test_pseudo_tools_filter.py -q
pytest tests/integration/test_recent_patches.py \
  tests/integration/metrics/test_metrics_contract.py \
  tests/integration/test_cockpit_endpoint_e2e.py -q
ruff check src/brain_v42/automation tests/unit/automation src/brain_v42/metrics/server.py
mypy src/brain_v42/automation src/brain_v42/metrics/server.py
```

Expected commit: `refactor(automation): extract shared GitLab webhook boundary`.

## Task 2 — Compose and isolate the lifecycles

Mandatory pre-impact on the GitNexus index aligned with `HEAD`:

- `Settings`;
- `metrics.__main__.main`, resolved with `gitnexus_context(name="main",
  file_path="src/brain_v42/metrics/__main__.py")` before the impact;
- `_dedup_loop`;
- any existing method actually modified after the Task 1 diff.

Report the blast radius and the HIGH/CRITICAL warning before editing, then run
`gitnexus_detect_changes` before the commit.

Files:

- new `src/brain_v42/automation/dedup.py`;
- new `src/brain_v42/automation/ownership.py`;
- new `src/brain_v42/automation/runtime.py`;
- new `src/brain_v42/automation/__main__.py`;
- modify `src/brain_v42/automation/webhook.py` to translate the ownership loss;
- modify `src/brain_v42/config.py` with additive defaults;
- new `src/brain_v42/metrics/runtime.py` for the real controllable lifecycle;
- modify `src/brain_v42/metrics/__main__.py` to delegate to this lifecycle;
- new `tests/unit/automation/test_dedup.py`;
- new `tests/unit/automation/test_runtime.py`;
- extend `tests/unit/automation/test_webhook.py` with the auth priority and the `503`;
- new `tests/integration/test_automation_runtime_independence.py`;
- adapt the imports of `tests/unit/test_dedup_loop_observability.py` while keeping the
  assertions;
- extend `tests/unit/test_config.py` or add a bounded config file.

Mandatory RED, observed separately for:

- defaults and env overrides;
- typed composition;
- start only after bind;
- ordered stop and close;
- fatal bind failure;
- cleanup exactly once on red bind, including runner, clients, lease, and engine;
- legacy flag `false` with no business construction/task;
- double start refused by the lease;
- loss/invalidation or PID change of the dedicated connection: scheduler and webhook
  fail-closed, with no reconnection or second acquisition;
- explicit release on the original connection, or physical invalidation if its result
  stays uncertain;
- lease release in `finally` on all legacy metrics exits;
- `ImportError` fallback: webhook absent but legacy dedup kept;
- real independent start-stop entrypoints/cycles on ephemeral ports.

Moving `_dedup_loop` keeps a temporary alias in `metrics.__main__` if an existing test
or internal call still imports it. This alias is a rollback facade, not a second
scheduler.

The independence test starts the real `AutomationRuntime` and the real
`brain_v42.metrics.runtime` lifecycle driven by `stop_event`, not two isolated aiohttp
apps. It uses two distinct-pool engines, checks via SQL that `current_database()` is
`brain_test`, and refuses to run against another database. It stops metrics then gets
`200` on automation, restarts metrics, stops automation then gets `200` on `/metrics`. It
also checks the absence of a legacy-off business task, the release of the metrics lease in
`finally`, and the exactly-once closure of clients, lease, and engine. A separate test
terminates the owner backend and observes the fail-closed before any new business
processing.

Targeted gates:

```bash
pytest tests/unit/automation tests/unit/test_metrics_webhook.py \
  tests/unit/test_metrics_server.py tests/unit/test_dedup_loop_observability.py \
  tests/unit/test_feature_dedup_job.py tests/unit/test_feature_dedup_safety.py \
  tests/unit/test_gitlab_ingestor.py tests/unit/test_config.py \
  tests/integration/test_automation_runtime_independence.py -q
ruff check src/brain_v42/automation src/brain_v42/metrics/__main__.py \
  src/brain_v42/metrics/runtime.py \
  src/brain_v42/config.py tests/unit/automation \
  tests/integration/test_automation_runtime_independence.py
mypy src/brain_v42/automation src/brain_v42/metrics/__main__.py \
  src/brain_v42/metrics/runtime.py src/brain_v42/config.py
```

Expected commit: `feat(automation): add independently managed runtime`.

## Task 3 — Ship the dormant unit and the reversible runbook

GitNexus pre-impact on any existing shell symbol modified in `install.sh`, then
`gitnexus_detect_changes` before the commit. If GitNexus does not index the shell symbol,
explicitly log the absence of a target and protect the change with the installation tests.

Files:

- new `deploy/systemd/brain-v42-automation.service.tmpl`;
- modify `deploy/systemd/install.sh` for generate/verify/uninstall without enable/start;
- new `tests/unit/deploy/test_automation_unit.py`;
- adapt `tests/integration/test_dream_systemd_install.sh` for the dormant generation;
- new `deploy/systemd/README.md` with topology, preflight, cutover, and rollback;
- modify `README.md` to distinguish metrics and automation;
- modify `docs/ARCHITECTURE.md` for the topology and the intentional loss of automation
  events in `cockpit.recent`;
- update this plan with the actual proof.

Mandatory RED: after a bounded existence test, the assertions must refuse any lifecycle
dependency toward metrics and any activation in the installer. A fake `systemctl` logs the
calls: no automation `start`/`enable` during installation, but `stop`/`disable` and removal
are mandatory at uninstall.

Documented cutover, no live execution:

1. capture the host-local `brain-metrics` unit, the systemd relations, the state of both
   services, the effective value of the environments, and the availability of `:9201`;
2. run `daemon-reload` after generation and prepare a dedicated late environment source,
   `EnvironmentFile=%h/.config/brain-v42/automation-owner.env`, containing
   `METRICS_LEGACY_AUTOMATION_ENABLED=false` and protected with mode `0600`;
3. verify that this source is last in `EnvironmentFiles`, then filter only the flag in
   `/proc/$MainPID/environ` after startup;
4. stop metrics to release the legacy owner's lease;
5. start automation and verify `GET :9201/health` as well as the single lease;
6. **abort on failure**: stop automation, re-enable legacy, restart metrics, verify
   `/metrics=200`, then interrupt the cutover;
7. restart metrics and verify `/metrics=200`, legacy webhook `=404`, and the absence of a
   second scheduler;
8. only on a separate decision, repoint a GitLab hook toward `:9201`;
9. only after explicit soak, allow a manual `enable`.

Documented rollback, no overlap:

1. disable the external hook without repointing it;
2. stop/disable automation and verify the lease release;
3. re-enable the legacy flag and verify the effective environment;
4. restart metrics;
5. verify `/metrics=200`, the legacy webhook contract, and the single scheduler;
6. only after this green, repoint/re-enable the hook if a separate decision asks for it;
7. do not remove either the template or the drop-in before the end of the soak.

Targeted gates:

```bash
pytest tests/unit/deploy/test_automation_unit.py -q
REQUIRE_SYSTEMD_ANALYZE=1 bash tests/integration/test_dream_systemd_install.sh
# On a host where the user manager is available, also make the transient probe mandatory:
REQUIRE_SYSTEMD_ANALYZE=1 REQUIRE_USER_SYSTEMD=1 \
  bash tests/integration/test_dream_systemd_install.sh
# systemd-analyze verifies the generated .service under a temporary XDG_CONFIG_HOME,
# after __REPO_ROOT__ substitution; the raw template is never verified.
```

Expected commit: `chore(automation): ship dormant systemd runtime`.

Task 3 delivery proof:

- The precommit validations only use temporary fixtures and fakes for their installer and
  documented block simulations. The integration gate adds a real
  `systemd-analyze --user verify` and, if the user manager is available, a real transient
  unit that proves the precedence of the base `true` then the late source `false`.
- The runbook describes host operator commands on the real user systemd directory; it does
  not claim to use the tests' fixtures or fakes.
- The `Preflight`, `Cutover`, `Immediate abort`, `Smoke tests`, and `Rollback` blocks are
  extracted from the Markdown and run under `set -euo pipefail`. The tests inject lease and
  HTTP failures to verify that no further mutation is performed.
- The lease probe filters the current database, the `ExclusiveLock` mode, and the exact
  representation of the advisory key; it requires `owners=<expected> waiters=0`.
- GitNexus indexes neither `warn_wiped_env` nor `deploy/systemd/install.sh`; the tool
  returned `Target not found` for both targets. The shell scope is therefore protected by
  the installation tests and the integration gate.
- Proof run on 2026-07-15: `35 passed` for the Task 3 contract, `63 passed` for
  `tests/unit/deploy`, then `owners=0 waiters=0` against `brain_test` with the
  `oid::bigint` casts. The final gate
  `REQUIRE_SYSTEMD_ANALYZE=1 REQUIRE_USER_SYSTEMD=1` passed with a real user manager.
  It verified the generated unit and the effective precedence of the base value `true`
  by the late source `false`.

## Final coordinator gates

After integrating the three tasks and the closing hardenings:

```bash
pytest tests/unit -q
pytest tests/integration -q
REQUIRE_SYSTEMD_ANALYZE=1 bash tests/integration/test_dream_systemd_install.sh
ruff check src/ tests/
ruff format --check src/ tests/
mypy src/
```

Then:

- `git diff --check`;
- secrets/debug/unexpected files scan;
- `gitnexus_detect_changes(scope="compare", base_ref="main")` before each commit and before
  the merge;
- independent review of the full diff;
- post-merge: targeted runtime gates + systemd unit + MCP surface smoke;
- normal push of `main` only if the local/upstream refs and the gates agree.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Webhook drift between the two runtimes | Single handler + parametrized tests on both hosts |
| Two simultaneous owners | Advisory lease on a dedicated connection + conflict test + old-off/new-on sequence |
| Silent lease loss | Heartbeat on the same connection + ownership_lost event + tested fail-closed |
| Code deployment cuts off automation | Legacy flag `true` by default |
| PostgreSQL unavailability delays the metrics bind | Legacy lease acquisition bounded to 2 s + tested metrics-only fallback |
| Stopping metrics shuts down automation | No systemd link and separately tested lifecycles |
| Stopping automation cuts off metrics | Distinct server and resources, integration test |
| Automation bind fails but scheduler is active | Bind before task + `try/finally` + exact cleanup test |
| HTTP drain exceeds the systemd budget | AppRunner bounded to 10 s within a unit bounded to 30 s + in-flight webhook and client disconnection tested |
| Accidental webhook re-exposure | Loopback by default, no activation nor hook in this lot |
| Loss of automation logs in the cockpit | Accepted and documented: `recent` is in-process; the systemd journal becomes the proof |
| Partial recovery after a post-commit loss | Medium risk accepted: the ClusterGuard, event, and artifact transactions stay independent. A loss detected after a commit can replay a merge or leave `feature_artifacts` absent; `gitlab_events.feature_id` keeps the link. Atomicity or a durable reconciliation belongs to a later lot. |
| Dedup commit already started at the moment of loss | Medium risk accepted: the advisory lease can neither cancel nor restore a `commit()` already entered into PostgreSQL. The post-commit guard stops the pass and the following logs. |
| Feature locks during re-embedding | Medium risk accepted: both feature rows stay locked between the `FOR UPDATE` and the end of the embedding await. Cancellation bounds the normal case, but a stuck embedding can extend these locks. |
| Reinstalling a unit with host-local overrides | The scanner rebuilds and counts the logical `Environment=` directives without displaying their values; any error or non-canonical output stops before overwrite. Other manual directives stay out of scope and must be migrated to drop-ins before even a `--dry-run`. |
| CRITICAL breakage of `MetricsServer` | Stable signature, minimal delegation, full metrics/webhook suite |
| MCP drift | No MCP/BrainService file modified + targeted MCP suites |

## Brain delivery condition

Before Task 1, the stable feature `ARC1 lot 1 — separate metrics and automation` must exist
and be `building`. At delivery, update it with the actual status. The lot's `done` status
must not push the global ARC1 to `done`. Capitalize separately the single-owner lease
decision and a cutover runbook; do not close the Brain session without an explicit user
command.

## Execution proof — July 15, 2026

The twenty-eight plan, test, implementation, and proof commits preceding this report bound
the lot:

1. `ffd9c39` — metrics/automation separation plan;
2. `bd97f76` — plan indexing;
3. `0e53dd5` — shared GitLab webhook boundary;
4. `82d6cd8` — runtime ownership contract;
5. `2535b63` — independent automation runtime;
6. `8fbd06a` — dormant systemd unit and runbook;
7. `9ecf394` — systemd preflight hardening;
8. `25e8172` — reproduction of webhook mutations after lease loss;
9. `9b0d60c` — guard against webhook mutations after lease loss;
10. `3e6b524` — first delivery proof report;
11. `8f8f554` — reproduction of dedup mutations after lease loss;
12. `ce50f98` — waiting for additional barriers in the scheduler;
13. `49c7962` — guard against dedup mutations after lease loss;
14. `1b9d0a3` — reproduction of the `Environment=` value leak;
15. `1501cb4` — redaction of values in the warning;
16. `f13bb76` — reproduction of spaces and scan errors;
17. `c62cf92` — fail-closed scan of physical directives;
18. `1a7ea9e` — reproduction of indentation and logical continuations;
19. `5800769` — AWK scanner for logical `Environment=` directives;
20. `3c11a0b` — final report on mutation barriers;
21. `7fe44a5` — reproduction of the unsafe preflight and nested runtimes;
22. `2d4c3f2` — leak-free preflight and three-sibling-runtime topology;
23. `cf7b8a3` — reproduction of the unbounded HTTP shutdown;
24. `d6410c9` — bounded asynchronous client wait;
25. `bb78f70` — HTTP drain bounded to 10 seconds;
26. `619b647` — delivery proof report;
27. `344bb8b` — reproduction of the legacy lease blocking the metrics bind;
28. `6ea57f6` — bounded legacy acquisition and metrics-only fallback.

Task 4 started on the pre-fix code with `9 failed, 36 passed`. Both PostgreSQL REDs
observed `mutations(feature,event,artifact)=(1,1,1)` and `persisted=1`. The independent
audit returned `APPROVE_TDD`. GREEN then produced `45/45` targeted tests and `2/2`
PostgreSQL tests, and the independent reviews found zero Critical and zero High. The
post-fast-forward smokes passed.

Task 5 started with `10/10` unit failures. Its PostgreSQL RED observed a modified snapshot
and `UPDATE FEATURES` / `DELETE FROM FEATURES` after the loss. GREEN produced `10/10`
units and `1/1` integration: owner backend terminated, successor actually acquired,
unchanged snapshot, zero post-loss DML, and recoverable locks with `FOR UPDATE NOWAIT`.
The independent audit returned `APPROVE_TDD` and the concurrency review found no Critical,
High, or Medium.

Task 6 ran three RED/GREEN cycles. They reproduced the initial leak, the ignored scanner
errors, the non-numeric output, the initial indentation, and the line continuations. The
final GREEN passes the `40/40` installer contracts, re-emits neither values nor scanner
output, and fails before overwrite on a non-zero code or an invalid counter. The
independent audits returned `APPROVE_TDD`; the final review found no Critical, High, or
Medium.

The closing documentation fix observed `3/3` RED failures: presence of
`systemctl --user cat`, actual emission of a sentinel secret, and a diagram nesting the
runtimes. GREEN keeps only non-sensitive properties via `systemctl show`, including
`FragmentPath` and `DropInPaths`, represents FastMCP, metrics, and automation as three
sibling boxes, then passes `42/42` contracts. The independent audit returned `APPROVE_TDD`
with no Critical, High, or Medium.

The shutdown fix observed `2/2` RED failures on the unbounded aiohttp default and the
absent override. After a RED refinement of the client wait, GREEN passes `2/2` contracts
then `24/24` server/runtime tests. A mutation test that accepts the option without passing
it to `AppRunner` does time out on the stuck webhook. The independent audit returned
`APPROVE_TDD` and the corrected review found zero Critical, High, or Medium. The only Low
point is that the test observes the client's end rather than an explicit `finally` in the
handler; `runner.cleanup()` and the runtime ordering tests cover the behavior.

The final review of the full diff then found that opening the PostgreSQL legacy lease
could delay the metrics bind up to the driver timeout. RED blocked `lease.acquire()` and
observed an external `TimeoutError` before any bind. GREEN bounds this attempt to two
seconds, verifies its cancellation, then starts metrics-only; it passes `1/1` contract,
`10/10` metrics runtime tests, and `6/6` independence integrations. The audit returned
`APPROVE_TDD` and the corrected review `APPROVE`, with no Critical, High, or Medium. The
only Low point is the second `legacy_lease_conflict` log after the precise timeout log.

The final gates produced:

- units, with loopback sockets allowed for aiohttp tests:
  `3261 passed, 48 skipped, 8 warnings`;
- integrations with `POSTGRES_URL` and `BRAIN_V42_TEST_DB_URL` pointing to `brain_test`:
  `119 passed, 2 warnings`;
- `ruff check` green and `ruff format --check`: `380 files already formatted`;
- `mypy` green on `129 source files`;
- mandatory real systemd gate
  `REQUIRE_SYSTEMD_ANALYZE=1 REQUIRE_USER_SYSTEMD=1`: PASS, real user manager and
  `true`-to-`false` precedence verified;
- GitNexus: expected CRITICAL risk on `36` files, `992` symbols, and `44` flows.

No deployment, service restart, or external hook was executed. The `completed` status
covers the implementation and its proof on the feature branch. Merging and pushing
`main`, then refreshing the Brain feature, decision, and runbook, remain the delivery
conditions; the Brain session stays open.
