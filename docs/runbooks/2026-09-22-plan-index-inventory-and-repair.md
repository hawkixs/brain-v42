# Plan index inventory and repair

## Scope

`indexed_plans` carries rows whose `file_path` no longer names anything the
scanner produces. This runbook measures them, says what each one is, and
repairs them. It is read-only until `--apply`. Repair preserves every plan,
chunk and feature; an explicit option can detach foreign feature links after
saving their complete records.

It does not cover arming the periodic sweep (`PLAN_INDEX_REFRESH_ENABLED`); see
`docs/OPERATIONS.md`.

## How the rows got wrong

Two independent defects, both now closed at the write side.

**Relative scan paths.** A `plan_scan_paths` entry like `docs/plans` was
resolved against the MCP service's own working directory — the brain-v42
checkout — so several projects indexed brain-v42's plans under their own key.
PR #171 refuses a non-absolute scan path at write time; the reader already
refused it. Neither repairs a row already written.

**Symlinked scan roots.** `ReD_v1/projects/brain-v42` is a symlink to
`git_repo/brain_v42`, and `ReD_v1/projects/auto-discord` to
`git_repo/auto_discord`. The traversal canonicalises, so rows written before
that change carry the alias name and are never matched again. The ticket named
brain-v42's nine; **auto-discord has eleven**.

## What the verdicts mean

| Verdict | What it is | What `--apply` does |
|---|---|---|
| `canonical` | the path is a real file owned by this project | nothing |
| `rewrite` | the content is on disk under another name, another owner, or both | `UPDATE` the path and owner, chunks included |
| `archive_duplicate` | another row already carries this content at its canonical path | `freshness_status = 'archived'` |
| `archive_orphan` | no file carries this content any more | `freshness_status = 'archived'` |

**No plan, chunk or feature is deleted.** Archiving takes a row out of every default
view — `indexed_plan_search_service` filters `freshness_status != 'archived'` —
and leaves every byte in place. An `archive_orphan` row is the *last remaining
copy* of that plan; it is the last row anybody should delete.

## Procedure

### 1. Measure, and read the report

```bash
uv run python scripts/plan_index_inventory.py
```

Exit `0` means nothing to do, `1` means repairs are pending, `2` means the
question was not answered — an unreachable database, or no scan path that
resolves. Two is never a pass.

Read the three sections, not only the counts:

- **Why** — the reason behind each non-canonical verdict. Path aliases,
  ownership corrections and content matches may produce rewrites; use the
  explicit verdict to distinguish these from archives.
- **Scan paths declaring two names for one directory** — a configuration to fix,
  not data. The scan collapses them, so this costs a walk and not a wrong row.
- **Scan paths that resolve to nothing** — these projects are **not indexed at
  all**, and no amount of repair changes that. Fix the path first, with SQL on
  the single column: `brain_set_project_context` is not a PATCH and would
  overwrite focus, blockers, group and metadata.

### 2. Fix the configuration before repairing the data

A project whose scan path resolves to nothing has no disk files for the
inventory to match against, so every one of its rows classifies as
`archive_orphan` — correctly, and uselessly. Repair it after its path is
absolute and resolvable. Repair ownership before indexing missing or changed
files: the indexer's ownership guard correctly refuses to take an existing
path away from another project.

### 3. Dry run the exact mutations

```bash
uv run python scripts/plan_index_inventory.py --json > /tmp/plan-inventory.json
```

The JSON carries one entry per row with its verdict, reason and target. The
plan is deterministic: the same tree and the same rows produce the same
mutations, in the same order, so the run you read is the run that executes.

### 4. Apply

```bash
uv run python scripts/plan_index_inventory.py \
    --apply --recovery-file /tmp/plan-index-recovery-$(date +%F).json
```

Do not apply from an incomplete inventory. `--apply` refuses when any configured
scan path is unresolvable or when an eligible file cannot be hashed; neither
case proves that a missing row is an orphan. Keep plan writers quiescent for the
whole window, including the separate feature-linking transaction used by a
normal reindex.

If a touched plan would remain linked to a feature in another project, the
default apply refuses it. Review the measured foreign links and intended owner
changes before adding `--detach-cross-project-links`. The transaction captures
each complete edge before detaching only foreign links for the touched plan
IDs, including archived plans. It never creates a replacement feature or
deletes a plan, chunk, or feature.

The recovery file is private (`0600`) and exclusive: the command refuses an
existing path because overwriting the previous recovery would remove the only
way back. Every mutation runs in one transaction: a half-applied repair is a
state nobody measured.

`--max-mutations` (default 500) refuses an apply larger than the one this tool
was reviewed for.

### 5. Verify, project by project

```bash
uv run python scripts/plan_index_inventory.py --verify
```

Live rows against real plan files, per project, exiting `1` on any mismatch.
Verification compares the `(project, canonical path, content hash)` coverage,
not merely equal totals: identical counts can still hide swapped ownership or
the wrong file.

Missing or changed files need indexing after ownership repair. During this
incident, use the maintenance `PlanIndexer(link_features=False)` path in
[the Codestral recovery runbook](2026-09-22-plan-codestral-recovery.md), then
verify again. `brain_reindex_plans` also invokes duplicate deletion and is not
the recovery entry point.

## Recovery

Recovery version 2 contains the parent fields changed or checked by repair
(path, owner, freshness/source, hash and timestamps), each affected chunk ID
and project key, and complete feature edges including creation time and score.
It is a repair snapshot, not a full text/vector dump.

Keep writers quiescent for recovery. First verify that current rows still match
the applied state. Restore parent ownership/path/freshness fields and the
recorded chunk project keys together in a transaction. If the explicit detach
option was used, restore only the edges it removed, with their original fields.
Do not overwrite newer edits or disable the parent `updated_at` trigger to
restore an old maintenance timestamp. The repair itself does not change vectors;
filesystem reindexing and vector refresh have separate recovery snapshots.

## Known trap: the older repair runbook

`docs/PLAN_INDEX_REPAIR_RUNBOOK.md` and `maintenance/plan_index_repair.py` are
a different, earlier boundary, scoped to seven hard-coded projects under
`BRAIN_PLAN_PROJECTS_ROOT`. Its normative block pins Alembic head **052**; the
head must be re-measured before that runbook is replayed, never copied from it.
This tool shares none of that state and pins no head.
