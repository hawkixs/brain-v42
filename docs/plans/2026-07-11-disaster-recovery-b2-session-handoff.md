---
title: "Verifiable disaster recovery — B2 session handoff"
status: completed
summary: "Historical checkpoint B2 delivered; do not resume its paths or its 035 head on the current production."
tags:
  - disaster-recovery
  - red-backup
  - handoff
  - pattern-auto
  - sol-ultra
---

# Verifiable disaster recovery — B2 session handoff

> **Historical closure — July 24, 2026.** B2 has been delivered; do not resume the instructions in
> this handoff. Brain decision `3d3d72e4-acb7-49fe-aabb-1618e648e627` adopted option A at
> head 035 then deployed. Production is now at head 037: any new proof must
> restore the exact deployed head, and no downgrade is authorized to follow this document.

> This checkpoint is no longer a resume point. The canonical sources remain the
> [Sol Ultra roadmap](2026-07-11-sol-ultra-audit-roadmap-plan.md) and the
> [DR implementation plan](2026-07-11-disaster-recovery-verified-implementation-plan.md).
> Resume the current effort from these sources and the DR-v3 ticket, not from the branches or
> historical worktrees below.

## State to resume

| Repo | Branch and checkpoint | Verified state |
|---|---|---|
| `brain_v42` | `codex/disaster-recovery-verified`; parent code checkpoint `39acc7df15eaf0a5a89fe983fabf8384a7ef8c26` | the commit containing this handoff is the fifth local commit ahead of `origin/main`; no upstream |
| isolated `red-backup` | `/tmp/red-backup-dr1`; `codex/disaster-recovery-verified` at `a6517bfdd62e2703e001adda0cf67ccc0fb0d2c2` | clean; ten commits ahead of `origin/main` |
| main `red-backup` | `/home/hawixs/hawkixs_infra/git_repo/ReD_v1/projects/red-backup`; `main` at `5328089796d4f795afd9a51445a6748c5cab320c` | clean and identical to `origin/main` |

The four Brain commits preceding the handoff, created locally, are `46ca070`, `0846fbd`,
`8e3ff0f` and `39acc7d`.
The ten `red-backup` commits, oldest to most recent, are `984ce8b`, `5ba1c75`,
`19eb7d7`, `0b84ffe`, `8bc6366`, `712dbbc`, `2c021aa`, `573bf58`, `0edf3b6` and
`a6517bf`.

B1 delivers the fail-closed models and checks, non-destructive retention, the
atomic publication of artifacts, DB streaming, the dormant V2 producers, the
immutable DR authority and the exact publication of a receipt to seven targets followed by
`.complete`. The closure gate reports **932 tests passed, 2 skipped and three known
`AsyncMock` warnings**, with Ruff on the modified Python files, `git diff --check` and three
independent `SHIP` code reviews. The format-check is not green across all
already-modified Python files. No JUnit report or git note persists these results: the
B2 session must replay all gates and not infer the format from this checkpoint.

The pipeline remains dormant at this checkpoint: `runner.py`, `config/backup.yaml` and
`deploy/systemd/*` are identical to `origin/main`; the CLI does not load
`RecoveryAuthorityV1`; `run_all()` uses the legacy producers; the
`load_dr_v1_authority()` and `publish_completed_run_v2()` functions have no production caller.
`verify_run()` does not yet compare `receipt.policy_sha256`. No deployment is reflected
by the repos or unit files visible. Since the systemd bus and `crontab -l` are forbidden in
the sandbox, the live runtime and cron were not revalidated by the closure pass.

## B2 — mandatory next slice

**Activation ban:** do not wire up the runner or the CLI before
`verify_run()` authenticates the exact authority, notably `policy_id` and `policy_sha256`, before
opening target manifests.

Implementation order:

1. move `RecoveryAuthorityV1`'s common validation into `recovery_profile.py`, while
   keeping a clean writer-specific public error envelope;
2. make `verify_run()` accept the immutable authority and exactly compare the policy ID
   and its SHA against the receipt; a historical run without a receipt stays
   `completeness=unknown`, while a receipt predating the policy SHA is invalid;
3. make `run_all()` accept `authority` as a named argument and run exactly the
   seven authorized V2 producers; keep going after ordinary target errors, but never
   catch a cancellation or a control signal;
4. publish the receipt then `.complete` only after seven successes; set `all_success=True`
   only after the durable marker and record a run error separately;
5. load the authority in the CLI before `run` and `verify-run`; any completion failure must
   exit 1, forbid retention and follow the red-alert path;
6. then harden the systemd template and its tests with `UMask=0077`, explicit read-only
   SSH access through `ProtectHome=tmpfs`, and a suitable global timeout (current
   target: `TimeoutStartSec=5400`); install nothing;
7. replay the failure injections, the full suite and the pattern-auto reviews before any
   operational change.

Definition of done for B2: a run can only go green with the exact authority, seven valid V2
manifests, a canonical receipt and its durable marker. Ordinary errors are recorded
without a false green; `KeyboardInterrupt`, `SystemExit` and cancellation propagate without a marker.
The CLI fails without launching retention on any uncertain completion. Old runs without a
receipt remain readable but unattested.

GitNexus blast radius recorded before B2: `verify_run` is **HIGH** with 20 direct
callers, `run_all` **MEDIUM** with 12 callers and `_validate_authority` **MEDIUM**. Warn
before modifying `verify_run`, then rerun `gitnexus_impact` on each symbol. The
`red-backup` index is fresh at `a6517bf` (5,014 nodes, 10,491 relations, 267 flows). The
Brain index was refreshed at the parent `39acc7d` (15,347 nodes, 31,724 relations, 300 flows); it will therefore be
technically behind the documentary handoff commit. Reindex it before any new
Brain symbol edit.

## Invariants to preserve

These Brain user changes are out of scope. Do not edit, format or stage them:

| File | Expected SHA-256 |
|---|---|
| `AGENTS.md` | `02a2831a24a28f4de44403a425c94aec4342da604de7b6566566f93fc90f0a21` |
| `CLAUDE.md` | `b92280a56a73c5ecc2f52b9b7b3e3d5a1540174536ae7ff767de84a8909c1a60` |
| `uv.lock` | `3728131a4dfe368004d424e29fd30068987e40dead36d95af6aa7478f78331c2` |

The SHA-256 of their aggregated Git diff is
`8bf616c19812ea0095a53c4831275e3579be0d9f116a1b2e01c2a0b42bdbc4a3`.
Work exclusively in `/tmp/red-backup-dr1`, never in the main checkout.
Do not push, merge, install systemd, modify cron, enable cleanup/prune, touch live
Neo4j, nor write to an off-site destination without new operator authority.

Accepted P2 debts: `run_receipt_v2.py` remains too large; private helpers and the legacy
loader are strongly coupled; a failure injection identified a micro-window
during acquisition that could leave a private FD/temp file without creating a false commit. A close
error combined with a control signal can also be turned into a fail-closed failure.
An arbitrary private callback added after the validated detach also remains outside the trusted
code contract. These debts do not block B2 as long as the invariants "no false receipt, no
false marker" remain proven.

DR1 will remain `building` after B2. The truly isolated PostgreSQL restore is not yet
implemented; `restore_sandbox.py`, `restore_checks.py`, `restore_report.py` and `restore-drill`
are absent. Also still open at this checkpoint were: option A proof, encrypted copy outside the
failure domain, scheduling/alerting and historical permissions. The current proof requires the
exact deployed head (`037` currently), not the 035 head of this handoff.

## New session bootstrap

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git status --short --branch
git rev-parse HEAD
git rev-parse HEAD^
git rev-list --left-right --count origin/main...HEAD
git diff -- AGENTS.md CLAUDE.md uv.lock | sha256sum
sha256sum AGENTS.md CLAUDE.md uv.lock

git -C /home/hawixs/hawkixs_infra/git_repo/ReD_v1/projects/red-backup \
  status --short --branch
git -C /home/hawixs/hawkixs_infra/git_repo/ReD_v1/projects/red-backup \
  worktree list --porcelain

git -C /tmp/red-backup-dr1 status --short --branch
git -C /tmp/red-backup-dr1 rev-parse HEAD
git -C /tmp/red-backup-dr1 rev-list --left-right --count origin/main...HEAD
```

Expected outputs are five Brain commits ahead of `origin/main`, `HEAD^` at `39acc7d`, a
clean isolated worktree, `0 10` for the `red-backup` divergence and the four exact user
hashes. If the `/tmp/red-backup-dr1` directory has disappeared, first check that the
local branch still points to `a6517bf`, remove only its stale worktree
registration, then recreate a worktree from that branch — never from `origin/main`.

Before any symbol edit: read `AGENTS.md`, refresh the index if stale and run
`gitnexus_impact`. Before commit: full suite, Ruff/format/diff-check,
`gitnexus_detect_changes`, final review and explicit staging of only the B2 files.
