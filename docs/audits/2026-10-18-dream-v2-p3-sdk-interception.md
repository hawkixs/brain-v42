# Dream P3 SDK interception audit — 2026-10-18

Original standalone spike, completed before the executor integration described below.

Scope: disposable experiment only. No repository source, tests, commits, branches or remotes were changed. No credential file, production system or external provider was used. Every API credential supplied by the harness is a synthetic literal. Reported runtime dollar amounts are calculations from fabricated usage, not expenditure.

## Measured identity

- Python SDK: `claude-agent-sdk==0.2.168`, installed with uv offline from the existing cache.
- Bundled runtime: `2.1.301`; binary SHA-256 in `summary.json`.
- Container image: locally cached `debian:bookworm-slim`, image ID `sha256:d4c45f66efc59f89b0597d343a9bd72808493c0eacecde5285961743252cee23`.
- Synthetic model identifier: `claude-sonnet-4-5`; no inference ran. This is not a Sonnet 5.5 compatibility or pricing measurement.
- Endpoint: `ANTHROPIC_BASE_URL=http://127.0.0.1:18081`.

## Commands executed

```sh
uv venv --offline --python 3.12 spike-sdk/venv
uv pip install --offline --python spike-sdk/venv/bin/python claude-agent-sdk==0.2.168
spike-sdk/run.sh tools locked
spike-sdk/run.sh retry locked
spike-sdk/run.sh disconnect locked
spike-sdk/run.sh refused locked
spike-sdk/run.sh compact locked
python3 spike-sdk/verify.py
```

Preliminary baseline, restricted, minimal and automatic-compaction experiments are retained in `results/`. The `locked-*` artifacts are the final evidence. The automatic-compaction preliminary probe did not cause compaction and is not counted as an internal-request proof. Manual `/compact` did.

`run.sh` contains the exact Docker command: `--pull never --network none --read-only`, a snapshot mounted read-only, no published ports, no host HOME/configuration/credential/Docker socket mounts. Only this disposable spike and a read-only Python interpreter distribution are mounted. The process starts with `env -i`; the SDK child receives explicitly constructed environment values. All containers use `--rm`. `strace -f -e trace=connect,sendto,openat` observes the Python server, SDK process and bundled runtime descendants; it does not capture headers, bodies or process environments.

## Results

`verify.py` passed against 25 Messages requests and 25 IPv4 connections, all to `127.0.0.1:18081`. All HTTP request targets were `/v1/messages?beta=true`.

| Probe | Requests | Observation |
|---|---:|---|
| Tool sequence | 17 | Three permitted operations succeeded; thirteen external-path or unavailable-tool operations failed |
| HTTP retries | 3 | 429, then 500, then successful SSE; all three payload hashes identical |
| Broken connection | 2 | Server closes connection before responding; runtime retries through the same endpoint |
| Gate refusal | 1 | HTTP 403 terminates the run with an error; no direct fallback connection |
| Internal summary | 2 | One ordinary request, then the runtime-generated `/compact` summarization request |

The compaction probe deliberately enables slash commands so the harness can invoke `/compact`; builtin skills consequently appear in that probe's init record. The other four final probes disable slash commands and have empty skills. The internal-summary probe is routing evidence, not evidence of the exact disabled-command policy profile.

The normal locked profile reports exactly `Glob`, `Grep`, `Read`; `agents`, `mcp_servers`, `plugins`, `skills`, `slash_commands` and `additional_directories` are empty. File reads, grep and glob in the snapshot work. External absolute paths, `..`, a file symlink to an external synthetic canary, external Grep paths, absolute external glob patterns and a glob traversal are rejected. The external canary never appears in model-visible payloads. Bash, Write, Edit, WebFetch, Agent and a fabricated MCP tool are unavailable. The source canary stays unchanged and no write/command marker is created.

No CLAUDE.md canary appears in any first request. Grep deliberately searches the snapshot and can find the synthetic CLAUDE.md as ordinary file content later; this is not automatic instructions loading. `--safe-mode`, empty setting sources and the empty isolated HOME prevent ambient customization loading in this experiment. Explicit inline security settings are supplied; a literal claim of “no settings whatsoever” would be false.

## Required configuration and source evidence

SDK paths below are relative to `venv/lib/python3.12/site-packages/claude_agent_sdk/`:

- `types.py:2329`: `tools` selects the actual base tool set; `allowed_tools` only auto-approves tools.
- `types.py:2514`: `can_use_tool` is called only when permission rules ask. It is not a universal pre-execution filter.
- `types.py:2652`: empty setting sources disable filesystem sources; project sources govern CLAUDE.md loading.
- `_internal/transport/subprocess_cli.py:591`: emits `--tools`; `:700` emits strict MCP configuration; `:729` emits setting sources.
- `_internal/transport/subprocess_cli.py:819`: inherits the parent environment before merging `options.env`. Therefore `options.env` alone does not scrub the environment; the outer clean process is required.
- `_version.py` and `_cli_version.py`: pinned SDK and runtime identities.

The executable `_bundled/claude` contains retrievable JavaScript source alongside bytecode. `runtime-source/` holds exact NUL-delimited source spans extracted from that executable, with original starting byte offsets in filenames; no runtime was patched:

- `anthropic-client-220885548.js`: `let ah={...a.ANTHROPIC_BASE_URL?{baseURL:a.ANTHROPIC_BASE_URL}:...}` constructs the first-party client. Messages use `this._client.post("/v1/messages",...)`. `retryRequest` calls `this.makeRequest(r,n-1,s)` on the same client/options. The base class constructs URLs from its configured base URL. This supports the observed ordinary, HTTP retry and broken-connection routing.
- `restricted-file-boundary-221935739.js`: `Ah` checks resolved paths against working directories; the refusal text explicitly identifies `--restricted`, including symlink escapes.
- `builtin-plugin-registration-234478654.js`: builtin plugins are registered even with SDK `plugins=[]`.
- `builtin-plugin-registry-218022925.js`: `CG` applies `enabledPlugins[id]` and separates enabled and disabled plugins. The locked profile explicitly sets all registered builtin IDs to false; `--safe-mode` alone leaves builtin plugins enabled.
- `runtime-help.txt`: runtime-provided descriptions of `--safe-mode`, `--restricted` and `--bare`. The minimal `--bare` experiment removed Grep/Glob, so it is not the accepted tool profile.

## Limits and disposition

Proven: all Messages in these scenarios, including actual retry and actual internal summary traffic, use the configured loopback endpoint. The network namespace has no external interface or direct external route; its isolation is a necessary part of this evidence. The tool policy and absence of active ambient customizations are demonstrated for the locked profile, with explicit inline security settings and builtin-plugin disabling.

Not proven: an exhaustive SDK-only guarantee covering every possible runtime branch, internal task, provider/profile mode, redirect, transport fallback or future release. No claim is made that `ANTHROPIC_BASE_URL` alone is an egress boundary. No paid execution, monetary reservation, usage-bound implementation, output clamp, live model compatibility or production configuration is certified. PR 2 must retain external network confinement and complete its own transport/cap acceptance before any paid P3 execution. If the prerequisite demands the universal SDK-only claim literally, it remains unproven.


## Executor integration follow-up

The standalone spike was accepted as the prerequisite for this draft. The executor
was then tested with the same pinned SDK/runtime against a synthetic loopback
upstream in a network namespace with no external route. No upstream key or provider
service was used.

The initial real-SDK run failed eight of nine cases. The runtime selected automatic
permissions when no mode was supplied, adding an unsupported classifier safeguard
payload. The executor now explicitly selects `permission_mode="default"`; the gate
continues to reject unknown billing and safeguard parameters. A second run exposed
relative Read paths being resolved against the private HOME; the runtime now starts
inside the staged snapshot. All nine acceptance cases then passed: normal tool
continuation, USD/token caps, SDK retry, interrupted stream, unknown usage, failed
reservation sync, injection attempts, and refusal of further forwarding routes.

The optional SDK-dependent tests explicitly skip when the `dream` extra is absent;
the nine-case acceptance run installs that extra. The full validation counts and
remaining delivery limits are recorded in the pull request.

This audit does not certify a production egress boundary. The executor must still
run inside the bench's network and credential isolation. Its programmatic permission
hook is part of the application policy; this differs from the standalone spike's
hook-free profile. SDK `plugins=[]` alone does not disable bundled plugins, as the
original spike demonstrated. Those distinctions remain review items for the draft;
the fake-upstream tests do not prove every runtime branch or a production sandbox.

A final read-only review also identified two snapshot integration gaps. The staged
files are `0400`, but their directories remain writable by the worker; a true
read-only mount still has to protect the staged tree. Evidence is currently
rechecked against the original snapshot rather than the staged copy, leaving a
concurrent-source-mutation case to cover before paid execution. Also verify the
counting semantics of forwarded `context_management`, which is absent from the
count request. These are explicit review follow-ups for the draft.

### Final local validation

With the `dev` and `dream` extras installed, the four CI shard assignments covered
all 18,599 collected unit tests: **18,581 passed, 18 skipped, 180 subtests passed**.
The four final JUnit reports contain zero failures and zero errors; their selected
counts are 2,453 / 3,232 / 9,051 / 3,863. The skipped cases require absent archived
logs, private receipts/plans or server-specific drop-ins. Combined line/branch
coverage reports **84%**. The dedicated `sdk_socket` run passed all nine cases.
A separate dev-only environment passed the socket gate test and explicitly skipped
the eight cases requiring the optional SDK.

Ruff, Ruff format check, mypy over `src/`, module layering and container image pin
checks passed. The local minimal container needed CI-equivalent tools, a writable
private runtime/home and an init to reap orphaned subprocesses; affected shards
were rerun after correcting the test harness. This did not require changes to
unrelated product code.
