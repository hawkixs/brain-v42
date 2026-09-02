---
title: "Verifiable disaster recovery — PostgreSQL + Neo4j + off-site"
status: active
summary: "Turn red-backup into fail-closed recovery proof: canonical manifest, genuinely isolated and attested PostgreSQL restore, then Neo4j/off-host/scheduling behind explicit authority gates."
tags:
  - disaster-recovery
  - red-backup
  - postgresql
  - neo4j
  - offsite
  - pattern-auto
  - sol-ultra
---

# Verifiable disaster recovery — PostgreSQL + Neo4j + off-site

> **Safety amendment — July 24, 2026.** The Brain decision
> `3d3d72e4-acb7-49fe-aabb-1618e648e627` adopts option A "canonical PostgreSQL +
> rebuild-on-doubt". For the graph ledger, an exact/correlated Neo4j restore is no longer
> a gate. The head 035 proof is historical since production moved to 037. The DR-v5 run
> `20260724_150315` renewed the PostgreSQL gate at head 037 with 24/24 checks.
> What remains is replaying roles, owners and ACLs, then rebuilding a dedicated, empty Neo4j
> projection with the graph protocol introduced in 035. Never downgrade to close a
> gate. The other DR proofs in this plan stay unchanged.

> Source: DR1 workstream from
> `docs/plans/2026-07-11-sol-ultra-audit-roadmap-plan.md`.
> Coordinated branches: `codex/disaster-recovery-verified` in `brain_v42` and
> `red-backup`. Pattern: pattern-auto; no external change before the requirements,
> architecture and quality judges converge.
> Active resumption checkpoint:
> [B3 operational proof](2026-07-12-disaster-recovery-b3-operational-evidence.md). The
> [B2 handoff](2026-07-11-disaster-recovery-b2-session-handoff.md) stays historical.

## Goal

Move from a structurally readable local backup to verifiable recovery. The first
delivered increment must make a `0/0` green impossible, restore the audited Brain run
into a disposable PostgreSQL/pgvector without touching an existing cluster, and produce a
redacted JSON proof. The DR1 feature stays `building` until Neo4j and an encrypted
off-host copy are genuinely restorable.

## Threat model and state proven as of July 11, 2026

The model stays LAN-only and personal agents. The relevant DR scenarios are loss of the
NVMe or the host, silent corruption, a privileged deletion, local ransomware, a partial
archive, an incomplete restore reported green, and the loss of non-derivable Neo4j
relations.

Healthy state:

- explicitly selected audited run: `20260711_030001`; Brain dump of 44,951,937 bytes,
  SHA-256 `7e0c34ddd4e863a07482d4a66d30864d5b5e04e63743610e2cd1b9305951daf2`;
- 16 daily Brain generations, 16/16 valid SHA and gzip;
- latest readable catalog: 207 TOC entries, 23 `TABLE DATA`, 73 indexes, 45 constraints and
  2 extensions;
- 14 consecutive global runs at 7/7 since June 28;
- `red-backup` baseline: 321 tests passed, 2 skipped, 3 existing warnings; `main` repo
  clean at commit `5328089`;
- the dump does not pin the extension version (`CREATE EXTENSION ...` with no `VERSION`)
  and the floating/live images now expose 0.8.4. The exact official 0.8.2/PG16 image is
  therefore pinned to digest index `00ba258a…`, downloaded and pending validation before
  the real drill.

Confirmed defects:

- `manifest.py` writes a flat DB manifest, while `verify.py` expects `entries=[]` and then
  treats `0 == 0` as a success; a deliberately corrupted dump reproduced this false
  green;
- the CLI with no manifest exits 0;
- `restore_test.py` can report success with `pg_restore rc=1` and zero tables, depends on
  a host `pg_restore` that is absent, builds the gzip stream via shell and creates a
  temporary database on the supplied cluster;
- on the real run `20260711_030001`, `scan_orphans()` classifies 41 valid artifacts as
  orphans — the five dumps, their SHA sidecars and the snapshots — while destructive
  retention is called automatically after every green run;
- PostgreSQL, Neo4j, backups and repos live on the same `/dev/nvme0n1p3`; no `.gpg`,
  remote receipt or Neo4j artifact exists;
- dumps/directories are `0664/0775`, and world-readable compose snapshots contain
  lexically sensitive keys;
- the 05:00 cron works but has neither catch-up nor a proven alert; the systemd timer is
  not installed and its hardening masks the SSH configuration needed by
  `red-writer-prod`.

## Architecture and ownership

`red-backup` owns the technical orchestration: manifests, artifact selection, disposable
containers, proofs, encryption, transfer, scheduling and cleanup.

`brain_v42` owns the business recovery invariants in a versioned JSON contract with
`contract_id` and SHA, plus a versioned fixed SQL attestation. `red-backup` keeps
byte-identical vendored copies of them; each repo validates the documents locally and a
coordinated gate compares the SHAs. No test depends on a `/tmp` worktree or a Python
import between repos. The DSL produces the primary proof; the SQL script run by a second
`psql` process produces an independent attestation in canonical JSON format.

The checks use a closed DSL, never free-form SQL in YAML. The Brain contract fixes:

- the 23 exact tables: `access_log`, `adrs`, `alembic_version`, `consolidation_log`,
  `decisions`, `dream_promotions`, `dream_runs`, `feature_artifacts`, `features`,
  `gitlab_events`, `indexed_plan_chunks`, `indexed_plans`, `learnings`,
  `metrics_timeseries`, `process_metrics`, `project_contexts`,
  `roadmap_curation_proposals`, `runbooks`, `search_log`, `snippets`,
  `ticket_extraction_proposals`, `ticket_messages`, `tickets`;
- head `031`, `vector` extension version `0.8.2` for the pinned run, 17 foreign keys,
  101 indexes and zero unvalidated constraint;
- aggregated corpus (`decisions`, `learnings`, `snippets`, `runbooks`, `adrs`) non-empty,
  `project_contexts`, `indexed_plans`, `indexed_plan_chunks` and `features` non-empty;
- 1536 dimensions for every non-null embedding in `decisions`, `learnings`, `snippets`,
  `runbooks`, `adrs`, `features`, `indexed_plans`, `indexed_plan_chunks` and `gitlab_events`;
- structural typmod `vector(1536)` for these nine columns, even when a table holds no
  non-null embedding;
- no orphans for `indexed_plan_chunks → indexed_plans` and
  `feature_artifacts → features`.

The PostgreSQL restore engine follows this flow:

1. explicitly select a run and a target;
2. load the manifest through a fail-closed adapter, then verify existence, size,
   checksum and compression;
3. require the immutable local image
   `pgvector/pgvector@sha256:00ba258a66dac104fd5171074a0084462a64a1369d8513f3d0a634e2f24d15bc`
   with `--pull=never`;
4. run a Docker/image/RAM preflight then `docker create`; capture and validate the CID
   before `docker start`;
5. enforce `--network none`, no port/bind/volume, a 1 GiB tmpfs PGDATA, small tmpfs
   mounts for `/var/run/postgresql` and `/tmp`, `nodev,nosuid,noexec` options where
   compatible, 2 GiB memory and memory-swap, 2 CPUs, 256 PIDs and bounded timeouts;
6. initialize `POSTGRES_DB=restore`, `POSTGRES_USER=restore_admin` and
   `POSTGRES_HOST_AUTH_METHOD=trust`, acceptable only inside this network-less sandbox;
7. wait for `pg_isready` on the Unix socket then run `psql -X ... -c 'SELECT 1'`;
8. stream the gzip into `docker exec -i CID pg_restore --dbname=restore
   --username=restore_admin --exit-on-error --single-transaction --no-owner --no-acl`,
   without a shell and without loading the whole dump into memory;
9. run the DSL engine, then the fixed SQL attestation in a second
   `psql -X -v ON_ERROR_STOP=1` process; compare their canonical JSON outputs and keep
   the checks in memory;
10. in `finally`, remove exclusively the CID via `docker rm -f -v`, verify the CID,
    label and captured volumes are absent, then compute the overall verdict;
11. write the atomic JSON report exactly once, including on failure. The final report is
    therefore post-cleanup; an incomplete cleanup turns it red.

The engine accepts no host, no port, no live DSN, and no free-form Docker argument from
YAML. It never mounts `/data/backups` into the container and never calls
`CREATE/DROP DATABASE` on an existing cluster. A read-only root is enabled only when the
pinned image's smoke test proves it compatible.

CLI selectors are never interpreted as paths. `RUN` must match `^\d{8}_\d{6}$`, name a
direct non-symlink child of `storage_dir`, and be opened under that root. `TARGET` must
match `^[a-z][a-z0-9_-]{0,63}$` and be an exact key of the loaded profile. The
`.drills/<run>/<target>` manifest and report paths are rebuilt only from these validated
values, then proven under their respective roots. A new run directory is created
exclusively and is never silently reused.

Each `TargetManifestV2` explicitly binds the artifact to its `contract_id` and SHA when a
recovery profile exists; the `RunManifestV2` holds the exact bindings map (explicit `null`
value for a target with no contract). A restore refuses a referenced contract that is absent:
it never substitutes the current version. The receipt is serialized as canonical JSON; `.complete`, written atomically last, holds the SHA-256 of those bytes.
`verify-run` requires a valid receipt, seven exact successes, the marker present, and a
matching SHA.

## Worktree and user-state protection

- `brain_v42` stays in the current workspace; the pre-existing `AGENTS.md`, `CLAUDE.md`
  and `uv.lock` changes keep their hashes and stay out of staging.
- `red-backup` is clean on `main`. Create an isolated worktree in `/tmp` with a
  `codex/disaster-recovery-verified` branch; do not modify its main checkout directly.
- Index this worktree with GitNexus before editing. The repo is currently neither
  indexed nor a member of `red-triad`; if indexing fails, record the limitation and use
  imports, tests and text search as an explicit blast radius.
- Before every commit, run `gitnexus_detect_changes` in the indexed repos and re-check
  both worktrees.

## Non-goals of the first autonomous increment

- Do not stop, restart or dump live Neo4j.
- Do not write to a second host, NAS, VPS or disk without an explicit destination choice.
- Do not install a systemd unit, remove the cron, or modify a webhook or credentials.
- Do not mass-change historical permissions under `/data/backups` without a dry-run and
  operator authorization.
- Do not enable `cleanup --execute`, `prune --execute`, or the old orchestrator
  branches.
- Do not announce DR1 `done` after the PostgreSQL restore alone.

## File structure — increment 1

### `red-backup`

| File | Planned change |
|---|---|
| `src/backup/manifest.py` | Canonical V2 models, legacy adapters and atomic writing |
| `src/backup/verify.py` | Fail-closed verification of DB and snapshot artifacts |
| `src/backup/cleanup.py` | Shared artifact references; no implicit deletion |
| `src/backup/inventory.py` / `status.py` / `history.py` / `retention.py` | Full V2 run vs legacy/unmanaged; `.drills` excluded from inventory and retention |
| `src/backup/pg_dump.py` | Atomic publication and permissions for the local dump |
| `src/backup/docker_dump.py` | V2 production and `0600/0700` modes |
| `src/backup/ssh_docker_dump.py` | V2 production and consistent structural control |
| `src/backup/config_snapshot.py` | Copy then explicit chmod, never trusting the source mode |
| `src/backup/runner.py` | Snapshot manifest; no automatic restore wiring yet |
| `src/backup/config.py` | Declarative restore profile, immutable image and bounded invariants |
| `src/backup/recovery_contract.py` | Strict DSL models and vendored contract SHA |
| `src/backup/restore_sandbox.py` | Bounded Docker lifecycle and shell-less streaming |
| `src/backup/restore_checks.py` | DSL checks, fixed SQL call and canonical comparison |
| `src/backup/restore_report.py` | Atomic local JSON attestation, post-cleanup |
| `src/backup/restore_test.py` | Shim rejecting the old live parameters |
| `src/backup/__main__.py` | `verify` non-zero with no artifact; explicit `restore-drill` command |
| `deploy/systemd/red-backup.service` | `UMask=0077` only, no live install |
| `config/backup.yaml` | Pinned Brain profile; no fictitious remote destination |
| `config/recovery/brain-v42-v1.json` / `.sql` | Byte-identical vendored copies of the Brain contract and fixed attestation |
| `tests/` | RED/GREEN for manifest, CLI, cleanup, permissions and orchestrated restore |
| `CLAUDE.md` | Honest documentation: manual restore proven, full DR still open |

### `brain_v42`

| File | Planned change |
|---|---|
| `ops/recovery/brain-v42-v1.json` | Canonical business contract with `contract_id` |
| `ops/recovery/brain-v42-v1.sql` | Second fixed attestation, independent of the DSL compiler |
| `tests/unit/test_recovery_contract.py` | The expected DR profile head tracks the Alembic head |
| this plan / roadmap | Session proofs, limitations and real status |

## Task 0 — Isolate, index, and pin the baselines

1. Create the `red-backup` worktree in `/tmp` from `origin/main`, branch
   `codex/disaster-recovery-verified`.
2. Record the HEAD SHA, status, hashes of any dirty files and the list of old branches;
   do not merge any orchestrator branch.
3. Run `npx gitnexus analyze` in the worktree, then verify the repo's availability.
4. Replay the 321 tests with `uv run --frozen` and Ruff `src/`.
5. In Brain, verify that migrations 001–031 remain unchanged and that the head is
   unique.

## DR1 traceability matrix

| Finding | Closed in this increment | Proof | Blocks `done` |
|---|---|---|---|
| `verify 0/0` | Yes | verify-target + corruption tests | Yes while red |
| no PG restore/RTO | Yes | local attestation of the pinned run | Yes while red |
| same NVMe | No | mount audit | Yes |
| Neo4j not exact | No | artifact absence + relation audit | Yes |
| no off-host/encryption | No | no remote receipt | Yes |
| cron with no catch-up/alert | No | scheduler/alerting audit | Yes |
| historical permissions | No | inventory/dry-run | Yes for config secrets |

## Task 1A — V2 models and legacy adapters

**RED:**

- both deployed `target_type` values (`docker_pg`, `ssh_docker_pg`) are parsed;
- a valid legacy DB manifest genuinely references the dump and its `.sha256` sidecar;
- a missing dump, mismatched size, mismatched SHA, truncated gzip, empty manifest or
  unknown format all fail;
- a hybrid manifest, unknown field, duplicate, empty component, absolute path, `..`,
  backslash, symlink or non-regular file are refused.

**GREEN:**

1. Introduce a strict V2 schema (`extra='forbid'`) with a non-empty root-relative
   artifact list, 64-hex SHA, timezone-aware timestamps, enums and `default_factory`
   lists, size/sha/kind/compression and target metadata.
2. Anchor every path to the run. Legacy DB has `dump_file` and `<target>.sha256`; the
   `entries[]` format stays a code fixture but is not presented as a deployed format.
3. Bound the manifest size and artifact count. Open files with `O_NOFOLLOW`, validate
   via `fstat`, and keep the same descriptor for checksum/gzip/restore.
4. Carry the exact contract binding in the models and refuse any implicit substitution
   for an absent contract.

pattern-auto checkpoint: spec review then quality.

## Task 1B — Targeted verify and full-run receipt

**RED:** zero artifacts, a missing manifest/receipt/target, a path-like run or target, a
run symlink, a missing marker, a malformed marker, a mismatched hash, a partial success
or a `skipped` target all make the CLI non-zero.

1. `verify-target RUN TARGET` can attest a single legacy archive, with syntactically
   bounded selectors and an exact target from the profile.
2. `verify-run RUN` requires a direct non-symlink child, a `RunManifestV2` listing the
   seven expected targets, their contract bindings, their statuses and the exact
   `expected == observed` equality; no required target may be `skipped`.
3. The canonical receipt is published after the target manifests; `.complete` holds its
   SHA-256 and exists only for a fully successful run. `verify-run` recomputes this SHA.
   A legacy run with no snapshot manifests stays `completeness=unknown` and non-zero.
4. The CLI with no manifest, receipt, expected target or artifact exits non-zero.
5. Run a read-only sweep of the 92 historical DB manifests and a full verification of
   the 16 Brain generations, without rewriting or needlessly re-reading an archive.

pattern-auto checkpoint: spec review then quality.

## Task 1C — Inventory, status and fail-closed cleanup

**RED:** the DB dump and its `.sha256` never become orphans; snapshots with no manifest
stay `legacy_unmanaged`; an invalid manifest cancels the plan before any mutation;
`.drills` appears in none of inventory, status, history, cleanup, or retention.

1. Have `inventory`, `status`, `history`, `cleanup` and `retention` all consume the same
   artifact resolver, while explicitly excluding `${storage_dir}/.drills`.
2. Historical snapshots with no manifest stay `legacy_unmanaged`.
3. Resolve and validate the entire plan before any mutation. The destructive path stays
   disabled in this increment; if the internal executor is kept, any I/O error after
   mutation is reported `partial=true` and red, never as a successful rollback.
4. Add a regression proving that the DB dump and its sidecar never become orphans, even
   with other invalid manifests.
5. `cleanup(dry_run=False)` and `prune_backups(dry_run=False)` fail before any
   scan/mutation; retention called after a run only produces a `dry_run=True` plan until
   a separate destructive review.

pattern-auto checkpoint: spec review then quality.

## Task 1D — Atomic producers and new permissions

1. Dumps, snapshots and metadata are written under temporary names on the same
   filesystem, fsynced and renamed. The shell-less TOC check succeeds before the target
   manifest is published; the receipt therefore never references an unchecked dump. Directory fsync included.
2. The runner creates the run directory exclusively, publishes the canonical receipt
   after all targets, then `.complete` holding its SHA-256, atomically and last, on
   success.
3. New directories are `0700`; each dump, snapshot, manifest, history entry, SHA and
   report gets an explicit `chmod(0600)` after write/copy. `umask` alone is not enough.
4. Produce V2 for new outputs without rewriting historical archives.

pattern-auto checkpoint: spec review then quality.

## Task 2A — Vendored contract, profile and preflight

**RED:**

- a vendored contract or SQL whose SHA differs from the Brain canonical one fails;
- an unknown DSL field, free-form SQL, an unbounded predicate or a missing contract binding fails;
- a floating image, insufficient capacity, an unavailable lock or a target with no profile are refused;
- a profile allowing a published port, bind, volume, network or free-form Docker
  argument is invalid.

**GREEN:**

1. Create the canonical Brain contract and its vendored copy; compare their SHAs at the
   gates.
2. Also create the canonical and vendored fixed SQL attestation. Validate the bounded
   DSL and each predicate negatively; no free-form SQL.
3. Preflight: local image/digest, Docker, capacity, limits and concurrency lock.
4. The old `restore-test --host/--port` exits non-zero; no live path remains.

pattern-auto checkpoint: spec review then quality.

## Task 2B — Docker lifecycle and streaming

**RED:** any non-zero `pg_restore` fails regardless of stderr; no `asyncpg.connect`
call, `CREATE/DROP DATABASE` against a supplied host, or shell is possible; a successful
create with a failed start, an ambiguous state, a timeout, a truncated gzip, a broken
pipe, two concurrent drills and a non-zero cleanup all fail.

1. `docker create`, capture the CID, inspect limits/mounts/network/ports, then `docker start`.
   If create/start returns an ambiguous state, resolve only the random
   name, validate its label, then capture the CID; never delete by name or label.
2. Use the hard-coded limits and startup/restore/checks/global timeouts.
3. Stream from the already-validated descriptor into `pg_restore --dbname=restore
   --single-transaction --exit-on-error`; handle broken pipe and timeout.
4. Extend the shell ban to the current TOC check in `docker_dump.py`.
5. After cleanup, verify the absence of the CID and the captured volumes; any ambiguity
   leaves the drill red with bounded identifiers for intervention, never a deletion
   glob.

pattern-auto checkpoint: spec review then quality.

## Task 2C — Invariants, attestation and redaction

**RED:** zero tables, a missing head/extension/count/`vector(1536)` typmod or invariant,
an unvalidated constraint, a `skipped` result, a DSL/fixed-SQL mismatch, an incomplete
cleanup, or a secret sentinel in an output all turn the drill red. A fault-injection test
forces a false result on only one of the two paths and proves the mismatch.

1. Run the contract's exact checks, each with `expected`, `observed`, `status`; no
   required check may be missing or skipped.
2. Run the fixed SQL in a second `psql` process, without reusing the DSL compiler,
   canonicalize its JSON separately, then compare the two attestations exactly.
3. Build the result in memory, clean up, compute the verdict, then write atomically to
   `${storage_dir}/.drills/<run>/<target>/<drill-id>.json`, after rebuilding and checking
   the path's containment. Every inventory or retention command ignores this root.
4. Record the dump hash, resolved image, `contract_id`/SHA, `attestation_sql_sha256`,
   head, counts, per-phase durations and cleanup. For legacy, write
   `source_completeness_comparison=unavailable`.
5. Keep no raw transcript. Allowed diagnostics: phase, return code, size and hash of
   stderr. A secret sentinel is absent from the report, logs and CLI.

pattern-auto checkpoint: spec review then quality.

## Task 2D — CLI and opt-in Docker test

1. Expose `restore-drill RUN TARGET`; no implicit `latest` selector. Validate `RUN` and
   `TARGET` before any filesystem access, then rebuild the paths under their roots.
2. Keep the hermetic mocked suite and add a separate opt-in Docker integration test.
3. The real drill of the pinned run is mandatory before SHIP.

pattern-auto checkpoint: spec review then quality.

## Task 3 — Prove the pinned Brain run

1. Before the run, record a normalized state for PostgreSQL and Neo4j: ID,
   running/status, restart count and real healthcheck; do not compare the full Docker JSON.
2. Run `verify-target` on the run's five DB manifests; obtain non-zero counts.
   `verify-run` stays non-zero/completeness unknown for this legacy run with no snapshot
   manifests.
3. Launch `restore-drill` with the pinned pgvector image already present locally.
4. The second independent attestation happens before cleanup in the engine; compare the
   two results in the report and measure the RTO.
5. Prove exact-CID cleanup, zero remaining labeled container/volume, and identical live
   IDs.
6. Index the synthesized report in Brain as a "local attestation", not durable proof
   against host loss.

## Task 4 — Harden new writes without mutating history

1. Apply `umask 077` to the CLI and `UMask=0077` to the systemd template.
2. Prove `0700/0600` modes on the local dump, Docker, SSH, snapshot, manifest, SHA,
   history and report in a temporary output.
3. Add a permission command in dry-run only if needed; no `--execute` option in this
   increment without a separate review.
4. Document that compose snapshots are secret and must never leave in the clear.
5. Fix the documentation's "automatic restore" promise until it is genuinely wired.

## Task 5 — Gates, reviews and coordinated commits

1. Full `red-backup` tests, Ruff `src/` and the modified modules, mypy if added to the project.
2. Targeted Brain tests and the affected unit suite.
3. Verify that no Brain migration or historical archive was modified.
4. `gitnexus_detect_changes` in each indexed repo.
5. Local multi-perspective review, reflexion and the pattern-auto final judge; `SHIP`
   verdict.
6. Atomic commits, separate per repo; no automatic push/merge while user worktrees are
   dirty.
7. Set the Brain feature to `building`, not `done`.
8. Record the commit SHAs, cleanly remove the `/tmp` worktree after committing, and
   verify the coordinated branches remain recoverable. The main red-backup checkout
   stays unchanged: it is the immediate code rollback. The DR-v1 cron, still unchanged at
   checkpoint B2, has since been removed in favor of the active DR-v3 timer.

## Batches requiring new authority

### Graph ledger — PostgreSQL restore + Neo4j rebuild (option A)

The exact Neo4j restore initially described is superseded for the graph ledger. The
DR-v5 run `20260724_150315` restores PostgreSQL 16 at head 037 into an isolated target
and passes 24/24 checks with an independent SQL attestation. An offline window still
needs to replay roles, owners and ACLs, then fully rebuild a dedicated, empty Neo4j
database with the graph recovery protocol introduced in 035. Then compare counts,
relations by type, constraints and semantic queries. A Neo4j dump cannot close any of
these gates.

### Encrypted off-host

After choosing another failure domain: encrypt before transfer, use a provisioned
`known_hosts` and `StrictHostKeyChecking=yes`, staging + atomic rename, remote checksum,
signed/timestamped receipt, independent retention, and an escrowed recovery key off the
PC server. Another path on the same NVMe does not count.

### Scheduling, alerting and historical permissions

The DR-v3 timer is installed, persistent and active; the DR-v1 cron was removed after
the historical automatic proofs. Current directories and receipts use `0700/0600`.
Still to be enabled: DR-v5 in a separate delivery, proving a scheduled cycle under this
authority, enabling the daily watchdog, and receiving a triggered Discord alert. Any
historical permission fix still follows an inventory/dry-run and a documented rollback.

## Acceptance criteria for the autonomous increment

- `verify-target` returns a non-zero count for every DB manifest and can no longer report `0/0 OK`.
- `verify-run` can only be green if a V2 receipt proves the seven targets complete; any
  incomplete legacy run stays unattested.
- Any artifact that is missing, truncated, corrupted, empty or outside the root makes
  the CLI non-zero.
- The Brain dump of run `20260711_030001` genuinely restores into a pgvector container
  with no network, port, bind or host volume.
- A non-zero `pg_restore`, zero tables, a missing invariant, or an incomplete cleanup
  turn the drill red.
- The local JSON attestation contains the hash, image, contract, head, checks, counts,
  durations and cleanup; it is written after cleanup, even on failure.
- Live containers keep their ID, state, restart count and real health; no temporary
  container/volume remains.
- New outputs are `0700/0600` and no historical archive is rewritten.
- Tests/gates/reviews are green in both repos.
- DR1 stays `building` with Neo4j/off-host/scheduler explicitly open.

## Provisional SLOs to measure

- Nominal PostgreSQL RPO: 24 h when the daily job succeeds; no maximum RPO is
  guaranteed before persistent scheduling and a freshness alert < 26 h.
- Expected freshness: local backup and future off-host copy < 26 h.
- PostgreSQL restore RTO on a ready Docker host: 30-minute target.
- RTO after complete host loss: 2-hour target, not proven in this increment.
- Daily vs weekly PG drill to be decided after measuring the cost; periodic Neo4j
  rebuild from this restored PostgreSQL once the offline window is authorized.

An RPO below 24 h requires a separate PITR project (pgBackRest/WAL-G); the daily dump
cannot promise it.

## Operational checkpoint B3

The [B3 checkpoint](2026-07-12-disaster-recovery-b3-operational-evidence.md)
authenticates two consecutive automatic systemd cycles: runs `20260712_010222` and
`20260713_010009`, each with seven targets and 42 artifacts. The "two automatic cycles"
blocker is closed. The current PostgreSQL proof is renewed by the DR-v5 run
`20260724_150315` at head 037 with 24/24 checks. Replaying roles, owners and ACLs, the
dedicated Neo4j rebuild, the scheduled DR-v5 activation, the encrypted off-host copy and
the Discord alert remain open; DR1 keeps the `building` status.
