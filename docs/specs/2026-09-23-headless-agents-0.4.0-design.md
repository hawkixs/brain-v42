# headless-agents 0.4.0 — uniform facade, seven providers, workspace writes, `ha` CLI

- **Date:** 2026-09-23
- **Status:** design, approved by the merge of PR #187; amended by operator decisions 7
  and 8 (section 8)
- **Package:** `packages/headless-agents` (workspace member of this repository)
- **Current version:** 0.3.0 (`headless-agents-v0.3.0`)
- **Brain references:** decision 3c5c56e1 (one shared agent runtime, public),
  decision ae839a85 (this package becomes the replacement target for red-lab and its
  factory), learning 2680192f (0.2.0 is read-only by construction), learning b532e416
  (provider portfolio, 2026-09-22), ticket e9087e13 (red-arena pilot), runbook eafde166
  (OpenCode as a worker from a Claude session).

## 1. Why

Three facts, all measured on 2026-09-22/23:

1. **One provider's quota saturates while the other subscriptions sit idle** (Codex,
   Gemini through agy, OpenCode). An interactive session has no simple way to
   hand a task to another provider: the only path today is the hand-driven runbook
   eafde166.
2. **Consumers still rebuild what the package should give them.** red-rail
   (`rail/reviewer/judges.py`) dispatches providers through an `if/elif`, reads the reply
   from `raw_log` for claude and `report_log` for the others, hard-codes per-provider prompt
   limits and does not know exit code `4` (it is pinned to 0.2.0). red-arena keeps 1,361
   lines of seat plumbing in `players/`, including its own OpenAI-compatible worker.
3. **Every CLI rail is read-only by construction** (learning 2680192f): `codex
   --sandbox read-only`, claude limited to MCP tools, opencode with `{"*": false}`, agy
   refusing to start unless its guard denies writes. No caller can have an agent edit code.

Decision ae839a85 sets the trajectory: this package grows into the runtime that replaces
red-lab's workflow engine (0.5.0, `ha workflow`) and its factory (0.6.0, `ha factory`).
0.4.0 implements neither, but lays the seams they need: a stable serialisable run result,
a per-run directory, and a workspace capability usable by a workflow step.

## 2. Scope

### In

| # | Item |
|---|---|
| 1 | Provider registry, uniform `RunResult.text`, `RunResult.to_dict()` (schema 1), `RunSpec.run_dir`, per-provider `max_prompt_bytes` |
| 2 | Workspace capability (`CapabilityProfile.workspace`, read-only or writable) on the four CLI rails, plus the context bundle |
| 3 | `openai-compat` provider with three presets: `openrouter`, `mistral`, `nvidia` |
| 4 | `ha` CLI: `providers`, `run` (read-only and `--write`), `runs`, `clean`; named MCP profiles; `allowed_networks` |

The seven providers of 0.4.0: `claude`, `codex`, `agy`, `opencode` (subscription CLIs,
already present), `openrouter`, `mistral`, `nvidia` (HTTP, new).

### Out (explicitly)

- `ha workflow` (0.5.0) and `ha factory` (0.6.0).
- More than one MCP server per run (0.5.0: workflows will need it).
- A usage ledger shipped to red-monitor: 0.4.0 only writes `result.json` per run, the raw
  material of that ledger.
- OS-level isolation of shell commands (bwrap): noted for the factory.
- Tool loops for `openai-compat`, streaming.
- red-watcher (no LLM use today) and red-lab (frozen by decision ae839a85).

## 3. Design

### 3.1 Facade and registry

**`headless_agents.registry`**

- `get_provider(name: str) -> AgentProvider`. An unknown name raises `UnknownProvider`
  whose message lists the valid names.
- `PROVIDER_NAMES = ("claude", "codex", "agy", "opencode", "openrouter", "mistral",
  "nvidia", "openai-compat")`.
- `probe(name) -> Probe(available: bool, detail: str, version: str | None)`. Zero quota:
  for a CLI rail, the executable on `PATH` and its `--version`; for an HTTP provider, the
  presence of its key variable in the environment. Never a model call.
- `max_prompt_bytes(name) -> int | None`. `None` for rails that read the prompt on stdin
  (claude, codex) and for HTTP providers; the existing `MAX_PROMPT_BYTES` for agy and
  opencode, which **must** take the prompt in argv (agy ignores stdin, measured
  2026-08-11; `opencode run` blocks forever on a piped stdin). A consumer reads this
  instead of hard-coding limits (red-rail's `prompt_limits`).

**`RunResult` additions (additive, non-breaking)**

- `text: str | None` — the final answer, already unwrapped by the provider (`envelope`
  logic moves behind the provider). `None` when the run produced no answer.
- `to_dict() -> dict` — a JSON-safe dict with `"schema": 1`. This is the contract of
  `ha run --json` and of `result.json`, and the input format of 0.5.0 workflow steps.
  Fields: `schema, run_id, provider, model, model_reported, exit_code, text, tokens,
  cost_usd, duration_seconds, tool_call_completed, context, workspace, branch, logs`.
  `null` keeps its meaning "not measured", never zero.

**`RunSpec.run_dir: Path | None`**

When set, the four logs default to fixed names inside it (`report.log`, `events.jsonl`,
`stderr.log`, `raw.log`) and the run writes `result.json` there. Explicit log paths still
win, so existing callers are untouched.

### 3.2 `openai-compat` provider

- **Process shape:** each HTTP call runs in a killable child process
  (`python -m headless_agents.providers._openai_worker`), the pattern red-arena proved in
  `players/openai_worker.py`. Deadline and process-group kill behave exactly as for the
  CLI rails. Prompt **and key** travel on the child's stdin in a JSON envelope: never in
  argv, logs or stderr.
- **Transport:** standard library only (`urllib.request`, `json`), `POST
  {base_url}/chat/completions`. The package keeps its single runtime dependency
  (`pydantic`); the boundary test keeps enforcing it.
- **Presets:**

  | Name | `base_url` | Key variable |
  |---|---|---|
  | `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
  | `mistral` | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` |
  | `nvidia` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` |
  | `openai-compat` | caller-supplied | caller-supplied |

  The package never reads a key from a file: the caller puts it in the environment.
- **Result:** `text` from `choices[0].message.content`; `model_reported` from the
  response `model` field; `tokens` from `usage` (`prompt_tokens`, `completion_tokens`,
  `prompt_tokens_details.cached_tokens`, `completion_tokens_details.reasoning_tokens` →
  `thinking`); `cost_usd` only when the API reports it (OpenRouter with
  `usage: {include: true}`), else `None`.
- **Failure classification** (never echoing the error body, which may contain the key):

  | Condition | Exit code | Chain behaviour |
  |---|---|---|
  | own deadline fired | `124` | stops |
  | HTTP 429 or 5xx, connection refused | `3` | advances |
  | HTTP 401/403 | `1` | stops (configuration error, not an unavailable provider) |
  | other | `1` | stops |

- **Limits:** no tools, no MCP, no streaming. Request options limited to
  `response_format` (JSON), `temperature`, `max_tokens` — what red-arena uses today. A
  profile declaring `mcp` or `workspace` is rejected with `ValueError`.

### 3.3 Workspace capability and context bundle

**Profile**

```python
class Workspace(BaseModel):
    path: Path          # absolute, must exist, must be a directory
    write: bool = False # False: read tools only, confined to path
    shell: bool = False # refused unless write=True

class CapabilityProfile(BaseModel):
    ...
    workspace: Workspace | None = None
```

`workspace=None` keeps every rail **byte-for-byte** on today's behaviour: the Dream golden
fixtures (3 rails x 6 phases) must stay identical.

A workspace is **read-only by default**. `write=False` gives the agent read tools only --
list, read, search -- confined to `path`: it is what `ha run` uses without `--write`, so an
agent asked about a repository reads it instead of receiving it pasted into its prompt.
`write=True` adds the edit and write tools. `shell=True` is refused with `ValueError`
unless `write=True`: on three rails out of four a shell can write whatever the read tools
cannot.

**Per-rail mapping when `workspace` is set**

| Rail | Read-only (`write=False`) | Writable (`write=True`) | `shell=True` adds | Isolation of the shell |
|---|---|---|---|---|
| codex | `--sandbox read-only -C <path>`, its shell tool enabled inside that sandbox (codex reads through it) | `--sandbox workspace-write -C <path>`, its shell tool enabled inside that sandbox too (decision 13) | nothing: codex's shell is on in every workspace mode | OS sandbox, network off |
| claude | `--restricted --tools Read,Glob,Grep --permission-mode dontAsk`, cwd `<path>` (measured: `--restricted` ignores the repository's own `.claude/settings.json`, so a trusted checkout cannot widen what this run may do) | `--restricted --tools Read,Edit,Write,Glob,Grep --permission-mode acceptEdits`, cwd `<path>` | `Bash`, added to `--allowedTools` | none — operator's user rights, `--restricted` does not confine it |
| opencode | allow `read`, `glob`, `grep`, `list`; `external_directory` denied; cwd `<path>` | also allow `edit`, `write` | allow `bash` | none — operator's user rights |
| agy | package-supplied **workspace guard**: reads allowed under `<path>`, every write and `run_command` denied | the same guard, writes allowed under `<path>` only | `run_command` allowed | none — operator's user rights |
| openai-compat | rejected | rejected | rejected | — |

**The agy workspace guard is a deliberate change of principle.** Until 0.3.0 the runtime
ships no guard: `ToolGuard` is a script the caller versions, tests and passes by absolute
path. With a workspace, read-only or writable, the guard *is* the confinement, so the
package owns it: a versioned script shipped as package data, pinned by its own unit
tests, and the `ToolGuard` docstring is amended to say so. The two do not compose in
0.4.0: an agy profile carrying both `workspace` and a caller `tool_guard` is rejected with
`ValueError` (a run without a workspace keeps the caller guard exactly as today).

`shell` is explicit and off by default because only codex confines it. For the same
reason codex ignores it: its shell is its only read tool and always runs inside its OS
sandbox, so a codex workspace run has it in both modes (decision 13).

What a workspace run sees of the operator's HOME, rail by rail:
- **agy and opencode**: an ephemeral HOME, in every case.
- **codex**: an ephemeral `CODEX_HOME` (decision 11); the process `HOME` is the caller's.
- **claude**: the caller's HOME; `--restricted` ignores the settings sources. Whether
  `~/.claude/CLAUDE.md` still loads under `--restricted` is UNMEASURED.

**Read confinement is per rail.** claude's permission checks, opencode's
`external_directory` permission and agy's guard are expected to keep reads inside `path`;
each is proven by a `live` test before the tag. codex's OS sandbox stops writes and
network, not reads: a codex agent can read outside `path` anything the operator can. That
residual is accepted and documented, as the writable mode already accepts it; the
ephemeral HOME keeps the operator's own configuration out of the paths an agent looks at
by default.

**`.git` in write mode.** claude, opencode and codex (unmeasured whether codex's
`workspace-write` keeps `.git` read-only) can write `<ws>/.git` in write mode: hooks and
config run later, outside any sandbox, when git runs in that checkout; agy denies it in its
guard. Lot 4 must not run git in a workspace whose `.git` changed (tracked by a Brain
ticket).

To measure during implementation (each gets a `live` test): the exact agy guard contract
for a read-only and a writable workspace; the opencode 1.18.x permission keys for both;
the claude permission mode that confines `Read` to `path` without an interactive prompt;
codex's read-only sandbox with its shell tool enabled.

**Context bundle**

A sub-agent without its instructions does worse work, and three leaks make that likely:
a worktree only checks out tracked files (an ignored `CLAUDE.md` vanishes); each CLI reads
a different file (codex and opencode read `AGENTS.md`, claude reads `CLAUDE.md`; red-arena
and red-alerts have no `AGENTS.md`); the ephemeral HOME drops the operator's user-level
instructions (for example "everything on GitHub in English").

- **Resolution:** from the source repository root, tracked or ignored: `CLAUDE.md`,
  `AGENTS.md`, `GEMINI.md`. Plus the user-level files listed by the caller (the CLI
  defaults to `~/.claude/CLAUDE.md`). Parent directories are included only on request
  (`--context-parents`).
- **Levels:** `full` (repository + user-level), `global` (user-level only), `none`.
- **One channel, not two.** The original design below described an instruction file
  written into a write-mode workspace, plus the preamble for everything else; decision 12
  (section 8) replaced it after a live measurement found the file channel broken on three
  rails out of four. What ships: the **preamble** is the SINGLE channel, in every mode, on
  every rail. Repository content (when `full`) and user-level content (`full`/`global`)
  both travel there, always — never only in read-only mode. claude takes it through
  `--append-system-prompt`; openai-compat as a system message; codex, opencode and agy as
  a delimited block prepended to the prompt, ahead of the task. **Nothing is written into
  the workspace**: there is no second, file-based channel.
- **The preamble counts against `max_prompt_bytes`.** For agy and opencode, which take the
  prompt in argv, the bundle's size is added before the check; a bundle that pushes the
  prompt over the limit fails the run with exit code `2` before any spawn, never by
  truncation. claude carries the preamble as one `--append-system-prompt` argv element
  instead, refused the same way past `131 071` bytes (the kernel's per-argument
  `MAX_ARG_STRLEN` minus one) — codex's stdin channel carries no such ceiling.
- **Traceability:** `result.json` lists every injected file with its size in bytes and
  its sha256 (the context files READ to build the preamble — there is nothing written to
  list separately).

Measured sizes (2026-09-23, ~4 bytes per token): a user-level `CLAUDE.md` of 4.3 KB
(~1.1k tokens); repository instruction files from 5.7 KB to 53.8 KB (~1.4k to ~13.4k
tokens). The cost lands on the sub-agent's provider quota, not on the calling
session's.

### 3.4 `ha` CLI

Entry point `[project.scripts] ha = "headless_agents.cli:main"`, `argparse` only.
Installed as a tool: `uv tool install "headless-agents @
git+https://github.com/hawkixs/brain-v42.git@headless-agents-v0.4.0#subdirectory=packages/headless-agents"`.

```
ha providers [--json]
ha run -p PROVIDER [-m MODEL] [--effort E] [--timeout SECONDS]
       [--chain P1,P2,...]
       [--context full|global|none] [--context-parents]
       [--mcp PROFILE]
       [--base-url URL --key-env VAR]
       [--write [--shell] [--repo PATH] [--base REF]]
       [--json] [--run-dir DIR]
       [PROMPT | -]
ha runs [--limit N] [--json]
ha clean RUN_ID
```

- **Defaults:** `--context global` read-only, `--context full` with `--write`; no MCP;
  prompt from the argument, or stdin when it is `-` or absent.
- **`--base-url` / `--key-env`:** both required with `-p openai-compat`, rejected (exit
  `2`) with any other provider — the presets fix their own URL and key variable. `--key-env`
  takes the variable **name**; the key itself never appears on the command line.
- **Read-only flow:** a CLI rail runs with `Workspace(path=<repository root>,
  write=False)`: the agent can list, read and search the current repository, and has no
  write tool and no shell. An HTTP provider runs without a workspace: it has no tool to
  read with. Output: `text`, or the schema-1 JSON with `--json`.
- **`--write` flow:**
  1. `git worktree add ~/.cache/ha/runs/<run_id>/wt -b ha/<run_id> <base>` (base
     defaults to `HEAD`).
  2. Install the context bundle; register its files in the local exclude.
  3. Run the provider with `Workspace(path=wt, write=True, shell=--shell)`.
  4. Commit the result on `ha/<run_id>` as `chore(ha): <run_id> via
     <provider>/<model_reported>` — a review carrier, not a final commit. **The
     repository's commit hooks run**: the CLI never passes `--no-verify` nor overrides
     `core.hooksPath`. If a hook refuses, the changes stay uncommitted in the worktree,
     the hook output is kept in the run directory, and the run exits `1` — the diff is
     still there to read.
  5. Print branch, diffstat, patch path and the agent's text.
  6. **Never merge.** The caller reads the diff and integrates, or runs `ha clean`.
- **`--chain`:** walks the list on exit codes `3` and `4`
  (`capability.FALLBACK_EXIT_CODES`), reusing `chain.run_chain`.
- **Exit codes (contract):** `0` answer; `1` failure; `2` invalid usage (for example
  `--write` with an HTTP provider, or `--shell` without `--write`); `3` provider
  unavailable, chain exhausted; `4`
  timeout with no tool call started (replayable); `5` `--write` finished with no change;
  `124` timeout.
- **Run records:** every run writes `~/.cache/ha/runs/<run_id>/` (logs + `result.json`).
  `ha runs` reads them back.

**MCP profiles** (`~/.config/ha/mcp.toml`, read with `tomllib`):

```toml
[brain-read]
url = "http://127.0.0.1:8765/mcp"
bearer_env = "BRAIN_TOKEN"   # the variable NAME; the value never sits in this file
tools = ["brain_search", "brain_get", "brain_recall", "brain_ticket_get"]
```

`--mcp NAME` maps one profile to `CapabilityProfile.mcp`. No MCP unless asked. The package
knows no server by name: `brain` exists only in the operator's configuration.

**`allowed_networks`** replaces the boolean `require_loopback` on `McpServer`:
`allowed_networks: tuple[str, ...] | None = ("127.0.0.0/8", "::1/128")`.

- **Default:** loopback only — a caller that never set `require_loopback` sees no change.
- **`None`:** no network restriction, the exact equivalent of `require_loopback=False`.
  It is spelled out, never implied by an empty tuple (an empty tuple is rejected).
- **A private network** (for example a VPN subnet) is allowed by listing that subnet
  explicitly, for the Dream as much as for the CLI.
- **Host names are never resolved.** A DNS answer at validation time proves nothing about
  the address connected to later. A literal IP is matched against the networks;
  `localhost` is accepted if and only if `127.0.0.0/8` is listed (today's rule); any
  other host name is rejected with a message asking for an IP literal. With
  `allowed_networks=None` no host check runs, as today with `require_loopback=False`.
- **Proxy bypass:** `merged_no_proxy` appends the URL's literal host when it is not a
  loopback entry, so a listed private address is not sent through `HTTP(S)_PROXY`. A
  host, never a CIDR: CIDR support in `NO_PROXY` differs between the CLIs' HTTP clients.

**In-repository caller:** `brain_v42.agents.capability.brain_mcp_server` passes
`require_loopback=False` **by default**, for every Dream rail — its URL is validated
by `build_child_environment` under enforcement instead. It migrates in the same lot as the
field, to `allowed_networks=None`, and the Dream golden fixtures gate that the argv, the
written configuration files and the child environment stay byte-for-byte identical.

**Session skill `ha-delegate`** (in `~/.claude/skills`, outside this repository): when to
delegate, to which provider, and the rule that the diff is read before integration. It
replaces runbook eafde166.

## 4. Testing

- **Unit (no network, no quota), in `tests/unit/headless_agents/`:** registry and probe;
  `build_command` for each rail without a workspace, with a read-only one and with a
  writable one; `shell=True` without `write=True` rejected; the `openai-compat` worker
  against a local fake HTTP server (200, 401, 429, 5xx, timeout; the key never appears in
  logs or stderr); context bundle resolution, including an ignored `CLAUDE.md`; the
  `--write` worktree flow on a throwaway git repository; `result.json` against schema 1;
  CLI argument validation and exit codes; `allowed_networks` (default, `None`, empty
  tuple rejected, IP literal in/out of a listed subnet, `localhost`, other host names
  rejected, `NO_PROXY` entry); the preamble counted against `max_prompt_bytes`; the
  carrier commit with a refusing hook; agy `workspace` + `tool_guard` rejected.
- **Boundary guard (existing):** runtime dependencies stay `pydantic` alone; no import of
  `brain_v42`.
- **Dream non-regression:** the existing golden fixtures pass unchanged.
- **`live` (marked, excluded from CI, run by hand on an operator machine):** one run per
  provider (7); for each CLI rail, a read and a write accepted inside the workspace and
  refused outside it (codex reads outside it by design: measured and documented, 3.3);
  the acceptance case "an ignored `CLAUDE.md` is read by a codex sub-agent"
  (the answer must quote an instruction from that file).

## 5. Versioning and delivery

- **0.4.0**, tag `headless-agents-v0.4.0`, per the existing convention.
- **CHANGELOG:** additive entries for the registry, `text`, `to_dict`, `run_dir`,
  `max_prompt_bytes`, `workspace`, the context bundle, `openai-compat` and its presets,
  and the CLI. Breaking entry: `McpServer.require_loopback` replaced by `allowed_networks`
  (the default keeps loopback-only, so a caller that never set the field is unaffected;
  one that set `require_loopback=False` must migrate to `allowed_networks=None` — the
  Dream's `brain_mcp_server` does so in lot 4, section 3.4). Behaviour entry: an agy
  profile with `workspace` uses the package-owned guard (section 3.3).
- **Implementation lots, in order:** (1) facade; (2) workspace (read-only and writable)
  and context bundle; (3) `openai-compat`; (4) CLI and MCP profiles. Each lot ships with
  its tests; the tag follows lot 4. The workspace comes before `openai-compat` by operator
  decision (section 8, decision 8).

## 6. Consumer migrations (after the tag, each from its own repository's session)

- **red-rail** (new ticket): 0.2.0 → 0.4.0. `judges.py` uses `get_provider` and `.text`,
  handles exit code `4`, reads `max_prompt_bytes` instead of hard-coded limits.
  Acceptance: suite and `rail check` green, one real reviewer pass.
- **red-arena** (ticket e9087e13, amended to target 0.4.0): remove `SubprocessSeat`,
  `OpenAICompatSeat`, `openai_worker.py`; reduce `envelope.py` to what is arena-specific.
  The acceptance criteria already in the ticket stand.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Write mode with `shell=True` on claude/opencode/agy runs commands with the operator's rights | Off by default, explicit flag, documented; bwrap deferred to the factory |
| agy / opencode write-mode flags differ from what is documented | `live` tests per rail before the tag |
| A consumer merges an unreviewed sub-agent diff | The CLI never merges; the skill states the rule |
| Context bundle cost in wide fan-outs | Levels, `global` by default read-only, sizes recorded in `result.json` |
| Workspace code paths perturb the Dream | `workspace=None` is byte-for-byte unchanged; golden fixtures gate it |
| A read-only codex agent reads outside its workspace | Accepted and documented (3.3): codex's sandbox confines writes and network, not reads; the other three rails confine reads, proven per rail by a `live` test |
| The `allowed_networks` migration perturbs the Dream | `brain_mcp_server` migrates to `None` in the same lot; golden fixtures gate it |

## 8. Review amendments (2026-09-23)

A first review, checked against the code at `d9a72644`, found six gaps; this revision
closes them.

| # | Gap | Resolution |
|---|---|---|
| 1 | `require_loopback` replacement claimed only external callers must migrate, but the Dream's `brain_mcp_server` sets it to `False` by default | `allowed_networks=None` spelled out as "no restriction"; in-repository migration in lot 4, gated by the golden fixtures (3.4) |
| 2 | CIDR matching said nothing about host names | Never resolved; `localhost` iff `127.0.0.0/8` is listed; other names rejected; literal host added to `NO_PROXY` (3.4) |
| 3 | `full` dropped user-level instructions for codex/opencode when the repository has an `AGENTS.md`, and read-only mode would have written into the caller's checkout | Two channels: workspace file for repository content in write mode only; preamble for user-level content always and for everything in read-only mode; preamble counted against `max_prompt_bytes` (3.3) |
| 4 | `ha run -p openai-compat` had no way to name its URL or key | `--base-url` and `--key-env`, required with `openai-compat`, rejected otherwise (3.4) |
| 5 | A package-supplied agy guard contradicted "the runtime ships no guard" without saying so | Stated as a deliberate change; guard shipped as package data; `workspace` + caller `tool_guard` on agy rejected (3.3) |
| 6 | The `--write` carrier commit did not say what happens with the repository's hooks | Hooks run, never bypassed; a refusal leaves the diff uncommitted and exits `1` (3.4) |

### Operator decisions (2026-09-23, after approval)

| # | Question | Decision |
|---|---|---|
| 7 | The read-only flow named the repository as the agent's working directory, yet no rail can read in read-only mode: claude runs with `--tools ""`, codex without its shell tool, opencode with every tool off, and agy's guard is only proven to deny `run_command` and `write_to_file` | Read access: `Workspace.write` (default `False`) gives read tools confined to `path` per rail; `shell` requires `write`; `ha run` without `--write` uses it, HTTP providers run without a workspace (3.3, 3.4) |
| 8 | Lot order: `openai-compat` before the workspace | The workspace becomes lot 2 and `openai-compat` lot 3: reading and editing a repository is what no caller can have an agent do today, while text-only HTTP calls already exist in red-arena (5) |

### Measurement amendments (2026-09-23, lot 2 plan)

Lot 2 (the workspace capability, 3.3) implemented gap 3's two-channel plan above,
measured it live against the four rails' real CLIs, and found it broken on three of them.
This section is what the code on `feat/headless-agents-lot2-workspace` ships instead —
read the source (`providers/*.py`, `guards/agy_workspace.py`, `context.py`) for the
mechanism, this table for the decision.

| # | Measured | Decision |
|---|---|---|
| 9 | agy's only read tool, `view_file`, cannot list a directory, and its bundled documentation discovers no project-level `.agents/hooks.json` (measured 2026-08-11, in a trusted workspace and a git repository) | A workspace run swaps the caller's `ToolGuard` for the package-owned guard (`headless_agents.guards.agy_workspace`): copied into the ephemeral HOME and PROVEN before spawn by its probes (a read inside allowed, a read of `/` denied, `run_command` gated on `shell`, a read of the guard's own config denied, and with writes armed a write to `<ws>/.git` or under it denied). The prompt carries the workspace's file list instead — tracked plus untracked-not-ignored, via `git ls-files --cached --others --exclude-standard`, pinned with `--work-tree` and `core.fsmonitor=false`. That pin covers `.git/config`, not a rewritten `.git` FILE (a linked worktree's gitdir pointer), which the pin alone did not stop; the guard now denies every write whose target has a `.git` component (raw or resolved, `casefold()`) |
| 10 | opencode's `read` tool confines the STARTING path to the workspace, not where a symlink under it leads: a `ws/link.txt -> outside/secret.txt` read returns the outside content (measured live 2026-09-23, `test_opencode_symlink_residual`: exit `0`, answer `OUTSIDE-<uuid>`) | Accepted and documented as a residual (3.3, `providers/opencode.py`) — the same shape as the shell escape already accepted for the rails whose shell is unconfined. Not fixed in lot 2 |
| 11 | codex resolves its login, its config and its session state from `CODEX_HOME`; handing a workspace run the operator's real one would expose its `AGENTS.md`, sessions and config to a sandboxed agent | A workspace run gets an EPHEMERAL `CODEX_HOME`: a private `0700` directory holding only a symlink to the real `auth.json`, built under a root outside every writable root the sandbox could itself reach (`_choose_codex_home_root`: rejects `/tmp`, `tempfile.gettempdir()`, `TMPDIR`, and the workspace path itself). Torn down after the run, with a hardened write-back of a rotated `auth.json` (opened `O_NOFOLLOW`, size-bounded, `account_id`-matched against the real file, compare-and-swap on its digest) so a legitimate OAuth refresh survives the teardown. Residual, deliberately not defended: the sandbox can still READ the real `auth.json` through the symlink — this rescue protects its integrity, not its confidentiality |
| 12 | The write-mode instruction-file channel (gap 3 above) was measured live on 2026-09-23 and failed on three rails out of four: claude's `--restricted` does not auto-load the workspace `CLAUDE.md`; opencode's `OPENCODE_DISABLE_PROJECT_CONFIG=1` (kept for isolation) also disables `AGENTS.md`; agy reads `AGENTS.md`/`GEMINI.md` only inside a git repository checkout | The instruction-file channel is removed. The preamble becomes the SINGLE channel for repository AND user-level instructions, in every mode, on every rail (3.3). codex sets `project_doc_max_bytes=0` in every mode, so a tracked `AGENTS.md` it would otherwise read natively never reaches it twice. **Correction to gap 3's "never in the diff" clause**: since nothing is written into the workspace, there is no file to keep out of it — the plan to do so via "the worktree's local exclude file" was itself unsound, measured: `.git/info/exclude` is shared by every linked worktree of a repository, not local to one, so a write there would have leaked into every other worktree sharing the checkout. Known limit, not fixed in lot 2: on codex/opencode/agy the preamble travels inside the user message (claude alone gets a system prompt), and a weak model — measured: opencode `glm-5.3-flash` — sometimes obeys the task over the `<instructions>` block |

### Operator decisions (2026-09-24, before the tag)

| # | Measured | Decision |
|---|---|---|
| 13 | Running `ha run --write` for real (lot 4) showed a writable codex without `shell` has NO read tool: codex reads only through its shell tool, which `shell=False` turned off, so the run changed nothing — silently for a library caller, while the CLI refused `--write -p codex` without `--shell` (exit `2`) | codex has its shell tool in EVERY workspace mode, read-only (as before) and writable: that shell runs inside codex's OS sandbox (writes confined to the writable roots, network off), so it widens nothing the `workspace-write` sandbox did not already allow `apply_patch`. `Workspace.shell` no longer changes codex's command, and the CLI refusal is removed. The other rails are unchanged: their shell is unconfined, the flag keeps its meaning there. Not a breaking change: 0.4.0 was not tagged yet |
| 14 | The live suite on the release `44a13a7e` (2026-09-24): 41 passed, 3 skipped (HTTP presets without their key), 1 failed twice: `test_repository_instructions_reach_the_agent[opencode]`, a generic answer instead of the repository's code word. A manual `ha` replay found it with `--context full` and `none`. The three HTTP presets, replayed with their keys, answered with measured usage (nvidia on `z-ai/glm-5.3-flash`: `openai/gpt-oss-20b`, a reasoning model, exceeds the 60 s deadline) | Tag with the failure documented: it is decision 12's known limit (a weak model obeying the task over the `<instructions>` block), not a regression of the rail |
