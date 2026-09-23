# headless-agents

Run headless CLI agents (`claude -p`, `codex exec`, `agy --print`,
`opencode run`) under a capability profile the caller supplies.

This package is the shared agent runtime of the ReD ecosystem, hosted as a uv
workspace member of the [brain-v42](https://github.com/hawkixs/brain-v42)
repository and installable on its own:

```sh
uv add "headless-agents @ git+https://github.com/hawkixs/brain-v42.git@headless-agents-v0.3.0#subdirectory=packages/headless-agents"
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
class Workspace(BaseModel):
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
suite):

| Rail | Read-only | Writable | `shell=True` adds |
|---|---|---|---|
| codex | `--sandbox read-only -C <path>`, shell tool ON (codex's only way to read) | `--sandbox workspace-write -C <path>` | commands inside the same OS sandbox, network off |
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

## Live tests

`tests/live/headless_agents/` replays the workspace confinement above against the real
CLIs of the machine it runs on -- not mocks. Every test spends real provider quota and
needs the operator's logged-in CLIs, so it is opt-in: marked `live`, excluded from the
default run, and skipped unless `HA_LIVE=1`. Run it deliberately, from a directory outside
`~/.claude` (the claude rail's own configuration lives there):

```sh
HA_LIVE=1 .venv/bin/pytest -m live tests/live -v -rA
```

Last full run (2026-09-23, against claude 2.1.280, codex-cli 0.156.0, opencode 1.18.30,
agy 1.2.9): `34 passed in 449.20s (0:07:29)`, 0 skipped, 0 failed. A failure here is a
finding, not a flake: re-run once to rule out the network, then report it -- never loosen
the assertion.

## Licence

Apache-2.0, same as the repository that hosts it.
