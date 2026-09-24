# headless-agents 0.5.0 — roles, two workflow shapes, one dispatch path, run reports

- **Date:** 2026-09-24
- **Status:** design, approved section by section by the operator on 2026-09-24; this
  written form awaits the operator's review before the implementation plan
- **Package:** `packages/headless-agents` (workspace member of this repository)
- **Current version:** 0.4.0 (`headless-agents-v0.4.0`, `32604bee`)
- **Brain references:** decisions af1e2fe0 (this design) and 1c1cdd14 (its trust
  boundary); decision ae839a85 (headless-agents replaces red-lab's workflow engine, then
  its factory); decision c4f1ea03 (the provisional `ha` configuration); runbook eafde166
  (the hand-driven delegation this release replaces); red-lab decision f9eff72c and
  learnings aef97769 and 91d92fed (what went wrong in the engine this one replaces); spec
  0.4.0 (`docs/specs/2026-09-23-headless-agents-0.4.0-design.md`).

## 1. Why

Four facts, measured on 2026-09-24:

1. **The 0.4.0 CLI has no caller yet.** `~/.cache/ha/runs` holds two runs, both from the
   live-suite replay; no skill, workflow or hook calls `ha`; the session skill
   `ha-delegate` that spec 0.4.0 §3.4 announced was never written. Its interface can still
   change at no migration cost — and it should: `ha run --help` documents one option out
   of sixteen and no exit code, there is no `--version`, and every call repeats provider,
   model and effort.
2. **Delegation is still driven by hand** (runbook eafde166): a model chosen per role
   from notes, a bespoke launcher (`oc-run.sh`), a bespoke report (`oc-report.py`: status,
   tool mix, tokens, cost, last text), and a rule that every diff is reviewed by another
   vendor before it is merged (campaign W8: 7 tasks out of 9 had defects).
3. **The next step of the trajectory is a workflow engine** (decision ae839a85: `ha
   workflow` replaces red-lab's engine; `ha factory` follows in 0.6.0).
4. **red-lab's engine, read on 2026-09-24:** a general DAG (`on: step.transition`
   triggers, `blocked_by` joins, `fan_out`, `resume` loops, PostgreSQL state) carrying
   exactly one workflow definition (`config/workflows/code-review.yaml`), whose steps name
   an agent, never a model. Its joins and its `resume_on_feedback` field are exercised by
   nothing. Its incidents came from a safety gate enforced by one entry point and not the
   other (f9eff72c: the workflow engine woke a paused agent for 9 h 17; learning aef97769)
   and from routing keys that drifted between prompts and engine (learning 91d92fed).

So 0.5.0 designs the workflow engine first, sized on the two workflows the operator wants
to run — implement then review, and a multi-provider review — and makes `ha run` its
one-step case. Roles are the unit both name.

## 2. Scope

### In

| # | Item |
|---|---|
| 1 | Roles (`roles.toml`): an executor plus optional instructions; every provider is an implicit role |
| 2 | Workflows (`workflows.toml`): named instances of two shapes coded in the package, `review` and `implement-review` |
| 3 | One engine (`plan` / `execute`) holding every gate; `cli.py` becomes a thin adapter |
| 4 | CLI 0.5.0: `ha run TARGET`, `ha show`, `ha runs`, `ha roles`, `ha workflows`, `ha providers`, `ha clean`, `--version`, documented help |
| 5 | Run records: `run.json` (a new document), `prompt.md`, one directory per step; tool counts per rail |
| 6 | The session skill `ha-delegate` (outside this repository); runbook eafde166 marked superseded |

### Out (explicitly)

- `ha factory` (0.6.0): intake, job queue, push and pull request, CI wait.
- A general workflow language (a flow grammar, a DAG), fan-out of implementation tasks,
  tasks produced by a step.
- A quota preflight and provider-session resume (`ha resume`): offered during the
  brainstorm, not selected.
- Resuming a workflow after a crash: the run reads as incomplete, its worktree is kept.
- Roles or workflows read from a repository (3.3).
- More than one MCP server per run: neither shape needs it.
- Detached runs: the calling session backgrounds `ha` itself.
- Any change to the key set of `result.json` schema 1, to the `AgentProvider` protocol,
  or to the Dream's direct use of the providers.

## 3. Design

### 3.1 Roles

A role is a named executor with optional instructions, declared in
`$XDG_CONFIG_HOME/ha/roles.toml` (default `~/.config/ha/roles.toml`), read with
`tomllib`, one table per role:

```toml
[implementer]
chain   = ["opencode:opencode-go/deepseek-v4.1-flash", "codex:gpt-6-luna"]
write   = true
timeout = 4800

[reviewer-codex]
provider = "codex"
effort   = "high"
context  = "full"
instructions = """
Review the change you are given. Report each finding with its severity
(critical, high, medium, low) and file:line. Never propose a rewrite.
"""
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `provider` | string | — | the one provider this role runs on; exactly one of `provider` and `chain` |
| `chain` | list of strings | — | `"provider"` or `"provider:model"` links, walked on exit codes `3` and `4` (0.4.0 `--chain` syntax); a provider appears once |
| `model` | string | `models.toml` | the model of a `provider` role; refused on a `chain` role, whose links name their own |
| `effort` | string | `"medium"` | reasoning effort, where the rail takes one |
| `timeout` | number | `300` | seconds, per run of this role |
| `context` | `"full"`, `"global"`, `"none"` | `"global"`; `"full"` when `write` | context bundle level (0.4.0 §3.3) |
| `context_parents` | bool | `false` | include the parent directories' instruction files |
| `mcp` | string | none | a profile name in `mcp.toml` |
| `write` | bool | `false` | a writable workspace in a worktree, then a carrier commit (0.4.0 `--write`) |
| `shell` | bool | `false` | the unconfined shell of claude, opencode and agy; requires `write` (codex keeps its sandboxed shell, 0.4.0 decision 13) |
| `base_url`, `key_env` | strings | none | required by `openai-compat`, refused on any other provider |
| `instructions` | string | none | the role's instructions |

- **Names:** lowercase letters, digits and `-`, starting with a letter, at most 64
  characters. Role, workflow and provider names are disjoint (3.8).
- **Implicit roles:** every name in `PROVIDER_NAMES` is a role `{provider = <name>}`
  with every default and no instructions: `ha run codex "…"` needs no declaration.
- **Model of a link**, first match wins: its own in `chain`; `-m`; the role's `model`;
  the provider's line in `models.toml`; none — refused before anything runs, except for
  agy (the 0.4.0 precedence, with the role level added).
- **Instructions channel:** the context bundle gains the scope `role`. A role's
  instructions travel as one `<instructions source="role:<name>" scope="role">` block of
  the preamble — the single instruction channel (0.4.0 decision 12) — placed after the
  user and repository blocks, so the most specific reads last. They are delivered at
  every context level, `none` included: they belong to the role, not to the ambient
  context. They count against `max_prompt_bytes` and against claude's
  `--append-system-prompt` bound (131 071 bytes) like every preamble byte, and the step's
  `result.json` lists them in `context` with `scope: "role"` — a new value of an existing
  field; the key set is unchanged. A bundle without role instructions is byte-for-byte the
  0.4.0 one (the Dream golden fixtures).
- **Validation**, before anything runs (exit `2`; the message names the file, the entry
  and the rule): unknown field or wrong type; both or neither of `provider` and `chain`;
  unknown provider; a provider twice in a chain; `model` on a chain role; `shell` without
  `write`; `write`, `shell` or `mcp` on an HTTP provider, in any link; `mcp` naming a
  profile `mcp.toml` lacks; `base_url` or `key_env` missing on `openai-compat` or present
  elsewhere; an invalid name, or one that collides with a provider or a workflow.

### 3.2 Workflows

A workflow is a named instance of a shape coded in the package, declared in
`$XDG_CONFIG_HOME/ha/workflows.toml`:

```toml
[implement-review]
shape      = "implement-review"
implement  = "implementer"
review     = ["reviewer-codex", "reviewer-agy"]
judge      = "judge"
max_rounds = 2

[multi-review]
shape  = "review"
review = ["reviewer-codex", "reviewer-agy", "reviewer-claude"]
judge  = "judge"
```

| Slot | `review` | `implement-review` |
|---|---|---|
| `implement` | — | a role with `write = true` |
| `review` | one role or more, each with `write = false` | the same |
| `judge` | a role with `write = false`; required with two reviewers or more, optional with one | the same |
| `max_rounds` | — | an integer ≥ 1, default `2`; a round is one review phase, so at most `max_rounds - 1` fixes |

A new shape is reviewed code in the package, never configuration. Validation follows the
roles' rules (exit `2`): unknown shape; missing or unknown slot; a slot naming an unknown
role; a capability that does not match its slot (3.3); the vendor rule (3.6).

### 3.3 Trust boundary

- **Where configuration comes from.** `roles.toml` and `workflows.toml` are read only
  from the operator's configuration directory — never from a repository, never from the
  working directory. A `roles.toml` shipped in a cloned repository could arm `write` and
  `shell`.
- **Who grants a capability.** `write`, `shell` and `mcp` are properties of a role,
  declared by the operator. A shape checks them and never grants them: the `implement`
  slot requires `write = true`; the `review` and `judge` slots require `write = false`
  (hence no shell). A reviewer cannot edit by construction, not by instruction.
- **Overrides.** `ha run` options may override a role or a provider target: the operator
  is typing. A workflow target accepts no capability override (3.8).
- **What the engine carries between agents.** The engine puts repository content (the
  diff) and agent output (the reviews) into other agents' prompts: a diff can try to
  steer a judge. The verdict is therefore advisory and nothing irreversible hangs on it:
  `ha` never merges and never pushes.

### 3.4 One engine, one dispatch path

In 0.4.0 the rules of `ha run` (`--shell` requires `--write`; `--write` and `--mcp`
refused on an HTTP provider; ...) live in the argparse layer (`cli._check_flags`): a
library caller bypasses them. That is the defect red-lab paid for (f9eff72c, aef97769): a
gate enforced by one entry point leaks through the other. 0.5.0 lowers every gate into the
engine that every entry point shares.

- **API** (final module names are the plan's call): `load_config()` reads and validates
  the four configuration files; `plan(target, request) -> Plan` resolves the target and
  applies every gate — configuration, slots and capabilities, the vendor rule, models, MCP
  profile, `openai-compat` endpoint, prompt presence and size, run directory naming — and
  raises a typed usage error before anything runs; `execute(plan) -> RunRecord` runs the
  steps and writes the run records (3.9).
- **Reuse, not rewrite.** A step is a 0.4.0 run: `run_chain` walks a role's links on `3`
  and `4`; the worktree, `.git` tripwire and carrier-commit logic of `cli_write.py` moves
  into the engine with its behaviour unchanged.
- **Parallel steps** (a review phase) run in threads, each in its own step directory;
  the judge starts once every reviewer has returned.
- **Prompt size.** A step whose prompt, preamble included, exceeds a link's
  `max_prompt_bytes` (agy and opencode: 120 000 bytes) or claude's preamble bound is
  refused before it starts, naming the rail, the limit and the size. Every link of the
  role is checked, not only the first. When the prompt is known at `plan()` time the run
  is refused with exit `2` and nothing ran; otherwise the phase is refused at its start,
  the workflow stops with exit `1` and the step is recorded with code `2`.
- **`cli.py`** parses arguments and prints; it enforces nothing. A unit test drives the
  engine API directly with each forbidden combination and expects the refusal the CLI
  gives.

### 3.5 Shape `review`

Inputs: an optional prompt (the angle to review; default "Review this change."),
`--head REF` (default `HEAD`), `--base REF` (default the remote's default branch,
`origin/HEAD`; exit `2` when it does not resolve), `--repo PATH` (default the git work
tree holding the current directory).

1. **The change.** The engine resolves `head` and the merge base of `base` and `head`,
   adds a detached worktree on `head` in the run directory, and takes the diff from the
   merge base to `head` (a pull request's three-dot diff) with its diffstat; the patch is
   saved as `change.patch`. An empty diff is refused (exit `2`: nothing to review).
2. **Reviewers, in parallel.** Each runs read-only on that worktree (0.4.0 workspace):
   it can read the changed files in full. Its prompt carries the task, the diff and the
   output contract (3.7).
3. **Every reviewer must answer** — exit `0` with a non-empty text. One failure stops the
   phase: the judge never sees a partial panel. Fault tolerance is a role's chain.
4. **The judge** receives the task, the diff and the reviews, each labelled with its
   role, provider and model; it checks the findings against the code, merges duplicates,
   discards the unfounded, and ends with the verdict line. With one reviewer and no judge,
   that reviewer's verdict decides.
5. **The verdict** is read from the last non-empty line of the deciding text, and nowhere
   else: once the `*`, `_` and backtick characters around it are stripped, it must match
   `VERDICT: APPROVE` or `VERDICT: CHANGES` (case-insensitive). Anything else is an
   unreadable verdict — a failure, never an approval. Each reviewer's own verdict line is
   recorded for the report and decides nothing when a judge runs.
6. **Cleanup.** The detached worktree is removed at the end of the run; `change.patch`
   stays.

Exit: `0` APPROVE; `6` CHANGES; `1` a step failed or the verdict is unreadable; `2`
invalid usage, nothing ran.

### 3.6 Shape `implement-review`

Inputs: the task (required), `--base REF` (default `HEAD`), `--repo PATH`.

1. **Worktree.** `git worktree add -b ha/<run_id> <run_dir>/wt <base>`, as 0.4.0
   `--write`.
2. **Implement.** The `implement` role runs with a writable workspace on that worktree
   (context `full` unless the role says otherwise). Its prompt adds: do not run git, the
   engine commits. After the step, exactly as 0.4.0 `--write`: a fired `.git` tripwire
   stops the run with no git command at all (exit `1`, worktree kept); a failed step stops
   the run (exit `1`, worktree kept); no change stops it (exit `5`); otherwise the engine
   commits `chore(ha): <run_id> implement via <provider>/<model>` with the repository's
   hooks running — a refusing hook stops the run (exit `1`, the diff left uncommitted, the
   hook output in the step's `commit.log`).
3. **Review phase.** 3.5 steps 2 to 5, on the cumulative diff `<base>..HEAD` of the
   worktree.
4. **Loop.** `APPROVE` ends the run (exit `0`). `CHANGES` with a round left runs a fix:
   the `implement` role again, prompted with the task and the deciding text's findings;
   then the same tripwire and commit rules (`chore(ha): <run_id> fix <n> via
   <provider>/<model>`) and a new round. A fix that changes nothing ends the run as not
   approved (exit `6`). `CHANGES` with no round left ends it as not approved (exit `6`).
5. **End.** Never a merge, never a push. The run ends on the branch `ha/<run_id>`, its
   diffstat, `change.patch` (`<base>..HEAD`) and the deciding text of the last round; the
   worktree stays until `ha clean`.

**Vendor rule.** Every provider of a reviewer role — every link of its chain — must be
absent from the `implement` role's providers; `plan()` refuses the workflow otherwise
(exit `2`). The judge is not constrained. This is the rule runbook eafde166 applied by
hand.

### 3.7 Prompts the engine composes

The engine owns four templates — implement, fix, review, judge — written in English and
versioned with the package. 0.5.0 does not let configuration edit them: a role shapes
behaviour through its instructions, which travel in the preamble. Each template delimits
what it carries in blocks whose attributes go through the existing `xml_attribute`
escaper: `<task>`, `<diff>`, `<review role="…" provider="…" model="…">`, `<findings>`.
The review and judge templates end with the output contract, verbatim:

> List each finding with its severity (critical, high, medium or low) and its `file:line`.
> End your answer with exactly one line: `VERDICT: APPROVE` if the change can be merged as
> it is, or `VERDICT: CHANGES` if anything must change first.

### 3.8 CLI

```text
ha run TARGET [PROMPT | -] [options]     TARGET: a workflow, a role or a provider
ha show RUN_ID [--json]
ha runs [--limit N] [--json]
ha roles [--json]
ha workflows [--json]
ha providers [--json]
ha clean RUN_ID
ha --version
```

- **Target.** Workflow, role and provider names are disjoint (validation refuses a
  collision), so `TARGET` is never ambiguous. The prompt is the argument, or stdin when it
  is `-` or absent, as in 0.4.0.
- **Options of a role or provider target:** `-m`, `--effort`, `--timeout`, `--context`,
  `--context-parents`, `--mcp`, `--write`, `--shell`, `--base`, `--repo`, `--base-url`,
  `--key-env`, `--json`, `--run-dir`. They override the role for this run; `--base` and
  `--shell` need a write run (the role's `write`, or `--write`).
- **Options of a workflow target:** `--base`, `--head` (shape `review` only), `--repo`,
  `--json`, `--run-dir`. A workflow runs as declared: any other option is refused (exit
  `2`).
- **Removed:** `-p` and `--chain`. A chain is declared in a role; a provider is a target.
- **Output.** Progress on stderr, one line per step start and end. On stdout: the final
  text — for a write run or `implement-review`, preceded by the branch, the diffstat and
  the patch path, as 0.4.0 `--write` — or `run.json` with `--json`.
- **Help.** Every option has a help line; `ha run --help` ends with the exit codes of a
  step and of a workflow, and three examples.
- **`ha roles` and `ha workflows`** list the declared entries, resolved (provider or
  chain with each link's model, effort, timeout, context, MCP profile, capabilities,
  instructions size; a workflow's shape and slots), and exit `2` on an invalid file,
  naming each problem.
- **`ha runs`** lists runs newest first: run id, target, exit code, duration, cost, first
  line of the task. A 0.4.0 run directory (no `run.json`) is listed as `legacy`, from its
  `result.json`.
- **`ha clean RUN_ID`** keeps its 0.4.0 behaviour: the worktree is removed through git
  unless the tripwire fired, and the branch is kept.

**Exit codes.** A one-step run (a role or provider target) keeps the 0.4.0 contract: `0`
answer; `1` failure; `2` invalid usage; `3` provider unavailable, chain exhausted; `4`
timeout with no tool call started; `5` write run with no change; `124` timeout. A
workflow:

| Code | Meaning |
|---|---|
| `0` | approved |
| `6` | changes requested (`review`), or not approved after `max_rounds` (`implement-review`, branch kept) |
| `5` | the implementation changed nothing |
| `1` | a step failed, or the verdict is unreadable; the step's own code is in the report |
| `2` | invalid usage or configuration; nothing ran |

Codes `3`, `4` and `124` stay a step's: a workflow that already wrote cannot promise that
nothing was written.

### 3.9 Run records and the report

Every run is a workflow run — a role run has one step:

```text
~/.cache/ha/runs/<run_id>/
  run.json            the run record (a new document, schema 1)
  prompt.md           the task, as given
  wt/                 the worktree of a write run or of implement-review
  change.patch        base..HEAD of a write run or of a workflow
  steps/<nn>-<slot>-<role>/
                      one ordinary 0.4.0 run directory per step: result.json
                      (schema 1), logs, links/ for a chain, commit.log for a write step
```

`<nn>` is the launch order on two digits; `<slot>` is `run`, `implement`, `review`,
`judge` or `fix`.

`run.json` — its key set pinned by a test; `null` means "not measured", never zero:

```json
{
  "schema": 1,
  "run_id": "20260924T101500-ab12cd34",
  "target": {"kind": "workflow", "name": "implement-review", "shape": "implement-review"},
  "status": "not_approved",
  "exit_code": 6,
  "verdict": "changes",
  "rounds": 2,
  "text": "…the deciding text of the last round…",
  "repository": "/path/to/repo",
  "base": "<sha>",
  "head": "<sha>",
  "branch": "ha/20260924T101500-ab12cd34",
  "pid": 12345,
  "started_at": "2026-09-24T10:15:00Z",
  "duration_seconds": 3012.4,
  "cost_usd": 0.55,
  "cost_complete": false,
  "steps": [
    {
      "index": 1, "round": 1, "slot": "implement", "role": "implementer",
      "dir": "steps/01-implement-implementer",
      "provider": "opencode", "model": "opencode-go/deepseek-v4.1-flash",
      "model_reported": null, "exit_code": 0, "duration_seconds": 2472.1,
      "tokens": {"input": 1200000, "output": 38000, "fresh": null, "cached": null, "thinking": null},
      "cost_usd": 0.15, "tools": {"edit": 23, "read": 41, "bash": 12}, "verdict": null
    }
  ]
}
```

- **Status:** `running`, `answered` (a one-step run that exited `0`), `failed`,
  `approved`, `changes`, `not_approved`, `no_change`. `run.json` is written at the start
  (`running`, with the `pid`) and replaced atomically after every step. A reader that
  finds `running` with no live process of that `pid` reports `incomplete` — derived, never
  written.
- **Cost:** the sum of the steps' measured costs; `cost_complete` is `false` when a
  step's cost was not measured.
- **`ha run --json`** prints `run.json`: its top-level `text` is what a script reads.
- **`ha show RUN_ID`** renders it; `ha show RUN_ID --json` prints it:

```text
20260924T101500-ab12cd34  implement-review  exit 6  not approved after 2 rounds
task    Add retry to the ingestion
branch  ha/20260924T101500-ab12cd34  3 commits  +120 -14  5 files
round 1
  implement  implementer     opencode  deepseek-v4.1-flash  0  41m12s  in 1.2M out 38k  $0.15  edit 23, read 41, bash 12
  review     reviewer-codex  codex     gpt-6-luna           0   3m05s  in 210k out 4k   -      command_execution 18  CHANGES
  review     reviewer-agy    agy       (auto)               0   2m40s  -                -      view_file 9           CHANGES
  judge      judge           claude    opus                 0   1m10s  in 90k out 3k    $0.40  -                     CHANGES
round 2
  ...
--- judge, round 2 ---
<the deciding text>
```

### 3.10 Tool counts

Each CLI rail gains a function that counts its tool calls by name from its own event log,
reached through the registry; the `AgentProvider` protocol is unchanged. Names stay the
rail's own: a normalisation across rails would lose what each call was.

| Rail | Source | Counted |
|---|---|---|
| codex | `events.jsonl` items | by the `type` of its tool items (`command_execution`, `file_change`, `mcp_tool_call`, ...), messages and reasoning excluded |
| opencode | `tool_use` events | by `part.tool` |
| agy | steps with `step_type == "tool"` | by `tool_name` |
| claude | OTEL `tool_result` records | by `tool_name`, only once a `live` test proves the telemetry complete at exit; `null` until then |
| HTTP providers | — | `{}`: they have no tools |

The counts live in `run.json` only; a step's `result.json` stays schema 1.

## 4. Testing

- **Unit, in `tests/unit/headless_agents/`** (no network, no quota): every validation
  refusal of 3.1 and 3.2, each message naming file, entry and rule; the trust boundary
  (nothing read outside the configuration directory); name collisions; the vendor rule;
  slot capabilities; the model precedence with the role level; role instructions in the
  preamble — their place, their size counted against the limits, and the bundle
  byte-identical without them; the verdict reader (last line only, emphasis stripped,
  malformed or absent = failure); the prompt-size refusal for every link of a chain; the
  exit-code mapping of 3.8; the `run.json` key set; `ha show` rendering against a golden
  text; the tool counters against recorded event logs, one per rail; CLI parsing (targets,
  overrides, options refused on a workflow target); the engine API refusing every
  combination the CLI refuses (3.4).
- **Engine, with fake providers and a throwaway git repository:** `review` with one and
  with three reviewers; `implement-review` through `CHANGES`, a fix, then `APPROVE`; a
  failing reviewer (no judge runs); an unreadable verdict; a fired tripwire (no git
  command runs); a refusing hook; exit `5` (the implementation changed nothing); exit `6`
  (a fix changed nothing; `max_rounds` exhausted); reviewers really running concurrently;
  a crashed run read as `incomplete`.
- **Boundary guard (existing):** runtime dependencies stay within `pydantic` and
  `structlog` — TOML, not YAML, for that reason; no import of `brain_v42`.
- **Dream non-regression (existing):** the golden fixtures pass unchanged.
- **`live`** (marked, excluded from CI, run by hand on an operator machine): one real
  `review` with two providers and a judge on a small diff; one real `implement-review` on
  a toy repository; two rails running concurrently; the claude telemetry measurement that
  decides whether its tool counts are published.

## 5. Versioning and delivery

- **0.5.0**, tag `headless-agents-v0.5.0` — the `ha workflow` of decision ae839a85; the
  factory stays 0.6.0.
- **CHANGELOG.** Breaking: the CLI grammar (`ha run TARGET`; `-p` and `--chain`
  removed), `ha run --json` prints `run.json`, run directories gain `steps/`. Additive:
  the engine API, roles, workflows and their two shapes, `run.json`, `ha show`,
  `ha roles`, `ha workflows`, `--version`, tool counts, the `role` context scope.
  Unchanged: `result.json` schema 1, the `AgentProvider` protocol, the providers'
  behaviour when no role instructions are given.
- **Lots, in order**, each with its tests, its own branch and pull request, reviewed by
  an independent reviewer from another provider, merged on green CI:
  1. roles (loader, validation, implicit roles, instructions in the bundle); the engine
     for one-step runs (the gates moved out of `cli.py`, the write flow moved into the
     engine); `run.json` and the `steps/` layout; the new `ha run` grammar, `ha roles`,
     `--version`, documented help — daily delegation works after this lot;
  2. `ha show`, `ha runs` (legacy runs included) and the tool counters;
  3. `workflows.toml`, `ha workflows` and the shape `review`;
  4. the shape `implement-review` and the vendor rule;
  5. the `live` suite, README and CHANGELOG (including the `brain-read` example, which
     still lists a `brain_recall` tool brain does not have), then the tag.
- **Outside this repository, after the tag:** the session skill `ha-delegate` in
  `~/.claude/skills` — when to delegate, to which role, and that a diff is read before it
  is integrated — written with the skill-writing discipline; runbook eafde166 marked
  superseded in Brain.

## 6. Consumer impact

- **red-rail and red-arena** use the library, not the CLI: `result.json` schema 1 and
  the `AgentProvider` protocol are unchanged, and their pending migrations to 0.4.0
  (spec 0.4.0 §6) are unaffected. A consumer that reads `context[].scope` sees `role`
  only on a run it gave role instructions to.
- **The Dream** calls the providers directly: unaffected, and gated by its golden
  fixtures.
- **The operator:** `models.toml` and `mcp.toml` keep working as they are. `roles.toml`
  and `workflows.toml` are new; the README shows a starting set. Writing them is where the
  provisional configuration of decision c4f1ea03 gets reviewed.

## 7. Risks

| Risk | Mitigation |
|---|---|
| A review panel multiplies quota use | Panels are declared by the operator; tokens and cost per step in `ha show`; nothing runs in parallel outside a review phase |
| A diff steers the judge (prompt injection) | The verdict is advisory; `ha` never merges nor pushes; material travels in delimited, escaped blocks; the operator reads the diff |
| A weak model ignores the output contract | An unreadable verdict is a failure, never an approval, and the report says so |
| Two rails running at once interfere | Each step has its own run directory and, where the rail has one, its own ephemeral HOME or `CODEX_HOME`; a `live` test runs two rails concurrently |
| claude's tool counts undercount | `null` until a `live` test proves the telemetry complete at exit |
| Breaking the CLI | No caller exists (§1); CHANGELOG entry; the skill is written against 0.5.0 |
| A crashed workflow leaves a worktree | The run reads as `incomplete`; `ha clean` removes it |
| A shape is too rigid for the next workflow | A new shape is a reviewed change to the package; a general language stays out until a third shape is needed |

## 8. Brainstorm decisions (2026-09-24)

The operator's answers, in order; the design above follows them.

| # | Question | Decision |
|---|---|---|
| 1 | What should revisiting the CLI achieve? | Prepare `ha workflow`, and make daily delegation practical — not an audit of the existing CLI alone |
| 2 | Which gestures of runbook eafde166 should `ha` absorb? | Named roles and a run report — not a quota preflight, not provider-session resume |
| 3 | What does a role carry? | An executor **and** optional instructions — not the executor alone |
| 4 | Which approach? | Workflow first: `ha run` is a one-step workflow — over roles plus a shared core without an engine (the first recommendation), and over roles as CLI aliases |
| 5 | Which real workflow first? | Implement then review, and the multi-provider review — not a batch of parallel tasks, not red-lab's full cycle |
| 6 | Which engine? | Two shapes coded in the package — not a flow grammar, not a general DAG |
| 7 | Design sections (vocabulary and trust; the two shapes; CLI and report; engine, tests and delivery) | Approved as presented. §5 moves `run.json` into lot 1 (the engine writes its record from its first lot) and `ha workflows` into lot 3 (with the first shape) |
