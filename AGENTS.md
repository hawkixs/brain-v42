# AGENTS.md — brain-v42

## Common instructions — mandatory reading

**Before any action on this project, read [CLAUDE.md](CLAUDE.md) in full, even though this
`AGENTS.md` exists.** If the output is truncated, resume reading section by section until the
whole document has been read.

`CLAUDE.md` is the shared source of this project's rules for Claude Code, Codex and OpenCode.
Its working instructions apply to every harness, not just its facts and commands. This file
provides the Codex / OpenCode adaptation and keeps the GitNexus instructions; it does not
duplicate the shared rules, so that their next updates stay aligned. System instructions,
developer instructions and explicit user requests keep their precedence; a nearer `AGENTS.md`
narrows its own scope.

Mandatory reading covers in particular:

| Domain | `CLAUDE.md` sections to apply |
|---|---|
| Real state and provenance of measurements | "Living state does NOT live in this file" |
| TDD and verification | "TDD workflow", "Tests", "Code conventions" |
| Brain: project key, sessions, memory and tools | "Second Cerveau" |
| Installation and code organisation | "Quick start", "Project structure" |
| Production, releases, data and secrets | "Architecture", "Configuration" |
| CI, commits, PRs and language on GitHub | "CI/CD", "Git workflow" |
| Impact analysis and index freshness | "GitNexus — operating notes" |

## This repository is public

`hawkixs/brain-v42` is public (Apache-2.0). **Anything you write here is published.**

- No LAN address, no username, no SSH key path, no personal filesystem path, no token.
  Machine access lives in brain:
  `brain_get(entity_type="learning", entity_id="2a23883d")`, or
  `brain_search(query="dev machines access", project_key="brain-v42")`.
- Everything is written in **English**: commits, branches, PRs, docs, code comments, test
  names, and these instruction files. Conversation with the operator stays in French.
- Plans, specs, design notes, receipts and handoffs go under `internal/` (the private
  `brain-v42-internal` repository cloned there, git-ignored), never directly under `docs/`.
  See `CLAUDE.md`, "Internal working material".

## Adaptation for Codex and OpenCode

- Read mentions of "Claude Code" as designating whichever agent is working on this project.
  Adapt only the Claude-specific syntax (tools, models, permissions, orchestration) to the
  real capabilities of your harness; that exempts you from no project rule about Brain, TDD,
  tests, Git or delivery.
- Personal harness instructions complement these rules: the installed skills and the `red-*`
  roles when delegation is required. A reference to a model, a tool or a permission in
  `CLAUDE.md` does not prove it is available in this session. Discover the tools actually
  present and respect their schemas.
- Loading these instructions opens, resumes and closes no Brain session. The explicit cycle
  described in `CLAUDE.md` still applies. For an open session, keep `client_key`,
  `session.id` and `started_focus_revision` together.
- If `CLAUDE.md` is absent or unreadable, report that limit explicitly; do not claim to have
  loaded its instructions and do not invent the project state.

## What can hurt here

| Rule | Why |
|---|---|
| **The PR gate is never bypassed** | `continuous-integration.yml` triggers on `pull_request` only. A direct push to `main` runs **no** lint, test or security gate. `main` is the base of every immutable release. No `--admin`, no `--no-verify`, no branch-protection bypass. |
| **A merge on `main` is not live** | The systemd writers run an immutable release from `~/.local/share/brain-v42/releases/<sha>/`. A new release must be built and switched over. |
| **Never copy a measured value** | This file's ancestor announced a wrong schema head for ten days. Measure it, or read it with `brain_fact_get`. |
| **`brain_test` is shared and not empty** | Never `TRUNCATE`. Integration fixtures place themselves in the future or in a rolled-back transaction. |
| **Never add an `alembic upgrade head` gate on a shared database** | It would stay ahead of `main` and the residual guard would refuse every later integration. |
| **`ruff check` alone is not enough** | CI also runs `ruff format --check`. And `mypy src/`. |
| **The module-layering guard is static** | A runtime `import_module` is invisible to it and bypasses the rule rather than respecting it. |

## Maintaining these instructions

Keep the shared rules in `CLAUDE.md` and the harness adaptations here. Both files are
tracked since 2026-09-22: change them through a pull request, like any other file.
GitNexus writes into neither of them any more (`--index-only`, ticket `07b9e892`): if a
diff ever shows a generated `gitnexus:start` block reappearing, a reindex ran without that
flag — revert the block rather than keep it.

## GitNexus

The GitNexus rules — impact before an edit, `detect_changes` before a commit, `UNKNOWN`
risk treated as unresolved, `--index-only` on every reindex — live in `CLAUDE.md`, section
"GitNexus — operating notes", and they bind every harness. Use the GitNexus MCP tools where
your harness exposes them, the `gitnexus` CLI otherwise (`gitnexus impact`,
`gitnexus detect-changes`, …). No GitNexus skill is installed in this repository:
`.agents/skills/` stays empty on purpose.

## GitNexus — cross-repo groups

This repository is listed under the **GitNexus group `red-triad`** (see
`~/.gitnexus/groups/`). For a blast radius that crosses repository boundaries, use the MCP
tools `group_impact`, `group_sync`, `group_query`, `group_contracts`, `group_status` and
`group_list`. From a terminal: `npx gitnexus group list`,
`npx gitnexus group sync <name>`,
`npx gitnexus group impact <name> --target <symbol> --repo <group-path>`.

### Indexing rule

**Index the canonical root, never a worktree.** The registry once held two `brain-v42`
entries — the root and a worktree 893 commits behind. An `impact` or a `detect_changes` could
therefore resolve against a stale index without flagging it. When in doubt about freshness,
check with `npx gitnexus list` that exactly one `brain-v42` entry exists.

> **A stale index answers CRITICAL/high wrongly.** Check freshness first; if the index is
> stale, measure the blast radius by hand and say so, rather than reporting a false verdict.

### Reindex: always `--index-only`

`gitnexus analyze` without it reinstalls standard skills into `.claude/skills/` and
`.agents/skills/` and writes a generated section into `CLAUDE.md` and this file. The nightly
reindex (`scripts/gitnexus-nightly.sh`, cron 04:30) runs this line; a manual reindex reuses
it:

```bash
gitnexus analyze --embeddings --index-only --wal-checkpoint-threshold 67108864 .
```
