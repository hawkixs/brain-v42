# Plan index inventory and repair

## Scope

`indexed_plans` carries rows whose `file_path` no longer names anything the
scanner produces. This runbook measures them, says what each one is, and
repairs them. It is read-only until `--apply`, and `--apply` never deletes.

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

**Nothing is deleted, in any mode.** Archiving takes a row out of every default
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

- **Why** — the reason behind each non-canonical verdict. `symlink_alias` and
  `content_found_on_disk_by_hash` are rewrites; the rest archive.
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
absolute and its plans have been indexed once, not before.

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

The recovery file is written **before** the transaction, mode `0600`, and the
command refuses to start if it already exists — overwriting the previous run's
recovery would remove the only way back. Every mutation runs in one
transaction: a half-applied repair is a state nobody measured.

`--max-mutations` (default 500) refuses an apply larger than the one this tool
was reviewed for.

### 5. Verify, project by project

```bash
uv run python scripts/plan_index_inventory.py --verify
```

Live rows against real plan files, per project, exiting `1` on any mismatch. Per
project and never as one total: a global count balances a project missing ten
plans against another carrying ten it does not own, and reads green.

A project showing `0` rows against a non-zero file count has never been
indexed — run `brain_reindex_plans(project_key=...)`, then verify again.

## Recovery

The recovery file holds the `before` state of every touched row: `file_path`,
`project_key`, `freshness_status`. Restoring is an `UPDATE` per row back to
`before`. Nothing was deleted, so nothing has to be re-embedded.

## Known trap: the older repair runbook

`docs/PLAN_INDEX_REPAIR_RUNBOOK.md` and `maintenance/plan_index_repair.py` are
a different, earlier boundary, scoped to seven hard-coded projects under
`BRAIN_PLAN_PROJECTS_ROOT`. Its normative block pins Alembic head **052**; the
head must be re-measured before that runbook is replayed, never copied from it.
This tool shares none of that state and pins no head.
