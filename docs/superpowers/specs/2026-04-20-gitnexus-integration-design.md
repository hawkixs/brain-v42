# GitNexus Integration — Design Spec

**Date** : 2026-04-20
**Status** : Draft (empirically validated via PoC install)
**Owner** : armandasm@gmail.com
**Related** : brain-v42 MCP stack, Red ecosystem navigation

## Context & Goal

Claude Code burns tokens on repeated `Grep` / `Glob` / `Read` cycles to answer navigation questions like "where is `X` called", "what breaks if I change `Y`", "trace the flow of a signal". GitNexus (`abhigyanpatwari/GitNexus`) is a local code-intelligence MCP server that builds a knowledge graph of the codebase (AST + semantic) and answers those questions in ~1s via a dedicated tool.

**Goal** : integrate GitNexus alongside (not inside) brain-v42 to give Claude a second, specialised navigation surface. Start with brain-v42 as the dogfood target, extend to the full Red stack once validated.

**Non-goals** :
- Replacing any `brain_*` tool. brain-v42 owns persistent memory (decisions, snippets, ADRs, runbooks). GitNexus owns code graph.
- Forking GitNexus or patching its storage.
- Shared infrastructure with brain-v42 (attempted under option B, blocked by GitNexus embedded LadybugDB + bundled transformers.js — see Architecture below).

## Decisions Locked In

| # | Decision | Value |
|---|---|---|
| 1 | Scope phase 1 | brain-v42 only (dogfood) |
| 1' | Scope phase 2 | Full Red stack (red-lab, red-monitor, red-data, red-api, red-cli, red-alerts, auto-discord) |
| 2 | Use cases | All (impact analysis, call chain, dead code, test coverage, cross-repo discovery, symbol search) |
| 3 | Architecture | Full separation — two MCP servers side-by-side, no shared infra |
| 4 | Deployment | MCP stdio local on dev host |
| 5 | Freshness | Hybrid — post-commit hook (notify only) + nightly full reindex cron at 04:30 (avoid 06:01 dream conflict) |

## Architecture

```
┌─────────── Claude Code (dev host) ───────────┐
│                                               │
│   MCP stdio #1 : brain-v42 (existing)         │
│   └─ tools: brain_* (30+)                     │
│   └─ infra: PG 5433 + Neo4j 7687 + GPU 8003   │
│                                               │
│   MCP stdio #2 : gitnexus (new)               │
│   └─ tools: gitnexus_* (16)                   │
│   └─ infra: LadybugDB embedded (.gitnexus/)   │
│   └─ embeds: transformers.js local (CPU/GPU) │
│                                               │
│   Hooks : PreToolUse (Grep|Glob|Bash)         │
│           PostToolUse (Bash → git mutations)  │
│   Skills : 7 gitnexus-* triggered on topic    │
│                                               │
└───────────────────────────────────────────────┘
```

**Isolation invariants**
- Disjoint data : GitNexus writes to `.gitnexus/` (per-repo) and `~/.gitnexus/registry.json` (global). brain-v42 writes to PG / Neo4j. No shared rows, no shared file.
- Disjoint compute : transformers.js runs independently of brain-v42 GPU embedding service (port 8003). No GPU VRAM contention.
- Disjoint MCP surface : `brain_*` vs `gitnexus_*`. Tool collision impossible.
- Hook scoping : PreToolUse matcher = `Grep|Glob|Bash` → never intercepts `brain_*`. Verified in `~/.claude/hooks/gitnexus/gitnexus-hook.cjs`.
- Hook early-exit : hook short-circuits if `.gitnexus/` absent from cwd → zero overhead on unindexed repos.

## Components

### GitNexus CLI & MCP server
- Install : `npm install -g gitnexus` (user-level prefix `~/.npm-global`)
- Version : 1.6.2 (tested)
- MCP entry written to `~/.claude.json` under `mcpServers.gitnexus`
- CLI binary : `~/.npm-global/bin/gitnexus`

### Claude Code hooks (auto-installed by `gitnexus setup`)
- File : `~/.claude/hooks/gitnexus/gitnexus-hook.cjs`
- PreToolUse (matcher `Grep|Glob|Bash`, timeout 10s): calls `gitnexus augment <pattern>` to enrich search results with graph context. No-op if `.gitnexus/` absent in cwd.
- PostToolUse (matcher `Bash`, timeout 10s): on `git commit|merge|rebase|cherry-pick|pull`, compares `HEAD` against `.gitnexus/meta.json`. If stale, notifies agent (does NOT auto-reindex).

### Skills (7, auto-installed by `gitnexus setup`)
- `gitnexus-exploring`, `gitnexus-refactoring`, `gitnexus-pr-review`, `gitnexus-guide`, `gitnexus-impact-analysis`, `gitnexus-cli`, `gitnexus-debugging`
- Location : `~/.claude/skills/gitnexus-*/`
- Loaded on trigger words (no perf cost unless activated)

### Per-repo index
- Location : `.gitnexus/` (gitignored by default)
- Backend : LadybugDB (embedded graph DB, vector-capable, ex-KuzuDB)
- Metadata : `.gitnexus/meta.json` stores `lastCommit`, `stats.embeddings`, etc.

## OS Prerequisite — libstdc++ ≥ GLIBCXX_3.4.32

GitNexus's LadybugDB native addon (`@ladybugdb/core/lbugjs.node`) is compiled against GCC 13+ → needs `GLIBCXX_3.4.32`. Ubuntu 22.04 ships GLIBCXX_3.4.30 max; `gitnexus analyze` crashes at `dlopen` without the upgrade.

**Fix (already applied on dev host)** :
```bash
sudo add-apt-repository -y ppa:ubuntu-toolchain-r/test
sudo apt update
sudo apt install --only-upgrade -y libstdc++6
# Verify: strings /lib/x86_64-linux-gnu/libstdc++.so.6 | grep GLIBCXX_3.4.32
```

Low risk : libstdc++ is ABI-stable, `--only-upgrade` touches nothing else, PPA maintained by Ubuntu core devs. No reboot needed.

## Reindex Flow

| Trigger | Action | Duration (measured) |
|---|---|---|
| First install (done 2026-04-20) | `gitnexus analyze --embeddings --skip-agents-md .` | 365.7s for brain-v42 → 9 357 nodes / 19 449 edges / 310 clusters / 219 flows |
| Post-commit (automatic) | Hook notifies Claude if stale (commit hash mismatch). No reindex. | <100ms |
| Nightly cron | Full reindex at **04:30** (avoid 06:01 dream timer collision) | ~6 min |
| After big refactor | Manual `gitnexus analyze` via Bash when I notice stale context | ~6 min |

`--skip-agents-md` **required** : without it, gitnexus injects a block into project `CLAUDE.md` which I do not want managed by a third-party tool.

Cron entry (to be added to user crontab during implementation) :
```cron
30 4 * * * cd /home/hawixs/hawkixs_infra/git_repo/brain_v42 && /home/hawixs/.npm-global/bin/gitnexus analyze --embeddings --skip-agents-md >> /tmp/gitnexus-nightly.log 2>&1
```

## Tool Surface & Disambiguation

| Need | Tool | Notes |
|---|---|---|
| "Where is X called / what breaks if I change X" | `gitnexus_impact` (upstream, `--include-tests`) | Graph static, ~1s. Empirically validated : `impact GPUEmbeddingService --include-tests` → 60 impacted, risk CRITICAL in 1.3s. |
| "360° on X — callers + callees + imports" | `gitnexus_context` | Empirically validated : `context GPUEmbeddingService` → full incoming+outgoing in 1.2s. |
| "Fuzzy symbol search by concept" | `gitnexus_query` | Semantic via transformers.js embeddings. |
| "Custom Cypher query on the graph" | `gitnexus_cypher` | Escape hatch for unusual relations. |
| "What changed since last index" | `gitnexus_detect_changes` | Post-commit use. |
| "Rename safely across codebase" | `gitnexus_rename` | Graph-aware refactor. |
| "Cross-repo impact (phase 2)" | `gitnexus group_*` (5 tools) | Requires group registration. |
| "Retrieve a past decision / ADR / learning" | **`brain_search`** (unchanged) | Persistent memory, not code. |
| "Save an insight / snippet / ADR / runbook" | **`brain_*`** writers (unchanged) | Memory writes, out of gitnexus scope. |

### Known Gotcha — MCP Tool Entry Points

`gitnexus_impact brain_learn` returns `impactedCount: 0, risk: LOW` because MCP-decorated tool functions have no static callers in Python (the MCP framework calls them reflectively). For MCP entry points, fall back to:
- `Grep` on the tool name to find registrations
- `gitnexus_context brain_learn` to see what the function calls (downstream still works)

This limitation applies to any framework-level entry point (FastAPI routes, decorators, registered handlers). Document in per-session onboarding if we ever add a project-level CLAUDE.md section for gitnexus use.

## Phase 2 Plan — Full Red Stack

After ≥ 1 week of brain-v42 dogfood passes the success criteria (below) :

1. For each Red repo (red-lab, red-monitor, red-data, red-api, red-cli, red-alerts, auto-discord) :
   ```bash
   cd <repo> && gitnexus analyze --embeddings --skip-agents-md
   ```
2. Register repos into a GitNexus group for cross-repo impact :
   ```bash
   gitnexus group create red-stack
   gitnexus group add red-stack brain-v42 red-lab red-monitor ...
   ```
3. Extend nightly cron to iterate over all repos sequentially :
   ```cron
   30 4 * * * /home/hawixs/bin/gitnexus-reindex-all.sh
   ```
4. Validate cross-repo queries : "trace a candle from red-lab ingestion to red-monitor display", "what consumes brain-v42 `/api/cockpit` across the stack".

## Success Criteria (2-week PoC window)

- ≥ 5 real occurrences where `gitnexus_impact` or `gitnexus_context` saved ≥ 3 `Grep` / `Read` round-trips on a concrete task. Tracked qualitatively in session journal.
- Zero brain-v42 incident attributable to gitnexus (MCP crash, hook latency > 1s p95, unexpected writes, CLAUDE.md pollution, etc.).
- PreToolUse hook latency < 500ms p95 inside brain-v42 cwd. Measured by wrapping the hook or via Claude Code telemetry.
- `gitnexus analyze` nightly cron succeeds 7/7 nights.

Fail any of these → proceed to kill switch, document findings in brain, reconsider CodeGraphContext (MIT alternative) for round 2.

## Kill Switch

Full rollback in case of issues :

```bash
# 1. Remove global install
npm uninstall -g gitnexus

# 2. Remove per-repo indexes
find ~/hawkixs_infra/git_repo -maxdepth 3 -type d -name .gitnexus -exec rm -rf {} +
rm -rf ~/.gitnexus

# 3. Remove hooks
rm -rf ~/.claude/hooks/gitnexus

# 4. Remove skills (7 dirs : gitnexus-exploring, gitnexus-refactoring, etc.)
rm -rf ~/.claude/skills/gitnexus-*

# 5. Restore claude.json
mv ~/.claude.json.bak.pre-gitnexus ~/.claude.json

# 6. Manually clean ~/.claude/settings.json : remove the 2 gitnexus hook blocks
#    (Stop + Notification hooks unrelated, leave them alone)

# 7. Remove nightly cron entry

# 8. Restart Claude Code → brain-v42 MCP unaffected
```

## Implementation Plan (to be expanded by writing-plans)

1. Write the nightly reindex cron entry.
2. Add the kill-switch command block to brain-v42 runbooks via `brain_create_runbook`.
3. Log the integration decision via `brain_log_decision` with pointer to this spec.
4. Monitor hook latency for 48h, record p50 / p95 in brain.
5. Draft phase 2 checklist as a brain snippet.

## Open Questions (for implementation phase, not blocking this spec)

- Do we want a wrapper skill or project CLAUDE.md section to remind me of the "MCP entry point gotcha" without reading this doc every time ?
- Is there value in auto-running `gitnexus analyze` in brain-v42 CI to keep a "canonical index" artifact somewhere, or is local sufficient ?

## References

- Install output, empirical latency measurements, 6-min full analyze : captured in session 2026-04-20 03:45–03:58 UTC.
- GitNexus repo : https://github.com/abhigyanpatwari/GitNexus
- brain-v42 learning `f70e2002-0a6e-4451-9c8b-712f1c9618ff` (initial GitNexus note, 2026-03-26).
