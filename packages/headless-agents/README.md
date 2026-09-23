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

## Licence

Apache-2.0, same as the repository that hosts it.
