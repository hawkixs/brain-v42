# GitNexus Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire up the nightly reindex cron, persist the integration knowledge into brain-v42 memory, and leave a documented follow-up for hook-latency observation so Phase 2 decisions are grounded in data.

**Architecture:** All prerequisite work (install, OS upgrade, first analyze, hooks/skills auto-registration) is already done on the dev host as part of the brainstorming PoC. This plan only covers the remaining administrative deltas: a cron helper script, a crontab entry, and four brain MCP writes (decision, runbook, snippet, follow-up learning).

**Tech Stack:** bash + crontab (system), brain-v42 MCP tools (`brain_log_decision`, `brain_create_runbook`, `brain_save_snippet`, `brain_learn`), GitNexus CLI 1.6.2 (already installed at `/home/hawixs/.npm-global/bin/gitnexus`).

**Reference spec:** `docs/superpowers/specs/2026-04-20-gitnexus-integration-design.md`

---

## File Structure

- Create: `scripts/gitnexus-nightly.sh` — bash helper that runs `gitnexus analyze` with logging and log rotation. One clear responsibility: "reindex brain-v42, write a rotated log, exit cleanly even on failure".
- No new Python files, no test files — tasks 3-6 call brain MCP tools, which are validated by querying the brain back after the write.
- User-level state (crontab entry, brain entries) is not committed. Only `scripts/gitnexus-nightly.sh` lands in the repo.

---

### Task 1: Nightly reindex helper script

**Files:**
- Create: `scripts/gitnexus-nightly.sh`

- [ ] **Step 1: Create the helper script**

Create the file with this exact content:

```bash
#!/usr/bin/env bash
# Nightly GitNexus reindex for brain-v42.
# Invoked by user crontab at 04:30. See spec:
#   docs/superpowers/specs/2026-04-20-gitnexus-integration-design.md

set -u  # note: NOT -e — we want the script to keep going past a single reindex failure so the rotation step still runs.

REPO_DIR="/home/hawixs/hawkixs_infra/git_repo/brain_v42"
LOG_DIR="/tmp"
LOG_FILE="${LOG_DIR}/gitnexus-nightly.log"
GITNEXUS_BIN="/home/hawixs/.npm-global/bin/gitnexus"

# Rotate previous log (keep last 7 nights).
for i in 6 5 4 3 2 1; do
  src="${LOG_FILE}.${i}"
  dst="${LOG_FILE}.$((i+1))"
  [ -f "$src" ] && mv "$src" "$dst"
done
[ -f "$LOG_FILE" ] && mv "$LOG_FILE" "${LOG_FILE}.1"

{
  echo "=== gitnexus-nightly run at $(date --iso-8601=seconds) ==="
  cd "$REPO_DIR" || { echo "ERROR: cannot cd to $REPO_DIR"; exit 2; }
  "$GITNEXUS_BIN" analyze --embeddings --skip-agents-md . 2>&1
  status=$?
  echo "=== exit status: $status ==="
  exit "$status"
} > "$LOG_FILE" 2>&1
```

- [ ] **Step 2: Make it executable**

Run:
```bash
chmod +x /home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/gitnexus-nightly.sh
```

Verify: `ls -la scripts/gitnexus-nightly.sh` shows `-rwxr-xr-x`.

- [ ] **Step 3: Dry-run the script**

Run:
```bash
/home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/gitnexus-nightly.sh
```

Expected:
- Script runs ~6 minutes (real `gitnexus analyze` execution).
- `/tmp/gitnexus-nightly.log` created, ends with `=== exit status: 0 ===`.
- Output contains `Repository indexed successfully` and node/edge counts.

If exit status is non-zero: read the log, do NOT proceed to Task 2 until the script runs cleanly on demand.

- [ ] **Step 4: Verify log rotation works**

Run the script a second time:
```bash
/home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/gitnexus-nightly.sh
```

Then:
```bash
ls -la /tmp/gitnexus-nightly.log*
```

Expected: both `/tmp/gitnexus-nightly.log` and `/tmp/gitnexus-nightly.log.1` exist, with the `.1` being the older run.

- [ ] **Step 5: Commit**

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git add scripts/gitnexus-nightly.sh
git commit -m "chore(gitnexus): nightly reindex helper with log rotation

Bash helper invoked by user crontab (04:30 daily). Reindexes brain-v42
with --embeddings, writes to /tmp/gitnexus-nightly.log, rotates last 7
nights. Part of gitnexus integration spec.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Install the crontab entry

**Files:** none (user-level crontab state, not in repo).

- [ ] **Step 1: Capture current crontab**

Run:
```bash
crontab -l 2>/dev/null > /tmp/crontab.before.gitnexus.txt || touch /tmp/crontab.before.gitnexus.txt
cat /tmp/crontab.before.gitnexus.txt
```

Expected: shows existing entries (if any) or empty file. Keep this file as safety backup for the rest of this task.

- [ ] **Step 2: Verify no existing gitnexus entry**

Run:
```bash
grep -c gitnexus /tmp/crontab.before.gitnexus.txt || true
```

Expected: `0`. If `1` or more, stop and inspect — a previous attempt may already have added an entry.

- [ ] **Step 3: Build the new crontab content**

Run:
```bash
{
  cat /tmp/crontab.before.gitnexus.txt
  echo ""
  echo "# gitnexus nightly reindex — see docs/superpowers/specs/2026-04-20-gitnexus-integration-design.md"
  echo "30 4 * * * /home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/gitnexus-nightly.sh"
} > /tmp/crontab.after.gitnexus.txt
cat /tmp/crontab.after.gitnexus.txt
```

Expected: original entries preserved, followed by one comment line and one cron entry at `30 4 * * *`.

- [ ] **Step 4: Install the new crontab**

Run:
```bash
crontab /tmp/crontab.after.gitnexus.txt
```

No output on success.

- [ ] **Step 5: Verify install**

Run:
```bash
crontab -l | grep -A1 "gitnexus nightly reindex"
```

Expected output (two lines):
```
# gitnexus nightly reindex — see docs/superpowers/specs/2026-04-20-gitnexus-integration-design.md
30 4 * * * /home/hawixs/hawkixs_infra/git_repo/brain_v42/scripts/gitnexus-nightly.sh
```

- [ ] **Step 6: Record the "before" crontab for rollback**

The file `/tmp/crontab.before.gitnexus.txt` is the rollback artefact. Leave it in place — it will be referenced from the runbook saved in Task 4. No git commit for this task (crontab is user state).

---

### Task 3: Persist the integration decision to brain

This task calls the brain MCP tool `brain_log_decision`. The agent executing this task has direct access to `mcp__brain-v42__brain_log_decision` in its tool list.

- [ ] **Step 1: Call `brain_log_decision` with the exact arguments**

Call `mcp__brain-v42__brain_log_decision` with:

```
title: "Integrate GitNexus as second MCP alongside brain-v42 (not fused)"
rationale: |
  GitNexus ships with embedded LadybugDB + bundled transformers.js,
  so the original B plan ("share Neo4j + GPU embedding service 8003")
  is infeasible without forking. Full separation (A') is actually
  better for the primary constraint "zero impact on brain-v42 perf":
  disjoint storage, disjoint compute, disjoint MCP tool namespace.
  Hook scoping (Grep|Glob|Bash only) keeps brain_* untouched.
  Cost: 2 MCP servers side-by-side, 1 extra index to maintain,
  nightly 6-minute cron.
alternatives_considered: |
  B' — fork GitNexus to use our Neo4j + embedding service: too
  expensive (tracking upstream) for a PoC.
  C' — switch to CodeGraphContext (MIT): deferred; revisit if
  GitNexus fails success criteria.
  D' — abandon and stay grep-only: leaves token spend on
  navigation questions on the table.
tags: ["gitnexus", "mcp", "architecture", "code-intelligence", "brain-v42"]
links:
  - "docs/superpowers/specs/2026-04-20-gitnexus-integration-design.md"
  - "docs/superpowers/plans/2026-04-20-gitnexus-integration.md"
```

- [ ] **Step 2: Verify the decision is retrievable**

Call `mcp__brain-v42__brain_search` with:
```
query: "gitnexus integration decision"
types: ["decision"]
```

Expected: at least one result with score ≥ 0.8 whose title starts with `Integrate GitNexus as second MCP`.

If score < 0.8 or no result, recheck step 1 arguments and retry.

---

### Task 4: Create the gitnexus install + kill-switch runbook

This task calls `brain_create_runbook`. It captures both the one-shot install sequence (for reuse on other machines / phase 2 repos) and the full kill switch (for emergency rollback).

- [ ] **Step 1: Call `brain_create_runbook` with the exact arguments**

Call `mcp__brain-v42__brain_create_runbook` with:

```
title: "GitNexus install and kill-switch (brain-v42 host)"
intent: "Install gitnexus on a new dev host, index a repo, and fully roll back if needed."
steps:
  - order: 1
    name: "Verify OS prereq — libstdc++ GLIBCXX_3.4.32"
    command: "strings /lib/x86_64-linux-gnu/libstdc++.so.6 | grep -E 'GLIBCXX_3\\.4\\.3[2-9]' | head -1"
    expected: "prints at least one matching line; if empty, upgrade libstdc++ first"
  - order: 2
    name: "Upgrade libstdc++ if needed (Ubuntu 22.04)"
    command: "sudo add-apt-repository -y ppa:ubuntu-toolchain-r/test && sudo apt update && sudo apt install --only-upgrade -y libstdc++6"
    expected: "ABI-stable upgrade, no reboot needed"
  - order: 3
    name: "Backup ~/.claude.json before gitnexus setup touches it"
    command: "cp ~/.claude.json ~/.claude.json.bak.pre-gitnexus"
    expected: "backup file exists"
  - order: 4
    name: "Install gitnexus globally"
    command: "npm install -g gitnexus"
    expected: "binary at ~/.npm-global/bin/gitnexus, version 1.6.2+"
  - order: 5
    name: "Run setup — auto-writes MCP entry, installs skills and hooks"
    command: "gitnexus setup"
    expected: "Claude Code MCP configured + 7 gitnexus-* skills + Pre/PostToolUse hooks"
  - order: 6
    name: "First analyze — brain-v42 example"
    command: "cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && gitnexus analyze --embeddings --skip-agents-md ."
    expected: "~6 minutes, ends with 'Repository indexed successfully'"
  - order: 7
    name: "Kill switch — full rollback (run if PoC fails)"
    command: |
      npm uninstall -g gitnexus
      find ~/hawkixs_infra/git_repo -maxdepth 3 -type d -name .gitnexus -exec rm -rf {} +
      rm -rf ~/.gitnexus ~/.claude/hooks/gitnexus ~/.claude/skills/gitnexus-*
      mv ~/.claude.json.bak.pre-gitnexus ~/.claude.json
      crontab /tmp/crontab.before.gitnexus.txt
    expected: "gitnexus tools disappear after next Claude Code restart; brain-v42 MCP unaffected"
tags: ["gitnexus", "runbook", "install", "rollback", "kill-switch"]
links:
  - "docs/superpowers/specs/2026-04-20-gitnexus-integration-design.md"
```

**Note on `--skip-agents-md`**: always pass this flag on first analyze to prevent gitnexus from injecting a block into the repo's CLAUDE.md / AGENTS.md.

- [ ] **Step 2: Verify the runbook is retrievable and executable**

Call `mcp__brain-v42__brain_search`:
```
query: "gitnexus install kill switch"
types: ["runbook"]
```

Expected: runbook retrieved, score ≥ 0.8, contains all 7 ordered steps.

---

### Task 5: Save the Phase 2 extension checklist as a snippet

Phase 2 (full Red stack rollout) happens later — we pre-save the checklist so future-me doesn't re-derive it.

- [ ] **Step 1: Call `brain_save_snippet` with the exact arguments**

Call `mcp__brain-v42__brain_save_snippet` with:

```
intent: "Extend gitnexus from brain-v42 PoC to full Red stack"
content: |
  # Pre-flight: confirm brain-v42 dogfood passed the 2-week success criteria
  # (see spec docs/superpowers/specs/2026-04-20-gitnexus-integration-design.md).

  # 1. Analyze each Red repo
  for repo in red-lab red-monitor red-data red-api red-cli red-alerts auto-discord; do
    cd "/home/hawixs/hawkixs_infra/git_repo/${repo}" || continue
    gitnexus analyze --embeddings --skip-agents-md .
  done

  # 2. Register a cross-repo group for group_* tools
  gitnexus group create red-stack
  gitnexus group add red-stack brain-v42 red-lab red-monitor red-data red-api red-cli red-alerts auto-discord

  # 3. Replace the single-repo cron with a multi-repo sweeper
  #    Edit scripts/gitnexus-nightly.sh to iterate the same list,
  #    OR create scripts/gitnexus-nightly-all.sh and repoint crontab.

  # 4. Smoke-test cross-repo impact
  gitnexus impact GPUEmbeddingService --include-tests   # should now list dependants beyond brain-v42
language: "bash"
tags: ["gitnexus", "phase-2", "red-stack", "checklist"]
```

- [ ] **Step 2: Verify snippet retrievable by intent**

Call `mcp__brain-v42__brain_use_snippet`:
```
intent: "Extend gitnexus to full Red stack"
```

Expected: returns the checklist with the 4 numbered steps above.

---

### Task 6: Schedule the 48-hour hook-latency follow-up

We cannot measure hook latency in one pass — it requires 48h of real Grep/Glob/Bash usage inside brain-v42. Persist a `brain_learn` reminder so the measurement is not forgotten.

- [ ] **Step 1: Call `brain_learn` with the exact arguments**

Call `mcp__brain-v42__brain_learn` with:

```
topic: "gitnexus PreToolUse hook latency — 48h measurement"
insight: |
  Follow-up to gitnexus integration (spec 2026-04-20). The PreToolUse hook
  runs `gitnexus augment <pattern>` on every Grep|Glob|Bash inside a cwd
  that contains .gitnexus/. Success criterion: p95 latency < 500ms.

  How to measure (run after ≥ 48h of real usage on 2026-04-22 or later):
    1. Inspect Claude Code telemetry / session logs for hook duration.
    2. If not exposed natively, wrap the hook temporarily:
         mv ~/.claude/hooks/gitnexus/gitnexus-hook.cjs \
            ~/.claude/hooks/gitnexus/gitnexus-hook.orig.cjs
         # wrapper writes (start - end) to /tmp/gitnexus-hook-lat.log
    3. Compute p50 / p95 from /tmp/gitnexus-hook-lat.log.
    4. If p95 ≥ 500ms, open a brain_log_decision: keep-vs-disable hook.
confidence: "high"
tags: ["gitnexus", "hook", "latency", "followup", "measurement-due-2026-04-22"]
```

- [ ] **Step 2: Verify the follow-up is retrievable**

Call `mcp__brain-v42__brain_search`:
```
query: "gitnexus hook latency 48h measurement"
types: ["learning"]
```

Expected: one result, score ≥ 0.8, `tags` contains `measurement-due-2026-04-22`.

---

## Self-Review

**Spec coverage** (cross-checked 2026-04-20) :
- Spec "Reindex Flow" cron entry → Tasks 1 + 2 ✅
- Spec "Implementation Plan" item 1 (cron) → Task 1 + 2 ✅
- Spec "Implementation Plan" item 2 (kill-switch runbook) → Task 4 ✅
- Spec "Implementation Plan" item 3 (decision log) → Task 3 ✅
- Spec "Implementation Plan" item 4 (48h hook latency) → Task 6 ✅
- Spec "Implementation Plan" item 5 (phase 2 checklist snippet) → Task 5 ✅
- Spec "Success Criteria" — two are in-session-measurable (0 incident, cron 7/7 nights), two are qualitative (≥ 5 saved round-trips, hook p95 < 500ms). The qualitative criteria are captured as Task 6 follow-up and by the decision log in Task 3.

**Placeholder scan** : no TBD / TODO / "implement later" / vague error handling left in the plan. All brain MCP calls have concrete argument bodies. All bash commands are complete.

**Type / argument consistency** :
- Script path `scripts/gitnexus-nightly.sh` matches in Tasks 1, 2, 4.
- Crontab backup path `/tmp/crontab.before.gitnexus.txt` matches in Tasks 2 and 4 (step 7).
- Binary path `/home/hawixs/.npm-global/bin/gitnexus` matches in Tasks 1 and 4.
- Backup file `~/.claude.json.bak.pre-gitnexus` matches in Task 4 steps 3 and 7.
- Tag `gitnexus` present on all brain writes for consistent retrieval.

No gaps detected.
