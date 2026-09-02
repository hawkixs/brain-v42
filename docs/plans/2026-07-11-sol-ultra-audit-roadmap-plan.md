---
title: "Brain-v42 Sol Ultra — audit remediation roadmap"
status: active
summary: "Remediation roadmap from the Sol Ultra audit of 2026-07-11: secure migrations and recovery first, then make memory writes fault-tolerant, close business-logic inconsistencies, and industrialize the runtime."
tags:
  - audit
  - sol-ultra
  - remediation-roadmap
  - reliability
  - recovery
  - agent-security
  - pattern-auto
---

# Brain-v42 Sol Ultra — audit remediation roadmap

> Audit conducted on 2026-07-11 on `brain-v42`, supplemented by a real verification of
> `ReD_v1/projects/red-backup`. This roadmap complements, without replacing, the
> [2026-07-03](2026-07-03-audit-gaps-backlog-plan.md) pass.

## Verdict and threat model

Brain-v42 is a mature foundation, not a prototype: coherent architecture, numerous tests,
documented incidents and explicit degradation mechanisms. The working score from the
audit is **7.5/10**. No P0 and no direct RCE was demonstrated.

The project remains intended for a private LAN, with no Internet exposure, and serves
personal agents. This assumption lowers the priority of multi-tenant auth and the
exposure of internal ports. It does not reduce the risks of data loss, a compromised
authorized agent, persisted prompt injection, migration against the wrong database, or
single-disk failure.

The chosen strategy is incremental improvement. A rewrite would destroy more evidence
and invariants than it would create.

## Starting evidence

- GitNexus was aligned on `HEAD db4caa7` during the audit: 15,163 symbols, 31,473
  relations and 300 execution flows.
- The scope executable in the sandbox produced **2,803 passing tests**, 39 skipped,
  0 failures and **87.31% branch coverage**. Eight files depending on sockets or
  SQLite were not run in this environment.
- Ruff, format and mypy pass on the declared CI scope. Lint widened to the GPU
  services finds 7 errors and 3 files out of format: green CI therefore does not cover the whole repo.
- The repo held 31 migrations at the time of the audit. The documentation still announces
  several older topologies and counters.
- Brain's PostgreSQL backup genuinely exists: daily cron at 05:00, last run on
  2026-07-11 at 7/7, a 44,951,937-byte dump, matching SHA-256, valid gzip and a
  structurally readable archive. The last 14 global runs are green; Brain has 16
  consecutive valid generations.
- The audit stayed non-destructive. No real restore was launched; full restorability
  therefore still remains to be proven.

## Delivery contract

This roadmap is a source of truth, not a detailed implementation plan. Each workstream
below becomes a separate feature the moment it starts.

For any workstream marked **pattern-auto required**:

1. create a dedicated branch and write a precise plan in `docs/plans/`;
2. have the plan critiqued in parallel by a requirements judge, an architecture judge and a
   quality judge, with at most three passes;
3. execute in TDD via sub-agents, with a compliance review and a quality review after each
   task;
4. run the full gates and a final branch review before merge;
5. write the real status into the roadmap with `brain_feature_update`.

Trivial, isolated changes can follow direct TDD. Before any symbol modification, apply
`gitnexus_impact`; before commit, apply `gitnexus_detect_changes`.

## Attachment to the existing roadmap

Only create a feature if no existing bounded context already carries the contract. The
pattern-auto plan for an effort must use a stable title, close to the target feature, so
that ClusterGuard attaches its artifacts to the right place.

| Workstream | Roadmap action at kickoff |
|---|---|
| SA1 | Create `Startup fail-closed & schema compatibility gate` |
| DR1 | Create `Disaster recovery vérifiable — PostgreSQL + Neo4j + off-site` |
| AV1 | Reuse `Commit-before-async: universal state safety pattern across all projects` |
| SEC1 | Reuse `Hawkixs runs the exact "LLM-in-prod-with-tool-access" systems its own lab-secu research flagged as unprotected — turn the threat model inward` |
| SEC2 | Treat as a sub-scope of SEC1/OPS1; create a feature only if the supervisor is redesigned |
| COR1 | Reopen `Memory Decay / Active Forgetting` |
| COR2 | Reuse `Design — Tickets cross-projet (coordination inter-sessions)` |
| COR3 | Reuse `Neo4j Knowledge Graph integration — implementation session 2026-03-16` |
| OPS1 | Reuse `Vérifier et réparer le CI GitLab brain_v42 après un push` |
| ARC1 | Defer; create a dedicated feature only at the start of the refactor |
| DOC1 | Keep as an OPS1 exit criterion, unless it becomes a standalone documentation effort |

## Delivery order

The waves execute in this order. Workstreams within the same wave can advance in
parallel if their branches and their runtime evidence stay isolated.

## Wave 1 — prevent irreversible losses

### SA1 — Alembic fail-closed and isolated migrations

**Priority: P1 · pattern-auto required**

**Status as of 2026-07-24: shipped in production.** The plan
`docs/plans/2026-07-11-alembic-fail-closed-implementation-plan.md` closed the fallbacks and the
ambiguous query strings. The cutover then verified backup `20260724_104148`, applied 036
then 037, ran `install.sh` and restarted MCP last. The schema, health,
lifecycle v4, E2E and watchdog canaries are green on build `be80cee`. Brain decision
`a665e495-3a92-4a46-852d-5c90177c6e06` still reserves cluster operations for bootstrap.

At the time of the audit, the June 30 fix had repaired the priority of `POSTGRES_URL` and
test isolation, but the last fallback remained dangerous: `alembic/env.py`
still returned the URL from `alembic.ini` if settings were absent or invalid, and
that URL targeted the live `brain` database.

Deliverable:

- remove any default live DSN from the Alembic path;
- require an explicit, valid URL, and fail before any connection otherwise;
- refuse the production database in drills/tests, except via explicit deployment opt-in;
- mask credentials and DSNs in all logs and error messages;
- evaluate a PostgreSQL role dedicated to migrations.

Exit evidence: negative tests with no env and with a malformed env, a successful
`upgrade head` migration on a disposable database, and proof that no implicit path resolves to `/brain`.

### DR1 — Verified disaster recovery, off-host

**Priority: P1 · pattern-auto required · coordination `brain-v42` + `red-backup`**

**Status as of 2026-07-24: `building`, current PostgreSQL restore acquired.** The
[implementation plan](2026-07-11-disaster-recovery-verified-implementation-plan.md) carries the
full contract, and [evidence B3](2026-07-12-disaster-recovery-b3-operational-evidence.md) is the
active checkpoint. Under DR-v5 authority, run `20260724_150315` contains eight targets and 47
artifacts. It restores PostgreSQL 16 to head 037, passes 24/24 checks, and matches an
independent SQL attestation. The object round-trip covers 33 objects and 52,832,376 bytes; the
disposable drill cleanup is complete, the DR-v1 cron is removed, and the explicit watchdog is fresh.

Production still schedules DR-v3. DR-v5 therefore has neither live activation nor an
authenticated automatic cycle. The isolated PostgreSQL restore to head 037 is acquired; DR1
remains open for replaying roles, owners and ACLs, a dedicated and empty Neo4j rebuild, an
encrypted copy outside the failure domain, a received alert and the full RTO.

The PostgreSQL dump is sound, but the source and `/data/backups` live on the same
`/dev/nvme0n1p3`. The pipeline therefore offers no recovery after loss of the NVMe.

Deliverable:

- activate DR-v5 separately and authenticate a scheduled cycle, without altering the v2/v3 evidence;
- copy at least one encrypted generation to another disk or host;
- wire up the existing encryption/transfer modules or adopt a proven library,
  without recreating a backup protocol;
- replay and verify roles, owners and ACLs in the isolated target;
- rebuild a dedicated, empty Neo4j projection from PostgreSQL and the ledger, then compare
  constraints, counts and relations;
- measure the full RTO and receive an alert triggered on failure or on no success for
  more than 26 hours.

Option A is settled: PostgreSQL and the ledger are canonical, Neo4j remains a disposable
projection. The remaining gate requires a full rebuild in a dedicated, empty database;
a correlated Neo4j backup cannot close it.

Exit evidence: scheduled DR-v5 cycle, recovery on a clean environment from the off-host
copy, verified checksum, roles/ACLs replayed, schema at head, green PG invariants, rebuilt
Neo4j projection, alert received and RTO recorded.

## Wave 2 — keep memory available and contain the agents

### AV1 — PG-first writes despite an embedding outage

**Priority: P1 · pattern-auto required**

**Status as of 2026-07-24: implementation shipped, final linking evidence to complete.** Commits
`6578200`, `2e087e0`, `f29ef7b` and `b1eb53e` cover the post-commit enrichment, the
five entity types and the bounded backfill. PostgreSQL integration with a fake endpoint proves
creation during an outage, vector recovery, FTS/vector search and a second idempotent run.
It does not, however, inject any linker: the integration evidence for expected links after
recovery remains open.

At the time of the audit, creating decisions, learnings, ADRs, runbooks and snippets
waited on the embedding before writing to PostgreSQL. A GPU outage therefore blocked the
product's primary function even though the columns accepted `NULL` and the
`--only-missing` backfill already existed.

Deliverable:

- persist the entity first in PostgreSQL with `embedding=NULL`;
- durably record the remaining vectorization/linking work, via an outbox or an idempotent
  job;
- make recovery observable and replayable without duplication;
- keep FTS search useful during degradation;
- expose backlog, age and failure rate of missing embeddings.

Exit evidence: every entity type registers with the embedding cut, receives its vector after
recovery, is created only once and recovers its expected links.

### SEC1 — Capabilities per agent, project and Dream phase

**Priority: P1 · pattern-auto required**

**Status of the webhook sub-lot as of 2026-07-24: shipped.** The secret comparisons on both
routes have used constant time since `20b2612`. Commit `f73aa6e` then disables
aiohttp auto-decompression on the automation server and centralizes, after authentication,
the bounded read and decompression. GitLab pipeline `4252` is green, 6/6 jobs, and
ticket `d6df267c…` is closed. The rest of SEC1 remains open on Dream capabilities.

The global Bearer token and the `mcp__brain-v42__*` allowlist give an authorized Dream agent
capabilities broader than its phase: deletion, modification of the project context,
configuration of scan paths and writing to `CLAUDE.md`. Network confinement does not
protect against a contaminated corpus or a compromised authorized agent.

Deliverable:

- define scopes per agent, project and operation class;
- replace the Dream wildcards with an exact list per phase;
- reserve destructive operations for a separate scope;
- bound `plan_scan_paths` roots and any filesystem write server-side;
- provide for token rotation/revocation and audit of refusals;
- add persisted prompt injection fixtures and cross-project negative tests.

Exit evidence: a Dream token can neither delete, nor write outside its roots, nor act on
another project, even if the stored content explicitly asks it to.

### SEC2 — Bounded local services

**Priority: P2 under the LAN-only model · pattern-auto required if treated as a batch**

**Status as of 2026-07-24: SEC2-A code-ready, not deployed.** Commits `210660d` and
`13e53c0` bound the canonical shim to 8 MiB/5 s, 8 ingress reads, 100/128 batches and a
single embedding + rerank calculation per worker. The versioned Compose binds the shim and the
legacy profile to `127.0.0.1:8003`, but the observed runtime remains LAN-wide until an
authorized, proven rollout. Targeted tests are at 61/61 and the widened embedding/reranking
matrix at 174/174, including these 61; four independent reviews conclude `SHIP`.

SEC2 remains open. Bearer authentication and the dedicated client Docker network require the
coordinated `auto-discord` cutover (`9ef5c69d…`). The historical QA URL of `red-shrik` must become
configurable without an implicit model change (`89140780…`). The legacy PyTorch profile remains
unbounded and does not preserve the `auto-discord` DNS alias; the superseded `deploy/dev-pc`
supervisor, its old aiohttp and its Docker access remain a separate sub-lot if this rollback is kept.

Global exit evidence: rollout with effective bind, atomic client/server bearer, dedicated
network, load tests and malformed requests, absence of a WAN listener and removal of any
general Docker access on a maintained rollback path.

## Wave 3 — close business-logic inconsistencies

### COR1 — Decay fed by all usage evidence

**Priority: P1 · pattern-auto required**

**Status as of 2026-07-19: deployed to production via MR `!66`, commit `main` `75c6b05`
(implementation `0bf272a`).** Signals now roll up to the canonical parent, specialized reads
feed the decay, and the parent/child scope and archived top-K are covered by
three PostgreSQL E2E tests. The live test refreshed the parent without touching the chunk counters.

Starting observation: plan accesses are logged with the chunk ID, but `plan` is missing
from the
`DecayFlusher` registry. `brain_get`, `brain_use_snippet` and runbook execution also do not
produce all the evidence the design intended.

Exit evidence: hits roll up to the parent plan, every expected read/execution
refreshes the entity, and an integration test demonstrates that a used entity does not
wrongly go stale.

### COR2 — Atomic Ticket transitions

**Priority: P1 · pattern-auto required**

**Status as of 2026-07-19: deployed to production via MR `!66`, commit `main` `75c6b05`
(implementation `50b38bc`).** The `id + status` CAS, the message within the same transaction,
real rollback and single-winner racing are validated on an isolated PostgreSQL and then confirmed
live by one winner, a stable conflict and a single consistent message.

Starting observation: state validation, the UPDATE and writing the message used separate
steps
and transactions. Two concurrent transitions could end up last-write-wins,
or return an error after the status had actually changed.

Exit evidence: compare-and-swap or `SELECT FOR UPDATE`, status and message in a single
transaction, deterministic concurrency tests and no observable partial success.

### COR3 — Merge wired to the graph write-through

**Priority: P1/P2 · pattern-auto required**

**Status as of 2026-07-19: deployed to production via MR `!66`, commit `main` `75c6b05`
(implementation `cd49067`).** The canonical job records source, target and audit in
PostgreSQL before the `MERGED_INTO` write-through; the admin/scoped paths and the
secret-safe degradation are validated on isolated PostgreSQL and Neo4j. The live test confirmed
the PG audit and the immediate Neo4j edge.

Starting observation: `brain_merge_entities` commits directly to PG without going through
`ConsolidationJob.merge`; `MERGED_INTO` can be missing until reconciliation.

Exit evidence: a single merge orchestration updates PG and Neo4j together, logs any
degradation, and leaves reconciliation as a safety net, not the
normal path.

Consolidated wave evidence: **3,954 tests passed out of 3,954**, PostgreSQL and Neo4j test
instances enabled, Ruff and format on `src tests`, mypy on 131 source files and `git diff --check`
all green. GitLab pipelines MR `4145` and `main` `4146` are green, registry build included.
`brain-mcp-http` and `brain-metrics` restarted on `75c6b05`; the production E2E for COR1/2/3
is green and all its PostgreSQL/Neo4j fixtures have been removed. The prepared rollback to
`5410c6a` was not needed.

## Wave 4 — make every commit reproducible and observable

### OPS1 — Lockfile, CI and supply chain actually enforced

**Priority: P2 · pattern-auto required**

**Status as of 2026-07-24: CI/Docker reproducibility shipped in `19d8cb7`; supply chain
still open.** The Python jobs and the image install the exact `uv.lock` graph with uv
0.10.7, the cache depends on the lockfile, Ruff covers the whole repo and the static tests
lock in the CI commands and the image smoke test. GitLab pipeline `4245` is green, build
and Docker smoke included.

The immutable operational images sub-lot is shipped on `main` at `90d50f4`.
Consumer commit `921b0e1` is merged into it, and the local lifecycle fix
`c41b2b3` prevents the upgrade and rollback paths from implicitly rebuilding the
images. Closing commit `833be90` hardens the gate and Neo4j rotation. The gate validates
24 consumers; its 1,390 tests, the 140-test OPS1 matrix and the 9
headless/lifecycle contracts are green. Three consecutive unit suites pass exactly
5,994 tests and skip 48. The official GitLab CI Lint responds `valid: true` with zero errors
and zero warnings. Three independent reviews conclude `SHIP` with no remaining reproducible finding.

Delivery commit `90d50f4` was pushed identically to GitHub and GitLab. Post-merge
validation under Python 3.12 passes 6,003 tests and skips 39 out of 6,042 collected. GitLab
pipeline `4248` is green: 6/6 jobs, 90% coverage and Docker build included. OPS1 remains open
for models, service-specific dependencies, system packages, dependency
auditing, the SBOM and SAST.

Deliverable: frozen install from `uv.lock`, identical versions in CI and the image, gates on
the omitted services, fixing the 7 Ruff errors and 3 format gaps, immutable images and models,
dependency auditing and SBOM adapted to a critical local project. At this stage, the operational
images are shipped; the other supply-chain elements remain to be delivered.

Exit evidence: two builds of the same commit resolve the same dependencies and the pipeline
fails on a regression placed in each of the previously ignored scopes.

### ARC1 — Separate business orchestration from infrastructure

**Priority: P2 · pattern-auto required, after the business invariants**

**Status as of 2026-07-24: batch 1 shipped, standalone runtime still dormant.** Commits from
`2cf491e` to `d04dbd8` extract the webhook and deduplication into `brain_v42.automation`, bound
the leases and shutdown, then ship a reversible systemd unit. The live owner remains the
legacy metrics path until the separate cutover. `build_services()` still returns a
`dict[str, Any]`, `BrainService` receives 15 dependencies and several MCP handlers execute
SQL directly; ARC1 as a whole therefore remains open.

Deliverable: separate metrics from business automations, type the composition, group
dependencies by bounded context and move queries out of handlers. No MCP surface
change is needed for this first pass.

Exit evidence: cutting the metrics sidecar no longer cuts the business automations; the
tested handlers depend on typed interfaces rather than direct SQL.

### DOC1 — A single operational truth

**Priority: P2 · TDD/direct, no pattern-auto if the pass stays documentation-only**

**Status as of 2026-07-23: shipped in `852c1b4` and `26a8299`, GitLab pipeline `4246` green.**
README, CLAUDE and ARCHITECTURE reflect the production HTTP transport, the stdio fallback,
the unified reranker endpoint, FastMCP 3.x and the graph 035 cutover. MCP_TOOLS already carried
the right counts. The LAN-only model and its residual GPU exposure are explicit, and
`.dockerignore` is present.

The `tests/unit/test_documentation_contract.py` test now derives the Alembic head, the
registered tools, the configuration values, the production MCP client and the locked
FastMCP version. It fails if these contracts or the operational summaries diverge. DOC1 is closed
by the adversarial review and pipeline `4246`. The associated systemd paths now publish
the validated units atomically, attest the private files without exposing their content and
enforce a canary with no watchdog race. Local evidence: 4,607 tests passed, 289 skipped,
review matrix 223/223 nominal and 223/223 under a hostile environment, systemd integration,
Ruff, format, Bash syntax and `git diff --check` all green.

Exit evidence: an automated check fails if the documented counts or contracts
diverge from the code.

## Historical findings register — 2026-07-11 baseline

This table preserves the initial audit evidence; it is not the current backlog. The dated
statuses of the workstreams above supersede it. Resolved or reduced rows stay visible to
avoid losing their provenance.

| Confirmed finding | Adjusted priority | Workstream |
|---|---:|---|
| Final Alembic fallback to the live DSN | P1 | SA1 |
| The embedding blocks all memory writes | P1 | AV1 |
| Global bearer and wildcard Dream capabilities | P1 | SEC1 |
| Filesystem write/scan without strict server-side roots | P1 | SEC1 |
| Embedding 8003 LAN-wide, unbounded, old aiohttp — caps and target bind versioned by SEC2-A, not deployed | P2 reduced LAN | SEC2 |
| Decay with no parent plan and incomplete usage evidence | P1 | COR1 |
| Last-write-wins Ticket transitions and a separate message | P1 | COR2 |
| MCP merge bypassing the Neo4j write-through | P1/P2 | COR3 |
| Local PG dump sound but on the same NVMe | P1 | DR1 |
| PostgreSQL head 037 restore acquired on July 24; historical graph 035 evidence, dedicated rebuild and off-host RTO open | P1 partial | DR1 |
| `red-backup verify` false-green `0/0` | P1 | DR1 |
| Cleanup/retention can delete 41 valid artifacts from the last run | P1 | DR1 |
| Neo4j relations made reconstructible by the ledger/rebuild 035 on July 22 | P1 resolved | DR1 |
| No offsite, encryption not wired, no alerting | P1/P2 | DR1 |
| Non-persistent cron and overly open backup permissions | P2 | DR1 |
| `uv.lock` enforced by CI and Docker in `19d8cb7` | P2 resolved | OPS1 |
| Full-repo CI/lint shipped in `19d8cb7` | P2 resolved | OPS1 |
| Immutable operational images, rotation and lifecycle shipped at `90d50f4`; pipeline `4248` green | P2 resolved | OPS1 |
| Metrics sidecar owning business functions | P2 | ARC1 |
| `Any` composition, 15 dependencies and SQL in handlers | P2 | ARC1 |
| Documentation and DOC1-A gate shipped; pipeline `4246` green | P2 resolved | DOC1 |
| `.dockerignore` present; models, dependencies, audit, SBOM/SAST still open | P2 partial | OPS1/DOC1 |

## Maintenance register — 2026-07-23 audit

These tickets are verifiable units of work, not shipped features. The initial Brain
snapshot from July 24 below also deduplicates the self-tickets from the audit and
maintenance runs against the roadmap; rows are then maintained as local sub-lots progress.

| Brain ticket | Factual status | Attachment and exit criterion |
|---|---|---|
| `a857705f…` migrations 036/037 and MCP restart | **closed on July 24**; backup `20260724_104148` verified 8/8 and restored 24/24 to head 035, 036 then 037 validated, three MCP units published, restart last, hardening/health/E2E/watchdog green on `be80cee` | SA1/runtime shipped; prod at head 037, canaries abandoned with no artifact and zero rollback guards |
| `74ab1931…` DR-v5/off-host | `in_progress`; run `20260724_150315` verified, PostgreSQL 16 restore to head 037 at 24/24, green object round-trip and v1 cron removed; the live timer remains DR-v3 | DR1; activate and prove DR-v5, replay roles/owners/ACLs, rebuild dedicated Neo4j, then prove encrypted off-host, alert and RTO |
| `530d796a…` SEC2 embedding `:8003` | SEC2-A shipped at `7508546`: 61/61 targeted, matrix 174/174, suite 6,053/298 and pipeline `4254` green 6/6; ticket kept open, runtime not deployed | SEC2; proven rollout, atomic auth, dedicated Docker network, live load and handling of the legacy/supervisor |
| `9ef5c69d…` `auto-discord` migration | open coordination | SEC2-B; bearer read from a secret file and dedicated client network, with atomic cutover/rollback |
| `89140780…` `red-shrik` QA URL | open coordination | SEC2-B; make the URL configurable and confirm the model owner without implicitly redirecting traffic |
| `1460c46c…` systemd sandbox | partial rollout on July 24: the three MCP units are live and canaried (`NoNewPrivs=1`, seccomp, private namespace, ro credential mounts, green E2E/watchdog); five fragments remain unpublished | SEC1/ARC1; rollout and canaries separate for Dream, graph-recon and automation, without implicitly republishing MCP |
| `c1ca450c…` legacy graph-recon `--fix` | closed on July 24; read-only ledger audit shipped at `75ebc83`, 277/277 deploy+ledger, 60/60 docs and pipeline `4258` green 6/6; no live rollout | ARC1/maintenance; writer removed from the scheduled path, recovery 035 kept as an operator action; separate rollout |
| `31d68c06…` CI security stage | open, deduplicated with the OPS1 remainder | OPS1; locked-down scanners, explicit offline/freshness policy, dated burn-in then a blocking gate |
| `5619c851…` targeted coverage | local delivery complete, ReD review required. Reconciliation: `9f41b01` already has its `ProjectGroupTicketService` service and plan, with a more complete merged current suite; `b547b87` is identical for the service, tests, plan and `RoadmapService` spec; `2f797d6`, limited to tests, is kept in this merged suite. `pg_ticket` sub-lot: partial transition message observed in RED then rejected before the session, 11/11 tests and 100% of 60 statements/6 branches. `thresholds` sub-lot: five invariants observed in RED then validated, 20/20 tests and 100% of 39 statements/16 branches. Unit suite: 6,256 passed/49 skipped | maintenance; have the exact Git interval reread by the ReD reviewer, with no merge, push or deployment |
| `1c6911a4…` `planned` statuses rejected by plan_indexer | `in_progress`; code shipped at `223fc1f`, pipeline `4276` green, no live rollout/reindex; Brain still at 34/35 and the OTLP plan is missing | maintenance/data quality; after `44ee7643…`, deploy then reindex and prove Brain 35/35 with OTLP `archived` |
| `44ee7643…` relative `plan_scan_paths` paths | open; seven contexts scan `brain_v42/docs/plans`, which pollutes or empties their corpora | maintenance/data quality; canonical absolute paths, dry-run and backup, recoverable transactional cleanup, then isolated reindex before rolling out `223fc1f` |
| `6fcd4463…` local/CI systemd false green | closed on July 24 with no mutation; 201/201 targeted, three consecutive unit suites and pipelines `4275`/`4276` green | maintenance; the initial diagnosis is no longer reproducible, preserve the production ancestor guard |
| `ccb8e988…` MCP token budget | open after the July 24 audit | maintenance/MCP UX; reduce session output schemas, bound `brain_search` per item and keep the all-projects roadmap cap |
| `45d77f10…` MCP protocol and annotations | open after the July 24 audit | maintenance/fix; ToolAnnotations, existing `Literal`s and the `isError`/`ToolError` contract tested without silently changing the surface |
| `49bda801…` JSON depth Python 3.14 | closed on July 24; shipped at `be80cee`, 143/143 validations under Python 3.12 and 3.14, unit suite 6,138 passed/48 skipped, max event-loop stall 33.4 ms, `SHIP` review and exact pipeline `4261` green 6/6 | SEC2/maintenance shipped code-side; rollout/smoke 64/65 remain tracked separately by `530d796a…` |
| `a7d85a85…` REORG review | open; 14 items flagged with no mutation: 6 Dream alerts and 8 `red-shrik` snapshots | maintenance/data quality; decide and prove requalification, purge or allowlist extension for each group |
| `8ba27dc0…` stale `reconcile_graph` hint | closed on July 24; source `83b3669` merged into `8c13206`, 31/31 tests and pipeline `4257` green 6/6 | maintenance/graph; preserve the source worktree until its foreign changes are triaged |
| `621fcc37…` constant-time webhook | closed on July 24 | SEC1; two paths shipped at `20b2612`, 181 targeted tests, pipeline `4250` green and bind documentation aligned |
| `56245929…` OPS1 flakiness | closed on July 24 | OPS1; three consecutive unit suites green, post-merge validation 6,003/39 and pipeline `4248` green on `90d50f4` |
| `d6df267c…` dedicated webhook decompression before auth | closed on July 24 | SEC1; shipped at `f73aa6e`, 92 post-merge tests, unit suite 6,006/48, two `SHIP` reviews and pipeline `4252` green 6/6 |

## Reconciliation with the July 3 audit

Known baseline on July 11: no restore drill, Neo4j outside backup, documentation drift,
partial CI and exposed local services. Since then, the PostgreSQL restore to head
037 and the historical Neo4j rebuild to head 035 are acquired, as are CI/Docker
reproducibility and the versioned bounding of the canonical shim. Off-host, the full RTO, the new dedicated
Neo4j rebuild, the supply chain, the SEC2 rollout, authentication, the dedicated Docker network and the
legacy/supervisor path remain open.

New or proven in the July 11 baseline: same NVMe, `0/0` verifier,
cleanup/retention classing valid artifacts as orphans, Neo4j relations then not
exactly reconstructible (resolved by the ledger/rebuild 035 on July 22), residual Alembic
fallback, writes blocked by the embedding,
decay gaps, Ticket non-atomicity, merge outside the write-through and the Dream capability model.

The Dream topics already closed on July 3 — missing token and invalidated soak — are not
reopened. The other standalone topics from that pass remain tracked in its original plan.

## Global definition of done

A feature in this roadmap is not `done` on the strength of mocks or a unit test alone.
It requires:

- unit, integration and failure-injection tests matched to the risk;
- runtime evidence on a disposable or genuinely isolated environment;
- documented rollback or recovery;
- no secret leakage in logs and error output;
- GitNexus analysis before the change and detection of affected flows before commit;
- a final `SHIP` review of the complete diff;
- Brain status updated with delivery evidence.

## Recommended next effort

The 036/037 rollout and the systemd MCP sub-scope are complete; SEC2-A and the five
Dream/graph-recon/automation profiles remain undeployed. The operational DR priority is to activate and
prove DR-v5 (`74ab1931…`), then close the roles/ACL, dedicated Neo4j, off-host and alert gates.

The data-quality priority is `44ee7643…`: fix the multi-project paths, inventory and
clean up the polluted rows, then reindex each corpus. Only after this fix,
deploy `223fc1f`, reindex Brain and prove 35/35 with OTLP `archived`. Without production
mutation authority, the next standalone lot remains the final AV1 linking integration
evidence, then the CI security stage (`31d68c06…`) and targeted coverage (`5619c851…`). DR1, SEC2-B,
the SEC1 remainder, OPS1 and ARC1 remain open; the overall roadmap is therefore not finished.
