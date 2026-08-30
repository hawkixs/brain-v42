# Maintenance campaign — 2026-08-03

Strict maintenance plan opened after the three-week review from 13/07 to 03/08.
Designed to survive context compaction: any session can pick it up exactly as is.

## Why this plan

Three weeks of intense work produced a lot of volume and little observable delivery.
This plan frames the catch-up and sets the rule that prevents a relapse.

## Starting assessment (verified on 2026-08-03)

### The code is healthy — this is not a repair plan

```
ruff check       : All checks passed
ruff format      : 555 files already formatted
mypy src/        : Success, no issues in 170 source files
module cycles   : 0   (preflight ratchet from 30/07 operational)
collected tests  : 6 742
```

No lint debt, no typing debt, acyclic module graph. **The refactor to
carry out is a trim-down, not a correction.** Any task phrased as "fix the
code" on this campaign starts from a false premise.

### The volume is the problem

| Zone | Lines |
|---|---|
| `tests/` | 136,527 |
| `src/` | 42,332 |
| `scripts/` | 24,362 |
| `services/` | 1,380 |
| `docs/` | 54,258 |

**Test-to-production-code ratio: 2.01:1** (136,527 versus 68,074). For a project with a
heavy load of contract and security tests, that is defensible.

> **ERRATUM 2026-08-03.** An earlier version of this plan announced 3.2:1. That figure
> divided tests by `src/` alone, omitting `scripts/` and `services/` — which are
> production code, and which are tested. The actual ratio is 2.01:1.
>
> The same erratum invalidates the "flagship case" cited at the time:
> `tests/unit/test_container_image_pins.py` (11,351 lines) covers
> `scripts/check_container_image_pins.py`, which comes to **11,220 lines, 543 functions and
> 27 classes**. The ratio there is **1.01:1** — proportionate. This test file is not
> over-engineering.
>
> The observation that survives shifts to the source: an image-pin gate became an
> 11,220-line AST static analyzer (scanning Dockerfiles, compose, CI YAML, shell
> and Python SDK calls) documented by **16 lines of docstring total**. The question
> is not "too many tests" but "why this surface, and who can still review it".

Over the three weeks: ≈320 commits, 110,579 insertions, 13,763 lines of markdown.

Breakdown: 122 `fix`, 69 `docs`, 55 `feat`, 51 `test`. Of the 55 `feat`, 21 touch
the MCP surface and only about ten add a capability visible in usage. The other 46
are internal infrastructure. **It is this observation, not the test ratio,
that carries the non-delivery diagnosis.**

### What is broken or blocked

- `brain-v42-dream.service` **failed** since 2026-08-02 06:15 (EXTRACT timeout on a
  backlog of non-comparable embeddings). Ticket `d104660d`, `in_progress`.
- ROADMAP killswitch still **DRY** after 19 clean nights → **335 curation
  proposals** pending since 14/07.
- Prod at migration **037**, repo at **039**.
- Roadmap: ~148 entries including **58 pseudo-features `research`** (auto-promoted learnings).
- **24 tickets** to process, 11 to confirm, 5 waiting on external input. The oldest at 9-11 days.
- 4 `building` items with no closure, including **Sol Ultra** (last activity 24/07).
- **Zero item moved to `done`** over the three weeks.

### Degraded tooling

GitNexus: 21 indexed repos, **2 entries named `brain-v42`** — the canonical root
(24 commits behind) and the `vigilant-euclid-da3597` worktree (**893 commits behind**).
The registry is polluted beyond the Brain: `red-writer` has 9 worktree entries,
`refondrre` has 3, one of them under `/tmp`.

Git: **11 worktrees** (994 MB in `.claude/worktrees` alone), **32 local branches**,
14 merged and 17 unmerged.

`CLAUDE.md` states "15,163 symbols, 31,473 relationships"; the actual index is at
19,245 nodes / 37,418 edges. The docs are stale on this point.

## Decisions made

### D1 — Migrations 038/039: we deploy, we don't abandon

Operator decision from 2026-08-03. The 6,400 lines pending between repo and prod
represent work worth keeping. Consequence: Phase 2.2 **keeps** the 3,820 lines
of `plan_index_repair` tests, and the rollout follows `docs/PLAN_INDEX_REPAIR_RUNBOOK.md`
(isolated restore, order 038→039, final restart).

### D2 — Security deprioritized, but the CI time bomb is defused separately

Operator decision from 2026-08-03: the security track is not a priority.

This decision is **backed by the facts**: on the findings of pipeline 4288
(sha `d2e37925`), Bandit gives 16 Medium / **0 High**, of which 15 are B608 false
positives and 1 is a genuine call (B104, network bind); Gitleaks gives 8+2 candidates,
**all false positives, 0 secrets, 0 rotation needed**. The evidence has already been kept
outside expirable artifacts since 01/08 (`~/.local/state/brain-v42/security-evidence/4288`,
SHA-256 recorded). The real security risk is low.

**But a mechanical consequence remains, independent of the security risk.**
`.gitlab-ci.yml:27` carries `SECURITY_BURN_IN_UNTIL: "2026-08-22"`, and
`test_non_blocking_security_jobs_expire_at_the_burn_in_deadline` asserts
`not (today > deadline and non_blocking)`. The 3 security jobs are still `allow_failure`.

→ **On 2026-08-23, the unit suite goes red in the blocking `test:unit` job**,
for any work at all, including work unrelated to security.

The CI comment itself anticipates both outcomes: *"Flip them to blocking, or move
this date deliberately."* The path compatible with deprioritization is therefore to
**move the date via a logged decision** — a few minutes, not a campaign. That is
task 3.1 below. Handling the 45 pip-audit advisories stays out of scope for this campaign.

## Orchestration protocol

Established on 2026-08-03.

**Verified attribution of the volume.** Of the 320 commits in the period, only 5 carry
a `Co-Authored-By: Claude` trailer. The remaining branches split into 12 `codex/`
versus 5 `claude/`, the merges name `codex/` branches, and Dream runs on
`provider=codex` (`gpt-5.6-terra`, `gpt-5.6-sol`). **The volume was produced by Codex, not
by Claude Code.** The Claude sessions in the period were audits and analysis.

Two distinct problems follow from this, with two distinct corrections:

| Symptom | Origin | Correction |
|---|---|---|
| 110k lines, 406 tests for a pin gate | Codex workflow | Proportionality gate (below) |
| Worktree sprawl (7 of 8 in `.claude/`) | Claude sessions left open | End-of-session hygiene |

The Codex-side amplification is readable in the branch naming — `round2`, `round3`,
`round4`, `round5` on the same ticket, `attempt-1` on others: successive passes each
stacking a layer, with no scope checkpoint.

The protocol below frames Claude's work. **The proportionality gate, on the other hand,
must constrain the Codex workflow first**, since that is where the volume is produced.

| Role | Model | What |
|---|---|---|
| Main thread | Opus | Scoping, sequencing, architecture decisions, git |
| Execution | Sonnet (`red-implementer`, `red-ops`) | The grind, mechanical batches |
| Review | Opus (`red-reviewer`) | **At batch boundaries only**, never per file |

Three hard rules:

1. No subagent spawn under ~4 files or without a broad read — inline is cheaper.
   A spawn starts cold and repays the full context.
2. **No multi-judge panel, no debate, no `pattern-auto`** on this campaign.
3. **GitNexus before grep** for any refactor work — but only once
   Phase 0.1 is closed (see below).

## Phases

### Phase 0 — Unblock tooling *(blocking for Phase 2)*

`CLAUDE.md` requires `gitnexus_impact` before any symbol edit. As long as the index
lies, this rule is a false safety net: you don't refactor on a false impact analysis.

- **0.1** Purge the dead entries from the GitNexus registry, reindex the canonical root,
  fix the rule: **index the root, never worktrees**. Verify that only one
  `brain-v42` entry remains.
- **0.2** Sort the 11 worktrees. Deletion only if the working tree is clean **and**
  the branch is merged or the commit is reachable from `main`. Never `--force`.
- **0.3** Delete the 14 merged branches (`git branch -d` only, never `-D`).
  Sort the 17 unmerged ones into *to integrate* / *to abandon* / *uncertain* — **report
  only, human decision**.

Constraint: the `codex/70cf97a7-round2→round5` chain is work pending
integration (active blocker: rebase/exact-SHA review required). It is not waste.

### Phase 1 — Get production running again

Nothing new enters the Brain as long as Dream is stopped.

- **1.1** Fix Dream (ticket `d104660d`, backlog of non-comparable embeddings).
- **1.2** Flip the ROADMAP killswitch to WET, then process the 335 pending curation
  proposals.
- **1.3** Deploy 038 then 039 to production per D1 and the runbook.

### Phase 2 — ~~Trim-down~~ · **2.1 and 2.2 CANCELED on 2026-08-03**

Measurement refuted the premise. **No test file in the repo exceeds 3:1** against its
actual subject:

| Test file | test | source | ratio |
|---|---|---|---|
| `test_container_image_pins.py` | 11,351 | 11,220 | 1.01:1 |
| `test_plan_index_repair.py` | 1,796 | 1,588 | 1.13:1 |
| `test_roadmap_curate.py` | 1,621 | 1,389 | 1.17:1 |
| `test_formatters.py` | 1,523 | 861 | 1.77:1 |
| `test_plan_index_repair_store.py` | 2,024 | 1,024 | 1.98:1 |
| `test_brain_service.py` | 1,623 | 703 | 2.31:1 |
| `test_pg_base.py` | 1,667 | 704 | **2.37:1** (maximum) |

There is no fat to trim. The phase rested on a ratio computed without its
denominator; a test-deletion campaign on healthy code was narrowly avoided.
The only residual, non-blocking observation: `check_container_image_pins.py` carries
**16 lines of docstring for 543 functions**, which will weigh on reviewing it — but does not
justify any deletion.

- **2.3** *(kept, but reordered)* Purge the `research` pseudo-features — **71**, of which
  64 are proven duplicates. To be done **after** fixing the mechanism that creates them.

### Phase 3 — Backlog

- **3.1** *(date-bound, before 2026-08-22)* Move `SECURITY_BURN_IN_UNTIL` via a logged
  decision, per D2. Defuses the 08-23 CI red without opening the security campaign.
- **3.2** Sort the 24 open tickets, close the stale ones.
- **3.3** Close or abandon the 4 `building` items, including Sol Ultra.

## Anti-relapse rule

**Any batch exceeding ~500 lines of test, or ~3× the src it covers, goes through
explicit validation before development.**

This is the guardrail that was missing until now. Without it, an audit ticket grows to its
maximal form — 406 tests for a pin gate — and nobody asks the proportionality
question.

## Journal

| Date | Phase | Event |
|---|---|---|
| 2026-08-03 | — | Three-week review, plan opened. D1 and D2 made. |
| 2026-08-03 | 0 | Phase 0 delegated (`red-ops`, Sonnet): inventory + provable deletions. Triage of the 17 branches escalated for decision. |
| 2026-08-03 | 0 | 5 worktrees and 13 branches deleted (994 → 753 MB). GitNexus registry cleaned up: a single `brain-v42` entry, on the root. Index reindexed to 19,358 symbols / 37,624 relations. |
| 2026-08-03 | 0 | Patch-id: 11 of the 17 unmerged branches are already in `main` under other SHAs — including the entire `70cf97a7` chain, contrary to the Brain blocker that marked it as pending. Blockers aren't cleaned up when the work lands. |
| 2026-08-03 | 0 | `CLAUDE.md`/`AGENTS.md`: Cross-Repo Groups section restored **outside** the `gitnexus:*` block, plus the indexing rule. Commit `983295c8`. |
| 2026-08-03 | — | `red-*` agents overhauled: automatic quality pipeline removed from `red-implementer`, **Disproportion** category + `TOO_BIG` verdict added to `red-reviewer`, cap of 2 rounds, budget at dispatch. 3 bugs fixed (`brain_session_start` forbidden, hyphenated keys, Opus 5 trailer) and 3 factual errors fixed in `red-ops` (IP, embedding container, VRAM threshold). |
| 2026-08-03 | 1.1 | Cause of the Dream block found: 12 lines out of 477 with no embedding were making EXTRACT fail-closed. 4 of them were "Dream night failure" learnings — Dream was blocking itself with its own failure reports. Backfill 12/12 OK, backlog at 0. |
| 2026-08-03 | 1.1 | The 18 "Dream night failure" learnings moved to `freshness_status='archived'` rather than deleted: no MCP deletion tool exists and a raw `DELETE` would have created PG↔Neo4j drift. `archived` is filtered at the repository level ([pg_base.py:494](src/brain_v42/repositories/pg_base.py:494)), so the effect is real. `updated_at` preserved by disabling the sole timestamp trigger. |
| 2026-08-03 | 0.3 | 11 branches deleted after 100% patch-id verification against an index of the 459 commits of `main` since 2026-07-01. 8 branches and 4 worktrees remain (versus 32 and 11 at the start), 457 MB versus 994. |
| 2026-08-03 | 3.1 | `SECURITY_BURN_IN_UNTIL` moved to 2026-09-30 (commit `c49a2654`), 66 tests green, Brain decision `52eb2232` with the three discarded alternatives. |
| 2026-08-03 | 1.3 | **Complete isolated 038→039 proof.** Today's backup verified (sha256 matching the manifest, `gzip -t` OK), restored into `brain_restore_test` on `pgvector/pgvector:0.8.2-pg16` — the same version as production, after a first attempt on the `pg16` tag gave pgvector 0.8.5 and made the attestation fail. `alembic current = 039 (head)`, attestation `brain-v42-v4-pgrestore.sql` at **25/25 pass**. Receipt and provenance in 0600 under `~/.local/state/brain-v42/migration-039-evidence/`. The `BRAIN_ALEMBIC_ALLOW_PROD` flag was never armed: the gate in [env.py:71](alembic/env.py:71) checks the database's **name**, so renaming the copy is enough to leave it active. |
| 2026-08-03 | — | **DR risk measured.** The backups (`/data/backups`) and the production volume share the `/dev/nvme0n1p3` device, at 92% occupancy. A disk loss takes both out. Yet the DR plan excludes this: "A different path on the same NVMe doesn't count." Aggravating factor: retention is silently violated — policy `max_count: 30`, reality 83 runs and 15 GB since 2026-06-12 — because `prune_backups` raises `ArtifactInventoryError` before pruning. The `red-backup` service therefore exits in failure every night **while the dumps succeed**: the alarm that should flag broken retention has already turned into noise. The roadmap item "Verifiable disaster recovery" therefore legitimately stays `building`. |
| 2026-08-03 | 2.3 | Roadmap measured: **171 features**, of which **71 active `research`**. Of these 71, **64 exactly duplicate** the name of an existing artifact (48 learning topics, 16 decision titles, zero overlap); the other 7 are commit messages promoted into features. The figures from ticket `2e921e14` (58 out of 148) are stale — the problem has grown since 2026-07-30. |
| 2026-08-03 | 1.1 | **The backfill is proven by the 06:00 Dream run.** The nature of the `extract` error changed underneath: run 860 on 02/08 "corpus dedup unavailable: corpus embedding backlog…", run 868 on 03/08 "14 ticket(s) deferred or timed out before run deadline". The fail-closed gate on the backlog no longer triggers; EXTRACT clears deduplication and actually processes tickets. Dream stays at 7/8: the cause is now `extract`'s deadline against the volume, not the embeddings. |
| 2026-08-03 | 1.3 | **Migration 038/039 applied to production**, 06:24 window. Quiescence proven by `pg_stat_activity` (0 connections, 0 prepared transactions) and not just by systemd. Three guards passed before the upgrade: literal URL on `:5433`, port owner verified, database at head 037 with 2,868 learnings. Live attestation **25/25 pass**. Health after restart: `/health` ok, pool 20/0, read-only MCP call working. Repo and production finally aligned on 039. Steps 8+ of the runbook were not run: they concern seven other projects. |
| 2026-08-03 | 2.3 | **Roadmap pollution is a flow, not a stock.** Logging decision `52eb2232` made a `research` pseudo-feature "Move SECURITY_BURN_IN_UNTIL…" appear immediately in the roadmap. Every `brain_log_decision` creates one. Purging the 71 without fixing this path is like emptying a bathtub with the tap still running — and `CLAUDE.md` encourages exactly these captures. Fix the automatic promotion **before** archiving anything. |
| 2026-08-03 | 2.3 | Promotion path traced: [decision_service.py:134](src/brain_v42/services/decision_service.py:134) calls `link_artifact_if_enabled(..., data.title)` as soon as an embedding is stored, then [cluster_guard.py:105](src/brain_v42/services/cluster_guard.py:105) creates a feature when no candidate matches. `ClusterGuard.resolve()` **has no "link without creating" mode**. This is not a bug: it is "Feature Auto-Tracking / Roadmap", status `deployed`, which does not distinguish a knowledge artifact from a work signal. The `signal_type` parameter already present in `resolve()` is the natural hook for the fix. |
| 2026-08-03 | 1.2 | **ROADMAP flip to WET suspended.** The operator authorization predated the discovery of the promotion mechanism. Flipping now would let the nightly curation apply its proposals onto a roadmap just shown to be continuously polluted. To be reconfirmed after the fix. |
| 2026-08-03 | 3.3 | **The 4 `building` items are legitimate, none should be closed.** Verifiable DR: Neo4j rebuild, off-host and alerting remain open. Systemd sandboxing: verified on the live units, `brain-mcp-http` carries `ProtectSystem=full`/`NoNewPrivileges`/`PrivateTmp` per the plan, `brain-metrics` and `brain-v42-dream` have nothing — partial delivery documented. **Sol Ultra is not abandoned**: it is a meta-plan whose SA1, SEC1a, COR1, COR2, COR3, OPS1 and ARC1 batch 1 are `done`; it is blocked on its two hardest constituents, DR1 and SEC2. The "18 days with no movement" measure this dependency, not negligence. |
