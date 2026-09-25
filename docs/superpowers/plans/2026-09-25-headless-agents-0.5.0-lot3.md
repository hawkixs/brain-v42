# headless-agents 0.5.0 — Lot 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Execution method, chosen by the operator for this lot (2026-09-25, the lot 1 and lot 2
> method Q60 = a):** this plan is reviewed by codex, then implemented inline, test-first, in
> the session that wrote it, in two pull requests (A and B below), each reviewed by an
> independent non-Claude reviewer and merged under delegation 39f7ea9f (approve verdict +
> required CI green, never `--admin`). No tag and no release in this lot: the tag follows
> lot 5 (spec §5), and both stay the operator's.

**Goal:** Deliver lot 3 of headless-agents 0.5.0 — `workflows.toml`, `ha workflows`, and the
shape `implement` run as `ha run WORKFLOW`, with `--continue RUN_ID` joining an implement
run's lineage under its lock.

**Architecture:** A new `workflows` module reads and validates `workflows.toml` against the
roles; the engine's `load_config` validates roles, MCP profiles and workflows as a whole
before any run, and `plan()` resolves a workflow target to its `implement` role, refuses
every role override, and wraps the task in the engine's implement template (a new
`templates` module). `execute()` runs that role through lot 1's write protocol
(`write_flow.run_write_step`), recording the run as a workflow run (`steps/01-implement-<role>`,
`target.kind = "workflow"`). A continuation is the same write joining an existing lineage:
the write flow takes that lineage's lock exclusively, checks it from the state, records its
intent in the lineage's state, then checks the lineage's worktree clean and on its branch
before the provider runs. The registry entry gains the continuation records `continues` and
`providers`, which `run.json` and `ha show` copy.

**Tech Stack:** Python 3.12 standard library (`tomllib`, `dataclasses`, `pathlib`), git,
pytest. No new dependency.

**Spec:** `docs/specs/2026-09-24-headless-agents-0.5.0-design.md`. Read §3.1, §3.2, §3.3,
§3.4, §3.6 (without `--findings`, lot 4), §3.7, §3.8.1–§3.8.3, §3.9, §3.10 and §4 before any
task. The lot 1 and lot 2 plans (`docs/superpowers/plans/2026-09-25-headless-agents-0.5.0-lot1.md`,
`…-lot2.md`) state what the earlier lots built; lot 2's P6 left `continues` and
`implement_providers` to this lot.

## Global Constraints

- Runtime dependencies stay `pydantic` and `structlog`; no import of `brain_v42` from
  `packages/headless-agents` (`tests/unit/headless_agents/test_package_boundary.py`).
- Unchanged: `result.json` schema 1 and its key set, the `AgentProvider` protocol, `run_chain`'s
  own result, and the `run.json` key sets `RUN_KEYS` and `STEP_KEYS` (spec §2 Out, §3.10).
- "`plan()` never runs git" (§3.8.2): it reads the configuration and resolves run ids
  through the registry; every decision that reads git or the mutable state is taken in
  `execute()`, under the locks, and "the early refusals `plan()` can give from the registry
  are re-checked there".
- "`roles.toml` and `workflows.toml` are read only from the operator's configuration
  directory — never from a repository, never from the working directory" (§3.3), through
  `config_paths.config_file`.
- "A workflow target accepts no capability override" (§3.3); its options are `--base`,
  `--repo`, `--json`, `--run-dir` and, for the shape `implement`, `--continue` (§3.9;
  `--findings` is lot 4's).
- A workflow's exit codes (§3.9): `0` committed, `5` the implementation changed nothing,
  `1` a step failed, the tripwire fired, an agent moved `HEAD` or a hook refused, `2` invalid
  usage, a refused `--continue` or a lineage lock not obtained — nothing ran. "Codes `3`,
  `4` and `124` stay a step's".
- The lock order of §3.8.2 is fixed: lifecycle, unconfined, lineage registry, lineage locks
  in ascending owner order. A continuation "waits for the lock for at most 10 seconds, then
  is refused (exit `2`)" (§3.6, `locks.LOCK_WAIT_SECONDS`).
- A report is never an authority (§3.8.1): `continues` and `implement_providers` "copy what
  the state directory records" (§3.10).
- Everything written for GitHub — code, comments, tests, commits, PRs — in English.
- Gates before every commit: `.venv/bin/pytest tests/unit/headless_agents tests/unit/agents -q
  -p no:cacheprovider`, `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  `.venv/bin/mypy src/ packages/headless-agents/src`; and the full `tests/unit/` before each
  push. Outside the canonical root, export `POSTGRES_URL` before `pytest tests/unit/`
  (CLAUDE.md, Tests); the worktree's own venv comes from `uv sync --locked --extra dev
  --python 3.12`.
- Every code and test block below is formatted by `ruff format` and was run as written
  (2026-09-25), on a copy of the package at `0c7feca0`: the Task 1–6 blocks with the PR A
  test set green (1195 passed), the Task 7–8 blocks with the full lot 3 test set green
  (1223 passed), `ruff check`, `ruff format --check` and `mypy` clean.

## Decisions this plan takes where the spec leaves the choice to it

| # | Decision | Why |
|---|---|---|
| P1 | `workflows.py`: `load_workflows(path, *, roles) -> dict[str, Workflow]` validates both shapes of §3.2 and refuses with lot 1's format, `{file}: [{entry}] {rule}`, first refusal first, as `roles.load_roles` does. A slot names a declared role or a provider (its implicit role). A workflow name collides with no provider and no role of `roles.toml`. | §3.2: "Validation follows the roles' rules (exit `2`)"; §3.1: "Role, workflow and provider names are disjoint". One refusal style across the two files. |
| P2 | A `review` workflow is validated, listed by `ha workflows`, and refused by `ha run` (exit `2`) until lot 4, naming the reviewer role a session can run directly. | §5 item 4: "no lot ever exposes a review that does not enforce the rule"; refusing the whole `workflows.toml` instead would break every run of an operator who declares a review ahead of time. §3.8.4: a session that wants an unconstrained review "runs a reviewer role directly". |
| P3 | `engine.load_config()` reads `roles.toml`, `mcp.toml` and `workflows.toml` as one validated `Config` for every `ha run`, whatever its target (an invalid `workflows.toml` refuses `ha run codex` too); `models.toml` stays read per link. `ha workflows [--json]` lists each workflow's shape and its slots, each slot with its role and that role's providers (every link). | §3.1 and §3.2: validation "before anything runs"; §3.4: "`load_config()` reads and validates the four configuration files". The providers per slot are what a session needs to reason about the vendor rule of lot 4. |
| P4 | A workflow target: any role override (`-m`, `--effort`, `--timeout`, `--context`, `--context-parents`, `--mcp`, `--write`, `--shell`, `--base-url`, `--key-env`) refused by `plan()` with exit `2`, naming it. The provider gets `templates.implement_prompt(task)`; `prompt.md` keeps the task as given; the size check counts the template. | §3.3: "A workflow target accepts no capability override"; §3.4: the gate is the engine's, the CLI's parsing enforces nothing; §3.10: "`prompt.md` the task, as given"; §3.4: the size refused is the prompt the provider gets. |
| P5 | The continuation records behind `continues` and `implement_providers` (§3.10) are two keys of the registry entry, written once by `Registry.create`: `continues` (the run `--continue` named, or `null`) and `providers` (every link of the run's role). `Registry._entry` requires both, as it requires every key `create` writes since lot 2. `run.json` copies them for a write run; `ha show` reads them from the entry, never from the report; lot 2's `LATER_AUTHORITIES` shrinks to `verdict`, `vendor_check`, `cleanup` and `findings_from`. | §3.8.1: the registry entry holds a run's identity; both facts are fixed at admission and never change. Measured 2026-09-25: the operator's state directory holds `proofs/` only — no registry entry exists that the new required keys would make unknown. |
| P6 | An engine commit's provenance records every link of the role, as agent and hook commits already do. Lot 1 recorded `providers: []` for it (`write_flow._publish`, pinned by `test_a_committed_write`): that test is updated to the spec's expectation. | §3.8.4 step 4: "A commit with provenance takes its recorded providers", and the §3.10 `vendor_check` example records an engine commit with the implementer's two providers. With `[]`, lot 4's vendor rule would let a reviewer share the implementer's vendor. |
| P7 | `--continue RUN_ID` is resolved by `plan()` from the registry only: not a run id, no such run, an unreadable entry, or a run that is not an `implement` run (its target a `workflow` of shape `implement`, with a lineage) → exit `2`; `--base` with `--continue` → exit `2`; `--continue` on any other target → exit `2`. `execute()` reads the entry again before it creates anything. | §3.8.2: "resolves run ids through the registry", early refusals "re-checked there"; §3.6: a continuation works "from its `base`". A run dir created before the re-check would outlive a refused run (lot 1: a run that never started leaves nothing behind). |
| P8 | Under the lineage lock, `write_flow` checks the continued lineage from the state: known; a pending write stale (this process holds the lock) → `unfinalized_write`, repository quarantine, refused, as `check_repository` does for any other lineage; not compromised; listing the named run; same repository (common dir); its worktree still on disk. The continuation takes the lineage registry lock **shared** (it creates no lineage). Its intent adds it as a member and the pending write in one `save`. | §3.6: the refusals "from the state, before any git command"; §3.8.3 step 1: "no stale pending write in any lineage of the repository"; §3.8.2: "enumerating lineages to decide anything holds it shared". |
| P9 | Preparation of a continuation: the worktree clean (`git status --porcelain`), on its lineage's branch (`git symbolic-ref HEAD`), that branch resolving, a base recorded — otherwise refused (exit `2`), the lineage restored as admission read it (its member and pending write gone, and an unconfined intent removed), the run's entry forgotten and its directory removed. The start point is the branch's tip: commits made by hand on the branch are kept, unattributed, and `change.patch` is the lineage's `base..HEAD`. | §3.8.3 step 3: "A continuation checks its worktree is clean (refused otherwise, the pending write cleared: nothing ran)"; §3.6: "Commits made by hand on the branch in between are kept: the diff the next review reads is always `<base>..HEAD`". A worktree switched to another branch would put the continuation's commit where no review of the lineage reads it. |
| P10 | A write run and an `implement` run print, before their text: `run: <run_id>`, `branch: <branch>`, `diffstat: <+N -M  F files>` read from `change.patch` (no git in `cli.py`), `patch: <path>` — whenever they exit `0`, with or without a text. | §3.9: "preceded by the run id, the branch, the diffstat and the patch path, as 0.4.0 `--write`, so the session can pass the run id to `--run`, `--continue` or `--findings`". Lot 1 printed the branch and the patch only, and only with a text. |
| P11 | A continuation holds the lineage lock from its admission to the publication of the lineage state — the member's final status and the pending write cleared — as every write of lot 1 does; `run.json` is written by the engine right after, outside it. | §3.8.3 step 9: the lineage rename is "the single point where the write becomes final"; §3.8.1: `run.json` is "a report … never read back to decide anything". A continuation admitted in that window reads the published lineage, so two continuations never write at once (§3.6). |
| P12 | Lot 3 ships as two pull requests: A (Tasks 1–6: `workflows.toml`, `ha workflows`, the template, a workflow target planned, the continuation records, an `implement` run) and B (Tasks 7–8: `--continue` in the engine, then in the CLI). No `--continue` code path exists before B: `Request.continue_run` arrives with it. | Each leaves `main` whole: after A, `ha run build "task"` works and nothing half-continues; B adds the lineage-joining write on its own. |

## Review Focus

Five inputs the spec implies but no spec test names, most likely first; each has a test in
the task that owns the code.

1. **A commit made by hand on the lineage's branch between two runs** — the operator fixes a
   typo in the worktree and commits. A person expects it kept, the continuation starting
   from it, the commit left unattributed (no provenance), and in the next `change.patch`.
   Test in Task 7.
2. **The lineage's worktree detached or switched to another branch by hand** — a person
   expects `--continue` refused (exit `2`, nothing ran), not a commit on a branch no review
   of the lineage reads. Test in Task 7.
3. **`--continue` run from inside the lineage's own worktree** — the continued lineage is
   also a source lineage of `--repo` (§3.8.2). A person expects it to run, not to deadlock
   on its own lock or be refused. Test in Task 7.
4. **An `implement` slot naming a provider** — `implement = "codex"` names codex's implicit
   role, which does not write. A person expects `workflows.toml` refused naming the slot and
   the rule, before anything runs. Test in Task 1.
5. **A `review` workflow declared before its shape ships** — a person expects it listed by
   `ha workflows`, and `ha run` on it refused with the reviewer role to run instead, while
   every other run still works. Tests in Tasks 2 and 4.

---

## File structure

```text
packages/headless-agents/src/headless_agents/
  workflows.py           NEW  Workflow, load_workflows: workflows.toml validated (§3.2, P1, P2)
  templates.py           NEW  implement_prompt: the implement template (§3.7)
  engine.py              MOD  Config, load_config, describe_workflows (P3); a workflow target
                              planned (P4) and executed; --continue planned and re-checked (P7);
                              the continuation records passed to the registry (P5)
  runs.py                MOD  Entry.continues, Entry.providers; create writes them, _entry
                              requires them (P5)
  show.py                MOD  LATER_AUTHORITIES shrinks; continues and implement_providers from
                              the entry (P5); format_diffstat (P10)
  write_flow.py          MOD  engine commits carry the role's providers (P6); a continuation
                              joins its lineage: admission, intent, preparation (P8, P9)
  cli.py                 MOD  ha workflows; the write header (P10); --continue; help
packages/headless-agents/README.md   MOD  synopsis and a workflow paragraph
tests/unit/headless_agents/
  test_workflows.py      NEW  every validation of workflows.toml
  test_templates.py      NEW  the implement template, pinned
  test_implement.py      NEW  an implement run and its continuations, real git, fake agent
  test_engine_plan.py    MOD  a workflow target planned; --continue refusals
  test_engine_execute.py MOD  an entry records its providers
  test_write_flow.py     MOD  a write run's providers; the engine commit's provenance (P6)
  test_runs.py           MOD  the continuation records of the registry entry
  test_show.py           MOD  continues and implement_providers from the entry
  test_cli.py            MOD  ha workflows; an invalid workflows.toml refuses a run
```

## Pull requests

| PR | Tasks | Content | Leaves `main` |
|---|---|---|---|
| A | 1–6 | `workflows.toml`, `ha workflows`, the implement template, a workflow target planned, the continuation records, an `implement` run, the write header, the engine commit's providers | `ha run build "task"` runs the shape `implement` on a new lineage |
| B | 7–8 | `--continue` in the engine and the write flow, then in the CLI and the help | lot 3 complete |

---

## PR A — Tasks 1–6

### Task 1: `workflows.toml` — the loader and its validation

**Files:**
- Create: `packages/headless-agents/src/headless_agents/workflows.py`
- Create: `tests/unit/headless_agents/test_workflows.py`

**Interfaces:**
- Consumes: `roles.NAME_PATTERN`, `roles.Role`, `roles.RolesError`,
  `roles.resolve_role(name, declared) -> Role`, `registry.PROVIDER_NAMES`.
- Produces:

```python
Shape = Literal["implement", "review"]
SHAPES: Final[tuple[Shape, ...]]                       # ("implement", "review")
SLOTS: Final[Mapping[Shape, frozenset[str]]]           # the keys each shape accepts
class WorkflowsError(ValueError)                       # "{file}: [{entry}] {rule}"
@dataclass(frozen=True)
class Workflow:
    name: str
    shape: Shape
    implement: str | None                              # the implement slot's role
    review: tuple[str, ...]                            # the review slot's roles, in order
    judge: str | None
    def slot_roles(self) -> tuple[tuple[str, str], ...]  # (slot, role) pairs, in slot order
def load_workflows(path: Path | None, *, roles: Mapping[str, Role]) -> dict[str, Workflow]
```

- [ ] **Step 1: Write the failing tests.** Create `tests/unit/headless_agents/test_workflows.py`:

```python
"""workflows.toml: named instances of the two shapes, validated before anything runs (spec 0.5.0 §3.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents.roles import load_roles
from headless_agents.workflows import Workflow, WorkflowsError, load_workflows

ROLES = """\
[implementer]
chain = ["opencode:oc-model", "codex:gpt-6-luna"]
write = true

[reviewer-codex]
provider = "codex"

[reviewer-agy]
provider = "agy"

[judge]
provider = "claude"

[scribe]
provider = "claude"
write = true
"""


def _load(tmp_path: Path, text: str) -> dict[str, Workflow]:
    roles_path = tmp_path / "roles.toml"
    roles_path.write_text(ROLES)
    path = tmp_path / "workflows.toml"
    path.write_text(text)
    return load_workflows(path, roles=load_roles(roles_path, mcp_profiles={}))


def test_an_implement_and_a_review_workflow_load(tmp_path: Path) -> None:
    workflows = _load(
        tmp_path,
        '[build]\nshape = "implement"\nimplement = "implementer"\n\n'
        '[multi-review]\nshape = "review"\nreview = ["reviewer-codex", "reviewer-agy"]\n'
        'judge = "judge"\n\n'
        '[quick-review]\nshape = "review"\nreview = "reviewer-codex"\n',
    )
    assert workflows["build"] == Workflow(
        name="build", shape="implement", implement="implementer", review=(), judge=None
    )
    assert workflows["multi-review"].review == ("reviewer-codex", "reviewer-agy")
    assert workflows["multi-review"].judge == "judge"
    assert workflows["quick-review"] == Workflow(
        name="quick-review", shape="review", implement=None, review=("reviewer-codex",), judge=None
    )


def test_a_slot_may_name_a_providers_implicit_role(tmp_path: Path) -> None:
    workflows = _load(tmp_path, '[peek]\nshape = "review"\nreview = "codex"\n')
    assert workflows["peek"].review == ("codex",)


def test_slot_roles_lists_the_reviewers_then_the_judge(tmp_path: Path) -> None:
    workflows = _load(
        tmp_path,
        '[m]\nshape = "review"\nreview = ["reviewer-codex", "reviewer-agy"]\njudge = "judge"\n',
    )
    assert workflows["m"].slot_roles() == (
        ("review", "reviewer-codex"),
        ("review", "reviewer-agy"),
        ("judge", "judge"),
    )


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('build = "implementer"\n', "a workflow must be a table"),
        ('[Build]\nshape = "implement"\nimplement = "implementer"\n', "invalid workflow name"),
        ('[codex]\nshape = "implement"\nimplement = "implementer"\n', "collides with a provider"),
        ('[judge]\nshape = "review"\nreview = "codex"\n', "collides with a role of roles.toml"),
        ('[build]\nimplement = "implementer"\n', "the shape is missing"),
        ('[build]\nshape = "pipeline"\n', "unknown shape 'pipeline'"),
        ("[build]\nshape = 3\n", "unknown shape 3"),
        (
            '[build]\nshape = "implement"\nimplement = "implementer"\njudge = "judge"\n',
            "unknown slot 'judge' for the shape implement",
        ),
        (
            '[r]\nshape = "review"\nreview = "codex"\nimplement = "implementer"\n',
            "unknown slot 'implement' for the shape review",
        ),
        ('[build]\nshape = "implement"\n', "the implement slot is missing"),
        (
            '[build]\nshape = "implement"\nimplement = ["implementer"]\n',
            "implement must name a role",
        ),
        (
            '[build]\nshape = "implement"\nimplement = "nobody"\n',
            "implement names unknown role 'nobody'",
        ),
        (
            '[build]\nshape = "implement"\nimplement = "reviewer-codex"\n',
            "the implement slot needs a role with write = true; reviewer-codex does not write",
        ),
        (
            '[build]\nshape = "implement"\nimplement = "codex"\n',
            "the implement slot needs a role with write = true; codex does not write",
        ),
        ('[r]\nshape = "review"\n', "the review slot is missing"),
        ('[r]\nshape = "review"\nreview = []\n', "review must name one role or a non-empty list"),
        ('[r]\nshape = "review"\nreview = 7\n', "review must name one role or a non-empty list"),
        ('[r]\nshape = "review"\nreview = ["codex", 7]\n', "review must name a role"),
        ('[r]\nshape = "review"\nreview = "nobody"\n', "review names unknown role 'nobody'"),
        (
            '[r]\nshape = "review"\nreview = ["codex", "codex"]\njudge = "judge"\n',
            "codex appears twice in review",
        ),
        (
            '[r]\nshape = "review"\nreview = "scribe"\n',
            "the review slot needs roles with write = false; scribe writes",
        ),
        (
            '[r]\nshape = "review"\nreview = "codex"\njudge = "nobody"\n',
            "judge names unknown role 'nobody'",
        ),
        (
            '[r]\nshape = "review"\nreview = "codex"\njudge = "scribe"\n',
            "the judge slot needs a role with write = false; scribe writes",
        ),
        (
            '[r]\nshape = "review"\nreview = ["codex", "agy"]\n',
            "a judge is required with two reviewers or more",
        ),
    ],
)
def test_every_refusal_names_the_file_the_entry_and_the_rule(
    tmp_path: Path, text: str, rule: str
) -> None:
    with pytest.raises(WorkflowsError) as refused:
        _load(tmp_path, text)
    message = str(refused.value)
    assert message.startswith(f"{tmp_path / 'workflows.toml'}: [")
    assert rule in message


def test_no_file_is_no_workflow(tmp_path: Path) -> None:
    assert load_workflows(None, roles={}) == {}
    assert load_workflows(tmp_path / "workflows.toml", roles={}) == {}


def test_a_file_that_does_not_parse_is_refused_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "workflows.toml"
    path.write_text("[build\n")
    with pytest.raises(WorkflowsError, match="workflows.toml"):
        load_workflows(path, roles={})


def test_a_file_nested_too_deeply_is_refused_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "workflows.toml"
    path.write_text("x = " + "[" * 2000 + "]" * 2000 + "\n")
    with pytest.raises(WorkflowsError, match="nested too deeply"):
        load_workflows(path, roles={})
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_workflows.py -q -p no:cacheprovider`
Expected: a collection error — `ModuleNotFoundError: No module named 'headless_agents.workflows'`.

- [ ] **Step 3: Implement.** Create `packages/headless-agents/src/headless_agents/workflows.py`:

```python
"""Workflows: named instances of the two shapes coded in the package (spec 0.5.0 §3.2).

One TOML table per workflow in ``workflows.toml``, read from the operator's
configuration directory only (:mod:`headless_agents.config_paths`, §3.3):

.. code-block:: toml

    [build]
    shape     = "implement"
    implement = "implementer"

    [multi-review]
    shape  = "review"
    review = ["reviewer-codex", "reviewer-agy", "reviewer-claude"]
    judge  = "judge"

A shape is reviewed code in the package, never configuration: a workflow only
names the roles that fill its slots, and a slot checks the capability its
role declares -- it never grants one (§3.3). Every refusal names the file,
the entry and the rule, before anything runs.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from .registry import PROVIDER_NAMES
from .roles import NAME_PATTERN, Role, RolesError, resolve_role

Shape = Literal["implement", "review"]
SHAPES: Final[tuple[Shape, ...]] = ("implement", "review")
#: The slots of each shape (§3.2); any other key of a workflow is refused.
SLOTS: Final[Mapping[Shape, frozenset[str]]] = {
    "implement": frozenset({"implement"}),
    "review": frozenset({"review", "judge"}),
}


class WorkflowsError(ValueError):
    """``workflows.toml`` or one of its workflows cannot be used; the message says where and why."""


@dataclass(frozen=True)
class Workflow:
    name: str
    shape: Shape
    #: The role of the ``implement`` slot; ``None`` for a ``review`` workflow.
    implement: str | None
    #: The roles of the ``review`` slot, in declaration order; ``()`` for ``implement``.
    review: tuple[str, ...]
    #: The role of the ``judge`` slot, when there is one.
    judge: str | None

    def slot_roles(self) -> tuple[tuple[str, str], ...]:
        """Every ``(slot, role)`` pair: the implementer, or the reviewers then the judge."""
        if self.implement is not None:
            return (("implement", self.implement),)
        pairs = tuple(("review", role) for role in self.review)
        return pairs + ((("judge", self.judge),) if self.judge is not None else ())


def _refuse(path: Path, name: str, rule: str) -> WorkflowsError:
    return WorkflowsError(f"{path}: [{name}] {rule}")


def _slot_role(path: Path, name: str, slot: str, value: object, roles: Mapping[str, Role]) -> Role:
    """The role a slot names: a role of ``roles.toml``, or a provider's implicit role."""
    if not isinstance(value, str):
        raise _refuse(path, name, f"{slot} must name a role")
    try:
        return resolve_role(value, roles)
    except RolesError:
        raise _refuse(path, name, f"{slot} names unknown role {value!r}") from None


def _implement(
    path: Path, name: str, table: Mapping[str, object], roles: Mapping[str, Role]
) -> Workflow:
    if "implement" not in table:
        raise _refuse(path, name, "the implement slot is missing")
    role = _slot_role(path, name, "implement", table["implement"], roles)
    if not role.write:
        raise _refuse(
            path,
            name,
            f"the implement slot needs a role with write = true; {role.name} does not write",
        )
    return Workflow(name=name, shape="implement", implement=role.name, review=(), judge=None)


def _review(
    path: Path, name: str, table: Mapping[str, object], roles: Mapping[str, Role]
) -> Workflow:
    raw = table.get("review")
    if raw is None:
        raise _refuse(path, name, "the review slot is missing")
    names = [raw] if isinstance(raw, str) else raw
    if not isinstance(names, list) or not names:
        raise _refuse(path, name, "review must name one role or a non-empty list of roles")
    reviewers = [_slot_role(path, name, "review", value, roles) for value in names]
    seen: set[str] = set()
    for role in reviewers:
        if role.name in seen:
            raise _refuse(path, name, f"{role.name} appears twice in review")
        seen.add(role.name)
        if role.write:
            raise _refuse(
                path, name, f"the review slot needs roles with write = false; {role.name} writes"
            )
    judge: str | None = None
    if "judge" in table:
        role = _slot_role(path, name, "judge", table["judge"], roles)
        if role.write:
            raise _refuse(
                path, name, f"the judge slot needs a role with write = false; {role.name} writes"
            )
        judge = role.name
    elif len(reviewers) > 1:
        raise _refuse(path, name, "a judge is required with two reviewers or more")
    return Workflow(
        name=name,
        shape="review",
        implement=None,
        review=tuple(role.name for role in reviewers),
        judge=judge,
    )


def _workflow(path: Path, name: str, table: object, roles: Mapping[str, Role]) -> Workflow:
    if not isinstance(table, dict):
        raise _refuse(path, name, "a workflow must be a table")
    if not NAME_PATTERN.fullmatch(name):
        raise _refuse(
            path,
            name,
            "invalid workflow name: lowercase letters, digits and '-', starting with a letter, "
            "at most 64 characters",
        )
    if name in PROVIDER_NAMES or name in roles:
        what = "a provider" if name in PROVIDER_NAMES else "a role of roles.toml"
        raise _refuse(
            path,
            name,
            f"collides with {what}: workflow, role and provider names are disjoint",
        )
    if "shape" not in table:
        raise _refuse(path, name, "the shape is missing; valid shapes: implement, review")
    value = table["shape"]
    if not isinstance(value, str) or value not in SHAPES:
        raise _refuse(path, name, f"unknown shape {value!r}; valid shapes: implement, review")
    shape = cast("Shape", value)
    unknown = sorted(table.keys() - {"shape"} - SLOTS[shape])
    if unknown:
        raise _refuse(path, name, f"unknown slot {unknown[0]!r} for the shape {shape}")
    if shape == "implement":
        return _implement(path, name, table, roles)
    return _review(path, name, table, roles)


def load_workflows(path: Path | None, *, roles: Mapping[str, Role]) -> dict[str, Workflow]:
    """Every workflow declared in ``path``, validated against ``roles``; ``{}`` without a file.

    ``roles`` are the roles of ``roles.toml``: a slot names one of them or a
    provider, and a workflow's name collides with none of them.
    """
    if path is None:
        return {}
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkflowsError(f"{path}: {exc}") from None
    except RecursionError:
        # tomllib recurses per nesting level: a value nested hundreds deep is a
        # broken file, not a crash (as roles.load_roles).
        raise WorkflowsError(f"{path}: nested too deeply to be a workflows file") from None
    return {name: _workflow(path, name, table, roles) for name, table in document.items()}


__all__ = ["SHAPES", "SLOTS", "Shape", "Workflow", "WorkflowsError", "load_workflows"]
```

- [ ] **Step 4: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_workflows.py -q -p no:cacheprovider`
Expected: `30 passed`. Then the gates of the Global Constraints.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/workflows.py tests/unit/headless_agents/test_workflows.py
git commit -m "feat(headless-agents): workflows.toml, the named instances of the two shapes"
```

### Task 2: `ha workflows`

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/engine.py` (`WORKFLOWS_FILE_NAME`,
  `Config`, `load_config`, `describe_workflows`, `__all__`)
- Modify: `packages/headless-agents/src/headless_agents/cli.py` (docstring, parser, `_workflows`,
  `main`)
- Modify: `packages/headless-agents/README.md` (synopsis)
- Modify: `tests/unit/headless_agents/test_cli.py`

**Interfaces:**
- Consumes: `workflows.load_workflows`, `workflows.Workflow.slot_roles`,
  `engine.declared_roles(environ, home) -> (roles, profiles)`,
  `config_paths.config_file(name, environ, home=...)`.
- Produces:

```python
WORKFLOWS_FILE_NAME: Final = "workflows.toml"
@dataclass(frozen=True)
class Config:
    roles: dict[str, Role]
    profiles: Mapping[str, object]
    workflows: dict[str, Workflow]
def load_config(environ: Mapping[str, str], home: Path) -> Config   # UsageError when invalid
def describe_workflows(environ: Mapping[str, str], home: Path) -> list[dict[str, object]]
#   [{"name": ..., "shape": ..., "slots": [{"slot": ..., "role": ..., "providers": [...]}]}]
```

- [ ] **Step 1: Write the failing tests.** In `tests/unit/headless_agents/test_cli.py`, insert
  before the section comment `# ── ha runs / ha clean ─…`:

```python
# ── ha workflows (lot 3) ───────────────────────────────────────────────────

_WORKFLOW_ROLES = (
    '[implementer]\nchain = ["opencode", "codex"]\nwrite = true\n\n[judge]\nprovider = "claude"\n'
)
_WORKFLOWS = (
    '[build]\nshape = "implement"\nimplement = "implementer"\n\n'
    '[duo]\nshape = "review"\nreview = ["codex", "agy"]\njudge = "judge"\n'
)


def _workflows(world: _World, text: str) -> None:
    (world.home / ".config" / "ha" / "workflows.toml").write_text(text)


def test_workflows_json_lists_each_slot_with_its_roles_providers(world: _World) -> None:
    world.roles(_WORKFLOW_ROLES)
    _workflows(world, _WORKFLOWS)
    code, out, _ = world.run("workflows", "--json")
    assert code == 0
    assert json.loads(out) == [
        {
            "name": "build",
            "shape": "implement",
            "slots": [
                {"slot": "implement", "role": "implementer", "providers": ["opencode", "codex"]}
            ],
        },
        {
            "name": "duo",
            "shape": "review",
            "slots": [
                {"slot": "review", "role": "codex", "providers": ["codex"]},
                {"slot": "review", "role": "agy", "providers": ["agy"]},
                {"slot": "judge", "role": "judge", "providers": ["claude"]},
            ],
        },
    ]


def test_workflows_prints_one_line_per_workflow(world: _World) -> None:
    world.roles(_WORKFLOW_ROLES)
    _workflows(world, _WORKFLOWS)
    code, out, _ = world.run("workflows")
    assert code == 0
    assert out.splitlines() == [
        "build                implement  implement: implementer (opencode, codex)",
        "duo                  review     review: codex (codex), agy (agy); judge: judge (claude)",
    ]


def test_workflows_without_a_file_lists_nothing(world: _World) -> None:
    code, out, _ = world.run("workflows", "--json")
    assert code == 0 and json.loads(out) == []


def test_workflows_on_an_invalid_file_exits_2_naming_the_problem(world: _World) -> None:
    _workflows(world, '[build]\nshape = "implement"\nimplement = "codex"\n')
    code, _, err = world.run("workflows")
    assert code == 2
    assert "workflows.toml: [build] the implement slot needs a role with write = true" in err


def test_the_readme_synopsis_lists_ha_workflows() -> None:
    readme = (
        Path(__file__).resolve().parents[3] / "packages" / "headless-agents" / "README.md"
    ).read_text(encoding="utf-8")
    assert "ha workflows [--json]" in readme
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_cli.py -q -p no:cacheprovider -k "workflows"`
Expected: 5 failed — the four `ha workflows` tests exit `2` on argparse's
`invalid choice: 'workflows'`, and the README synopsis lacks `ha workflows [--json]`.

- [ ] **Step 3: Implement.**

  In `engine.py`, import the workflows module beside the others, before
  `from .workspace import prepend`:

```python
from .workflows import Workflow, WorkflowsError, load_workflows
```

  add under `ROLES_FILE_NAME: Final = "roles.toml"`:

```python
WORKFLOWS_FILE_NAME: Final = "workflows.toml"
```

  add after `declared_roles`:

```python
@dataclass(frozen=True)
class Config:
    """The operator's configuration, validated as a whole before anything runs (§3.4)."""

    roles: dict[str, Role]
    profiles: Mapping[str, object]
    workflows: dict[str, Workflow]


def load_config(environ: Mapping[str, str], home: Path) -> Config:
    """``roles.toml``, ``mcp.toml`` and ``workflows.toml``, validated; ``models.toml``
    is read per link, when a run resolves its models.

    Every file comes from the operator's configuration directory only (§3.3),
    and an invalid one refuses every run, whatever its target: validation
    happens before anything runs (§3.1, §3.2).
    """
    roles, profiles = declared_roles(environ, home)
    try:
        path = config_file(WORKFLOWS_FILE_NAME, environ, home=home)
        workflows = load_workflows(path, roles=roles)
    except (ConfigPathError, WorkflowsError) as exc:
        raise UsageError(str(exc)) from None
    return Config(roles=roles, profiles=profiles, workflows=workflows)


def describe_workflows(environ: Mapping[str, str], home: Path) -> list[dict[str, object]]:
    """``ha workflows``: every declared workflow, its shape and its slots, each slot's role
    with the providers of every link of that role (spec §3.9)."""
    config = load_config(environ, home)
    rows: list[dict[str, object]] = []
    for workflow in config.workflows.values():
        slots = [
            {
                "slot": slot,
                "role": name,
                "providers": list(resolve_role(name, config.roles).providers),
            }
            for slot, name in workflow.slot_roles()
        ]
        rows.append({"name": workflow.name, "shape": workflow.shape, "slots": slots})
    return rows
```

  and add `"Config"`, `"describe_workflows"` and `"load_config"` to `__all__`.

  In `cli.py`: the module docstring's synopsis gains the line `ha workflows [--json]` after
  `ha roles [--json]`; import `describe_workflows` from `.engine` beside `describe_roles`;
  in `_parser()`, before `runs = commands.add_parser("runs", ...)`, add:

```python
    workflows = commands.add_parser(
        "workflows", help="list the workflows declared in workflows.toml"
    )
    workflows.add_argument("--json", action="store_true", help="print the list as JSON")
```

  add before the section comment `# ── ha runs / ha clean ─…`:

```python
# ── ha workflows ────────────────────────────────────────────────────────────


def _workflows(args: argparse.Namespace, io: Io) -> int:
    rows = describe_workflows(io.environ, io.home)
    if args.json:
        io.stdout.write(json.dumps(rows, indent=2) + "\n")
        return 0
    for row in rows:
        slots = row["slots"]
        assert isinstance(slots, list)
        grouped: dict[str, list[str]] = {}
        for slot in slots:
            providers = ", ".join(slot["providers"])
            grouped.setdefault(slot["slot"], []).append(f"{slot['role']} ({providers})")
        described = "; ".join(f"{name}: {', '.join(roles)}" for name, roles in grouped.items())
        io.stdout.write(f"{row['name']:<20} {row['shape']:<9}  {described}\n")
    return 0
```

  in `main()`, after the `roles` dispatch:

```python
        if args.command == "workflows":
            return _workflows(args, io)
```

  and make the last refusal of `main()` read
  `"a command is required: run, roles, workflows, providers, runs, show or clean"`.

  In `packages/headless-agents/README.md`, the synopsis gains `ha workflows [--json]` after
  `ha roles [--json]`.

- [ ] **Step 4: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_cli.py -q -p no:cacheprovider -k "workflows"`
Expected: `5 passed`. Then the gates.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/engine.py packages/headless-agents/src/headless_agents/cli.py packages/headless-agents/README.md tests/unit/headless_agents/test_cli.py
git commit -m "feat(headless-agents): ha workflows lists each workflow, its slots and their providers"
```

### Task 3: the implement template

**Files:**
- Create: `packages/headless-agents/src/headless_agents/templates.py`
- Create: `tests/unit/headless_agents/test_templates.py`

**Interfaces:**
- Produces: `IMPLEMENT_TEMPLATE: Final[str]`; `implement_prompt(task: str) -> str` — the task,
  stripped, in the `<task>` block, then the instruction not to run git (§3.6 step 2, §3.7).

- [ ] **Step 1: Write the failing tests.** Create `tests/unit/headless_agents/test_templates.py`:

```python
"""The prompts the engine composes (spec 0.5.0 §3.7): the implement template of lot 3."""

from __future__ import annotations

from headless_agents.templates import implement_prompt

GOLDEN_IMPLEMENT = """\
Implement the task below in the repository of your workspace.

<task>
Add a --verbose flag to the CLI.
</task>

Do not run git: the engine commits your changes when you finish. A commit, a checkout, \
a reset or any other move of HEAD or of a branch fails the run.
"""


def test_the_implement_template_is_pinned() -> None:
    assert implement_prompt("Add a --verbose flag to the CLI.\n") == GOLDEN_IMPLEMENT


def test_the_task_travels_verbatim_braces_and_markup_included() -> None:
    task = "Render {name} as <b>{name}</b>.\n\nKeep {{literal}} braces."
    prompt = implement_prompt(task)
    assert f"<task>\n{task}\n</task>" in prompt


def test_the_template_tells_the_agent_the_engine_commits() -> None:
    prompt = implement_prompt("x")
    assert "Do not run git: the engine commits your changes" in prompt
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_templates.py -q -p no:cacheprovider`
Expected: a collection error — `ModuleNotFoundError: No module named 'headless_agents.templates'`.

- [ ] **Step 3: Implement.** Create `packages/headless-agents/src/headless_agents/templates.py`:

```python
"""The prompts the engine composes (spec 0.5.0 §3.7).

Written in English and versioned with the package; configuration never edits
them -- a role shapes behaviour through its instructions, which travel in the
preamble. Each template delimits what it carries in blocks. Lot 3 ships the
``implement`` template; the ``fix``, ``review`` and ``judge`` ones come with the
``review`` shape (lot 4).
"""

from __future__ import annotations

from typing import Final

IMPLEMENT_TEMPLATE: Final = """\
Implement the task below in the repository of your workspace.

<task>
{task}
</task>

Do not run git: the engine commits your changes when you finish. A commit, a checkout, \
a reset or any other move of HEAD or of a branch fails the run.
"""


def implement_prompt(task: str) -> str:
    """The implement template around ``task`` (§3.6 step 2: do not run git, the engine commits).

    The task travels verbatim: braces, markup and blank lines inside it are the
    operator's words, never template syntax.
    """
    return IMPLEMENT_TEMPLATE.format(task=task.strip())


__all__ = ["IMPLEMENT_TEMPLATE", "implement_prompt"]
```

- [ ] **Step 4: Run them again, then the gates.** Expected: `3 passed`.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/templates.py tests/unit/headless_agents/test_templates.py
git commit -m "feat(headless-agents): the implement prompt template"
```

### Task 4: a workflow target, planned

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/engine.py` (imports, `Plan`, `_models`,
  `_mcp`, `_run_dir`, `_OVERRIDE_FLAGS`, `_plan_workflow`, `plan`)
- Modify: `tests/unit/headless_agents/test_engine_plan.py`, `tests/unit/headless_agents/test_cli.py`

**Interfaces:**
- Consumes: `load_config` (Task 2), `implement_prompt` (Task 3), `Workflow` (Task 1).
- Produces: `Plan.workflow: Workflow | None = None` and `Plan.task: str | None = None` (the
  task as given; `None` means `prompt`), both defaulted so every existing `Plan(...)` stays
  valid; `plan(request)` for an `implement` workflow returns the plan of its `implement`
  role with `prompt = implement_prompt(task)`; an unknown target's refusal lists
  workflows, roles and providers.

- [ ] **Step 1: Write the failing tests.** In `tests/unit/headless_agents/test_engine_plan.py`,
  import beside the engine import:

```python
from headless_agents.registry import max_prompt_bytes
from headless_agents.templates import implement_prompt
```

  and append:

```python
def test_an_invalid_workflows_file_refuses_every_target(env: Env) -> None:
    """§3.2: validated before anything runs, whatever the target -- a provider's included."""
    (env.config / "workflows.toml").write_text(
        '[build]\nshape = "implement"\nimplement = "codex"\n'
    )
    with pytest.raises(UsageError, match=r"workflows\.toml: \[build\] the implement slot needs"):
        plan(env.request("codex", "task"))


def test_a_workflows_file_linking_outside_the_config_dir_is_a_usage_error(
    env: Env, tmp_path: Path
) -> None:
    """§3.3: a workflows.toml shipped in a repository could name a write role."""
    planted = tmp_path / "repo-workflows.toml"
    planted.write_text('[build]\nshape = "review"\nreview = "codex"\n')
    (env.config / "workflows.toml").symlink_to(planted)
    with pytest.raises(UsageError, match="outside the configuration directory"):
        plan(env.request("codex", "task"))


# ── a workflow target (lot 3) ──────────────────────────────────────────────


def _workflows(env: Env) -> None:
    env.roles(
        '[implementer]\nprovider = "codex"\nwrite = true\n\n[reviewer]\nprovider = "claude"\n'
    )
    (env.config / "workflows.toml").write_text(
        '[build]\nshape = "implement"\nimplement = "implementer"\n\n'
        '[check]\nshape = "review"\nreview = "reviewer"\n'
    )


def test_a_workflow_target_plans_its_implement_role_on_the_template(env: Env) -> None:
    _workflows(env)
    planned = plan(env.request("build", "Add a flag."))
    assert planned.workflow is not None and planned.workflow.name == "build"
    assert planned.role.name == "implementer" and planned.role.write
    assert planned.task == "Add a flag." and planned.prompt == implement_prompt("Add a flag.")


def test_a_role_target_keeps_its_task_as_its_prompt(env: Env) -> None:
    planned = plan(env.request("codex", "Explain."))
    assert planned.workflow is None and planned.task == planned.prompt == "Explain."


@pytest.mark.parametrize(
    ("overrides", "flag"),
    [
        (Overrides(model="m"), "-m"),
        (Overrides(effort="high"), "--effort"),
        (Overrides(timeout=5.0), "--timeout"),
        (Overrides(context="none"), "--context"),
        (Overrides(context_parents=True), "--context-parents"),
        (Overrides(mcp="p"), "--mcp"),
        (Overrides(write=True), "--write"),
        (Overrides(shell=True), "--shell"),
        (Overrides(base_url="http://x"), "--base-url"),
        (Overrides(key_env="K"), "--key-env"),
    ],
)
def test_a_workflow_target_refuses_every_override(
    env: Env, overrides: Overrides, flag: str
) -> None:
    """§3.3, §3.9: a workflow runs its roles as declared; none of its options is a capability."""
    _workflows(env)
    with pytest.raises(UsageError, match=f"runs its roles as declared, so {flag} is refused"):
        plan(env.request("build", "task", overrides=overrides))


def test_a_review_workflow_is_refused_until_its_shape_ships(env: Env) -> None:
    """Spec §5, lot 4: no lot exposes a review that does not enforce the vendor rule."""
    _workflows(env)
    with pytest.raises(UsageError, match=r"review shape is not available.*ha run reviewer"):
        plan(env.request("check", "task"))


def test_an_implement_workflow_needs_a_task(env: Env) -> None:
    _workflows(env)
    with pytest.raises(UsageError, match="no prompt"):
        plan(env.request("build", None, stdin_is_tty=True))


def test_the_implement_template_counts_against_the_prompt_limit(env: Env) -> None:
    """§3.4: the size checked is the prompt the provider gets -- the template around the task."""
    env.roles('[implementer]\nprovider = "opencode"\nwrite = true\n')
    (env.config / "workflows.toml").write_text(
        '[build]\nshape = "implement"\nimplement = "implementer"\n'
    )
    limit = max_prompt_bytes("opencode")
    assert limit is not None
    task = "x" * (limit - 50)
    plan(env.request("opencode", task))
    with pytest.raises(UsageError, match="opencode: the prompt with its context exceeds"):
        plan(env.request("build", task))


def test_an_unknown_target_lists_the_workflows_too(env: Env) -> None:
    _workflows(env)
    with pytest.raises(UsageError, match=r"workflows, roles and providers: .*\bbuild\b"):
        plan(env.request("nobody", "task"))
```

  In `tests/unit/headless_agents/test_cli.py`, in the `ha workflows` section of Task 2, after
  `test_workflows_on_an_invalid_file_exits_2_naming_the_problem`:

```python
def test_an_invalid_workflows_file_refuses_a_provider_run(world: _World) -> None:
    """§3.2: validation happens before anything runs, whatever the target."""
    _workflows(world, '[codex]\nshape = "review"\nreview = "claude"\n')
    code, _, err = world.run("run", "codex", "task")
    assert code == 2 and "collides with a provider" in err
    assert "codex" not in world.fakes
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_engine_plan.py tests/unit/headless_agents/test_cli.py -q -p no:cacheprovider -k "workflow or unknown_target or task_as_its_prompt"`
Expected: every new test fails — `plan()` reads no `workflows.toml` (the invalid and the
linked files are not refused, and `ha run codex` runs), `build` and `check` are
`unknown target` for `resolve_role`, the unknown-target message names no workflow, and
`Plan` has no attribute `workflow`.

- [ ] **Step 3: Implement.** In `engine.py`, import:

```python
from .registry import (
    HTTP_PROVIDER_NAMES,
    PROVIDER_NAMES,
    get_provider,
    max_prompt_bytes,
    probe,
    tool_counts,
)
```

```python
from .templates import implement_prompt
```

  replace the `Plan` dataclass with:

```python
@dataclass(frozen=True)
class Plan:
    request: Request
    role: Role
    models: Mapping[str, str]
    #: What the provider gets: the task as given, or a template around it (§3.7).
    prompt: str
    mcp: McpServer | None
    environment: dict[str, str]
    run_dir: Path | None
    state: Path
    #: The workflow a workflow target names; ``None`` for a role or a provider.
    workflow: Workflow | None = None
    #: The task as given, written to ``prompt.md`` (§3.10); ``None`` means ``prompt``.
    task: str | None = None
```

  and replace `plan()` with these helpers and the new `plan()` (they keep the role path's
  gates in their order: overrides, capabilities, `--base`, models, prompt, size, MCP
  profile, run directory):

```python
def _models(role: Role, request: Request) -> Mapping[str, str]:
    links = tuple((link.provider, link.model) for link in role.links)
    try:
        return models_for(
            links,
            default=request.overrides.model or "",
            role_model=role.model,
            environ=request.environ,
            home=request.home,
        )
    except ModelsError as exc:
        raise UsageError(str(exc)) from None


def _mcp(role: Role, request: Request) -> McpServer | None:
    if role.mcp is None:
        return None
    try:
        return mcp_server(role.mcp, environ=request.environ, home=request.home)
    except McpProfileError as exc:
        raise UsageError(str(exc)) from None


def _run_dir(request: Request) -> Path | None:
    if request.run_dir is None:
        return None
    run_dir = Path(os.path.abspath(request.cwd / request.run_dir))
    if not run_dir.name:
        raise UsageError(
            f"--run-dir {request.run_dir} has no name: a run is named by its directory"
        )
    return run_dir


#: The ``ha run`` options that override a role (§3.9), by their ``Overrides`` field.
_OVERRIDE_FLAGS: Final[Mapping[str, str]] = {
    "model": "-m",
    "effort": "--effort",
    "timeout": "--timeout",
    "context": "--context",
    "context_parents": "--context-parents",
    "mcp": "--mcp",
    "write": "--write",
    "shell": "--shell",
    "base_url": "--base-url",
    "key_env": "--key-env",
}


def _plan_workflow(request: Request, workflow: Workflow, config: Config) -> Plan:
    """A workflow target: its roles run as declared (§3.3), its prompt is a template (§3.7)."""
    given = [
        flag
        for field, flag in _OVERRIDE_FLAGS.items()
        if getattr(request.overrides, field) is not None
    ]
    if given:
        raise UsageError(
            f"workflow {workflow.name}: a workflow runs its roles as declared, so "
            f"{', '.join(given)} is refused; its options are --base, --repo, --json and "
            "--run-dir"
        )
    if workflow.implement is None:
        raise UsageError(
            f"workflow {workflow.name}: the review shape is not available in this version of ha; "
            f"run a reviewer role directly (ha run {workflow.review[0]} ...)"
        )
    role = resolve_role(workflow.implement, config.roles)
    # Validated when workflows.toml was read; the engine checks again (§3.4).
    rule = capability_rule(role, config.profiles)
    if rule is not None:
        raise UsageError(f"{workflow.name}: {rule}")
    models = _models(role, request)
    task = _prompt(request)
    prompt = implement_prompt(task)
    _check_prompt_size(role, prompt, _bundle(role, request, None))
    mcp = _mcp(role, request)
    return Plan(
        request=request,
        role=role,
        models=models,
        prompt=prompt,
        mcp=mcp,
        environment=_environment(request.environ, mcp),
        run_dir=_run_dir(request),
        state=state_dir(request.environ, home=request.home),
        workflow=workflow,
        task=task,
    )


def plan(request: Request) -> Plan:
    """Resolve ``request`` and apply every gate that needs no git; raise :class:`UsageError`."""
    config = load_config(request.environ, request.home)
    workflow = config.workflows.get(request.target)
    if workflow is not None:
        return _plan_workflow(request, workflow, config)
    declared, profiles = config.roles, config.profiles
    if request.target not in declared and request.target not in PROVIDER_NAMES:
        known = sorted({*config.workflows, *declared, *PROVIDER_NAMES})
        raise UsageError(
            f"unknown target {request.target!r}; workflows, roles and providers: {', '.join(known)}"
        )
    role = _with_overrides(resolve_role(request.target, declared), request.overrides)
    rule = capability_rule(role, profiles)
    if rule is not None:
        raise UsageError(f"{request.target}: {rule}")
    if request.base is not None and not role.write:
        raise UsageError("--base needs a write run: the role's write, or --write")

    models = _models(role, request)
    prompt = _prompt(request)
    _check_prompt_size(role, prompt, _bundle(role, request, None))
    mcp = _mcp(role, request)
    return Plan(
        request=request,
        role=role,
        models=models,
        prompt=prompt,
        mcp=mcp,
        environment=_environment(request.environ, mcp),
        run_dir=_run_dir(request),
        state=state_dir(request.environ, home=request.home),
        task=prompt,
    )
```

- [ ] **Step 4: Run them again, then the gates.** Expected: every test of Step 1 passes;
  the whole package suite stays green (`resolve_role`'s own refusal is unchanged for its
  other callers).

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/engine.py tests/unit/headless_agents/test_engine_plan.py tests/unit/headless_agents/test_cli.py
git commit -m "feat(headless-agents): plan a workflow target -- its role as declared, its task in the template"
```

### Task 5: the continuation records

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/runs.py` (`Entry`, `_MISSING`,
  `Registry.create`, `Registry._entry`)
- Modify: `packages/headless-agents/src/headless_agents/engine.py` (`_admit`, `execute`'s call to
  it, `_execute_write`'s report)
- Modify: `packages/headless-agents/src/headless_agents/show.py` (`LATER_AUTHORITIES`, `rebuild`)
- Modify: `tests/unit/headless_agents/test_runs.py`, `test_show.py`, `test_engine_execute.py`,
  `test_write_flow.py`

**Interfaces:**
- Produces: `Entry.continues: str | None` and `Entry.providers: tuple[str, ...]`;
  `Registry.create(..., continues: str | None = None, providers: Sequence[str] = ())`, both
  written into the entry; `_entry` refuses an entry without them, a `continues` that is
  not a run id, a `continues` without a lineage, and `providers` that is not a list of
  non-empty strings (`Unknown`); `engine._admit(..., providers: Sequence[str] = ())`;
  `show.LATER_AUTHORITIES == ("verdict", "vendor_check", "cleanup", "findings_from")`; a write
  run's `run.json` copies `continues` and `implement_providers` from its entry, and `lineage`
  from `entry.lineage`.

- [ ] **Step 1: Write the failing tests.** In `tests/unit/headless_agents/test_runs.py`, append:

```python
def test_create_writes_the_continuation_records(tmp_path: Path) -> None:
    """Lot 3: the registry entry is the one authority of continues and providers (§3.10)."""
    registry = Registry(tmp_path / "state", runs_root=tmp_path / "runs")
    owner, run_id = "20260926T090000-dddddddd", "20260926T100000-cccccccc"
    registry.create(
        run_id,
        run_dir=None,
        target={"kind": "workflow", "name": "build", "shape": "implement"},
        repository=None,
        lineage=owner,
        continues=owner,
        providers=("opencode", "codex"),
    )
    entry = registry.resolve(run_id)
    assert entry.continues == owner and entry.providers == ("opencode", "codex")


@pytest.mark.parametrize("key", ["continues", "providers"])
def test_an_entry_missing_a_continuation_record_is_unknown(tmp_path: Path, key: str) -> None:
    registry, run_id, path, document = _entry_document(tmp_path)
    del document[key]
    path.write_text(json.dumps(document))
    with pytest.raises(Unknown, match=key):
        registry.resolve(run_id)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("continues", "nope"),
        ("continues", 7),
        ("providers", None),
        ("providers", "codex"),
        ("providers", [""]),
        ("providers", [1]),
    ],
)
def test_a_continuation_record_ha_never_writes_is_unknown(
    tmp_path: Path, key: str, value: object
) -> None:
    registry, run_id, path, document = _entry_document(tmp_path)
    document[key] = value
    path.write_text(json.dumps(document))
    with pytest.raises(Unknown, match=key):
        registry.resolve(run_id)


def test_a_continuation_outside_any_lineage_is_unknown(tmp_path: Path) -> None:
    """A continuation joins a lineage by definition (§3.6): one without is not ha's writing."""
    registry, run_id, path, document = _entry_document(tmp_path)
    document["continues"] = "20260926T090000-dddddddd"
    path.write_text(json.dumps(document))
    with pytest.raises(Unknown, match="has no lineage"):
        registry.resolve(run_id)
```

  In `tests/unit/headless_agents/test_show.py`: import `replace` (`from dataclasses import
  replace`); in `Home.write_run`, give the entry its providers — `registry.create(RUN, ...,
  lineage=RUN, providers=("opencode", "codex"))`; replace
  `test_a_report_never_supplies_what_a_later_lots_state_record_owns` — `continues` and
  `implement_providers` leave the forged set, whose authority this lot ships — with:

```python
def test_a_report_never_supplies_what_a_later_lots_state_record_owns(home: Home) -> None:
    """Codex review of this plan (round 2): a review's verdict and vendor check live in its
    review result in the state (spec §3.8.1, §3.8.6) -- a forged run.json must not show them."""
    forged: dict[str, object] = {
        "verdict": "APPROVE",
        "vendor_check": {"commits": [], "authors": ["codex"], "reviewers": {}},
        "cleanup": {"status": "done"},
        "findings_from": OTHER,
    }
    assert set(forged) == set(show.LATER_AUTHORITIES)
    home.read_only(**forged)
    report = home.rebuild().report
    assert all(report[key] is None for key in forged)
```

  and append after it:

```python
CONTINUATION = "20260925T140000-ef56ab12"


def test_a_write_runs_providers_come_from_its_entry_not_the_report(home: Home) -> None:
    """Lot 3: implement_providers and continues copy the entry's continuation records (§3.10)."""
    home.write_run(implement_providers=["forged"], continues=OTHER)
    report = home.rebuild().report
    assert report["implement_providers"] == ["opencode", "codex"]
    assert report["continues"] is None


def test_a_continuation_is_shown_with_the_run_it_continued(home: Home) -> None:
    home.write_run()
    home.registry().create(
        CONTINUATION,
        run_dir=None,
        target={"kind": "workflow", "name": "build", "shape": "implement"},
        repository=home.root / "repo",
        lineage=RUN,
        continues=RUN,
        providers=("codex",),
    )
    current = lineages.load(home.state, RUN)
    lineages.save(
        home.state, replace(current, members={**current.members, CONTINUATION: "committed"})
    )
    report = home.rebuild(CONTINUATION).report
    assert report["continues"] == RUN and report["lineage"] == RUN
    assert report["implement_providers"] == ["codex"] and report["status"] == "committed"


def test_a_run_outside_any_lineage_has_no_continuation_records(home: Home) -> None:
    home.read_only(continues=OTHER, implement_providers=["forged"])
    report = home.rebuild().report
    assert report["continues"] is None and report["implement_providers"] is None
```

  In `tests/unit/headless_agents/test_engine_execute.py`, after
  `test_a_one_step_run_writes_its_records`:

```python
def test_a_read_only_runs_entry_records_its_providers_and_no_continuation(world: World) -> None:
    """Lot 3: every entry records its role's providers; the report's copy is a write run's."""
    outcome = world.run("codex")
    entry = world.registry().resolve(outcome.run_id)
    assert entry.providers == ("codex",) and entry.continues is None
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["implement_providers"] is None and report["continues"] is None
```

  In `tests/unit/headless_agents/test_write_flow.py`, after `test_a_committed_write`:

```python
def test_a_write_run_records_its_roles_providers(world: World) -> None:
    """Lot 3, §3.10: implement_providers copies the entry's providers, every link of the role."""
    world.agent.edit = _edit_app
    outcome = world.write()
    assert world.registry().resolve(outcome.run_id).providers == ("codex",)
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["implement_providers"] == ["codex"] and report["continues"] is None
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_runs.py tests/unit/headless_agents/test_show.py tests/unit/headless_agents/test_engine_execute.py tests/unit/headless_agents/test_write_flow.py -q -p no:cacheprovider`
Expected: failures for the missing feature only — `Registry.create()` got an unexpected
keyword argument (`continues`, `providers`: every `test_show.py` test built on
`Home.write_run` fails that way too, until Step 3), the malformed entries are not refused
(`DID NOT RAISE`), `Entry` has no attribute `providers`, and `test_a_report_never_supplies…`
fails on its set comparison against the old `LATER_AUTHORITIES`.

- [ ] **Step 3: Implement.** In `runs.py`, import `Sequence` beside `Mapping`, and add under
  `MINT_ATTEMPTS`:

```python
#: A key absent from an entry: never a value ``ha`` writes, so it reads as malformed.
_MISSING: Final = object()
```

  replace `Entry`, `Registry.create` and `Registry._entry` with:

```python
@dataclass(frozen=True)
class Entry:
    run_id: str
    run_dir: Path
    repository: Path | None
    target: Mapping[str, str]
    lineage: str | None
    status: str | None
    cleaned_at: str | None
    #: The run ``--continue`` named, for a continuation; ``None`` otherwise (§3.6, §3.10).
    continues: str | None
    #: The providers of every link of the run's role, as it ran: a write run's
    #: ``implement_providers`` in its report (§3.10).
    providers: tuple[str, ...]
```

```python
    def create(
        self,
        run_id: str,
        *,
        run_dir: Path | None,
        target: Mapping[str, str],
        repository: Path | None,
        lineage: str | None,
        continues: str | None = None,
        providers: Sequence[str] = (),
    ) -> Entry:
        """Create the entry of ``run_id`` once; ``FileExistsError`` when it is taken.

        The engine calls this while holding the id's lifecycle lock, so no
        registered run is ever seen with a free lock before it starts (§3.8.3).
        ``continues`` and ``providers`` are the continuation records of §3.10:
        their one authority is this entry, written once, never the report.
        """
        path = run_dir if run_dir is not None else self.runs_root / run_id
        document: dict[str, object] = {
            "run_id": run_id,
            "run_dir": str(path),
            "repository": str(repository) if repository is not None else None,
            "target": dict(target),
            "lineage": lineage,
            "status": "running",
            "cleaned_at": None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "continues": continues,
            "providers": list(providers),
        }
        create_once(self._path(run_id), document)
        return self._entry(document, self._path(run_id))
```

```python
    @staticmethod
    def _entry(document: Mapping[str, object], path: Path) -> Entry:
        """The entry ``document`` states; :class:`Unknown` when a key :meth:`create` writes
        is missing or holds what ``ha`` never writes there.

        ``read`` vouches for the JSON and the id only: a well-formed object without its
        ``run_dir`` escaped as ``KeyError`` and crashed ``ha runs`` and ``ha clean``
        (codex review of the lot 2 plan, round 3); a target turned into text showed a
        run of target ``None`` (codex review of lot 2 PR B, round 1); and a missing
        status let a run outside any lineage take ``running`` or ``incomplete`` from
        its lock (round 3) -- a silent authority is no status (plan P6). Unknown is
        never empty, nor invented (§3.8.1).
        """
        run_dir, target = document.get("run_dir"), document.get("target")
        if not isinstance(run_dir, str) or not run_dir:
            raise Unknown(f"{path}: run_dir is malformed")
        if not isinstance(target, dict) or not _is_target(target):
            raise Unknown(f"{path}: target is malformed")
        for key in ("repository", "lineage", "cleaned_at"):
            if key not in document or not _null_or_text(document[key]):
                raise Unknown(f"{path}: {key} is malformed")
        status = document.get("status")
        if not isinstance(status, str) or status not in STORED_STATUSES:
            raise Unknown(f"{path}: status is malformed")
        created_at = document.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise Unknown(f"{path}: created_at is malformed")
        continues = document.get("continues", _MISSING)
        if continues is not None and (
            not isinstance(continues, str) or not RUN_ID_PATTERN.fullmatch(continues)
        ):
            raise Unknown(f"{path}: continues is malformed")
        if continues is not None and document.get("lineage") is None:
            # A continuation joins a lineage by definition (§3.6).
            raise Unknown(f"{path}: continues names a run, but the entry has no lineage")
        providers = document.get("providers")
        if not isinstance(providers, list) or not all(isinstance(p, str) and p for p in providers):
            raise Unknown(f"{path}: providers is malformed")
        return Entry(
            run_id=str(document["run_id"]),
            run_dir=Path(run_dir),
            repository=_optional_path(document.get("repository")),
            target=dict(target),
            lineage=_optional_str(document.get("lineage")),
            status=status,
            cleaned_at=_optional_str(document.get("cleaned_at")),
            continues=continues,
            providers=tuple(providers),
        )
```

  In `engine.py`, import `Sequence` beside `Callable, Mapping`; replace `_admit` with:

```python
def _admit(
    registry: Registry,
    held_locks: ExitStack,
    *,
    run_dir: Path | None,
    target: Mapping[str, str],
    repository: Path,
    write: bool = False,
    providers: Sequence[str] = (),
) -> Entry:
    """Mint an id, take its lifecycle lock, then publish its entry (§3.8.3 step 1).

    A write run's entry names its lineage -- the one it starts, owned by its
    own id -- so its status lives in the lineage state only (§3.8.1).
    ``providers`` is a continuation record the report copies (§3.10).

    The lock comes first: an entry is never visible with a free lock before
    its run starts, so ``ha clean`` cannot forget a run that is being admitted
    (codex review of #207, round 2). An id whose lock another process holds,
    or whose entry exists, is taken: mint again. The lock stays on
    ``held_locks`` until the run ends.
    """
    last: LockTimeout | None = None
    for _ in range(MINT_ATTEMPTS):
        run_id = registry.mint()
        with ExitStack() as attempt:
            try:
                attempt.enter_context(
                    held(
                        registry.lifecycle_lock(run_id),
                        rank=Rank.LIFECYCLE,
                        exclusive=True,
                        wait=None,
                        what=f"the lifecycle lock of {run_id}",
                    )
                )
            except LockTimeout as exc:
                last = exc
                continue
            try:
                entry = registry.create(
                    run_id,
                    run_dir=run_dir,
                    target=target,
                    repository=repository,
                    lineage=run_id if write else None,
                    providers=providers,
                )
            except FileExistsError:
                continue
            held_locks.push(attempt.pop_all())
            return entry
    if last is not None:
        raise last
    raise RegistryError(f"could not mint a fresh run id in {MINT_ATTEMPTS} attempts")
```

  in `execute()`, pass the role's providers to it — `_admit(registry, held_locks, ...,
  write=role.write, providers=role.providers)`; and in `_execute_write`, replace the report's
  `lineage=entry.run_id,` with:

```python
        lineage=entry.lineage,
        continues=entry.continues,
        implement_providers=list(entry.providers),
```

  In `show.py`, replace `LATER_AUTHORITIES` and the comment above it with:

```python
#: Fields whose one authority is a state record a later lot introduces (lot 2 plan P6):
#: a review's result (spec §3.8.1, §3.8.6), and the findings a fix reads (§3.6). ``ha
#: show`` never takes them from ``run.json``; lot 4 fills them from the state.
LATER_AUTHORITIES: Final = ("verdict", "vendor_check", "cleanup", "findings_from")
```

  and in `rebuild`, after `document.update(dict.fromkeys(LATER_AUTHORITIES))`:

```python
    # §3.10: a write run's continuation records, copied from its registry entry.
    document.update(
        continues=entry.continues,
        implement_providers=list(entry.providers) if entry.lineage is not None else None,
    )
```

- [ ] **Step 4: Run them again, then the gates.** Expected: every test of Step 1 passes, and
  the package suite stays green: every entry lot 1 and 2 write goes through `create`.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/runs.py packages/headless-agents/src/headless_agents/engine.py packages/headless-agents/src/headless_agents/show.py tests/unit/headless_agents/test_runs.py tests/unit/headless_agents/test_show.py tests/unit/headless_agents/test_engine_execute.py tests/unit/headless_agents/test_write_flow.py
git commit -m "feat(headless-agents): the registry records a run's providers and the run it continues"
```

### Task 6: an `implement` run

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/write_flow.py` (`_publish`)
- Modify: `packages/headless-agents/src/headless_agents/engine.py` (`_execute_write`, `execute`)
- Modify: `packages/headless-agents/src/headless_agents/show.py` (`format_diffstat`, `render`)
- Modify: `packages/headless-agents/src/headless_agents/cli.py` (docstring, `_RUN_EPILOG`, the
  `target` argument, imports, `_write_header`, `_run`)
- Modify: `packages/headless-agents/README.md`
- Create: `tests/unit/headless_agents/test_implement.py`
- Modify: `tests/unit/headless_agents/test_write_flow.py`

**Interfaces:**
- Consumes: `Plan.workflow`, `Plan.task` (Task 4); `_admit(..., providers=...)` (Task 5).
- Produces: a workflow run registered and reported with
  `target = {"kind": "workflow", "name": <workflow>, "shape": "implement"}`, its step in
  `steps/01-implement-<role>` with `slot = "implement"`, `prompt.md` holding the task as
  given; `show.format_diffstat(stat: Diffstat) -> str`; `cli._write_header(outcome, branch)`.

- [ ] **Step 1: The engine commit's providers (P6) — failing test first.** In
  `tests/unit/headless_agents/test_write_flow.py`, `test_a_committed_write` expects the
  engine commit's provenance with `"providers": ["codex"]` instead of `"providers": []`.

Run: `.venv/bin/pytest tests/unit/headless_agents/test_write_flow.py -q -p no:cacheprovider -k test_a_committed_write`
Expected: FAIL — the recorded providers are `[]`.

- [ ] **Step 2: Fix it.** In `write_flow.py`, replace `_publish` with:

```python
def _publish(
    write: _Write,
    *,
    status: str,
    commits: Sequence[tuple[str, MadeBy]],
    compromised: str | None,
) -> None:
    # Every commit carries every link of the role: the engine's commit holds the
    # agent's work, and the vendor rule reads it from here (§3.8.4 step 4, §3.10).
    providers = write.plan.role.providers
    for sha, made_by in commits:
        try:
            provenance.record(
                write.state,
                sha,
                run_id=write.run_id,
                lineage=write.run_id,
                made_by=made_by,
                providers=providers,
            )
        except FileExistsError:
            pass
    current = write.current
    write.save(
        replace(
            current,
            members={**current.members, write.run_id: status},
            pending=None,
            compromised=compromised or current.compromised,
        )
    )
    _crash_after("lineage_published")
    if write.unconfined:
        (write.state / UNCONFINED_INTENT).unlink(missing_ok=True)
```

Run the same test: PASS. Then the gates, and commit:

```bash
git add packages/headless-agents/src/headless_agents/write_flow.py tests/unit/headless_agents/test_write_flow.py
git commit -m "fix(headless-agents): an engine commit is attributed to every link of its role"
```

- [ ] **Step 3: Write the failing tests of the shape.** Create
  `tests/unit/headless_agents/test_implement.py`:

```python
"""The shape ``implement`` through the engine and the CLI (spec 0.5.0 §3.6, lot 3).

The provider is a fake that edits its workspace like an agent would; the
worktree, the engine commit, the lineage state, the registry, provenance and
the report are the real code and a real git, as in test_write_flow.py.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from headless_agents import cli, engine, lineage, provenance, write_flow
from headless_agents.engine import Overrides, Request, execute, plan
from headless_agents.proofs import CLI_RAILS, record_proof
from headless_agents.registry import Probe
from headless_agents.result import RunResult
from headless_agents.run_record import record, run_id_of
from headless_agents.runs import Registry
from headless_agents.spec import RunSpec
from headless_agents.templates import implement_prompt

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
IMPLEMENT = {"kind": "workflow", "name": "build", "shape": "implement"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout


Edit = Callable[[Path], None]


@dataclass
class _Agent:
    """A provider that applies ``edit`` to its writable workspace and answers ``code``."""

    name: str
    edit: Edit | None = None
    code: int = 0
    specs: list[RunSpec] = field(default_factory=list)

    def run(self, spec: RunSpec) -> RunResult:
        self.specs.append(spec)
        workspace = spec.profile.workspace
        assert workspace is not None and workspace.write
        if self.edit is not None:
            self.edit(workspace.path)
        spec = spec.with_run_dir_defaults()
        return record(
            spec,
            RunResult(
                exit_code=self.code,
                provider=self.name,
                model=spec.model,
                model_reported="served-model",
                report_path=spec.report_log,
                events_log=spec.events_log,
                tokens=None,
                duration_seconds=0.1,
                tool_call_completed=False,
                text="I added it" if self.code == 0 else None,
                run_id=run_id_of(spec),
            ),
        )


@dataclass
class World:
    home: Path
    repo: Path
    agent: _Agent
    said: list[str] = field(default_factory=list)

    @property
    def state(self) -> Path:
        return (self.home / ".local" / "state" / "ha").resolve()

    def registry(self) -> Registry:
        return Registry(self.state, runs_root=self.home / ".cache" / "ha" / "runs")

    def request(self, task: str | None = "Add a flag.", **fields: object) -> Request:
        request = Request(
            target="build",
            prompt=task,
            stdin_is_tty=False,
            overrides=Overrides(),
            base=None,
            repo=None,
            run_dir=None,
            cwd=self.repo,
            environ={"PATH": os.environ["PATH"], "HOME": str(self.home)},
            home=self.home,
        )
        return replace(request, **fields)  # type: ignore[arg-type]

    def implement(self, task: str | None = "Add a flag.", **fields: object) -> engine.Outcome:
        return execute(plan(self.request(task, **fields)), say=self.said.append)

    def cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            list(argv),
            environ={"PATH": os.environ["PATH"], "HOME": str(self.home)},
            stdin=io.StringIO(""),
            stdout=out,
            stderr=err,
            cwd=self.repo,
            home=self.home,
        )
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    home = tmp_path / "home"
    config = home / ".config" / "ha"
    config.mkdir(parents=True)
    (config / "models.toml").write_text('codex = "codex-default"\nopencode = "oc-default"\n')
    (config / "roles.toml").write_text(
        '[implementer]\nprovider = "codex"\nwrite = true\n\n'
        '[pair]\nchain = ["opencode", "codex"]\nwrite = true\n'
    )
    (config / "workflows.toml").write_text(
        '[build]\nshape = "implement"\nimplement = "implementer"\n\n'
        '[build-pair]\nshape = "implement"\nimplement = "pair"\n'
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Op")
    _git(repo, "config", "user.email", "op@example.test")
    (repo / "app.py").write_text("print('v1')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "init")
    agent = _Agent("codex")
    monkeypatch.setattr(engine, "get_provider", lambda name: agent)
    monkeypatch.setattr(
        engine,
        "probe",
        lambda name, **_: Probe(available=True, detail="fake", version=f"{name} 1.0"),
    )
    state = (home / ".local" / "state" / "ha").resolve()
    for rail in CLI_RAILS:
        record_proof(
            state, rail, version=f"{rail} 1.0", isolation=True, confinement=True, today="2026-09-25"
        )
    return World(home=home, repo=repo, agent=agent)


def _flag(root: Path) -> None:
    (root / "app.py").write_text("print('v1')\nVERBOSE = False\n")


def _subjects(world: World, branch: str) -> list[str]:
    return _git(world.repo, "log", "--format=%s", f"main..{branch}").splitlines()


# ── a new implement run (plan Task 6) ──────────────────────────────────────


def test_an_implement_run_commits_on_a_new_lineage(world: World) -> None:
    world.agent.edit = _flag
    outcome = world.implement("Add a flag.")
    assert outcome.exit_code == 0, world.said
    run_id = outcome.run_id
    branch = f"ha/{run_id}"
    assert _subjects(world, branch) == [f"chore(ha): {run_id} implement via codex/served-model"]
    assert lineage.load(world.state, run_id).members == {run_id: "committed"}
    entry = world.registry().resolve(run_id)
    assert entry.target == IMPLEMENT and entry.lineage == run_id and entry.continues is None
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["target"] == IMPLEMENT and report["status"] == "committed"
    assert [(s["slot"], s["dir"]) for s in report["steps"]] == [
        ("implement", "steps/01-implement-implementer")
    ]
    assert report["implement_providers"] == ["codex"] and report["continues"] is None


def test_the_task_is_kept_as_given_and_the_provider_gets_the_template(world: World) -> None:
    world.agent.edit = _flag
    outcome = world.implement("Add a flag.")
    assert (outcome.run_dir / "prompt.md").read_text() == "Add a flag."
    assert world.agent.specs[0].prompt == implement_prompt("Add a flag.")


def test_the_engine_commit_is_attributed_to_every_link_of_the_role(world: World) -> None:
    """Plan P6: the vendor rule reads the implementer's providers from the engine's commit."""
    world.agent.edit = _flag
    outcome = world.implement(target="build-pair")
    tip = _git(world.repo, "rev-parse", f"ha/{outcome.run_id}").strip()
    recorded = provenance.lookup(world.state, tip)
    assert recorded is not None and recorded["made_by"] == "engine"
    assert recorded["providers"] == ["opencode", "codex"]


def test_an_implement_run_that_changes_nothing_exits_5(world: World) -> None:
    outcome = world.implement()
    assert outcome.exit_code == write_flow.NO_CHANGE_EXIT_CODE
    assert lineage.load(world.state, outcome.run_id).members == {outcome.run_id: "no_change"}


def test_a_step_code_stays_the_steps_and_the_workflow_exits_1(world: World) -> None:
    """§3.9: codes 3, 4 and 124 stay a step's -- a workflow that wrote cannot promise nothing was."""
    world.agent.edit, world.agent.code = _flag, 3
    outcome = world.implement()
    assert outcome.exit_code == 1
    report = json.loads((outcome.run_dir / "run.json").read_text())
    assert report["exit_code"] == 1 and report["steps"][0]["exit_code"] == 3
    run_id = outcome.run_id
    assert _subjects(world, f"ha/{run_id}") == [
        f"chore(ha): {run_id} residue via codex/served-model"
    ]


def test_the_cli_prints_the_run_id_branch_diffstat_and_patch(world: World) -> None:
    """§3.9: the run id first, so a session can pass it to --continue."""
    world.agent.edit = _flag
    code, out, _ = world.cli("run", "build", "Add a flag.")
    assert code == 0
    (run_id,) = world.registry().run_ids()
    run_dir = world.registry().resolve(run_id).run_dir
    assert out == (
        f"run: {run_id}\nbranch: ha/{run_id}\ndiffstat: +1 -0  1 file\n"
        f"patch: {run_dir / write_flow.PATCH_FILE}\n\nI added it\n"
    )


def test_ha_show_names_the_workflow_and_its_implement_step(world: World) -> None:
    world.agent.edit = _flag
    outcome = world.implement()
    code, out, _ = world.cli("show", outcome.run_id)
    lines = out.splitlines()
    assert code == 0 and lines[0] == f"{outcome.run_id}  build  exit 0  committed"
    assert lines[2].startswith(f"head    ha/{outcome.run_id} @ ")
    assert lines[3].startswith("  implement  implementer  codex  ")
```

- [ ] **Step 4: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_implement.py -q -p no:cacheprovider`
Expected: 4 failed, 3 passed. The failures are the missing feature: the run is registered
as the role `implementer`, its step is `01-run-implementer`, `prompt.md` holds the template,
the CLI prints `branch:` and `patch:` only, and `ha show` names the role. The three that
pass pin what the shape keeps: `test_the_engine_commit_is_attributed_to_every_link_of_the_role`
(Step 2's fix, through a workflow's chain role), and what it inherits from lot 1's write
protocol — `test_an_implement_run_that_changes_nothing_exits_5` and
`test_a_step_code_stays_the_steps_and_the_workflow_exits_1` (§3.9: "Codes `3`, `4` and `124`
stay a step's").

- [ ] **Step 5: Implement.** In `engine.py`, replace `_execute_write` and `execute` with:

```python
def _execute_write(
    plan: Plan,
    bundle: ContextBundle,
    *,
    registry: Registry,
    entry: Entry,
    identity: RepoIdentity,
    start: Path,
    report: dict[str, object],
    slot: str,
    step_name: str,
    started: float,
    unconfined: bool,
    say: Callable[[str], None],
) -> Outcome:
    """A write run -- a role's, or an ``implement`` workflow's: §3.8.3, then its report."""
    role, run_dir = plan.role, entry.run_dir
    step_dir = run_dir / "steps" / step_name

    def run_links(workspace: Workspace, directory: Path) -> RunResult:
        say(f"step 1 {slot} {role.name}: started")
        final = _run_links(
            plan, bundle, run_id=entry.run_id, step_dir=directory, workspace=workspace, say=say
        )
        say(f"step 1 {slot} {role.name}: exit {final.exit_code}")
        return final

    try:
        outcome = write_flow.run_write_step(
            plan,
            run_id=entry.run_id,
            run_dir=run_dir,
            state=plan.state,
            identity=identity,
            start=start,
            step_dir=step_dir,
            run_links=run_links,
            say=say,
            unconfined=unconfined,
        )
    except write_flow.WriteRefused as exc:
        _refused(registry, entry)
        raise UsageError(str(exc)) from None
    if outcome.final is not None:
        report = with_step(
            report,
            step_entry(
                index=1,
                slot=slot,
                role=role.name,
                step_dir=f"steps/{step_name}",
                result=outcome.final,
                tools=tool_counts(outcome.final),
            ),
        )
    report.update(
        status=outcome.status,
        exit_code=outcome.exit_code,
        text=outcome.final.text if outcome.final is not None else None,
        branch=outcome.branch,
        base=outcome.base,
        head=outcome.head,
        lineage=entry.lineage,
        continues=entry.continues,
        implement_providers=list(entry.providers),
        commits=[{"sha": sha, "made_by": made_by} for sha, made_by in outcome.commits],
        failure_reason=outcome.failure_reason,
        duration_seconds=round(time.monotonic() - started, 3),
    )
    write_report(run_dir, report)
    return Outcome(
        exit_code=outcome.exit_code,
        run_id=entry.run_id,
        run_dir=run_dir,
        report=report,
        final=outcome.final,
    )
```

```python
def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
    """Run a planned one-step run -- a role's, or an ``implement`` workflow's -- under its
    locks, and record it.

    In order: identify the repository from the filesystem (no git, plan
    decision P2); mint the run, hold its lifecycle lock, then register it; take
    the unconfined lock shared (§3.8.2); build the real context bundle and
    check the prompt size again; run the role's chain in
    ``steps/01-<slot>-<role>``; write ``run.json`` and the registry status. An
    interruption propagates with every lock released and the status left
    non-final, so the run reads ``incomplete``.
    """
    request, role = plan.request, plan.role
    start = request.repo.resolve() if request.repo is not None else request.cwd.resolve()
    try:
        identity = discover(start)
    except RepoError as exc:
        raise UsageError(str(exc)) from None
    repository = identity.work_tree if identity is not None else start
    if role.write and identity is None:
        raise UsageError(f"a write run needs a git repository; {start} is not in one")

    runs = runs_root(request.home)
    registry = Registry(plan.state, runs_root=runs)
    if plan.run_dir is not None:
        forbidden = {"the state directory": plan.state, "the runs directory": runs}
        if identity is not None:
            forbidden["the repository"] = identity.work_tree
        try:
            make_run_dir(plan.run_dir, forbidden=forbidden)
        except RegistryError as exc:
            raise UsageError(str(exc)) from None
    workflow = plan.workflow
    if workflow is not None:
        target = {"kind": "workflow", "name": workflow.name, "shape": workflow.shape}
        slot: str = workflow.shape
    else:
        target = {"kind": "provider" if role.implicit else "role", "name": role.name}
        slot = "run"

    with ExitStack() as held_locks:
        try:
            entry = _admit(
                registry,
                held_locks,
                run_dir=plan.run_dir,
                target=target,
                repository=repository,
                write=role.write,
                providers=role.providers,
            )
        except (LockTimeout, RegistryError) as exc:
            # A run that never started leaves nothing behind (codex review of #207).
            if plan.run_dir is not None:
                shutil.rmtree(plan.run_dir, ignore_errors=True)
            raise UsageError(f"{exc}: nothing ran") from None
        if plan.run_dir is None:
            entry.run_dir.mkdir(parents=True, mode=0o700)
        unconfined = role.write and write_is_unconfined(plan)
        try:
            held_locks.enter_context(
                held(
                    plan.state / "unconfined.lock",
                    rank=Rank.UNCONFINED,
                    exclusive=unconfined,
                    wait=locks.LOCK_WAIT_SECONDS,
                    what="the unconfined lock",
                )
            )
        except LockTimeout:
            _refused(registry, entry)
            if unconfined:
                raise UsageError(
                    "runs and writes still running after the bound: an unconfined write "
                    "waits for none of them; nothing ran"
                ) from None
            raise UsageError(
                "an unconfined write is running: nothing ran; retry once it has ended"
            ) from None

        try:
            _check_isolation(plan)
        except UsageError:
            _refused(registry, entry)
            raise

        bundle = _bundle(role, request, identity.work_tree if identity is not None else None)
        try:
            _check_prompt_size(role, plan.prompt, bundle)
        except UsageError:
            _refused(registry, entry)
            raise

        started = time.monotonic()
        run_dir = entry.run_dir
        # §3.10: prompt.md is the task as given; the provider gets its template around it.
        task = plan.task if plan.task is not None else plan.prompt
        (run_dir / PROMPT_FILE).write_text(task, encoding="utf-8")
        report = new_report(
            run_id=entry.run_id,
            target=target,
            repository=repository,
            pid=os.getpid(),
            started_at=_utc_now(),
        )
        write_report(run_dir, report)
        step_name = step_dir_name(1, slot, role.name)
        step_dir = run_dir / "steps" / step_name
        if role.write:
            assert identity is not None
            return _execute_write(
                plan,
                bundle,
                unconfined=unconfined,
                registry=registry,
                entry=entry,
                identity=identity,
                start=start,
                report=report,
                slot=slot,
                step_name=step_name,
                started=started,
                say=say,
            )
        say(f"step 1 run {role.name}: started")
        final = _run_links(
            plan,
            bundle,
            run_id=entry.run_id,
            step_dir=step_dir,
            workspace=Workspace(path=repository),
            say=say,
        )
        say(f"step 1 run {role.name}: exit {final.exit_code}")
        report = with_step(
            report,
            step_entry(
                index=1,
                slot="run",
                role=role.name,
                step_dir=f"steps/{step_name}",
                result=final,
                tools=tool_counts(final),
            ),
        )
        status = "answered" if final.exit_code == 0 else "failed"
        report.update(
            status=status,
            exit_code=final.exit_code,
            text=final.text,
            duration_seconds=round(time.monotonic() - started, 3),
        )
        write_report(run_dir, report)
        registry.set_status(entry.run_id, status)
        return Outcome(
            exit_code=final.exit_code,
            run_id=entry.run_id,
            run_dir=run_dir,
            report=report,
            final=final,
        )
```

  In `show.py`, add before `format_duration`:

```python
def format_diffstat(stat: Diffstat) -> str:
    """``+120 -14  5 files``: what ``ha show`` and a write's header print (§3.9, §3.10)."""
    return (
        f"+{stat.insertions} -{stat.deletions}  {stat.files} file{'' if stat.files == 1 else 's'}"
    )
```

  export it in `__all__`, and in `render`, replace the diffstat's inline f-string with it:

```python
        if shown.diffstat is not None:
            line += f"  {format_diffstat(shown.diffstat)}"
```

  In `cli.py`: the module docstring's first synopsis line ends with
  `TARGET: a workflow, a role or a provider`; import `Outcome` from `.engine`, and
  `format_diffstat` and `read_diffstat` from `.show` beside the names already imported
  from it; replace `_RUN_EPILOG` with:

```python
_RUN_EPILOG: Final = """\
exit codes of a run (a role or a provider):
  0 the answer
  1 failure
  2 invalid usage or configuration; nothing ran
  3 provider unavailable (a chain that runs out of links returns its last link's 3 or 4)
  4 timeout with no tool call started
  5 write run with no change
  124 timeout
  130 interrupted (Ctrl-C); the run reads incomplete

exit codes of a workflow (implement):
  0 committed
  5 the implementation changed nothing
  1 a step failed (its residue committed), the tripwire fired, HEAD moved or a hook
    refused; the step's own code is in run.json
  2 invalid usage or configuration; nothing ran
  130 interrupted (Ctrl-C); the run reads incomplete

examples:
  ha run codex -m gpt-6-luna "Explain what this repository does."
  ha run reviewer-codex - < task.md
  ha run build "Add a --verbose flag to the CLI."
"""
```

  make the `run` sub-parser's help `"run one task on a workflow, a role or a provider"` and
  its `target` argument:

```python
    run.add_argument(
        "target",
        help="a workflow of workflows.toml, a role of roles.toml, or a provider name",
    )
```

  add before `_run`:

```python
def _write_header(outcome: Outcome, branch: str) -> str:
    """What a write prints before its text (§3.9): the run id -- what ``--continue``
    takes -- the branch, the diffstat of ``change.patch`` and its path. No git: the
    patch the engine saved is read."""
    stat = read_diffstat(outcome.run_dir)
    return (
        f"run: {outcome.run_id}\nbranch: {branch}\n"
        f"diffstat: {format_diffstat(stat) if stat is not None else '-'}\n"
        f"patch: {outcome.run_dir / PATCH_FILE}\n\n"
    )
```

  and replace `_run` with:

```python
def _run(args: argparse.Namespace, io: Io) -> int:
    if args.provider is not None:
        raise UsageError(
            '-p was removed in 0.5.0: the provider is the TARGET (ha run codex "..."); '
            "a chain is declared in roles.toml"
        )
    if args.chain is not None:
        raise UsageError(
            "--chain was removed in 0.5.0: declare the chain in a role of roles.toml "
            '(chain = ["codex:MODEL", "claude:MODEL"]) and run the role as the TARGET'
        )
    prompt, is_tty = _prompt(args, io)
    request = Request(
        target=args.target,
        prompt=prompt,
        stdin_is_tty=is_tty,
        overrides=Overrides(
            model=args.model,
            effort=args.effort,
            timeout=args.timeout,
            context=args.context,
            context_parents=args.context_parents,
            mcp=args.mcp,
            write=args.write,
            # nosec B604: ``shell`` is a role capability override, not a subprocess argument.
            shell=args.shell,  # nosec B604
            base_url=args.base_url,
            key_env=args.key_env,
        ),
        base=args.base,
        repo=args.repo,
        run_dir=args.run_dir,
        cwd=io.cwd,
        environ=io.environ,
        home=io.home,
    )
    outcome = execute(plan(request), say=io.say)
    branch = outcome.report.get("branch")
    if args.json:
        io.stdout.write(json.dumps(outcome.report, ensure_ascii=False, indent=2) + "\n")
    elif outcome.exit_code == 0 and outcome.final is not None:
        if isinstance(branch, str):
            io.stdout.write(_write_header(outcome, branch))
        text = outcome.final.text
        if text:
            io.stdout.write(text if text.endswith("\n") else text + "\n")
    if outcome.exit_code != 0:
        provider = outcome.final.provider if outcome.final is not None else args.target
        io.say(
            f"{provider} exited {outcome.exit_code}; run {outcome.run_id}, "
            f"logs in {outcome.run_dir}"
        )
    return outcome.exit_code
```

  In `packages/headless-agents/README.md`, replace the paragraph that begins
  `` `TARGET` is a provider `` with:

```markdown
`TARGET` is a provider (`ha run codex "..."`), a role declared in
`~/.config/ha/roles.toml` -- an executor: one provider, or a `chain` of them, with optional
instructions -- or a workflow declared in `~/.config/ha/workflows.toml`. `-p` and `--chain`
were removed in 0.5.0: the provider is the target, and a chain is declared in a role. This
section is being rewritten with the 0.5.0 lots.

- **A workflow** names the roles that fill the slots of a shape coded in the package.
  `shape = "implement"` takes one role with `write = true` in its `implement` slot: `ha run
  build "task"` runs it on a new `ha/<run_id>` branch, as a write run, with the task
  wrapped in the engine's implement prompt, and prints the run id, the branch, the diffstat
  and the patch path before the agent's text. A workflow runs its roles as declared: `-m`,
  `--write` and the other role options are refused. `ha workflows` lists what
  `workflows.toml` declares; `shape = "review"` is validated there and arrives with the
  vendor rule in a later 0.5.0 lot.
```

- [ ] **Step 6: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_implement.py -q -p no:cacheprovider`
Expected: `7 passed`; the package suite stays green (`test_run_help_ends_with_the_exit_codes_and_three_examples` included).

- [ ] **Step 7: Commit, then the full suite before PR A is pushed.**

```bash
git add packages/headless-agents/src/headless_agents/engine.py packages/headless-agents/src/headless_agents/show.py packages/headless-agents/src/headless_agents/cli.py packages/headless-agents/README.md tests/unit/headless_agents/test_implement.py
git commit -m "feat(headless-agents): ha run WORKFLOW runs the shape implement on a new lineage"
```

Run the full `tests/unit/` (Global Constraints) before the push of PR A.

---

## PR B — Tasks 7–8

### Task 7: `--continue` in the engine and the write flow

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/engine.py` (`Request`, `Plan`,
  `_continued_lineage`, `_plan_workflow`, `plan`, `_admit`, `execute`, `_execute_write`)
- Modify: `packages/headless-agents/src/headless_agents/write_flow.py` (module docstring,
  `_Write`, `check_repository`, `_unfinalized`, `_check_continued`, `_admit`, `_intent`,
  `_ContinuationRefused`, `_prepare_continued`, `_withdraw`, `_prepare`, `_publish`,
  `run_write_step`)
- Modify: `tests/unit/headless_agents/test_engine_plan.py`, `tests/unit/headless_agents/test_implement.py`

**Interfaces:**
- Consumes: `Registry.resolve`, `Entry.target`, `Entry.lineage`, `Entry.continues` (Task 5);
  `lineage.load`, `lineage.save`, `lineage.lineage_lock`, `lineage.registry_lock`;
  `quarantine.check`, `quarantine.publish`.
- Produces: `Request.continue_run: str | None = None`; `Plan.continues: str | None = None`
  (the run `--continue` named) and `Plan.joins: str | None = None` (its lineage's owner);
  `engine._continued_lineage(run_id, *, state, home) -> str`;
  `engine._admit(..., joins: str | None = None, continues: str | None = None, providers=...)`;
  `write_flow.run_write_step(..., joins: str | None = None, named: str | None = None)` —
  without them a write starts a lineage of its own, as before.

- [ ] **Step 1: Write the failing tests.** In `tests/unit/headless_agents/test_engine_plan.py`,
  import `from headless_agents.runs import Registry`; give `Env.request` the keyword
  `continue_run: str | None = None` and pass `continue_run=continue_run` to `Request(...)`;
  add to `Env`:

```python
    @property
    def state(self) -> Path:
        return (self.home / ".local" / "state" / "ha").resolve()

    def register(self, run_id: str, *, target: dict[str, str], lineage: str | None) -> None:
        Registry(self.state, runs_root=self.home / ".cache" / "ha" / "runs").create(
            run_id, run_dir=None, target=target, repository=self.cwd, lineage=lineage
        )
```

  above `_workflows` (Task 4), add:

```python
RUN = "20260926T100000-aaaaaaaa"
OWNER = "20260926T090000-bbbbbbbb"
IMPLEMENT = {"kind": "workflow", "name": "build", "shape": "implement"}
```

  and append:

```python
def test_continue_needs_an_implement_workflow_target(env: Env) -> None:
    _workflows(env)
    with pytest.raises(UsageError, match="--continue needs an implement workflow"):
        plan(env.request("codex", "task", continue_run=RUN))


def test_continue_and_base_exclude_each_other(env: Env) -> None:
    _workflows(env)
    with pytest.raises(UsageError, match="--base and --continue exclude each other"):
        plan(env.request("build", "task", base="main", continue_run=RUN))


@pytest.mark.parametrize(("run_id", "rule"), [("nope", "not a run id"), (RUN, f"no run {RUN}")])
def test_continue_refuses_what_names_no_registered_run(env: Env, run_id: str, rule: str) -> None:
    _workflows(env)
    with pytest.raises(UsageError, match=rule):
        plan(env.request("build", "task", continue_run=run_id))


def test_continue_refuses_an_unreadable_entry(env: Env) -> None:
    _workflows(env)
    (env.state / "runs").mkdir(parents=True)
    (env.state / "runs" / f"{RUN}.json").write_text("{not json")
    with pytest.raises(UsageError, match="recover it by hand"):
        plan(env.request("build", "task", continue_run=RUN))


@pytest.mark.parametrize(
    ("target", "lineage"),
    [
        ({"kind": "provider", "name": "codex"}, None),
        ({"kind": "role", "name": "implementer"}, RUN),
        ({"kind": "workflow", "name": "check", "shape": "review"}, None),
    ],
    ids=["read-only-run", "role-write-run", "review-run"],
)
def test_continue_refuses_a_run_that_is_not_an_implement_run(
    env: Env, target: dict[str, str], lineage: str | None
) -> None:
    """§3.6: only a member of an implement lineage can be continued."""
    _workflows(env)
    env.register(RUN, target=target, lineage=lineage)
    with pytest.raises(UsageError, match=f"--continue {RUN}: not an implement run"):
        plan(env.request("build", "task", continue_run=RUN))


def test_continue_names_the_run_and_the_lineage_it_joins(
    env: Env, no_subprocess: list[object]
) -> None:
    """Read from the registry only: plan() starts no subprocess (§3.8.2)."""
    _workflows(env)
    env.register(RUN, target=IMPLEMENT, lineage=OWNER)
    planned = plan(env.request("build", "Fix it.", continue_run=RUN))
    assert planned.continues == RUN and planned.joins == OWNER
```

  In `tests/unit/headless_agents/test_implement.py`, extend the imports to:

```python
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from headless_agents import cli, engine, lineage, locks, provenance, quarantine, write_flow
from headless_agents.engine import Overrides, Request, UsageError, execute, plan
from headless_agents.proofs import CLI_RAILS, record_proof
from headless_agents.registry import Probe
from headless_agents.result import RunResult
from headless_agents.run_record import record, run_id_of
from headless_agents.runs import Registry
from headless_agents.spec import RunSpec
from headless_agents.templates import implement_prompt
```

  and append:

```python
# ── --continue (plan Tasks 7 and 8) ────────────────────────────────────────


def _fix(root: Path) -> None:
    (root / "app.py").write_text("print('v1')\nVERBOSE = True\n")


def _first(world: World) -> engine.Outcome:
    world.agent.edit = _flag
    outcome = world.implement("Add a flag.")
    assert outcome.exit_code == 0, world.said
    return outcome


def _continue(
    world: World, run_id: str, task: str = "Turn it on.", **fields: object
) -> engine.Outcome:
    world.agent.edit = _fix
    return world.implement(task, continue_run=run_id, **fields)


def _run_dirs(world: World) -> list[str]:
    return sorted(path.name for path in (world.home / ".cache" / "ha" / "runs").iterdir())


def test_a_continuation_commits_on_the_lineages_branch(world: World) -> None:
    first = _first(world)
    second = _continue(world, first.run_id)
    assert second.exit_code == 0, world.said
    owner, run_id = first.run_id, second.run_id
    branch = f"ha/{owner}"
    assert _subjects(world, branch) == [
        f"chore(ha): {run_id} implement via codex/served-model",
        f"chore(ha): {owner} implement via codex/served-model",
    ]
    assert lineage.load(world.state, owner).members == {owner: "committed", run_id: "committed"}
    workspace = world.agent.specs[-1].profile.workspace
    assert workspace is not None and workspace.path == first.run_dir / "wt"
    entry = world.registry().resolve(run_id)
    assert entry.lineage == owner and entry.continues == owner
    report = json.loads((second.run_dir / "run.json").read_text())
    assert report["lineage"] == owner and report["continues"] == owner
    assert report["branch"] == branch
    tip = _git(world.repo, "rev-parse", branch).strip()
    assert provenance.lookup(world.state, tip) == {
        "sha": tip,
        "run_id": run_id,
        "lineage": owner,
        "made_by": "engine",
        "providers": ["codex"],
    }
    patch = (second.run_dir / write_flow.PATCH_FILE).read_text()
    assert "+VERBOSE = True" in patch and "VERBOSE = False" not in patch


def test_a_continuation_may_name_any_member_of_the_lineage(world: World) -> None:
    first = _first(world)
    second = _continue(world, first.run_id)
    world.agent.edit = lambda root: (root / "extra.py").write_text("x = 1\n")
    third = world.implement("And more.", continue_run=second.run_id)
    assert third.exit_code == 0, world.said
    assert world.registry().resolve(third.run_id).lineage == first.run_id
    members = lineage.load(world.state, first.run_id).members
    assert set(members) == {first.run_id, second.run_id, third.run_id}


def test_a_commit_made_by_hand_between_runs_is_kept(world: World) -> None:
    """Review Focus 1, §3.6: kept, unattributed, and in the cumulative patch."""
    first = _first(world)
    worktree = first.run_dir / "wt"
    (worktree / "notes.md").write_text("by hand\n")
    _git(worktree, "add", "notes.md")
    _git(worktree, "commit", "-q", "-m", "docs: notes by hand")
    hand = _git(worktree, "rev-parse", "HEAD").strip()
    second = _continue(world, first.run_id)
    assert second.exit_code == 0, world.said
    assert _subjects(world, f"ha/{first.run_id}")[1] == "docs: notes by hand"
    assert provenance.lookup(world.state, hand) is None
    assert "notes.md" in (second.run_dir / write_flow.PATCH_FILE).read_text()


def test_a_dirty_worktree_refuses_the_continuation_and_leaves_no_trace(world: World) -> None:
    first = _first(world)
    (first.run_dir / "wt" / "app.py").write_text("edited by hand, not committed\n")
    before = lineage.load(world.state, first.run_id)
    with pytest.raises(UsageError, match="has uncommitted changes"):
        _continue(world, first.run_id)
    assert lineage.load(world.state, first.run_id) == before
    assert world.registry().run_ids() == [first.run_id]
    assert _run_dirs(world) == [first.run_id]
    assert len(world.agent.specs) == 1


def test_a_worktree_off_its_branch_refuses_the_continuation(world: World) -> None:
    """Review Focus 2: a worktree detached or switched by hand would commit elsewhere."""
    first = _first(world)
    _git(first.run_dir / "wt", "checkout", "-q", "--detach")
    with pytest.raises(UsageError, match="is not on its branch"):
        _continue(world, first.run_id)
    assert lineage.load(world.state, first.run_id).pending is None


def test_a_continuation_from_inside_its_worktree_is_admitted(world: World) -> None:
    """Review Focus 3: its lineage is both the one it joins and the source of --repo."""
    first = _first(world)
    second = _continue(world, first.run_id, cwd=first.run_dir / "wt")
    assert second.exit_code == 0, world.said


def test_a_continuation_after_ha_clean_is_refused(world: World) -> None:
    first = _first(world)
    code, _, err = world.cli("clean", first.run_id)
    assert code == 0, err
    with pytest.raises(UsageError, match="it was cleaned"):
        _continue(world, first.run_id)


def test_a_continuation_from_another_repository_is_refused(world: World, tmp_path: Path) -> None:
    first = _first(world)
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    with pytest.raises(UsageError, match="belongs to"):
        _continue(world, first.run_id, cwd=other)


def test_a_continuation_refused_before_admission_creates_no_run_dir(
    world: World, tmp_path: Path
) -> None:
    """§3.8.2: what plan() read from the registry is read again before anything is created."""
    first = _first(world)
    custom = tmp_path / "custom"
    planned = plan(world.request("Turn it on.", continue_run=first.run_id, run_dir=custom))
    (world.state / "runs" / f"{first.run_id}.json").write_text("{not json")
    with pytest.raises(UsageError, match="recover it by hand"):
        execute(planned, say=world.said.append)
    assert not custom.exists()


def test_a_compromised_lineage_refuses_the_continuation(world: World) -> None:
    first = _first(world)
    current = lineage.load(world.state, first.run_id)
    lineage.save(world.state, replace(current, compromised="agent_moved_head"))
    with pytest.raises(UsageError, match=r"compromised \(agent_moved_head\)"):
        _continue(world, first.run_id)


def test_an_unreadable_lineage_refuses_the_continuation(world: World) -> None:
    first = _first(world)
    lineage.lineage_path(world.state, first.run_id).write_text("{not json")
    with pytest.raises(UsageError, match=f"lineage {first.run_id} is unknown"):
        _continue(world, first.run_id)


def test_the_named_run_must_be_a_member_of_its_lineage(world: World) -> None:
    first = _first(world)
    stranger = "20260926T120000-cccccccc"
    world.registry().create(
        stranger,
        run_dir=None,
        target=IMPLEMENT,
        repository=world.repo,
        lineage=first.run_id,
        providers=("codex",),
    )
    with pytest.raises(UsageError, match=f"{stranger} is not a member of lineage {first.run_id}"):
        _continue(world, stranger)


def test_a_tripwire_in_a_continuation_compromises_the_whole_lineage(world: World) -> None:
    """§4: a tripwire fired in B, then --continue A refused and ha clean A running no git."""
    first = _first(world)
    world.agent.edit = lambda root: (root / ".git").write_text("gitdir: /somewhere/else\n")
    second = world.implement("Tamper.", continue_run=first.run_id)
    assert second.exit_code == 1
    assert lineage.load(world.state, first.run_id).compromised == "tripwire"
    with pytest.raises(UsageError, match="unfinished write"):
        _continue(world, first.run_id)
    code, _, err = world.cli("clean", first.run_id)
    assert code == 1 and "no git command" in err


def test_a_crash_after_a_continuations_intent_is_found_by_the_next(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4: the next --continue marks the lineage unfinalized_write and is refused."""
    first = _first(world)

    def crash(step: str) -> None:
        if step == "intent":
            raise SystemExit("crashed")

    monkeypatch.setattr(write_flow, "_crash_after", crash)
    with pytest.raises(SystemExit):
        _continue(world, first.run_id)
    assert lineage.load(world.state, first.run_id).pending is not None
    monkeypatch.setattr(write_flow, "_crash_after", lambda step: None)
    with pytest.raises(UsageError, match="unfinished write"):
        _continue(world, first.run_id)
    assert lineage.load(world.state, first.run_id).compromised == "unfinalized_write"
    assert quarantine.check(world.state, (world.repo / ".git").resolve()) is not None


_HOLD = """
import fcntl, os, pathlib, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX if sys.argv[2] == "ex" else fcntl.LOCK_SH)
pathlib.Path(sys.argv[3]).write_text("ok")
time.sleep(30)
"""


@contextmanager
def _holding(world: World, lock: Path, mode: str) -> Iterator[None]:
    """``lock`` held by another process, as a running continuation holds its lineage's."""
    ready = world.home / f"ready-{lock.name}-{mode}"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock), mode, str(ready)])
    try:
        while not ready.exists():
            time.sleep(0.02)
        yield
    finally:
        holder.kill()
        holder.wait()


def test_two_continuations_of_one_lineage_never_run_at_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.6: the second waits for the lineage lock at most the bound, then is refused."""
    first = _first(world)
    second = _continue(world, first.run_id)
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.3)
    lock = lineage.lineage_lock(world.state, first.run_id)
    with (
        _holding(world, lock, "ex"),
        pytest.raises(UsageError, match=f"the lineage lock of {first.run_id}: not obtained"),
    ):
        world.implement("Meanwhile.", continue_run=second.run_id)
    assert sorted(world.registry().run_ids()) == sorted([first.run_id, second.run_id])


def test_ha_clean_waits_for_a_running_continuation_then_refuses(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4: ha clean A while B runs -- it waits for the lineage lock, then is refused."""
    first = _first(world)
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.3)
    with _holding(world, lineage.lineage_lock(world.state, first.run_id), "ex"):
        code, _, err = world.cli("clean", first.run_id)
    assert code == 2 and "the lineage is in use" in err
    assert (first.run_dir / "wt").is_dir()


def test_a_refused_unconfined_continuation_withdraws_its_intent(world: World) -> None:
    first = _first(world)
    record_proof(
        world.state,
        "codex",
        version="codex 1.0",
        isolation=True,
        confinement=False,
        today="2026-09-26",
    )
    (first.run_dir / "wt" / "app.py").write_text("dirty\n")
    with pytest.raises(UsageError, match="uncommitted changes"):
        _continue(world, first.run_id)
    assert not (world.state / write_flow.UNCONFINED_INTENT).exists()
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_engine_plan.py tests/unit/headless_agents/test_implement.py -q -p no:cacheprovider`
Expected: every new test fails on the missing feature — `Request.__init__()` (and
`dataclasses.replace`) got an unexpected keyword argument `continue_run`.

- [ ] **Step 3: Implement the engine side.** In `engine.py`, replace `Request` and `Plan`
  with:

```python
@dataclass(frozen=True)
class Request:
    target: str
    #: The task; ``None`` when absent. The CLI resolves ``-`` and a piped
    #: stdin into text; ``stdin_is_tty`` says why it may be absent.
    prompt: str | None
    stdin_is_tty: bool
    overrides: Overrides
    base: str | None
    #: Unresolved: the repository is identified in ``execute`` (plan decision P2).
    repo: Path | None
    run_dir: Path | None
    cwd: Path
    environ: Mapping[str, str]
    home: Path
    #: ``--continue RUN_ID``: an implement run whose lineage this run joins (§3.6).
    continue_run: str | None = None


@dataclass(frozen=True)
class Plan:
    request: Request
    role: Role
    models: Mapping[str, str]
    #: What the provider gets: the task as given, or a template around it (§3.7).
    prompt: str
    mcp: McpServer | None
    environment: dict[str, str]
    run_dir: Path | None
    state: Path
    #: The workflow a workflow target names; ``None`` for a role or a provider.
    workflow: Workflow | None = None
    #: The task as given, written to ``prompt.md`` (§3.10); ``None`` means ``prompt``.
    task: str | None = None
    #: The run ``--continue`` names, and the owner of the lineage it joins (§3.6),
    #: both read from the registry; ``execute`` checks them again under the lock.
    continues: str | None = None
    joins: str | None = None
```

  add before `_plan_workflow`:

```python
def _continued_lineage(run_id: str, *, state: Path, home: Path) -> str:
    """The owner of the lineage ``--continue RUN_ID`` joins, read from the registry.

    No git and no lineage state here (§3.8.2): only the run's registry entry,
    which must name an ``implement`` run -- a member of an ``implement``
    lineage (§3.6). ``execute`` checks it again, then the lineage itself,
    under the lineage lock.
    """
    registry = Registry(state, runs_root=runs_root(home))
    try:
        entry = registry.resolve(run_id)
    except RegistryError as exc:
        raise UsageError(f"--continue: {exc}") from None
    except Unknown as exc:
        raise UsageError(f"--continue {run_id}: {exc}; recover it by hand") from None
    target = entry.target
    if (
        entry.lineage is None
        or target.get("kind") != "workflow"
        or target.get("shape") != "implement"
    ):
        raise UsageError(
            f"--continue {run_id}: not an implement run; only an implement run's lineage "
            "can be continued"
        )
    return entry.lineage
```

  replace `_plan_workflow`, `plan` and `_admit` with:

```python
def _plan_workflow(request: Request, workflow: Workflow, config: Config) -> Plan:
    """A workflow target: its roles run as declared (§3.3), its prompt is a template (§3.7)."""
    given = [
        flag
        for field, flag in _OVERRIDE_FLAGS.items()
        if getattr(request.overrides, field) is not None
    ]
    if given:
        raise UsageError(
            f"workflow {workflow.name}: a workflow runs its roles as declared, so "
            f"{', '.join(given)} is refused; its options are --base, --repo, --json, --run-dir "
            "and --continue"
        )
    if workflow.implement is None:
        raise UsageError(
            f"workflow {workflow.name}: the review shape is not available in this version of ha; "
            f"run a reviewer role directly (ha run {workflow.review[0]} ...)"
        )
    if request.continue_run is not None and request.base is not None:
        raise UsageError(
            "--base and --continue exclude each other: a continuation works from its lineage's base"
        )
    role = resolve_role(workflow.implement, config.roles)
    # Validated when workflows.toml was read; the engine checks again (§3.4).
    rule = capability_rule(role, config.profiles)
    if rule is not None:
        raise UsageError(f"{workflow.name}: {rule}")
    models = _models(role, request)
    task = _prompt(request)
    prompt = implement_prompt(task)
    _check_prompt_size(role, prompt, _bundle(role, request, None))
    mcp = _mcp(role, request)
    run_dir = _run_dir(request)
    state = state_dir(request.environ, home=request.home)
    joins = (
        _continued_lineage(request.continue_run, state=state, home=request.home)
        if request.continue_run is not None
        else None
    )
    return Plan(
        request=request,
        role=role,
        models=models,
        prompt=prompt,
        mcp=mcp,
        environment=_environment(request.environ, mcp),
        run_dir=run_dir,
        state=state,
        workflow=workflow,
        task=task,
        continues=request.continue_run,
        joins=joins,
    )


def plan(request: Request) -> Plan:
    """Resolve ``request`` and apply every gate that needs no git; raise :class:`UsageError`."""
    config = load_config(request.environ, request.home)
    workflow = config.workflows.get(request.target)
    if workflow is not None:
        return _plan_workflow(request, workflow, config)
    if request.continue_run is not None:
        raise UsageError("--continue needs an implement workflow as the target")
    declared, profiles = config.roles, config.profiles
    if request.target not in declared and request.target not in PROVIDER_NAMES:
        known = sorted({*config.workflows, *declared, *PROVIDER_NAMES})
        raise UsageError(
            f"unknown target {request.target!r}; workflows, roles and providers: {', '.join(known)}"
        )
    role = _with_overrides(resolve_role(request.target, declared), request.overrides)
    rule = capability_rule(role, profiles)
    if rule is not None:
        raise UsageError(f"{request.target}: {rule}")
    if request.base is not None and not role.write:
        raise UsageError("--base needs a write run: the role's write, or --write")

    models = _models(role, request)
    prompt = _prompt(request)
    _check_prompt_size(role, prompt, _bundle(role, request, None))
    mcp = _mcp(role, request)
    return Plan(
        request=request,
        role=role,
        models=models,
        prompt=prompt,
        mcp=mcp,
        environment=_environment(request.environ, mcp),
        run_dir=_run_dir(request),
        state=state_dir(request.environ, home=request.home),
        task=prompt,
    )
```

```python
def _admit(
    registry: Registry,
    held_locks: ExitStack,
    *,
    run_dir: Path | None,
    target: Mapping[str, str],
    repository: Path,
    write: bool = False,
    joins: str | None = None,
    continues: str | None = None,
    providers: Sequence[str] = (),
) -> Entry:
    """Mint an id, take its lifecycle lock, then publish its entry (§3.8.3 step 1).

    A write run's entry names its lineage -- the one it starts, owned by its
    own id, or the one it ``joins`` as a continuation -- so its status lives in
    the lineage state only (§3.8.1). ``continues`` and ``providers`` are the
    continuation records the report copies (§3.10).

    The lock comes first: an entry is never visible with a free lock before
    its run starts, so ``ha clean`` cannot forget a run that is being admitted
    (codex review of #207, round 2). An id whose lock another process holds,
    or whose entry exists, is taken: mint again. The lock stays on
    ``held_locks`` until the run ends.
    """
    last: LockTimeout | None = None
    for _ in range(MINT_ATTEMPTS):
        run_id = registry.mint()
        with ExitStack() as attempt:
            try:
                attempt.enter_context(
                    held(
                        registry.lifecycle_lock(run_id),
                        rank=Rank.LIFECYCLE,
                        exclusive=True,
                        wait=None,
                        what=f"the lifecycle lock of {run_id}",
                    )
                )
            except LockTimeout as exc:
                last = exc
                continue
            try:
                entry = registry.create(
                    run_id,
                    run_dir=run_dir,
                    target=target,
                    repository=repository,
                    lineage=joins if joins is not None else (run_id if write else None),
                    continues=continues,
                    providers=providers,
                )
            except FileExistsError:
                continue
            held_locks.push(attempt.pop_all())
            return entry
    if last is not None:
        raise last
    raise RegistryError(f"could not mint a fresh run id in {MINT_ATTEMPTS} attempts")
```

  in `execute()`, right after the refusal `a write run needs a git repository`, before the
  run directory is made:

```python
    if plan.continues is not None:
        # §3.8.2: what plan() read from the registry is read again, before anything
        # is created; the lineage itself is checked under its lock (write_flow).
        if _continued_lineage(plan.continues, state=plan.state, home=request.home) != plan.joins:
            raise UsageError(f"--continue {plan.continues}: its lineage changed since the plan")
```

  and pass `joins=plan.joins, continues=plan.continues,` to its `_admit(...)` call, before
  `providers=role.providers`; in `_execute_write`, pass `joins=plan.joins,
  named=plan.continues,` to `write_flow.run_write_step(...)`, after `unconfined=unconfined`.

- [ ] **Step 4: Implement the write flow.** In `write_flow.py`, replace the module
  docstring with:

```python
"""A write, from admission to publication (spec 0.5.0 §3.8.3).

:func:`run_write_step` runs the nine steps of §3.8.3 for a write run -- a
role's, or an ``implement`` workflow's, new or continuing a lineage (§3.6) --
each a function of its own, in this order:

1. admission, without git -- the lineage registry lock, then the lineage
   locks in ascending owner order, then every state check under them;
2. intent -- the pending write, before any mutation: in a new lineage's state,
   created here, or in the continued lineage's; the registry lock released;
3. preparation -- the only git before the provider, hooks off: a new worktree,
   or a continuation's checked clean and on its branch;
4. start point -- the branch tip, recorded after preparation;
5. the step -- the role's chain, on a writable worktree, the tripwire armed;
6. tripwire -- fired: quarantine, lineage compromised, no git at all;
7. agent commits -- a moved ``HEAD`` or branch tip: every new commit
   recorded ``made_by: agent``, lineage compromised;
8. engine commit -- the repository's hooks run for it only; every commit
   that appeared is attributed whatever ``git commit`` returned;
9. publication -- provenance, then the lineage state in one rename (the
   single point where the write becomes final).

The caller (:func:`headless_agents.engine.execute`) holds the lifecycle lock
and the unconfined lock (§3.8.2) around the whole call and writes the report.
:func:`_crash_after` is called between steps: tests inject a crash there.
"""
```

  replace `_Write`, `check_repository`, `_admit`, `_intent`, `_prepare`, `_publish` and
  `run_write_step` with the versions below, and add `_unfinalized` (after
  `check_repository`), `_check_continued` (before `_admit`), and `_ContinuationRefused`,
  `_prepare_continued` and `_withdraw` (after `_PreparationFailed`):

```python
@dataclass
class _Write:
    """What one write knows, step after step."""

    plan: Plan
    run_id: str
    run_dir: Path
    state: Path
    identity: RepoIdentity
    start: Path
    unconfined: bool
    say: Callable[[str], None]
    locks: ExitStack
    #: The lineage's owner: this run's own id for a new lineage, or the owner of the
    #: lineage a continuation joins (§3.6); ``""`` means this run's id.
    owner: str = ""
    #: The run ``--continue`` named, a member of the joined lineage.
    named: str | None = None
    worktree: Path = field(init=False)
    branch: str = field(init=False)
    git_dir: Path | None = None
    reflog_start: tuple[bytes | None, ...] = ()
    lineage: LineageState | None = None
    #: A continuation's lineage as admission read it: restored if preparation refuses.
    before: LineageState | None = None

    def __post_init__(self) -> None:
        self.owner = self.owner or self.run_id
        # A continuation takes both from its lineage state at admission.
        self.worktree = self.run_dir / "wt"
        self.branch = f"ha/{self.owner}"

    @property
    def continuing(self) -> bool:
        return self.owner != self.run_id

    @property
    def environ(self) -> Mapping[str, str]:
        return self.plan.environment

    def git(self, root: Path, args: Sequence[str], *, hooks: bool = False) -> tuple[int, str, str]:
        result = git(root, args, self.environ, state=self.state, hooks=hooks)
        return result.returncode, result.stdout, result.stderr

    def save(self, lineage: LineageState) -> None:
        self.lineage = lineage
        lineages.save(self.state, lineage)

    @property
    def current(self) -> LineageState:
        assert self.lineage is not None, "the intent creates the lineage"
        return self.lineage
```

```python
def check_repository(state: Path, common: Path, *, own: str) -> None:
    """No lineage of the repository unknown, and no stale pending write in any (§3.8.3 step 1).

    A stale pending write compromises its lineage (``unfinalized_write``) and
    publishes the repository quarantine -- the operator's too when that write
    was unconfined. ``own`` is skipped: the caller holds its lock.
    """
    for owner in lineages.of_repository(state, common):
        if owner == own:
            continue
        try:
            other = lineages.load(state, owner)
        except Unknown as exc:
            raise WriteRefused(f"lineage {owner} is unknown ({exc}); nothing ran") from None
        if other.pending is not None and is_free(lineages.lineage_lock(state, owner)):
            raise _unfinalized(state, other, common)


def _unfinalized(state: Path, lineage: LineageState, common: Path) -> WriteRefused:
    """A stale pending write: its lineage compromised (``unfinalized_write``), the
    repository quarantined -- the operator too when that write was unconfined."""
    pending = lineage.pending
    assert pending is not None, "only a pending write can be stale"
    lineages.save(state, replace(lineage, compromised="unfinalized_write"))
    quarantine.publish(
        state,
        "repository",
        reason="unfinalized_write",
        run_id=pending.run_id,
        paths=[],
        common_dir=common,
    )
    if pending.unconfined:
        quarantine.publish(
            state,
            "operator",
            reason="unfinalized_write",
            run_id=pending.run_id,
            paths=[],
            common_dir=None,
        )
    return WriteRefused(
        f"lineage {lineage.owner} holds the unfinished write of run {pending.run_id}: "
        "it is compromised (unfinalized_write) and the repository quarantined; "
        "nothing ran"
    )
```

```python
def _check_continued(write: _Write) -> None:
    """A continuation's own lineage, from the state only, under its lock (§3.6).

    Known; its pending write, if any, stale -- this process holds the lineage
    lock exclusively, so no writer of it is alive (§3.8.2); not compromised;
    listing the named run; in the repository of this run; its worktree not
    removed by ``ha clean``. Whether that worktree is clean is a git
    question, answered in preparation.
    """
    state, owner = write.state, write.owner
    try:
        current = lineages.load(state, owner)
    except Unknown as exc:
        raise WriteRefused(f"lineage {owner} is unknown ({exc}); nothing ran") from None
    if current.pending is not None:
        raise _unfinalized(state, current, write.identity.common_dir)
    if current.compromised is not None:
        raise WriteRefused(f"lineage {owner} is compromised ({current.compromised}); nothing ran")
    if write.named not in current.members:
        raise WriteRefused(f"{write.named} is not a member of lineage {owner}; nothing ran")
    if current.common_dir.resolve() != write.identity.common_dir.resolve():
        raise WriteRefused(
            f"lineage {owner} belongs to {current.repository}, not to "
            f"{write.identity.work_tree}; nothing ran"
        )
    if not current.worktree.is_dir():
        raise WriteRefused(
            f"the worktree of lineage {owner} is gone ({current.worktree}): it was cleaned; "
            "nothing ran"
        )
    write.lineage = write.before = current
    write.worktree, write.branch = current.worktree, current.branch


def _admit(write: _Write, registry: ExitStack) -> None:
    """Step 1: every lock first, in the §3.8.2 order, then every state check under them.

    A new lineage holds the lineage registry lock exclusively: it creates its
    state under it. A continuation creates none, and holds it shared while it
    enumerates the repository's lineages (§3.8.2); its own lineage lock is
    exclusive, waited for at most :data:`locks.LOCK_WAIT_SECONDS`.
    """
    state = write.state
    registry.enter_context(
        held(
            lineages.registry_lock(state),
            rank=Rank.LINEAGE_REGISTRY,
            exclusive=not write.continuing,
            wait=locks.LOCK_WAIT_SECONDS,
            what="the lineage registry lock",
        )
    )
    sources = _sources(state, write.start)
    for owner in sorted({write.owner, *sources}):
        own = owner == write.owner
        write.locks.enter_context(
            held(
                lineages.lineage_lock(state, owner),
                rank=Rank.LINEAGE,
                exclusive=own,
                wait=locks.LOCK_WAIT_SECONDS,
                what=f"the lineage lock of {owner}",
                key=owner,
            )
        )
    refusal = quarantine.check(state, write.identity.common_dir)
    if refusal is not None:
        raise WriteRefused(f"{refusal}; nothing ran")
    check_unconfined_intent(state, write.run_id)
    check_repository(state, write.identity.common_dir, own=write.owner)
    # A continuation run from inside its own worktree finds its lineage as a source:
    # its own checks, below, are the stricter ones.
    _check_sources(state, [owner for owner in sources if owner != write.owner])
    if write.continuing:
        _check_continued(write)
```

```python
def _intent(write: _Write) -> None:
    """Step 2: the pending write, before any mutation -- in a new lineage's state, created
    here, or added to the joined lineage's with the continuation as a member."""
    pending = PendingWrite(
        run_id=write.run_id,
        providers=tuple(write.plan.role.providers),
        unconfined=write.unconfined,
        start_tip=None,
        start_reflog=None,
    )
    if write.continuing:
        current = write.current
        write.save(
            replace(current, members={**current.members, write.run_id: "running"}, pending=pending)
        )
    else:
        write.lineage = LineageState(
            owner=write.run_id,
            repository=write.identity.work_tree,
            common_dir=write.identity.common_dir,
            worktree=write.worktree,
            branch=write.branch,
            base=None,
            members={write.run_id: "running"},
            pending=pending,
            compromised=None,
        )
        lineages.create(write.state, write.lineage)
    if write.unconfined:
        _publish_unconfined_intent(write)
```

```python
class _ContinuationRefused(Exception):  # noqa: N818 - a refusal, not a crash
    """A continuation's worktree cannot be continued: nothing ran (§3.8.3 step 3)."""


def _prepare_continued(write: _Write) -> str:
    """A continuation's worktree: clean, on its lineage's branch, which exists; its base.

    A worktree switched to another branch or detached by hand would put the
    continuation's commit elsewhere than on the branch a review reads.
    """
    worktree, branch = write.worktree, write.branch
    code, out, err = write.git(worktree, ["status", "--porcelain"])
    if code != 0:
        raise _ContinuationRefused(f"git status failed in {worktree}: {err.strip()}")
    if out.strip():
        raise _ContinuationRefused(
            f"the worktree {worktree} has uncommitted changes: commit or discard them first"
        )
    code, out, _ = write.git(worktree, ["symbolic-ref", "-q", "HEAD"])
    if code != 0 or out.strip() != f"refs/heads/{branch}":
        raise _ContinuationRefused(
            f"the worktree {worktree} is not on its branch {branch}: check it out again first"
        )
    base = write.current.base
    if _tip(write) is None or base is None:
        raise _ContinuationRefused(f"the branch {branch} or the lineage's base is missing")
    write.git_dir = resolve_git_dir(worktree)
    return base


def _withdraw(write: _Write) -> None:
    """A refused continuation leaves its lineage as admission found it: nothing ran."""
    assert write.before is not None, "admission read the lineage"
    write.save(write.before)
    if write.unconfined:
        (write.state / UNCONFINED_INTENT).unlink(missing_ok=True)


def _prepare(write: _Write) -> str:
    """Resolve the base and add the worktree -- or check a continuation's; the base commit."""
    if write.continuing:
        return _prepare_continued(write)
    base_ref = write.plan.request.base or "HEAD"
    repository = write.identity.work_tree
    code, out, err = write.git(repository, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"])
    if code != 0:
        raise _PreparationFailed(f"cannot resolve --base {base_ref!r}: {err.strip()}")
    base = out.strip()
    code, _, err = write.git(
        repository, ["worktree", "add", "-q", "-b", write.branch, str(write.worktree), base]
    )
    if code != 0:
        raise _PreparationFailed(f"git worktree add failed: {err.strip()}")
    write.git_dir = resolve_git_dir(write.worktree)
    write.save(replace(write.current, base=base))
    return base
```

```python
def _publish(
    write: _Write,
    *,
    status: str,
    commits: Sequence[tuple[str, MadeBy]],
    compromised: str | None,
) -> None:
    # Every commit carries every link of the role: the engine's commit holds the
    # agent's work, and the vendor rule reads it from here (§3.8.4 step 4, §3.10).
    providers = write.plan.role.providers
    for sha, made_by in commits:
        try:
            provenance.record(
                write.state,
                sha,
                run_id=write.run_id,
                lineage=write.owner,
                made_by=made_by,
                providers=providers,
            )
        except FileExistsError:
            pass
    current = write.current
    write.save(
        replace(
            current,
            members={**current.members, write.run_id: status},
            pending=None,
            compromised=compromised or current.compromised,
        )
    )
    _crash_after("lineage_published")
    if write.unconfined:
        (write.state / UNCONFINED_INTENT).unlink(missing_ok=True)
```

```python
def run_write_step(
    plan: Plan,
    *,
    run_id: str,
    run_dir: Path,
    state: Path,
    identity: RepoIdentity,
    start: Path,
    step_dir: Path,
    run_links: RunLinks,
    say: Callable[[str], None],
    unconfined: bool = False,
    joins: str | None = None,
    named: str | None = None,
) -> WriteOutcome:
    """§3.8.3 for one write run; :class:`WriteRefused` when admission refuses.

    ``joins`` names the lineage a continuation joins (its owner) and ``named``
    the member ``--continue`` named (§3.6); without them the write starts a
    lineage of its own.
    """
    with ExitStack() as locks:
        write = _Write(
            plan=plan,
            run_id=run_id,
            run_dir=run_dir,
            state=state,
            identity=identity,
            start=start,
            unconfined=unconfined,
            say=say,
            locks=locks,
            owner=joins or run_id,
            named=named,
        )
        with ExitStack() as registry:
            try:
                _admit(write, registry)
            except LockTimeout as exc:
                raise WriteRefused(f"{exc}; nothing ran") from None
            _intent(write)
            _crash_after("intent")
        # The registry lock is released: the lineage exists with its intent.
        try:
            base = _prepare(write)
        except _ContinuationRefused as exc:
            _withdraw(write)
            raise WriteRefused(f"{exc}; nothing ran") from None
        except _PreparationFailed as exc:
            _publish(write, status="failed", commits=(), compromised=None)
            say(f"{exc}; nothing ran")
            return _outcome(1, "failed", "preparation_failed", write)
        _crash_after("preparation")
        tip = _tip(write)
        # A new lineage starts at its base; a continuation at its branch's tip, which
        # keeps the commits made by hand since the last member (§3.6).
        if tip is None or (not write.continuing and tip != base):
            _compromise(write, "preparation_moved_head")
            say(f"the branch {write.branch} is not at the base after preparation")
            return _outcome(1, "failed", "preparation_moved_head", write, head=tip)
        _start_point(write, tip)
        _crash_after("start_point")

        # nosec B604: ``shell`` is a Workspace capability flag, not a subprocess argument.
        workspace = Workspace(path=write.worktree, write=True, shell=plan.role.shell)  # nosec B604
        tripwire = Tripwire.arm(workspace, home=plan.request.home, environ=plan.environment)
        final = run_links(workspace, step_dir)
        _crash_after("step")

        tampered = _tampered(final, tripwire)
        if tampered:
            scope = quarantine.widest_scope(
                tampered,
                worktree=write.worktree,
                git_dir=write.git_dir or write.worktree / ".git",
                common_dir=identity.common_dir,
                home=plan.request.home,
                environ=plan.environment,
            )
            if scope != "lineage":
                quarantine.publish(
                    state,
                    scope,
                    reason="tripwire",
                    run_id=run_id,
                    paths=tampered,
                    common_dir=identity.common_dir,
                )
            _compromise(write, "tripwire")
            for path in tampered:
                say(f"git tripwire: {path} changed during the run")
            say(
                f"no git command was run; the worktree is kept for inspection at "
                f"{write.worktree}. Do not run git inside it."
            )
            return _outcome(1, "failed", "tripwire", write, final=final)

        head, moved_tip = _head(write), _tip(write)
        reflog_moved, rewritten, reflog_commits = (
            _reflog_gained(write, tip) if unconfined else (False, False, [])
        )
        if rewritten:
            # No witness can name what appeared: never published as final. The
            # pending write and the intent stay, so the next admission finds
            # them stale and quarantines the operator (§3.8.3 steps 1 and 6).
            found = _new_commits(write, tip, [head, moved_tip])
            for sha in found:
                try:
                    provenance.record(
                        state,
                        sha,
                        run_id=run_id,
                        lineage=write.owner,
                        made_by="agent",
                        providers=plan.role.providers,
                    )
                except FileExistsError:
                    pass
            _compromise(write, "reflog_rewritten")
            say(
                "the worktree's reflog was rewritten during an unconfined write: the lineage "
                "is compromised and left uncertain; recover it by hand"
            )
            commits_found: list[tuple[str, MadeBy]] = [(sha, "agent") for sha in found]
            return _outcome(
                1,
                "failed",
                "reflog_rewritten",
                write,
                commits=commits_found,
                head=head,
                final=final,
            )
        if head != tip or moved_tip != tip or reflog_moved:
            found = _new_commits(write, tip, [head, moved_tip])
            found += [sha for sha in reflog_commits if sha not in found]
            commits: list[tuple[str, MadeBy]] = [(sha, "agent") for sha in found]
            _publish(write, status="failed", commits=commits, compromised="agent_moved_head")
            say("the agent moved HEAD or the branch: nothing committed, the lineage compromised")
            return _outcome(
                1, "failed", "agent_moved_head", write, commits=commits, head=head, final=final
            )

        code, out, err = write.git(write.worktree, ["status", "--porcelain"])
        if code != 0:
            _compromise(write, "status_failed")
            say(f"git status failed in {write.worktree}: {err.strip()}")
            return _outcome(1, "failed", "status_failed", write, head=head, final=final)
        failed_step = final.exit_code != 0
        if not out.strip():
            status = "failed" if failed_step else "no_change"
            _publish(write, status=status, commits=(), compromised=None)
            if failed_step:
                return _outcome(1, "failed", "step_failed", write, head=head, final=final)
            say(f"no change in {write.worktree}; nothing committed")
            return _outcome(NO_CHANGE_EXIT_CODE, "no_change", None, write, head=head, final=final)

        model = final.model_reported or final.model or "unknown"
        verb = "residue" if failed_step else "implement"
        message = f"chore(ha): {run_id} {verb} via {final.provider}/{model}"
        reason, commits, head = _commit(write, message, tip, step_dir)
        _crash_after("commit")
        if reason is not None:
            _publish(write, status="failed", commits=commits, compromised=reason)
            say(
                f"the commit was refused or a hook committed ({reason}): see "
                f"{step_dir / COMMIT_LOG}; the lineage is compromised"
            )
            return _outcome(1, "failed", reason, write, commits=commits, head=head, final=final)
        status = "failed" if failed_step else "committed"
        _publish(write, status=status, commits=commits, compromised=None)
        code, patch, _ = write.git(write.worktree, ["diff", "--binary", base, "HEAD"])
        (run_dir / PATCH_FILE).write_text(patch, encoding="utf-8", errors="replace")
        if failed_step:
            return _outcome(
                1, "failed", "step_failed", write, commits=commits, head=head, final=final
            )
        return _outcome(0, "committed", None, write, commits=commits, head=head, final=final)
```

- [ ] **Step 5: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_engine_plan.py tests/unit/headless_agents/test_implement.py -q -p no:cacheprovider`
Expected: every test passes (`test_implement.py`: 24), and the package suite stays green —
lot 1's write tests (`test_write_flow.py`) run the new-lineage path unchanged.

- [ ] **Step 6: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/engine.py packages/headless-agents/src/headless_agents/write_flow.py tests/unit/headless_agents/test_engine_plan.py tests/unit/headless_agents/test_implement.py
git commit -m "feat(headless-agents): --continue joins an implement run's lineage under its lock"
```

### Task 8: `--continue` in the CLI

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/cli.py` (`_RUN_EPILOG`, the `--continue`
  argument, `_run`)
- Modify: `packages/headless-agents/README.md`
- Modify: `tests/unit/headless_agents/test_implement.py`

**Interfaces:**
- Consumes: `Request.continue_run` (Task 7).
- Produces: `ha run WORKFLOW --continue RUN_ID [PROMPT]`; its refusals are the engine's.

- [ ] **Step 1: Write the failing tests.** Append to `tests/unit/headless_agents/test_implement.py`:

```python
def test_the_cli_continues_a_lineage_and_prints_the_new_run_id(world: World) -> None:
    first = _first(world)
    world.agent.edit = _fix
    code, out, err = world.cli("run", "build", "--continue", first.run_id, "Turn it on.")
    assert code == 0, err
    (run_id,) = set(world.registry().run_ids()) - {first.run_id}
    assert out.startswith(f"run: {run_id}\nbranch: ha/{first.run_id}\n")


def test_the_cli_refuses_continue_on_a_role_target(world: World) -> None:
    first = _first(world)
    code, _, err = world.cli("run", "codex", "--continue", first.run_id, "task")
    assert code == 2 and "--continue needs an implement workflow" in err
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_implement.py -q -p no:cacheprovider -k cli`
Expected: the two new tests fail — `ha` exits `2` on argparse's
`unrecognized arguments: --continue`.

- [ ] **Step 3: Implement.** In `cli.py`, add after the `--run-dir` argument:

```python
    run.add_argument(
        "--continue",
        dest="continue_run",
        metavar="RUN_ID",
        help="an implement workflow only: join the lineage of RUN_ID, an implement run, "
        "and work on its branch in its worktree",
    )
```

  replace `_run` with:

```python
def _run(args: argparse.Namespace, io: Io) -> int:
    if args.provider is not None:
        raise UsageError(
            '-p was removed in 0.5.0: the provider is the TARGET (ha run codex "..."); '
            "a chain is declared in roles.toml"
        )
    if args.chain is not None:
        raise UsageError(
            "--chain was removed in 0.5.0: declare the chain in a role of roles.toml "
            '(chain = ["codex:MODEL", "claude:MODEL"]) and run the role as the TARGET'
        )
    prompt, is_tty = _prompt(args, io)
    request = Request(
        target=args.target,
        prompt=prompt,
        stdin_is_tty=is_tty,
        overrides=Overrides(
            model=args.model,
            effort=args.effort,
            timeout=args.timeout,
            context=args.context,
            context_parents=args.context_parents,
            mcp=args.mcp,
            write=args.write,
            # nosec B604: ``shell`` is a role capability override, not a subprocess argument.
            shell=args.shell,  # nosec B604
            base_url=args.base_url,
            key_env=args.key_env,
        ),
        base=args.base,
        repo=args.repo,
        run_dir=args.run_dir,
        cwd=io.cwd,
        environ=io.environ,
        home=io.home,
        continue_run=args.continue_run,
    )
    outcome = execute(plan(request), say=io.say)
    branch = outcome.report.get("branch")
    if args.json:
        io.stdout.write(json.dumps(outcome.report, ensure_ascii=False, indent=2) + "\n")
    elif outcome.exit_code == 0 and outcome.final is not None:
        if isinstance(branch, str):
            io.stdout.write(_write_header(outcome, branch))
        text = outcome.final.text
        if text:
            io.stdout.write(text if text.endswith("\n") else text + "\n")
    if outcome.exit_code != 0:
        provider = outcome.final.provider if outcome.final is not None else args.target
        io.say(
            f"{provider} exited {outcome.exit_code}; run {outcome.run_id}, "
            f"logs in {outcome.run_dir}"
        )
    return outcome.exit_code
```

  and `_RUN_EPILOG` with:

```python
_RUN_EPILOG: Final = """\
exit codes of a run (a role or a provider):
  0 the answer
  1 failure
  2 invalid usage or configuration; nothing ran
  3 provider unavailable (a chain that runs out of links returns its last link's 3 or 4)
  4 timeout with no tool call started
  5 write run with no change
  124 timeout
  130 interrupted (Ctrl-C); the run reads incomplete

exit codes of a workflow (implement):
  0 committed
  5 the implementation changed nothing
  1 a step failed (its residue committed), the tripwire fired, HEAD moved or a hook
    refused; the step's own code is in run.json
  2 invalid usage or configuration, a refused --continue, or its lineage in use for
    more than 10 s; nothing ran
  130 interrupted (Ctrl-C); the run reads incomplete

examples:
  ha run codex -m gpt-6-luna "Explain what this repository does."
  ha run build "Add a --verbose flag to the CLI."
  ha run build --continue 20260926T101500-ab12cd34 "Also document the flag."
"""
```

  In `packages/headless-agents/README.md`, replace the bullet that begins
  `- **A workflow**` with:

```markdown
- **A workflow** names the roles that fill the slots of a shape coded in the package.
  `shape = "implement"` takes one role with `write = true` in its `implement` slot: `ha run
  build "task"` runs it on a new `ha/<run_id>` branch, as a write run, with the task
  wrapped in the engine's implement prompt, and prints the run id, the branch, the diffstat
  and the patch path before the agent's text. `--continue RUN_ID` joins that run's lineage
  instead: the next run works in the same worktree, on the same branch, from its tip --
  commits made there by hand included -- once the worktree is clean. A workflow runs its
  roles as declared: `-m`, `--write` and the other role options are refused. `ha workflows`
  lists what `workflows.toml` declares; `shape = "review"` is validated there and arrives
  with the vendor rule in a later 0.5.0 lot.
```

- [ ] **Step 4: Run them again, then the gates.** Expected: `test_implement.py`: 26 passed;
  the package suite green.

- [ ] **Step 5: Commit, then the full suite before PR B is pushed.**

```bash
git add packages/headless-agents/src/headless_agents/cli.py packages/headless-agents/README.md tests/unit/headless_agents/test_implement.py
git commit -m "feat(headless-agents): ha run WORKFLOW --continue RUN_ID"
```

Run the full `tests/unit/` (Global Constraints) before the push of PR B.

---

## Spec coverage (lot 3)

| Spec | Where |
|---|---|
| §3.2 `workflows.toml`, its slots, its validation | Task 1 (P1); a `review` workflow validated, not run (P2, Tasks 1 and 4) |
| §3.1 disjoint names; §3.3 configuration from the operator's directory only | Task 1 (collisions); Tasks 2 and 4 (`config_file`, a linked file refused) |
| §3.4 `load_config`, every gate in the engine, the prompt size of every link | Tasks 2 and 4 (P3, P4) |
| §3.9 `ha workflows`; workflow options; `--continue`; the write header; help and exit codes | Tasks 2, 4, 6 (P10), 7, 8 |
| §3.6 the shape `implement`: its worktree, commit subject, exit codes | Task 6 (lot 1's write protocol, `implement` subject, `steps/01-implement-<role>`) |
| §3.6 `--continue`: every refusal, the lock and its bound, hand commits kept | Task 7 (P7, P8, P9) |
| §3.7 the implement template | Task 3 |
| §3.8.3 steps 1–3 for a continuation | Task 7 |
| §3.8.4 step 4 the providers an engine commit carries | Task 6 (P6) |
| §3.10 `target.kind = "workflow"`, `prompt.md`, `continues`, `implement_providers` | Tasks 5 and 6 (P5) |
| §4: `--continue` refusals, lineage locking, shared compromise, crash after intent, corrupt lineage or registry | Tasks 4 and 7 |

Out of this lot, by the spec's own order (§5): the shape `review`, the vendor rule, the
review result, `--run`, `--head`, `ha show --dir` and `implement --findings` with the fix
template (lot 4); the `live` suite, the README rewrite and the CHANGELOG (lot 5).
