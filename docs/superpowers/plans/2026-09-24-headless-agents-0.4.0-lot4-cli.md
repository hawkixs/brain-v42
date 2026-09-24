# headless-agents 0.4.0 — Lot 4 (`ha` CLI) Implementation Plan

**Goal:** Ship spec section 3.4 and close 0.4.0: `allowed_networks` (replacing `require_loopback`, with the Dream's migration), named MCP profiles, and the `ha` CLI (`providers`, `run` read-only and `--write`, `runs`, `clean`).

**Spec:** `docs/specs/2026-09-23-headless-agents-0.4.0-design.md` 3.4 and 5; Brain ticket `8ebebf41`. Entry condition met: the `.git` tripwire and `git_command` (#195, ticket `0b622f47`). Lots 1–3 merged (#190, #192, #194).

## Tasks (TDD: each test seen failing first; one commit each)

1. `allowed_networks` on `McpServer` (default loopback CIDRs, `None` = no restriction, empty rejected, IP literals only, `localhost` iff a listed network holds `127.0.0.1`, `require_loopback` refused with a migration message); `mcp_no_proxy_hosts` + `scoped_environment(no_proxy_hosts=)`. The Dream's `brain_mcp_server` migrates to `allowed_networks=None`; the golden fixtures (`tests/unit/agents/`) must pass unchanged — they do.
2. `mcp_profiles`: tomllib, one table per profile, names never values.
3. `cli`: argparse, fully injectable `main(argv, environ, stdin, stdout, stderr, cwd, home)`; providers through `registry.get_provider` (a fake in tests).
4. `cli_write`: the worktree, the double tripwire read, the carrier commit, `ha clean`.
5. Version `0.4.0`, `[project.scripts] ha`, README, CHANGELOG, real end-to-end check.

## Decisions where the spec is silent

| # | Question | Decision |
|---|---|---|
| 1 | The error for a non-loopback URL under the default | Unchanged ("not a loopback URL"): a caller that never set the field sees nothing new. Other network lists say "outside the allowed networks" or "host names are never resolved". |
| 2 | "No restriction" in TOML, which has no null | `allowed_networks = "any"`. |
| 3 | Where a chain's links write | `<run_dir>/links/<i>-<provider>/`, the final link's `result.json` copied to `<run_dir>/`. An exhausted chain returns the LAST link's code (`3` or `4`), per the CLI's exit-code contract. |
| 4 | Which environment a CLI child gets | The caller's, minus the parent Claude Code session markers (`CLAUDE_CODE_*`, `CLAUDECODE`, …) — the same rule as the live suite; a listed private MCP host is added to `NO_PROXY`. |
| 5 | Credentials per rail | The Dream's: agy's four `.gemini` files, opencode's `auth.json`; codex copies its own auth into an ephemeral `CODEX_HOME`; claude keeps the caller's HOME. |
| 6 | Trusting the rail's tripwire alone | No: the CLI arms its own tripwire around the whole run and unions it with the rail's report. Either firing means no git command at all. |
| 7 | `ha clean` and the branch | The worktree is removed through git, the branch is **kept** (it may already be integrated). A run whose tripwire fired gets no git command, not even `worktree remove`: the directory is deleted and the operator is told to prune. |
| 8 | A failed agent run in `--write` | Nothing committed; the worktree is kept and named. |
| 9 | `--write -p codex` without `--shell` | Refused (exit `2`). Measured with the real CLI: codex reads only through its shell tool, which a writable workspace without `shell` turns off; the run changed nothing and ended in a misleading `5`. Changing the codex rail itself (enabling its sandboxed shell in write mode, as read-only mode does) is left to an operator decision: it changes the lot-2 contract "`shell=False` means no shell". |

## Measured (2026-09-24, real CLIs, throwaway repository)

`ha providers` found the four CLI rails; `ha run -p codex` read the repository and answered; `ha run --write` with codex `--shell` and with claude each fixed the bug and committed it on its own `ha/<run_id>` branch, `main` untouched; `ha clean` removed each worktree and kept its branch.
