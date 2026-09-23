# headless-agents 0.4.0 — uniform facade, seven providers, workspace writes, `ha` CLI

- **Date:** 2026-09-23
- **Status:** design, awaiting operator review
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
| 2 | `openai-compat` provider with three presets: `openrouter`, `mistral`, `nvidia` |
| 3 | Workspace capability (`CapabilityProfile.workspace`) on the four CLI rails, plus the context bundle |
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
    shell: bool = False

class CapabilityProfile(BaseModel):
    ...
    workspace: Workspace | None = None
```

`workspace=None` keeps every rail **byte-for-byte** on today's behaviour: the Dream golden
fixtures (3 rails x 6 phases) must stay identical.

**Per-rail mapping when `workspace` is set**

| Rail | Read/edit | `shell=True` adds | Isolation of the shell |
|---|---|---|---|
| codex | `--sandbox workspace-write -C <path>` | commands inside the same sandbox | OS sandbox, network off |
| claude | `--tools Read,Edit,Write,Glob,Grep`, `--permission-mode acceptEdits`, cwd `<path>` | `Bash` | none — operator's user rights |
| opencode | allow `read`, `edit`, `write`, `glob`, `grep`; cwd `<path>` | allow `bash` | none — operator's user rights |
| agy | package-supplied **workspace guard**: writes allowed under `<path>` only | `run_command` allowed | none — operator's user rights |
| openai-compat | rejected | rejected | — |

`shell` is explicit and off by default because only codex confines it. The ephemeral HOME
stays in every case: none of the operator's hooks, MCP servers or user-level instructions
leak into the child by accident.

To measure during implementation (each gets a `live` test): the exact agy guard contract
in write mode, and opencode 1.18.x permission keys for write mode.

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
- **Delivery, in each rail's native form, never duplicated:**
  - claude: repository `CLAUDE.md` present in the workspace; user-level through
    `--append-system-prompt`.
  - codex, opencode: the repository `AGENTS.md` if it exists; otherwise one composed
    `AGENTS.md` (repository `CLAUDE.md` + user-level) written into the workspace.
  - agy: same rule, in the file agy discovers (to measure).
  - openai-compat: system-message preamble.
- **Never in the diff:** files the bundle writes are added to the worktree's local
  exclude file, never to a tracked `.gitignore`.
- **Traceability:** `result.json` lists every injected file with its size in bytes and
  its sha256.

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
       [--write [--shell] [--repo PATH] [--base REF]]
       [--json] [--run-dir DIR]
       [PROMPT | -]
ha runs [--limit N] [--json]
ha clean RUN_ID
```

- **Defaults:** `--context global` read-only, `--context full` with `--write`; no MCP;
  prompt from the argument, or stdin when it is `-` or absent.
- **Read-only flow:** the current repository is the agent's working directory, without
  any write tool. Output: `text`, or the schema-1 JSON with `--json`.
- **`--write` flow:**
  1. `git worktree add ~/.cache/ha/runs/<run_id>/wt -b ha/<run_id> <base>` (base
     defaults to `HEAD`).
  2. Install the context bundle; register its files in the local exclude.
  3. Run the provider with `Workspace(path=wt, shell=--shell)`.
  4. Commit the result on `ha/<run_id>` as `chore(ha): <run_id> via
     <provider>/<model_reported>` — a review carrier, not a final commit.
  5. Print branch, diffstat, patch path and the agent's text.
  6. **Never merge.** The caller reads the diff and integrates, or runs `ha clean`.
- **`--chain`:** walks the list on exit codes `3` and `4`
  (`capability.FALLBACK_EXIT_CODES`), reusing `chain.run_chain`.
- **Exit codes (contract):** `0` answer; `1` failure; `2` invalid usage (for example
  `--write` with an HTTP provider); `3` provider unavailable, chain exhausted; `4`
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
`allowed_networks: tuple[str, ...] = ("127.0.0.0/8", "::1/128")`. Callers that never set
the field see no change. An MCP server reached over a private network (for example a
VPN subnet) is allowed by listing that subnet explicitly, for the Dream as much as for
the CLI.

**Session skill `ha-delegate`** (in `~/.claude/skills`, outside this repository): when to
delegate, to which provider, and the rule that the diff is read before integration. It
replaces runbook eafde166.

## 4. Testing

- **Unit (no network, no quota), in `tests/unit/headless_agents/`:** registry and probe;
  `build_command` with and without `workspace` for each rail; the `openai-compat` worker
  against a local fake HTTP server (200, 401, 429, 5xx, timeout; the key never appears in
  logs or stderr); context bundle resolution, including an ignored `CLAUDE.md`; the
  `--write` worktree flow on a throwaway git repository; `result.json` against schema 1;
  CLI argument validation and exit codes.
- **Boundary guard (existing):** runtime dependencies stay `pydantic` alone; no import of
  `brain_v42`.
- **Dream non-regression:** the existing golden fixtures pass unchanged.
- **`live` (marked, excluded from CI, run by hand on an operator machine):** one run per
  provider (7); for each CLI rail, a write accepted inside the workspace and refused
  outside it; the acceptance case "an ignored `CLAUDE.md` is read by a codex sub-agent"
  (the answer must quote an instruction from that file).

## 5. Versioning and delivery

- **0.4.0**, tag `headless-agents-v0.4.0`, per the existing convention.
- **CHANGELOG:** additive entries for the registry, `text`, `to_dict`, `run_dir`,
  `max_prompt_bytes`, `workspace`, the context bundle, `openai-compat` and its presets,
  and the CLI. Breaking entry: `McpServer.require_loopback` replaced by `allowed_networks`
  (the default keeps loopback-only, so a caller that never set the field is unaffected;
  one that set `require_loopback=False` must migrate).
- **Implementation lots, in order:** (1) facade; (2) `openai-compat`; (3) workspace and
  context bundle; (4) CLI and MCP profiles. Each lot ships with its tests; the tag
  follows lot 4.

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
