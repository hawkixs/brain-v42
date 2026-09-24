# headless-agents 0.5.0 — roles, two workflow shapes, one dispatch path, run reports

- **Date:** 2026-09-24
- **Status:** design, approved section by section by the operator on 2026-09-24, then
  amended the same day on the operator's review of the written form (§8, decision 8):
  two independent workflows, `implement` and `review`, which the operator's sessions
  combine — the combined `implement-review` shape is dropped from 0.5.0
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
to run — one that implements and one that reviews, single- or multi-provider — which his
sessions pick and combine as the task requires, and makes `ha run` its one-step case.
Roles are the unit both name.

## 2. Scope

### In

| # | Item |
|---|---|
| 1 | Roles (`roles.toml`): an executor plus optional instructions; every provider is an implicit role |
| 2 | Workflows (`workflows.toml`): named instances of two independent shapes coded in the package, `implement` and `review`, combined by the calling session (a fix continues an implementation's branch; a review can read the findings' origin and enforce the vendor rule across runs) |
| 3 | One engine (`plan` / `execute`) holding every gate; `cli.py` becomes a thin adapter |
| 4 | CLI 0.5.0: `ha run TARGET`, `ha show`, `ha runs`, `ha roles`, `ha workflows`, `ha providers`, `ha clean`, `--version`, documented help |
| 5 | Run records: `run.json` (a new document), `prompt.md`, one directory per step; tool counts per rail |
| 6 | The session skill `ha-delegate` (outside this repository); runbook eafde166 marked superseded |

### Out (explicitly)

- `ha factory` (0.6.0): intake, job queue, push and pull request, CI wait.
- A general workflow language (a flow grammar, a DAG), fan-out of implementation tasks,
  tasks produced by a step.
- A combined implement-then-review shape with an automatic fix loop (`implement-review`,
  in the first draft of this spec): the session combines the two workflows itself (§8,
  decision 8). It can return later as a third shape if the operator asks for it.
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
  characters. Role, workflow and provider names are disjoint (3.9).
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
[build]
shape     = "implement"
implement = "implementer"

[multi-review]
shape  = "review"
review = ["reviewer-codex", "reviewer-agy", "reviewer-claude"]
judge  = "judge"

[quick-review]
shape  = "review"
review = "reviewer-codex"
```

| Slot | `implement` | `review` |
|---|---|---|
| `implement` | a role with `write = true` | — |
| `review` | — | one role or more, each with `write = false` |
| `judge` | — | a role with `write = false`; required with two reviewers or more, optional with one |

The two shapes are independent: neither calls the other. A session combines them — run
`build`, review its branch with `multi-review`, feed the findings back into `build`
(3.6, 3.7) — and decides at each step whether to go on. What the engine keeps from the
combined shape it replaces is the part a session cannot check by itself: the vendor rule,
now enforced across runs (3.8).

A new shape is reviewed code in the package, never configuration. Validation follows the
roles' rules (exit `2`): unknown shape; missing or unknown slot; a slot naming an unknown
role; a capability that does not match its slot (3.3).

### 3.3 Trust boundary

- **Where configuration comes from.** `roles.toml` and `workflows.toml` are read only
  from the operator's configuration directory — never from a repository, never from the
  working directory. A `roles.toml` shipped in a cloned repository could arm `write` and
  `shell`.
- **How that directory is resolved.** `XDG_CONFIG_HOME` (and `XDG_STATE_HOME`, 3.8) is
  used only when it is an absolute path, as the XDG specification requires; a relative
  value is ignored for the default under `HOME`, never resolved against the working
  directory. The directory is resolved once with its symbolic links followed, and every
  configuration file is then resolved the same way and must lie inside it: a file that is,
  or links to, anything outside the resolved directory is refused (exit `2`, naming the
  file and where it points).
- **Who grants a capability.** `write`, `shell` and `mcp` are properties of a role,
  declared by the operator. A shape checks them and never grants them: the `implement`
  slot requires `write = true`; the `review` and `judge` slots require `write = false`
  (hence no shell). A reviewer cannot edit by construction, not by instruction.
- **Overrides.** `ha run` options may override a role or a provider target: the operator
  is typing. A workflow target accepts no capability override (3.9).
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
  steps and writes the run records (3.10).
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
tree holding the current directory). `--run RUN_ID` is the shortcut for reviewing what an
`implement` run produced: resolved through the registry (3.8), it names any member of an
`implement` lineage and stands for `--head` = the **current tip** of the lineage's
branch — later continuations included, which is what a session wants reviewed — and
`--base` = the lineage's base. It excludes `--head` and `--base` (exit `2`); a run that is
not an `implement` run, or whose branch no longer exists, is refused (exit `2`). The
review records the exact commit it read as its `head`.

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
6. **Vendor rule.** Before step 1, `plan()` applies the vendor rule across runs (3.8)
   and the run records the check it made (`vendor_check`).
7. **Cleanup.** The detached worktree is removed at the end of the run; `change.patch`
   stays. The run records the `head` it reviewed and its deciding text, which an
   `implement` run can take as findings (3.6).

Exit: `0` APPROVE; `6` CHANGES; `1` a step failed or the verdict is unreadable; `2`
invalid usage, nothing ran.

### 3.6 Shape `implement`

Inputs: the task (required, unless `--findings` is given, when it is optional extra
guidance), `--base REF` (default `HEAD`), `--repo PATH`, `--continue RUN_ID`,
`--findings RUN_ID`.

1. **Worktree.** A new run starts a lineage (3.8):
   `git worktree add -b ha/<run_id> <run_dir>/wt <base>`, as 0.4.0 `--write`. With
   `--continue RUN_ID`: no new branch — the run joins the lineage of the run it names and
   works in the lineage's worktree, on its branch, from its `base`, so a fix lands on the
   branch under review instead of stacking a new one.
2. **Implement.** The `implement` role runs with a writable workspace on that worktree
   (context `full` unless the role says otherwise). Its prompt is the implement template,
   or the fix template when `--findings` is given (3.7), and adds: do not run git, the
   engine commits. After the step, in this order: a fired `.git` tripwire stops the run
   with no git command at all (exit `1`, lineage compromised, worktree kept); a moved
   `HEAD` or branch tip stops it (exit `1`, `agent_moved_head`, 3.8); a failed step stops
   it (exit `1`, worktree kept); no change stops it (exit `5`); otherwise the engine
   commits `chore(ha): <run_id> implement via <provider>/<model>` (`fix` instead of
   `implement` when `--findings` is given) with the repository's hooks running, and
   records the commit's provenance — a refusing hook stops the run (exit `1`, the diff
   left uncommitted, the hook output in the step's `commit.log`).
3. **End.** Never a merge, never a push, never a review. The run ends on its branch, with
   its diffstat, `change.patch` (the cumulative `<base>..HEAD` of the lineage) and the
   implementer's final text; the worktree stays until `ha clean`.

**`--continue RUN_ID`** takes the lineage lock (3.8) and, under it, before any git
command, refuses (exit `2`, nothing ran) when the named run is not a member of an
`implement` lineage of the same repository; when the lineage is compromised, or one of
its members is running or not `committed`/`no_change`; when the worktree is gone
(`ha clean`); when it has uncommitted changes. The lock is held until the run's records
are published, so two continuations — naming the same run or two members of one
lineage — never run at once: the second waits for the lock for at most 10 seconds, then
is refused (exit `2`). Commits made by hand on the branch in between are kept: the diff
the next review reads is always `<base>..HEAD`.

**`--findings RUN_ID`** names a `review` run whose verdict was read (`approved` or
`changes`), resolved through the registry. Its deciding text travels in the fix
template's `<findings>` block. It is refused (exit `2`) when the reviewed `head` recorded
by that run is not the commit this run starts from — the lineage's tip read under the
lock, or `--base` for a new run: findings about another revision would steer the
implementer against code it cannot see. A session that wants to act on such findings
anyway passes them in the task instead, where they are plainly the operator's words.

Exit: `0` committed; `5` the implementation changed nothing; `1` a step failed, the
tripwire fired, `HEAD` moved or a hook refused; `2` invalid usage, nothing ran.

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

The fix template is the implement template plus the `<findings>` block of 3.6, and the
instruction to address each finding or say why not.

### 3.8 Combining runs: registry, lineage, provenance and the vendor rule

A session combines the two shapes; the engine keeps what a session cannot check by
itself: that two runs never write one worktree at once, that a compromised worktree is
never touched by git again, and that no reviewer shares a vendor with the code's author
(runbook eafde166, campaign W8). All three live in a **state directory**,
`$XDG_STATE_HOME/ha/` (default `~/.local/state/ha/`, resolved by the rules of 3.3), which
is state, not cache: deleting `~/.cache/ha` loses reports, never identity, locks or
provenance.

- **Run identity and the registry.** The engine always mints the run id
  (`<UTC timestamp>-<8 hex>`), whatever `--run-dir` says: `--run-dir` only chooses where
  the run's records live, and two custom directories with the same name are two different
  runs. Every run is registered as `<state>/runs/<run_id>.json`, created exclusively
  (`O_EXCL`: an id is never registered twice), holding its run directory, repository,
  target, lineage and status. `--run`, `--continue`, `--findings`, `ha show` and `ha clean`
  resolve an id through the registry, never by joining it to a cache path. 0.4.0 runs are
  not registered: `ha runs` lists them as `legacy`, and none of those options accepts them.
- **Lineage.** A new `implement` run (or a write run of a role) starts a lineage and owns
  its worktree and branch. A `--continue` run joins the lineage of the run it names,
  whichever member it names: the registry resolves every member to the owner. One lock,
  `<state>/lineages/<owner_run_id>.lock` (an exclusive `flock`), serialises everything that
  touches that worktree: a continuation holds it from before its first git command until
  its records are published, and `ha clean` of any member takes it too. The lineage state,
  `<state>/lineages/<owner_run_id>.json`, lists the members with their statuses and says
  whether the worktree is `compromised`; it is read under the lock before any git command.
- **Compromise belongs to the worktree, not to a run.** A fired `.git` tripwire, a step
  that moved `HEAD` (below), or a member found `incomplete` (crashed: the integrity of the
  worktree is unknown) marks the lineage `compromised`, with the member and the reason,
  written by the run that saw it before it exits. A compromised lineage refuses every
  `--continue` (exit `2`), and `ha clean` runs no git command on it — whichever member is
  named, not only the one whose tripwire fired. A continuation is admitted only when every
  member is `committed` or `no_change` and none is running. Recovering a compromised
  worktree is a manual operation, outside `ha`.
- **Provenance, per commit.** Every commit that lands during a write step is recorded as
  `<state>/provenance/<commit sha>.json`: run id, lineage, how it was made, and the
  providers of the role — every link of its chain, not only the one that answered. The
  engine's own commit (`chore(ha): <run_id> implement|fix via <provider>/<model>`, or a
  role write run's `chore(ha): <run_id> via <provider>/<model>`) is `made_by: engine`.
- **An agent that commits by itself** is caught, not trusted: "do not run git" is an
  instruction, and the tripwire watches `.git` configuration, not objects or refs. The
  engine records `HEAD` and the branch tip before the step and compares them after it.
  Any movement fails the run (exit `1`, status `failed`, reason `agent_moved_head`),
  records every commit of `<before>..<after>` with `made_by: agent` and the role's
  providers, marks the lineage compromised, and commits nothing.
- **Vendor rule.** Before a `review` run starts, `plan()` walks the commits of
  `<merge-base>..<head>` and looks each one up in the provenance. It takes the union of
  the providers of every commit found. Every provider of every reviewer role — every link
  of its chain — must be absent from that union; otherwise the run is refused (exit `2`,
  naming the reviewer, the provider and the commit). The judge is not constrained. A
  commit with no provenance entry and no `chore(ha)` subject constrains nothing: a
  hand-written change is reviewed by whoever the operator chose.
- **A lineage that cannot be read is refused, not assumed.** A commit whose subject says
  `chore(ha): …` but which has no provenance entry — a 0.4.0 write run, a deleted state
  directory, a subject typed by hand — fails the check (exit `2`, naming the commit). No
  legacy fallback is attempted: a 0.4.0 chain that succeeded on its first link leaves no
  trace of the links it declared, so its records cannot prove what the rule requires. A
  session that still wants that review runs a reviewer role directly (`ha run
  reviewer-x`), a one-step run that claims nothing about vendors.
- **The proof travels with the review.** A `review` run records the check it made in its
  own `run.json` (`vendor_check`, 3.10): every commit examined with its run id, `made_by`
  and providers, their union, and each reviewer's providers. `ha show` renders it from
  that record, so it survives `ha clean` of the implementation runs.
- **Where the rule applies.** The same check runs whatever names the head — `--run`,
  `--head ha/<run_id>`, or any ref whose history holds recorded commits.

### 3.9 CLI

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
  collision), so `TARGET` is never ambiguous.
- **Prompt.** The argument, or stdin when it is `-`. When it is absent: a target that
  needs one — a role or a provider, or `implement` without `--findings` — reads stdin as
  in 0.4.0, but only when stdin is not a terminal; on a terminal it is refused (exit `2`,
  "no prompt") instead of waiting. A target whose prompt is optional — `review`, or
  `implement` with `--findings` — never reads stdin unless given `-`, and uses its
  default.
- **Options of a role or provider target:** `-m`, `--effort`, `--timeout`, `--context`,
  `--context-parents`, `--mcp`, `--write`, `--shell`, `--base`, `--repo`, `--base-url`,
  `--key-env`, `--json`, `--run-dir`. They override the role for this run; `--base` and
  `--shell` need a write run (the role's `write`, or `--write`).
- **Options of a workflow target:** `--base`, `--repo`, `--json`, `--run-dir`; for the
  shape `review`, `--head` and `--run`; for the shape `implement`, `--continue` and
  `--findings`. None of them is a capability: a workflow runs its roles as declared, and
  any other option is refused (exit `2`).
- **Removed:** `-p` and `--chain`. A chain is declared in a role; a provider is a target.
- **Output.** Progress on stderr, one line per step start and end. On stdout: the final
  text — for a write run or an `implement` run, preceded by the run id, the branch, the
  diffstat and the patch path, as 0.4.0 `--write`, so the session can pass the run id to
  `--run`, `--continue` or `--findings` — or `run.json` with `--json`.
- **Help.** Every option has a help line; `ha run --help` ends with the exit codes of a
  step and of a workflow, and three examples.
- **`ha roles` and `ha workflows`** list the declared entries, resolved (provider or
  chain with each link's model, effort, timeout, context, MCP profile, capabilities,
  instructions size; a workflow's shape and slots), and exit `2` on an invalid file,
  naming each problem.
- **`ha runs`** lists runs newest first: run id, target, exit code, duration, cost, first
  line of the task. A 0.4.0 run directory (no `run.json`) is listed as `legacy`, from its
  `result.json`.
- **`ha clean RUN_ID`** resolves the run through the registry. For a run that owns a
  worktree it takes the lineage lock, then keeps the 0.4.0 behaviour — the worktree is
  removed through git and the run directory deleted, the branch kept — except that it
  runs no git command at all when the **lineage** is compromised, whichever member is
  named (exit `1`, naming the member and the reason). Cleaning a run never touches the
  state directory: its registry entry is marked `cleaned`, and the provenance of its
  commits and the lineage state stay, so a later review can still prove independence.

**Exit codes.** A one-step run (a role or provider target) keeps the 0.4.0 CLI contract:
`0` answer; `1` failure; `2` invalid usage; `3` provider unavailable; `4` timeout with no
tool call started; `5` write run with no change; `124` timeout. A chain that runs out of
links returns its **last link's** code, `3` or `4`, exactly as 0.4.0 `cli.run_links`
does (pinned by `test_an_exhausted_chain_returns_the_last_links_code`); the library's
`run_chain` keeps its own 0.4.0 result, unchanged. A workflow:

| Code | Meaning |
|---|---|
| `0` | approved (`review`), or committed (`implement`) |
| `6` | changes requested (`review`) |
| `5` | the implementation changed nothing (`implement`) |
| `1` | a step failed, the tripwire fired, an agent moved `HEAD`, a hook refused, or the verdict is unreadable; the step's own code is in the report |
| `2` | invalid usage or configuration, including a refused `--run`, `--continue`, `--findings`, a lineage lock not obtained, or the vendor rule; nothing ran |

Codes `3`, `4` and `124` stay a step's: a workflow that already wrote cannot promise that
nothing was written.

### 3.10 Run records and the report

Every run is a workflow run — a role run has one step:

```text
~/.cache/ha/runs/<run_id>/       (or the directory --run-dir names)
  run.json            the run record (a new document, schema 1)
  prompt.md           the task, as given
  wt/                 the worktree of a lineage's first run (continuations work in it)
  change.patch        base..HEAD of a write run or of a workflow
  steps/<nn>-<slot>-<role>/
                      one ordinary 0.4.0 run directory per step: result.json
                      (schema 1), logs, links/ for a chain, commit.log for a write step

~/.local/state/ha/                (3.8 — never removed by ha clean)
  runs/<run_id>.json          registry entry: run directory, repository, target,
                              lineage, status (cleaned when ha clean ran)
  lineages/<owner>.json       members, statuses, compromised (with member and reason)
  lineages/<owner>.lock       the lineage lock
  provenance/<sha>.json       run id, lineage, made_by (engine | agent), providers
```

`<nn>` is the launch order on two digits; `<slot>` is `run`, `implement`, `review` or
`judge`.

`run.json` — its key set pinned by a test; `null` means "not measured", never zero, or
"not applicable to this target":

```json
{
  "schema": 1,
  "run_id": "20260924T111500-cd34ef56",
  "target": {"kind": "workflow", "name": "build", "shape": "implement"},
  "status": "committed",
  "exit_code": 0,
  "verdict": null,
  "text": "…the implementer's final text…",
  "repository": "/path/to/repo",
  "base": "<sha>",
  "head": "<sha>",
  "branch": "ha/20260924T101500-ab12cd34",
  "lineage": "20260924T101500-ab12cd34",
  "continues": "20260924T101500-ab12cd34",
  "findings_from": "20260924T104000-9f8e7d6c",
  "implement_providers": ["opencode", "codex"],
  "commits": [{"sha": "<sha>", "made_by": "engine"}],
  "failure_reason": null,
  "vendor_check": null,
  "pid": 12345,
  "started_at": "2026-09-24T11:15:00Z",
  "duration_seconds": 1210.4,
  "cost_usd": 0.08,
  "cost_complete": true,
  "steps": [
    {
      "index": 1, "slot": "implement", "role": "implementer",
      "dir": "steps/01-implement-implementer",
      "provider": "opencode", "model": "opencode-go/deepseek-v4.1-flash",
      "model_reported": null, "exit_code": 0, "duration_seconds": 1208.9,
      "tokens": {"input": 610000, "output": 21000, "fresh": null, "cached": null, "thinking": null},
      "cost_usd": 0.08, "tools": {"edit": 11, "read": 25, "bash": 6}, "verdict": null
    }
  ]
}
```

`lineage`, `continues`, `findings_from`, `implement_providers` and `commits` belong to a
write run — an `implement` run, or a write run of a role (`continues` and `findings_from`
stay `null` there) — and copy what the state directory records (3.8), for the report.
`failure_reason` names why a run failed when the engine knows (`tripwire`,
`agent_moved_head`, `hook_refused`, `unreadable_verdict`, `step_failed`). A `review` run
sets the write fields to `null`, records the exact `head` it read, its `verdict`, the
deciding text, and `vendor_check`:

```json
"vendor_check": {
  "commits": [{"sha": "<sha>", "run_id": "20260924T101500-ab12cd34", "made_by": "engine",
               "providers": ["opencode", "codex"]}],
  "authors": ["codex", "opencode"],
  "reviewers": {"reviewer-agy": ["agy"], "reviewer-claude": ["claude"]}
}
```

- **Status:** `running`, `answered` (a one-step run that exited `0`), `failed`,
  `committed` (an `implement` run or a write run that committed), `no_change`,
  `approved`, `changes`. `run.json` is written at the start
  (`running`, with the `pid`) and replaced atomically after every step. A reader that
  finds `running` with no live process of that `pid` reports `incomplete` — derived, never
  written.
- **Cost:** the sum of the steps' measured costs; `cost_complete` is `false` when a
  step's cost was not measured.
- **`ha run --json`** prints `run.json`: its top-level `text` is what a script reads.
- **`ha show RUN_ID`** renders it; `ha show RUN_ID --json` prints it:

```text
20260924T104000-9f8e7d6c  multi-review  exit 6  changes requested
task    Review this change.
head    ha/20260924T101500-ab12cd34 @ 1a2b3c4  2 commits  +120 -14  5 files
vendors authors opencode, codex (2 recorded commits) — reviewers agy, claude: independent
  review     reviewer-agy     agy       (auto)  0   2m40s  -               -      view_file 9  CHANGES
  review     reviewer-claude  claude    sonnet  0   2m05s  in 150k out 3k  $0.12  Read 14      APPROVE
  judge      judge            claude    opus    0   1m10s  in 90k out 3k   $0.40  -            CHANGES
--- judge ---
<the deciding text>
```

### 3.11 Tool counts

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
  exit-code mapping of 3.9; the `run.json` key set; `ha show` rendering against a golden
  text; the tool counters against recorded event logs, one per rail; CLI parsing (targets,
  overrides, options refused on a workflow target); the engine API refusing every
  combination the CLI refuses (3.4); every refusal of `--run`, `--continue` and
  `--findings` (3.5, 3.6); the configuration path rules (a relative `XDG_CONFIG_HOME`
  ignored; a configuration file linking into the repository or anywhere outside the
  resolved directory refused); the prompt rules (absent on a terminal refused, absent on
  an optional-prompt target never reading stdin); the chain exit code (an exhausted chain
  returns its last link's `3` or `4`, a workflow step's code recorded apart from the
  workflow's).
- **Engine, with fake providers and a throwaway git repository:** `review` with one and
  with three reviewers; the combination a session drives — `implement`, then `review
  --run` returning `CHANGES`, then `implement --continue --findings` committing a fix on
  the same branch, then `review --run` returning `APPROVE`; `--run` after the branch
  advanced past the named run (the current tip is reviewed and recorded); the vendor rule
  (a reviewer sharing a provider with any link of an earlier implementer on the branch is
  refused; a hand-written commit constrains nothing; a `chore(ha)` commit with no
  provenance — a 0.4.0 write run, a deleted state directory — is refused); the report of
  a review after `ha clean` of the implementation runs and deletion of `~/.cache/ha`
  (rendered from its own `vendor_check`); stale findings refused; a fake provider that
  commits by itself (`agent_moved_head`, commits recorded `made_by: agent`, lineage
  compromised, a later review still constrained by them); lineage locking — `--continue A`
  and `--continue B` where B continues A (one waits, then is refused), `ha clean A` while
  B runs (waits); compromise shared by the lineage — a tripwire fired in B, then
  `--continue A` refused and `ha clean A` running no git command; a crashed member
  refusing continuation; `--run-dir` with two custom directories of the same name (two
  runs, each resolved through the registry); a failing reviewer (no judge runs); an
  unreadable verdict; a refusing hook; exit `5` (the implementation changed nothing);
  reviewers really running concurrently; a crashed run read as `incomplete`.
- **Boundary guard (existing):** runtime dependencies stay within `pydantic` and
  `structlog` — TOML, not YAML, for that reason; no import of `brain_v42`.
- **Dream non-regression (existing):** the golden fixtures pass unchanged.
- **`live`** (marked, excluded from CI, run by hand on an operator machine): one real
  `review` with two providers and a judge on a small diff; one real `implement`, `review
  --run`, `implement --continue --findings` sequence on a toy repository; two rails running concurrently; the claude telemetry measurement that
  decides whether its tool counts are published.

## 5. Versioning and delivery

- **0.5.0**, tag `headless-agents-v0.5.0` — the `ha workflow` of decision ae839a85; the
  factory stays 0.6.0.
- **CHANGELOG.** Breaking: the CLI grammar (`ha run TARGET`; `-p` and `--chain`
  removed), `ha run --json` prints `run.json`, run directories gain `steps/`. Additive:
  the engine API, roles, workflows and their two shapes (`implement`, `review`) with
  `--run`, `--continue`, `--findings` and the vendor rule across runs, the state directory
  (`~/.local/state/ha`: registry, lineages, provenance), `run.json`, `ha show`,
  `ha roles`, `ha workflows`, `--version`, tool counts, the `role` context scope.
  Unchanged: `result.json` schema 1, the `AgentProvider` protocol, the providers'
  behaviour when no role instructions are given.
- **Lots, in order**, each with its tests, its own branch and pull request, reviewed by
  an independent reviewer from another provider, merged on green CI:
  1. roles (loader, validation, implicit roles, instructions in the bundle); the engine
     for one-step runs (the gates moved out of `cli.py`, the write flow moved into the
     engine); `run.json` and the `steps/` layout; the state directory — registry, lineage
     lock and compromise, per-commit provenance and `agent_moved_head` — for every write
     run from this first lot, so no commit `ha` makes is ever unrecorded; the new `ha run`
     grammar, `ha roles`, `--version`, documented help, `ha clean` through the registry —
     daily delegation works after this lot;
  2. `ha show`, `ha runs` (legacy runs included) and the tool counters;
  3. `workflows.toml`, `ha workflows` and the shape `implement` with `--continue` (a
     continued lineage under its lock);
  4. the shape `review`, shipped with the vendor rule and `vendor_check` from its first
     commit, with `--run`, and `implement --findings` (which needs a review run to name) —
     no lot ever exposes a review that does not enforce the rule;
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
| A session combines the workflows badly (reviews the wrong head, fixes against stale findings) | `--run` reviews the lineage's current tip and records the exact commit read; `--findings` is refused unless the reviewed head is the fix's starting commit; `ha show` names the head each review read |
| Separate runs lose the vendor independence the combined shape guaranteed | Per-commit provenance in the state directory, commits an agent made itself included; the vendor rule reads it (3.8); a `chore(ha)` commit without provenance is refused, never assumed |
| Two runs write one worktree, or git runs in a compromised one | One lock per lineage, taken by every continuation and by `ha clean`; compromise recorded on the lineage and read under the lock before any git command |
| The state directory is lost | Reports survive in the cache and in each review's `vendor_check`; reviews of branches whose provenance is gone are refused, not waved through |
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
| 8 | Review of the written spec: one combined `implement-review` shape, or separate workflows? | Separate: one workflow to implement, one to review, which the operator's sessions pick and combine — "more flexible". The shapes become `implement` and `review`; `implement-review` and its automatic loop are dropped from 0.5.0 (could return later as a third shape); a fix continues the same branch (`--continue`) and takes a review's findings (`--findings`); `review --run` reads an implementation run; the vendor rule moves across runs. Lots 3 and 4 re-cut accordingly |
