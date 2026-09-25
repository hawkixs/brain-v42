# headless-agents

Run headless CLI agents (`claude -p`, `codex exec`, `agy --print`,
`opencode run`) and OpenAI-compatible HTTP providers under a capability profile the
caller supplies -- from Python, or from a terminal with the `ha` CLI.

This package is the shared agent runtime of the ReD ecosystem, hosted as a uv
workspace member of the [brain-v42](https://github.com/hawkixs/brain-v42)
repository and installable on its own:

```sh
uv add "headless-agents @ git+https://github.com/hawkixs/brain-v42.git@headless-agents-v0.4.0#subdirectory=packages/headless-agents"
```

Versions are tagged `headless-agents-vX.Y.Z` on this repository; `CHANGELOG.md` lists the
surface under contract and every breaking change. Its dependencies are `pydantic` and
`structlog` -- nothing else. Importing it
pulls no database driver, no MCP server framework and no HTTP server.

## What it does, and what it refuses to decide

The runtime executes; it never decides. Every policy is data the caller hands
in:

- **which provider and which model** -- the caller builds a `RunSpec`;
- **which MCP server, with which bearer and which tool allowlist** -- a
  `CapabilityProfile` carries the server name, URL, bearer, headers and tools,
  or `mcp=None` for a run that may reach no server at all;
- **which credentials** -- relative paths under the caller's real `HOME`,
  symlinked into the ephemeral one by default, copied `0600` on request, never
  copied silently;
- **which tool guard** -- a `PreToolUse` hook script the caller ships and
  points at.

The package knows nothing about the Brain MCP server, about the nightly
Dream's phases, or about any project. It must never import `brain_v42`; a
test guards that boundary.

## Usage

One run of `codex exec` that may call two tools on one loopback MCP server,
with the bearer scoped into the child environment and nothing else inherited
beyond the base allowlist:

```python
from pathlib import Path
import os

from headless_agents.capability import scoped_environment
from headless_agents.profile import CapabilityProfile, McpServer
from headless_agents.providers.codex import CHILD_ENV_PASSTHROUGH, CodexProvider
from headless_agents.spec import RunSpec

server = McpServer(
    name="example",
    url="http://127.0.0.1:8765/mcp",
    bearer_env_var="EXAMPLE_TOKEN",
    headers={"X-Agent": "example-run"},
    tools=("example_search", "example_get"),
)
spec = RunSpec(
    prompt="Summarise what changed since yesterday.",
    model="<model>",
    profile=CapabilityProfile(mcp=server),
    environment=scoped_environment(
        os.environ,
        passthrough=CHILD_ENV_PASSTHROUGH,
        overrides={"EXAMPLE_TOKEN": "<the scoped bearer>"},
    ),
    report_log=Path("out/report.log"),
    events_log=Path("out/events.jsonl"),
    stderr_log=Path("out/stderr.log"),
)
result = CodexProvider().run(spec)
# result.exit_code: 0 done, 1 failed, 124 timed out,
# 3 failed AND proved no tool call succeeded (safe to replay elsewhere),
# 4 timed out AND proved no tool call ever started (safe to replay elsewhere,
#   and the link did not answer for a whole deadline: chain.run_chain names it
#   in ChainResult.dead_links). claude never returns 4: its OTEL telemetry is
#   exported on an interval, so an empty log cannot prove an empty run.
```

The same kind of run through the facade, by provider name, with one directory
holding its logs and its `result.json`:

```python
from headless_agents.registry import get_provider, max_prompt_bytes, probe

if probe("codex").available:  # zero quota: the executable and its --version
    result = get_provider("codex").run(
        RunSpec(prompt="Summarise the diff.", model="<model>", run_dir=Path("runs/r-001"))
    )
    print(result.text)  # the final answer, or None when the run produced none
    # runs/r-001/ now holds report.log, events.jsonl, stderr.log and result.json,
    # which is result.to_dict(): schema 1, the same keys for every provider.

limit = max_prompt_bytes("agy")  # an int for the argv rails, None for claude and codex
```

An isolated seat -- no server, no user-level configuration, credentials
copied `0600` into a throwaway HOME -- runs `claude -p` under a rebuilt
environment:

```python
import tempfile

from headless_agents.profile import CapabilityProfile, Credentials
from headless_agents.sandbox import build_toolless_home, ephemeral_root, sandbox_environment
from headless_agents.providers.claude import ClaudeProvider

home = build_toolless_home(
    root=ephemeral_root(os.environ) or Path(tempfile.gettempdir()),  # a tmpfs by preference
    name="seat-1",
    real_home=Path.home(),
    credentials=Credentials(paths=(".claude/.credentials.json",), mode="copy"),
)
spec = RunSpec(
    prompt="...",
    model="<model>",
    profile=CapabilityProfile(),  # reaches nothing
    environment=sandbox_environment(home, environ=os.environ),
    raw_log=home / "raw.log",
)
result = ClaudeProvider().run(spec)
```

`headless_agents.envelope.unwrap(provider, stdout)` turns a CLI's JSON
envelope into the text it answered, the model it *reported*, its token
counts and its cost -- and never raises: an unreadable envelope yields the
raw text.

The `agy` rail takes its prompt in `argv`, not on stdin (measured: it
ignores stdin), and refuses to run without a `ToolGuard` whose script
provably denies machine tools -- the guard is the only wall between agy and
a shell.

The `opencode` rail needs no guard script: its wall is the inline config's
`tools` map, a fail-closed ALLOWLIST (`{"*": false, "<server>_<tool>": true}`)
that removes every built-in tool before the model sees it. The config travels
in `OPENCODE_CONFIG_CONTENT` and references the bearer as `{env:<var>}`, so
this rail writes no secret to disk. It borrows the operator's
`~/.config/opencode/node_modules` into the ephemeral HOME (a fresh HOME would
otherwise `bun install` from npm on every run) and refuses to start when the
real HOME has none. `spec.reasoning_effort` becomes `--variant`; `spec.name`
becomes the session title. The subscription credential to declare is
`.local/share/opencode/auth.json`.

## Workspace and context

A `Workspace` gives an agent a directory to read, or read and edit, and nothing outside
it -- confined by each rail's own mechanism, per
`docs/specs/2026-09-23-headless-agents-0.4.0-design.md` (3.3) and its measurement
amendments in section 8:

```python
from pathlib import Path

from pydantic import BaseModel

class Workspace(BaseModel):  # headless_agents.profile.Workspace
    path: Path          # absolute, must exist, must be a directory
    write: bool = False # False: read tools only, confined to path
    shell: bool = False # refused unless write=True
```

`workspace=None` (the default) leaves every rail exactly as it ran before 0.4.0 --
`CapabilityProfile.workspace` is the only field that changes anything. Read-only:

```python
from pathlib import Path

from headless_agents.profile import CapabilityProfile, Workspace
from headless_agents.providers.claude import ClaudeProvider
from headless_agents.spec import RunSpec

spec = RunSpec(
    prompt="Summarise what this repository does.",
    model="<model>",
    profile=CapabilityProfile(workspace=Workspace(path=Path("/home/op/some-repo"))),
    run_dir=Path("runs/read-1"),
)
result = ClaudeProvider().run(spec)
# claude runs --restricted --tools Read,Glob,Grep --permission-mode dontAsk, cwd
# /home/op/some-repo -- no edit tool ever reaches the model.
```

Writable, with a shell:

```python
from headless_agents.providers.codex import CodexProvider

spec = RunSpec(
    prompt="Fix the failing test in tests/test_foo.py and re-run it.",
    model="<model>",
    profile=CapabilityProfile(
        workspace=Workspace(path=Path("/home/op/some-repo"), write=True, shell=True),
    ),
    run_dir=Path("runs/write-1"),
)
result = CodexProvider().run(spec)
# codex runs --sandbox workspace-write -C /home/op/some-repo with its shell tool
# armed inside the SAME OS sandbox (network off); apply_patch and the shell can both edit.
```

**Per rail, when a workspace is set** (measured 2026-09-23, `d9a72644` and the lot-2 live
suite; codex's writable row per spec decision 13, measured live 2026-09-24):

| Rail | Read-only | Writable | `shell=True` adds |
|---|---|---|---|
| codex | `--sandbox read-only -C <path>`, shell tool ON (codex's only way to read) | `--sandbox workspace-write -C <path>`, shell tool ON inside that sandbox too | nothing: codex's shell is on in both modes, inside its OS sandbox, network off |
| claude | `--restricted --tools Read,Glob,Grep --permission-mode dontAsk`, cwd `<path>` | `--restricted --tools Read,Edit,Write,Glob,Grep --permission-mode acceptEdits` | `Bash`, unconfined by `--restricted` -- operator's user rights |
| opencode | allow `read`/`glob`/`grep`/`list`; `external_directory` denied; `--dir <path>` | also allow `edit`/`write` | allow `bash`, unconfined -- operator's user rights |
| agy | package-owned **workspace guard**, reads confined to `<path>`, every write and `run_command` denied | the same guard, writes confined to `<path>` | `run_command` allowed, unconfined -- operator's user rights |

What a workspace run sees of the operator's HOME, rail by rail:
- **agy and opencode**: an ephemeral HOME, in every case.
- **codex**: an ephemeral `CODEX_HOME` (see below); the process `HOME` is the caller's.
- **claude**: the caller's HOME; `--restricted` ignores the settings sources. Whether
  `~/.claude/CLAUDE.md` still loads under `--restricted` is UNMEASURED.

**Residuals, measured and accepted, not fixed:**
- **codex reads outside the workspace by design.** Its sandbox stops writes and network,
  not reads: a codex agent can read anything the operator can, in both modes.
- **opencode's `read` follows an inside symlink to an outside target.** The tool confines
  the starting path, not where it leads (`ws/link.txt -> outside/secret.txt` reads the
  outside content) -- measured live, `test_opencode_symlink_residual`.
- **The shell is unconfined on claude, opencode and agy.** With `shell=True`, the shell
  itself runs with the operator's own rights on all three; only codex's shell runs inside
  its OS sandbox. Off by default, and the caller's to accept when arming it.
- **A write run can change `<ws>/.git` on claude, opencode and codex.** Hooks and config
  written there run later, outside any sandbox, when git runs in that checkout (whether
  codex's `workspace-write` keeps `.git` read-only is unmeasured). agy denies it in its
  guard. Lot 4 must not run git in a workspace whose `.git` changed (tracked by a Brain
  ticket).

**agy gets a package-owned guard, not the caller's.** Until 0.3.0 the runtime shipped no
guard at all -- `ToolGuard` was a script the caller versioned and passed by path. With a
`workspace`, the guard *is* the confinement, so the package owns it
(`headless_agents.guards.agy_workspace`): shipped as package data, copied into the
ephemeral HOME and PROVEN there before spawn by its probes (a read inside allowed, a read
of `/` denied, `run_command` gated on `shell`, a read of the guard's own config denied, and
with writes armed a write to `<ws>/.git` or under it denied). A
profile carrying both `workspace` and a caller `tool_guard` is rejected with `ValueError`
-- the two do not compose. Because agy's only read tool, `view_file`, cannot list a
directory, the prompt also carries the workspace's file list (`git ls-files`, tracked plus
untracked-not-ignored).

**codex gets an ephemeral `CODEX_HOME`.** A workspace run authenticates through a private
`0700` directory holding only a symlink to the real `auth.json`, torn down after the run
with a hardened write-back of a rotated token (so a legitimate OAuth refresh is not lost).
Residual, deliberately not defended: the sandbox can still READ the real `auth.json`
through the symlink -- this rescue protects its integrity, not its confidentiality.

**Context**, `headless_agents.context.resolve_context(level, repository_root, user_files)`,
resolves `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` at a repository root (tracked or ignored) plus
the caller's user-level files into a `ContextBundle`, set on `RunSpec.context`. **The
preamble is the single channel**, on every rail, in every mode -- claude through
`--append-system-prompt`, codex/opencode/agy as a delimited block prepended to the prompt.
An earlier design also wrote a native instruction file into a write-mode workspace; it was
measured live on 2026-09-23 and removed, broken on three rails out of four (claude's
`--restricted` does not auto-load a workspace `CLAUDE.md`, opencode's
`OPENCODE_DISABLE_PROJECT_CONFIG=1` also disables `AGENTS.md`, agy reads `AGENTS.md` only
inside a git repository). **Nothing is written into a workspace.** The preamble counts
against each rail's `max_prompt_bytes` (agy, opencode) or against claude's own
`131 071`-byte `--append-system-prompt` ceiling; a bundle that pushes a run over its limit
refuses with `capability.INVALID_USAGE_EXIT_CODE` (`2`) before any spawn, never by
truncation. Known limit: on codex, opencode and agy the preamble travels inside the user
message (claude alone gets a system prompt), and a weak model -- measured: opencode
`glm-5.3-flash` -- sometimes obeys the task over the `<instructions>` block.

## HTTP providers: `openai-compat` and its presets

Four registry names speak the OpenAI chat-completions API instead of running a CLI:
`openrouter`, `mistral` and `nvidia` fix their endpoint and the **name** of their key
variable (`OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `NVIDIA_API_KEY`); `openai-compat` takes
both from the caller. The package never reads a key from a file: put it in the
environment.

```python
from pathlib import Path

from headless_agents.registry import get_provider, probe
from headless_agents.spec import RunSpec

probe("mistral")  # Probe(available=True, detail="MISTRAL_API_KEY is set") -- zero quota
result = get_provider("mistral").run(
    RunSpec(
        prompt="Summarise this diff in one sentence: ...",
        model="mistral-small-latest",
        run_dir=Path("runs/2026-09-24-001"),
        extra={"temperature": 0, "max_tokens": 200},  # also: response_format
    )
)

# Any other OpenAI-compatible endpoint:
get_provider("openai-compat").run(
    RunSpec(
        prompt="...",
        model="my-model",
        extra={"base_url": "http://10.0.0.5:8000/v1", "key_env": "MY_LLM_KEY"},
    )
)
```

Each call runs in a killable child process, so the deadline and the process-group kill
behave as on the CLI rails; the prompt **and the key** travel on the child's stdin, never
in argv, the environment or a log. Text only: no tools, no MCP, no workspace, no
streaming -- a profile declaring `mcp` or `workspace` raises `ValueError`. The context
bundle's preamble becomes a `system` message. `tokens` comes from `usage` (cached and
reasoning tokens when the API reports them, `None` otherwise); `cost_usd` is filled only
when the API reports a cost (OpenRouter, which the preset asks for it).

| Outcome | Exit code | Chain |
|---|---|---|
| answer | `0` | |
| own deadline | `124` | stops |
| HTTP 429 or 5xx, host unreachable | `3` | advances -- no tool could have written |
| HTTP 401/403, a malformed reply, anything else | `1` | stops |
| no model, no/invalid `base_url`, an unsupported option, `base_url`/`key_env` given to a preset | `2` | stops, nothing sent |

A failure is recorded as a category and a status (`request failed: http (HTTP 401)`),
never as the provider's error body: that body may quote the key.

## The `.git` tripwire

A writable workspace can let an agent plant what the **next** git command executes
outside every sandbox: a hook, a `core.fsmonitor`, a filter driver, a rewritten `.git`
file of a linked worktree, a hook in a `core.hooksPath` directory. None of it shows in
`git status` or `git diff`, so reading the diff does not reveal it.

Every CLI rail arms `git_tripwire.Tripwire` on a writable workspace: the executable git
state is fingerprinted before the run and compared after it. A change makes the run a
non-replayable failure (`1`), names the paths on stderr, and lists them in `result.json`:

```python
result = get_provider("claude").run(spec)          # spec with Workspace(..., write=True)
result.workspace["git_tampered"]                    # [] when clean, else the changed paths
```

Before running **any** git command in an agent-written tree, check that list, and go
through `git_tripwire.git_command(root, tampered=...)` with
`git_tripwire.git_environment(os.environ, root)`: it pins the repository and the work
tree, disables the fsmonitor and implicit bare repositories, bounds discovery, and
refuses a tree that tripped. Never run git from a subdirectory of such a tree: a
repository planted there is discovered from inside it.

## The `ha` CLI

Hand a task to any provider from a terminal or a session. Install it as a tool:

```sh
uv tool install "headless-agents @ git+https://github.com/hawkixs/brain-v42.git@headless-agents-v0.4.0#subdirectory=packages/headless-agents"
```

```text
ha run TARGET [PROMPT | -] [-m MODEL] [--effort E] [--timeout SECONDS]
       [--context full|global|none] [--context-parents] [--mcp PROFILE]
       [--base-url URL --key-env VAR] [--repo PATH] [--json] [--run-dir DIR]
       [--write [--shell] [--base REF]]
ha roles [--json]
ha providers [--json]
ha runs [--limit N] [--json]
ha show RUN_ID [--json]
ha clean RUN_ID
ha --version
```

`TARGET` is a provider (`ha run codex "..."`) or a role declared in
`~/.config/ha/roles.toml`: an executor -- one provider, or a `chain` of them -- with optional
instructions. `-p` and `--chain` were removed in 0.5.0: the provider is the target, and a
chain is declared in a role. This section is being rewritten with the 0.5.0 lots.

- **Read-only** (default): a CLI rail reads the current repository through a read-only
  workspace (no write tool; no shell, except codex, whose shell is its only read tool and
  runs inside its read-only OS sandbox); an HTTP provider runs without one. Context defaults
  to `global` (the user-level `~/.claude/CLAUDE.md`); `--context full` adds the
  repository's `CLAUDE.md`/`AGENTS.md`/`GEMINI.md`, even ignored ones.
- **`--write`** (a role's `write = true`): the write protocol of spec §3.8.3. Under its
  locks, and before any git command, the run is refused while a quarantine covers the
  repository or the operator, or another write of the repository died unfinished. Then
  `git worktree add ~/.cache/ha/runs/<run_id>/wt -b ha/<run_id> <base>` with hooks off, the
  agent edits there (context `full` by default), and the change is committed on
  `ha/<run_id>` as `chore(ha): <run_id> implement via <provider>/<model>` (`residue` after
  a failed step) with the repository's hooks running for that commit only. Every commit
  is recorded with who made it (engine, agent or hook) in the state directory. Branch,
  patch path and the agent's text are printed; exit `5` when nothing changed. **It never
  merges**: read the diff, integrate, or `ha clean`. If the `.git` tripwire fired, no git
  command runs at all, the lineage is compromised or the repository or operator
  quarantined, and the worktree is kept for inspection; `ha clean` then refuses until
  the operator recovers it by hand. A write role on a rail without a passing
  confinement proof for its installed version is serialised against every other run
  (`ha roles` shows `confined` or `unconfined`). codex needs no `--shell`
  here: it reads files only through its shell, which is always on and stays inside its OS
  sandbox; `--shell` arms the unconfined shell of the other rails.
- **A role's `chain = ["codex:gpt-6-luna", "claude:sonnet"]`** walks the list on exit codes `3` and `4`
  (proof that nothing was written); each link gets its own directory under `links/` and
  may name its own model after the FIRST colon (`openrouter:meta/llama:free`). A provider
  appears at most once in a chain.
- **Models**: the rails never pick one for you (opencode's own default can be a
  contributor model its vendor trains on), so each link's model is, first match wins: its
  own in the chain, then `-m`, then the role's `model`, then your declared default in `~/.config/ha/models.toml`
  (or `$XDG_CONFIG_HOME/ha/models.toml`); with none, the run is refused before anything
  starts (exit `2`). Only agy chooses safely on its own. The package hard-codes no model
  name:

  ```toml
  codex = "gpt-6-luna"
  claude = "sonnet"
  opencode = "opencode-go/glm-5.3-flash"
  mistral = "mistral-small-latest"
  ```
- **`--mcp NAME`** maps a profile from `~/.config/ha/mcp.toml` (or
  `$XDG_CONFIG_HOME/ha/mcp.toml`) to the run; no MCP unless asked:

  ```toml
  [brain-read]
  url = "http://127.0.0.1:8765/mcp"
  bearer_env = "BRAIN_TOKEN"   # the variable NAME; the value never sits in this file
  tools = ["brain_search", "brain_get", "brain_recall", "brain_ticket_get"]
  headers = { "X-Brain-Tool-Profile" = "native", "X-Brain-Agent" = "ha" }
  # allowed_networks = ["10.8.0.0/24"]   # default loopback only; "any" = no restriction
  ```

  For brain the `X-Brain-Tool-Profile = "native"` header is required: brain's default
  `compact` catalogue publishes its session lifecycle tools and, for everything else,
  only the two gateways `brain_find_tool` and `brain_call_tool` -- and
  `brain_call_tool` reaches every tool, writes included -- so a read-only `tools` list
  names tools that catalogue does not publish, and the agent finds none (measured
  end-to-end 2026-09-24: claude refused, codex exited `3` with no tool call; with the
  header all four rails answered through `brain_search`).

- **`--base-url` / `--key-env`**: required with the `openai-compat` target, refused otherwise;
  `--key-env` takes the variable name, never the key.
- **Runs**: every run writes `~/.cache/ha/runs/<run_id>/` (logs, `result.json` schema 1,
  and for `--write` the worktree, `change.patch`, `commit.log`). `ha runs` lists them
  newest first; `ha clean RUN_ID` removes one (its worktree through git, its branch kept).
- **Exit codes**: `0` answer; `1` failure; `2` invalid usage (nothing ran); `3` provider
  unavailable, chain exhausted; `4` timeout with no tool call started; `5` `--write`
  finished with no change; `124` timeout.

## Live tests

`tests/live/headless_agents/` replays the workspace confinement above against the real
CLIs of the machine it runs on -- not mocks. Every test spends real provider quota and
needs the operator's logged-in CLIs, so it is opt-in: marked `live`, excluded from the
default run, and skipped unless `HA_LIVE=1`. Run it deliberately, from a directory outside
`~/.claude` (the claude rail's own configuration lives there):

```sh
HA_LIVE=1 .venv/bin/pytest -m live tests/live -v -rA
```

Historical full run (2026-09-23, lot 2, against claude 2.1.280, codex-cli 0.156.0,
opencode 1.18.30, agy 1.2.9): `34 passed in 449.20s (0:07:29)`, 0 skipped, 0 failed. The
2026-09-24 runs (the suite on `44a13a7e`, the HTTP presets with their keys, the codex
suites after the pre-tag hardening) and their two documented failures are recorded in the
CHANGELOG's 0.4.0 "Measured" section. A failure here is a finding, not a flake: re-run
once to rule out the network, then report it -- never loosen the assertion.

## Licence

Apache-2.0, same as the repository that hosts it.
