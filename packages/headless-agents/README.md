# headless-agents

Run headless CLI agents (`claude -p`, `codex exec`, `agy --print`,
`opencode run`) and OpenAI-compatible HTTP providers under a capability profile the
caller supplies -- from Python, or from a terminal with the `ha` CLI.

This package is the shared agent runtime of the ReD ecosystem, hosted as a uv
workspace member of the [brain-v42](https://github.com/hawkixs/brain-v42)
repository and installable on its own:

```sh
uv add "headless-agents @ git+https://github.com/hawkixs/brain-v42.git@headless-agents-v0.5.1#subdirectory=packages/headless-agents"
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
`docs/specs/2026-09-23-headless-agents-0.4.0-design.md` (3.3, in the private brain-v42-internal repository) and its measurement
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
both from the caller.

A preset's key comes from the environment first. Otherwise, `~/.config/ha/keys.toml`
names, per preset, the `.env` file that holds it:

```toml
openrouter = "~/.config/red/openrouter.env"
mistral    = "~/.config/red/mistral.env"
```

ha reads only the preset's own variable from that file (`KEY=value`, `export KEY=value`,
quoted or not), and only if the file belongs to you and nobody else can read it (mode
`0600`). The key goes to the HTTP request only. `keys.toml` is read from the configuration
directory only, never from a repository. `ha providers` shows where each key came from.
`openai-compat` still takes its key from the variable `--key-env` names.

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
uv tool install "headless-agents @ git+https://github.com/hawkixs/brain-v42.git@headless-agents-v0.5.1#subdirectory=packages/headless-agents"
```

```text
ha run TARGET [PROMPT | -] [-m MODEL] [--effort E] [--timeout SECONDS] [--wait SECONDS]
       [--context full|global|none] [--context-parents] [--mcp PROFILE]
       [--base-url URL --key-env VAR] [--repo PATH] [--json] [--run-dir DIR]
       [--write [--shell] [--base REF]]
       [--continue RUN_ID] [--findings RUN_ID] [--head REF] [--run RUN_ID]
ha roles [--json]
ha workflows [--json]
ha providers [--json]
ha runs [--limit N] [--json]
ha show RUN_ID [--json]
ha show --dir PATH [--json]
ha clean RUN_ID
ha --version
```

- **`--wait SECONDS`** (spec §3.3): a bounded admission wait. Ordinary reads and confined
  writes share the global lock; an unconfined write holds it exclusively, serialising every
  other run while it is in flight. `ha clean` is admitted through this same gate, not a lock
  of its own. `--wait` gives one explicit, positive number of seconds, spent as a single
  absolute deadline across the global lock and, for a write or a review, the lineage
  registry and lineage locks it admits under -- time spent on one does not extend the
  budget for the next, and a lock granted past the deadline is refused, never accepted
  late. Without `--wait`, each of those locks keeps its own existing ten-second bound. A
  deadline that expires -- or an invalid `--wait` -- exits `2` before any provider step
  runs and leaves nothing behind (an unstarted run's entry is forgotten), naming the
  contested lock and the wait requested; `--timeout` is unrelated, and always the provider
  run's own timeout. Example: `ha run codex --wait 30 "Summarise the change"`.

  An unconfined writer that already holds the short-lived admission gate excludes every
  later run -- shared or not -- until it releases it; a reader that arrives after a writer
  has already won that gate queues behind it too. This is a **best-effort** mitigation, not
  a fairness guarantee: `flock` does not order waiters, so a writer still *polling* for
  admission (not yet holding it) can in principle be overtaken by a continuous, overlapping
  stream of readers, and a continuous stream of writers can likewise make a waiting reader
  time out. A fair FIFO admission queue is planned for 0.5.3.

`TARGET` is a provider (`ha run codex "..."`), a role declared in
`~/.config/ha/roles.toml` -- an executor: one provider, or a `chain` of them, with optional
instructions -- or a workflow declared in `~/.config/ha/workflows.toml`. `-p` and `--chain`
were removed in 0.5.0: the provider is the target, and a chain is declared in a role.

- **A workflow** names the roles that fill the slots of a shape coded in the package.
  `shape = "implement"` takes one role with `write = true` in its `implement` slot: `ha run
  build "task"` runs it on a new `ha/<run_id>` branch, as a write run, with the task
  wrapped in the engine's implement prompt, and prints the run id, the branch, the diffstat
  and the patch path before the agent's text. `--continue RUN_ID` joins that run's lineage
  instead: the next run works in the same worktree, on the same branch, from its tip --
  commits made there by hand included -- once the worktree is clean. A workflow runs its
  roles as declared: `-m`, `--write` and the other role options are refused. `ha workflows`
  lists what `workflows.toml` declares.
- **A review** (`shape = "review"`: one reviewer role or more, and a judge with two or
  more) reads a change read-only: `ha run multi-review --run RUN_ID` reviews the current
  tip of an implement run's lineage from its base; `--head REF` and `--base REF` (default
  `origin/HEAD`) name any other range. Before anything runs, the vendor rule proves that no
  reviewer shares a provider with whoever wrote a commit of the range -- from the
  provenance `ha` records for its own commits -- and refuses the review otherwise (exit
  `2`). The reviewers run in parallel, the judge weighs their findings, and the last line
  of the deciding text is the verdict: exit `0` APPROVE, `6` CHANGES. `ha run build
  --continue RUN_ID --findings REVIEW_ID` then hands those findings to the implementer, on
  the commit the review read; `ha show REVIEW_ID` renders the review, its vendor check
  included, from the state directory, and `ha show --dir PATH` a run directory's report.

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
- **Model catalogue and live drift** (`ha models [--provider NAME] [--json] [--refresh]`):
  the catalogue is the operator's own file, never ha's -- `~/.config/ha/catalog.toml`, or
  `$XDG_CONFIG_HOME/ha/catalog.toml` when that variable is absolute (a relative value falls
  back to the default path, like every other file `config_paths` resolves). `ha` validates
  it against a frozen schema v1 and never rewrites it:

  ```toml
  schema = 1

  [codex."gpt-6-sol"]
  purpose = "Judgment: reviews of specs, plans and code, and builds that need reasoning."
  tasks = [{kind = "design-review", effort = "high"}, {kind = "build-deep", effort = "high"}]
  cost = {kind = "subscription", windows = ["5h", "weekly"]}
  pitfalls = ["A codex write is unconfined in ha 0.5.1: it serialises every ha run."]
  verified_at = 2026-09-27
  source = "red-skills decision: codex model and effort tiering for ha runs"

  [openrouter."deepseek/deepseek-v4.1-flash"]
  purpose = "Cheap wide reading and drafts; every finding and number verified by hand."
  tasks = [{kind = "bulk-read"}, {kind = "draft"}]
  cost = {kind = "per_token", note = "0.004 to 0.35 USD per task"}
  verified_at = 2026-09-27
  source = "Brain decision ea4e57a1"
  ```

  A top-level key is either `schema` (the integer `1`, required) or a provider table named
  after one of the eight rails; any other table-valued key is an unknown-provider error,
  and any non-table top-level value is a warning (named, and ignored) whether or not its
  name matches a provider. Inside a model table, an unknown field in the model, in a
  `tasks` entry or in `cost` is likewise a warning naming the key; every other departure
  (a missing field, a wrong type, a value outside a closed list, a duplicate task kind, a
  model value that is not a table) is an error naming the key. `tasks[].effort` follows a
  per-provider rule: optional for `codex` (one of `none`, `minimal`, `low`, `medium`,
  `high`, `xhigh`, `max`, `ultra`); optional and passed through as-is for `opencode`'s own
  `--variant` (ha performs no model-specific variant check); forbidden for `agy`, `claude`,
  `openrouter`, `mistral`, `nvidia` and `openai-compat`, an error even when the value would
  otherwise be valid.

  `ha models` merges the catalogue with three sources per provider: the live list, where
  the provider has one (`opencode models`, `agy models`, and OpenRouter's own
  `GET /api/v1/models` with the key `keys.toml` or the environment names); `roles.toml`'s
  declared links; and `models.toml`'s defaults. Only `opencode`, `agy` and `openrouter`
  have a live list; the other five rails (`claude`, `codex`, `mistral`, `nvidia`,
  `openai-compat`) are always reported `catalogue-only` -- this reflects which providers
  publish one, never a claim that the others have no models. A live query that times out
  or answers unreadable garbage is reported `unavailable`/`unreadable`, never fatal, and
  never turns into a claim that a catalogued model has disappeared: that judgement is
  only ever drawn from a live list that actually answered.

  ```bash
  ha models                                # every provider, catalogue vs. declared use
  ha models --provider opencode --json     # one provider, machine-readable
  ha models --refresh                      # add drift: uncatalogued, gone, unknown, stale
  ```

  `--refresh` reports drift without changing anything: a model the live list offers but
  the catalogue does not know (`live_uncatalogued`), a catalogued model the live list no
  longer offers (`catalogued_gone`, only ever raised for a provider whose live list
  answered), a role pointing at a model the catalogue does not know
  (`unknown_role_model`), and a catalogue entry whose `verified_at` is more than 30 days
  old (`stale_verification`; exactly 30 days is not stale). The JSON form always includes
  the drift, whatever `--refresh` is, so a client never has to guess from display mode.
- **`--mcp NAME`** maps a profile from `~/.config/ha/mcp.toml` (or
  `$XDG_CONFIG_HOME/ha/mcp.toml`) to the run; no MCP unless asked:

  ```toml
  [brain-read]
  url = "http://127.0.0.1:8765/mcp"
  bearer_env = "BRAIN_TOKEN"   # the variable NAME; the value never sits in this file
  tools = ["brain_search", "brain_get", "brain_fact_get", "brain_ticket_get"]
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
- **Runs**: every run writes `~/.cache/ha/runs/<run_id>/`, with `run.json` at its top --
  the run's own report, schema 1, `"kind": "run"` right after `"schema"`, written at the
  start (`status: "running"`) and replaced atomically after every step; `null` means "not
  measured" or "not applicable", never zero. Its keys: `run_id`, `target`, `status`,
  `exit_code`, `verdict`, `text`, `repository`, `base`, `head`, `branch`, `lineage`,
  `continues`, `findings_from`, `implement_providers`, `commits`, `failure_reason`,
  `vendor_check`, `cleanup`, `pid`, `started_at`, `duration_seconds`, `cost_usd`,
  `cost_complete` and `steps`. Also at the top: `prompt.md` (the task, written once), and
  for `--write` the worktree and `change.patch`. Each entry of `steps` names its `provider`,
  `model`, `exit_code`, `tokens`, `cost_usd`, `tools` and `dir` -- the run-relative path of
  that step's own directory, `steps/<NN>-<slot>-<role>/`, holding its logs, its `commit.log`
  for a write step, and its own `result.json` (schema 1, unchanged, no `"kind"`, still
  `"provider"`): `run.json` is the run's report, `result.json` stays each provider's own
  contract, unaffected by anything above -- a chained role's own link result lands under
  `steps/<NN>-<slot>-<role>/links/<index>-<provider>/` and is copied up to the step's
  `result.json` for the link that answered. `ha runs` lists runs newest first; `ha clean
  RUN_ID` removes one (its worktree through git, its branch kept).
- **Exit codes**: `0` answer, or a review's APPROVE; `1` failure; `2` invalid usage
  (nothing ran); `3` provider unavailable, chain exhausted; `4` timeout with no tool call
  started; `5` `--write` finished with no change; `6` a review's CHANGES verdict; `124`
  timeout.

## Live tests

`tests/live/headless_agents/` replays the workspace confinement above, and now the roles
and workflows layer, against the real CLIs of the machine it runs on -- not mocks. Every
test spends real provider quota and needs the operator's logged-in CLIs, so it is opt-in:
marked `live`, excluded from the default run, and skipped unless `HA_LIVE=1`. Run it
deliberately, from a directory outside `~/.claude` (the claude rail's own configuration
lives there):

```sh
HA_LIVE=1 .venv/bin/pytest -m live tests/live -v -rA
```

`tests/live/headless_agents/test_workflows_live.py` (0.5.0) exercises the engine end to
end: a one-step read-only run through a role, a real `implement` write run on a toy
repository, and a `review` run under the vendor rule -- reviewers and their judge on a
small real diff, refusing to run if a reviewer shares a provider with whoever wrote the
range.

Historical full run (2026-09-23, lot 2, against claude 2.1.280, codex-cli 0.156.0,
opencode 1.18.30, agy 1.2.9): `34 passed in 449.20s (0:07:29)`, 0 skipped, 0 failed. The
2026-09-24 runs (the suite on `44a13a7e`, the HTTP presets with their keys, the codex
suites after the pre-tag hardening) and their two documented failures are recorded in the
CHANGELOG's 0.4.0 "Measured" section. A failure here is a finding, not a flake: re-run
once to rule out the network, then report it -- never loosen the assertion.

## Licence

Apache-2.0, same as the repository that hosts it.
