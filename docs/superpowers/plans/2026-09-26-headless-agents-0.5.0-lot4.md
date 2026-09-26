# headless-agents 0.5.0 — Lot 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Execution method (the lot 1–3 method, Q60 = a):** this plan is reviewed by codex, then
> implemented inline, test-first, in three pull requests (A, B, C below), each reviewed by an
> independent non-Claude reviewer and merged under delegation 39f7ea9f (approve verdict +
> required CI green, never `--admin`); a final Opus review then a codex review of the whole
> lot. No tag and no release in this lot: the tag follows lot 5 (spec §5), and both stay the
> operator's.

**Goal:** Deliver lot 4 of headless-agents 0.5.0 — the shape `review`, shipped with the vendor
rule, its review result and `vendor_check` from its first commit, with `--run`, `--head`,
`ha show --dir`, and `implement --findings` with the fix template.

**Architecture:** Three pure modules first: `templates` gains the review, judge and fix
prompts and the verdict reader; `vendor` attributes every commit of a range from the
provenance `ha` records and checks that no reviewer shares a provider with its authors;
`reviews` writes a review's check and result once in the state directory. Then
`review_flow` holds a review's state and git — the locks of §3.8.4, admission, the pinned
head, the check, the patch, the detached worktree, the result and the cleanup — while the
engine plans the panel, runs the reviewers in parallel threads and the judge through the
0.4.0 chain, reads the verdict and writes the report. `ha show` renders a review from its
records, `ha show --dir` a run directory for display, `ha clean` removes a kept review
worktree through git. Finally `implement --findings` takes a review's deciding text, the
head check done in the write flow's preparation, and the CLI gains `--head`, `--run` and
`--findings`.

**Tech Stack:** Python 3.12 standard library (`concurrent.futures`, `dataclasses`, `re`,
`pathlib`), git, pytest. No new dependency.

**Spec:** `docs/specs/2026-09-24-headless-agents-0.5.0-design.md`. Read §3.2, §3.3, §3.4,
§3.5, §3.6 (`--findings`), §3.7, §3.8.1, §3.8.2, §3.8.4, §3.8.5, §3.8.6, §3.9, §3.10 and §4
before any task. The lot 3 plan (`docs/superpowers/plans/2026-09-25-headless-agents-0.5.0-lot3.md`)
states what `workflows.toml`, the implement run and `--continue` already do; its decision P2
(a review refused until its shape ships) is lifted here.

## Global Constraints

- Runtime dependencies stay `pydantic` and `structlog`; no import of `brain_v42` from
  `packages/headless-agents` (`tests/unit/headless_agents/test_package_boundary.py`).
- Unchanged: `result.json` schema 1 and its key set, the `AgentProvider` protocol,
  `run_chain`'s own result, and the `run.json` key sets `RUN_KEYS` and `STEP_KEYS` (§2 Out,
  §3.10).
- "`plan()` never runs git" (§3.8.2): it reads the configuration and resolves run ids
  through the registry; every decision that reads git or the mutable state is taken in
  `execute()`, under the locks, and "the early refusals `plan()` can give from the registry
  are re-checked there".
- "No lot ever exposes a review that does not enforce the rule" (§5 item 4): the review is
  planned and executed in one task (Task 4), with the vendor rule.
- The lock order of §3.8.2 is fixed: lifecycle, unconfined, lineage registry, lineage locks
  in ascending owner order; a review holds the unconfined lock shared "from its admission to
  its end, providers, chains, reviewers and judge included", and every lineage lock of its
  repository shared "before its first git command" (§3.8.4 step 1).
- A review's exit codes (§3.5, §3.9): `0` APPROVE, `6` CHANGES, `1` a step failed or the
  verdict is unreadable, `2` invalid usage, a refused `--run`, `--findings` or the vendor
  rule — nothing ran. "Codes `3`, `4` and `124` stay a step's".
- A report is never an authority (§3.8.1): a review's head, verdict and text come from its
  result in the state; `--findings` "reads only the final result".
- Everything written for GitHub — code, comments, tests, commits, PRs — in English.
- Gates before every commit: `.venv/bin/pytest tests/unit/headless_agents tests/unit/agents -q
  -p no:cacheprovider`, `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  `.venv/bin/mypy src/ packages/headless-agents/src`; and the full `tests/unit/` before each
  push. Outside the canonical root, export `POSTGRES_URL` before `pytest tests/unit/`.
- Every test and code block below is a diff taken from a branch where the task was done
  test-first on `main` at `11e6e1f5` (2026-09-26): each task's tests were watched failing for
  the reason its Step 2 names, then passing; after Task 8 the package suite has 1499 tests
  green, the full `tests/unit/` 12656 (its two known worktree-venv failures in
  `test_delivery_deployment.py` aside), `ruff check`, `ruff format --check` and `mypy` clean.
  A task's diffs apply with `git apply` on the state the previous task left.

## Decisions this plan takes where the spec leaves the choice to it

| # | Decision | Why |
|---|---|---|
| P1 | `templates`: the review, judge and fix templates; `OUTPUT_CONTRACT` is the spec's text verbatim; with `--findings` and no task, the task is `Address the findings below.`; a block's content travels verbatim (only attributes are escaped). `read_verdict` reads the last non-empty line, strips `*`, `_` and backticks around it, and fullmatches `VERDICT:\s*(APPROVE\|CHANGES)` case-insensitively; anything else is `None`. | §3.5 step 5, §3.7; §3.3: "a diff can try to steer a judge. The verdict is therefore advisory". |
| P2 | `vendor`: a commit without provenance, while no unconfined write is recorded, is attributed `made_by: hand` with no provider — the spec names `engine`, `agent`, `hook`, `unknown` for provenance and needs a word for "no provider"; an unreadable provenance record or `unconfined-writers.json` refuses the review. | §3.8.4 step 4; §3.8.1: "unknown is treated as compromised — never as empty". |
| P3 | A review is planned and executed in one task (Task 4): planned alone, it would reach `execute()` and run as a one-step run of its first reviewer, without the rule. | §5 item 4. |
| P4 | Planning a review: `SlotPlan(slot, role, models, mcp, environment)` per slot, `Plan.panel` in launch order (reviewers, then the judge); `Plan.role` is the first reviewer's; a blank or absent prompt is `Review this change.`; `--run` is resolved like `--continue` (an implement run's registry entry) and excludes `--head` and `--base`; `--head` and `--run` are refused off a review, `--continue` and `--findings` off an implement. | §3.5, §3.9; §3.8.2 (registry only). |
| P5 | `review_flow.prepare` takes the lineage registry lock shared and every lineage of the repository — and any lineage whose worktree holds `--repo` — shared, ascending, each bounded to 10 s; under them a pending write is stale by construction (a live writer holds its lock exclusively), so it is handled as a write's admission handles one (`write_flow.unfinalized`: lineage compromised, repository quarantined) and refuses. The head, the merge base, `git log` and the diff all use the pinned commit; an empty diff refuses; the check is written once, then the lineage and registry locks are released before `change.patch` and `git worktree add --detach`. | §3.8.2, §3.8.4 steps 1–6, §3.8.5. |
| P6 | The engine runs a phase's steps in threads (`ThreadPoolExecutor`), each through the unchanged `_run_links` with the slot's own plan; every prompt of a phase is size-checked before any starts — one too large refuses the phase with its step recorded at `2` and `failure_reason: prompt_too_large`; any reviewer that does not answer (exit `0` and a non-empty text) fails the review before the judge (`step_failed`); an unreadable verdict fails it (`unreadable_verdict`) and writes no result. A review's registry entry has no lineage and records no providers; its report records `base` (the merge base), `head`, `verdict`, `text`, `vendor_check` and `cleanup`, the write fields `null`, and each step's own verdict. Every panel role's rail needs its isolation proof. | §3.4, §3.5 steps 2–5, §3.8.0, §3.10. |
| P7 | `ha show` of a review takes its head, verdict and text from its result, its check from the result or, before a verdict, from the check file; `cleanup` and `base` have no record in the state, so they come from the review's own report, for display. A review's head line is `head    <sha7>  N commits  +x -y  f files` (it has no branch), followed by `vendors …`; step verdicts render upper-case; a failed cleanup is named on the header. `ha show --dir PATH` renders a run directory's report, noting it is display only; `ha show` takes a run id or `--dir`, exactly one. | §3.8.6, §3.9, §3.10 (its `ha show` example). |
| P8 | `--findings`: `plan()` checks the registry only (the named run is a review); `execute()` reads the review result before anything is created (written once, immutable) and composes `fix_prompt(task, result.text)`; the head check is done in the write flow's preparation — a continuation compares the lineage's tip, a new run its resolved base **before** `git worktree add` — because the intent precedes the first git command (§3.6 step 2, §3.8.3 step 2). A refused new run withdraws its lineage state, which its intent created and nothing else touched. The engine's commit says `fix`; the registry entry records `findings_from` (required, as `continues`: lot 3's P5 argument holds — no released `ha` wrote an entry). | §3.6, §3.8.2, §3.8.3, §3.10. |
| P9 | `ha clean` of a review whose cleanup failed removes its kept worktree through git (`git worktree remove --force`), and runs no git at all under a quarantine (exit `1`, nothing cleaned). | §3.9 (`ha clean`). |
| P10 | The CLI asks the engine whether a target's prompt is optional (`prompt_is_optional`, configuration only) before reading stdin: a review, or an implement with `--findings`, never reads it unless given `-`. A review prints `run: <id>`, `head: <sha>` and its deciding text for APPROVE and CHANGES alike (a verdict is an answer, and CHANGES is what `--findings` feeds back); a failure's stderr line names its `failure_reason`. | §3.9 (prompt, output). |

## Review Focus

Five inputs the spec implies but no spec test names, most likely first; each has a test in
the task that owns the code.

1. **A range with a hand commit after an unconfined write ran** — a person expects that
   commit attributed to every recorded unconfined writer's providers, and a reviewer sharing
   one refused (`test_after_an_unconfined_write_a_commit_without_provenance_is_its_writers`,
   Task 4).
2. **`--run` after the branch advanced** — the current tip is reviewed and recorded, not the
   named run's commit (`test_run_reviews_the_current_tip_after_the_branch_advanced`, Task 4).
3. **A review whose worktree cannot be removed** — the verdict stands, the result is
   published, the worktree is kept and `ha clean` removes it later (Tasks 4 and 6).
4. **A diff too large for a reviewer's rail** — refused at the phase's start, the workflow
   exits `1` with the step at `2`, nothing ran (Task 4).
5. **Findings about another revision** — the lineage moved after the review, or a new run
   starts elsewhere: refused, the lineage left as it was, no lineage created (Task 7).

---

## File structure

```text
packages/headless-agents/src/headless_agents/
  templates.py     MOD  review, judge and fix prompts; OUTPUT_CONTRACT; read_verdict (P1)
  vendor.py        NEW  attribute, check_independence, VendorCheck (P2)
  reviews.py       NEW  a review's check and result, each written once (§3.8.1)
  review_flow.py   NEW  prepare (locks, admission, pin, check, patch, worktree), finish,
                        remove_worktree (P5, P9)
  engine.py        MOD  SlotPlan, Plan.panel, _plan_review, _run_phase, _execute_review (P4, P6);
                        --findings (P8); prompt_is_optional (P10); clean of a review (P9)
  write_flow.py    MOD  unfinalized and source_lineages public (P5); --findings head check in
                        preparation, withdrawal of a new lineage, the fix verb (P8)
  report.py        MOD  refused_step_entry (P6)
  runs.py          MOD  Entry.findings_from (P8)
  show.py          MOD  a review from its records, vendors_line, from_dir (P7)
  cli.py           MOD  --head, --run, --findings; show --dir; the review's output; help (P7, P10)
packages/headless-agents/README.md   MOD  synopsis; a review paragraph
tests/unit/headless_agents/
  test_templates.py  MOD   test_vendor.py  NEW   test_reviews.py  NEW   test_review.py  NEW
  test_engine_plan.py MOD  test_show.py  MOD     test_runs.py  MOD      test_cli.py  MOD
```

## Pull requests

| PR | Tasks | Content | Leaves `main` |
|---|---|---|---|
| A | 1–3 | the templates and the verdict reader, the vendor rule, the review records | nothing new exposed: pure modules and their tests |
| B | 4–6 | a review run (planned and executed, with the rule), `ha show` of a review and `--dir`, `ha clean` of a review | `ha run REVIEW [--base REF]` works, the vendor rule enforced from its first commit |
| C | 7–8 | `implement --findings`, the CLI options `--head`, `--run`, `--findings`, the review's output, the help and the README | lot 4 complete |

---

### Task 1: the review, judge and fix templates, and the verdict reader

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/templates.py`
- Modify: `tests/unit/headless_agents/test_templates.py`

**Interfaces:**
- Produces: `OUTPUT_CONTRACT: Final[str]` (§3.7 verbatim); `FIX_DEFAULT_TASK: Final[str]`;
  `ReviewText(role, provider, model, text)`; `review_prompt(task, diff) -> str`;
  `judge_prompt(task, diff, reviews: Sequence[ReviewText]) -> str`;
  `fix_prompt(task, findings) -> str`; `Verdict = Literal["approve", "changes"]`;
  `read_verdict(text: str | None) -> Verdict | None` (P1).

- [ ] **Step 1: Write the failing tests.** Apply (`git apply`):

```diff
diff --git a/tests/unit/headless_agents/test_templates.py b/tests/unit/headless_agents/test_templates.py
index 2d90c388..f9aecb8a 100644
--- a/tests/unit/headless_agents/test_templates.py
+++ b/tests/unit/headless_agents/test_templates.py
@@ -1,8 +1,19 @@
-"""The prompts the engine composes (spec 0.5.0 §3.7): the implement template of lot 3."""
+"""The prompts the engine composes (spec 0.5.0 §3.7), and the verdict a review ends with (§3.5)."""
 
 from __future__ import annotations
 
-from headless_agents.templates import implement_prompt
+import pytest
+
+from headless_agents.templates import (
+    FIX_DEFAULT_TASK,
+    OUTPUT_CONTRACT,
+    ReviewText,
+    fix_prompt,
+    implement_prompt,
+    judge_prompt,
+    read_verdict,
+    review_prompt,
+)
 
 GOLDEN_IMPLEMENT = """\
 Implement the task below in the repository of your workspace.
@@ -29,3 +40,123 @@ def test_the_task_travels_verbatim_braces_and_markup_included() -> None:
 def test_the_template_tells_the_agent_the_engine_commits() -> None:
     prompt = implement_prompt("x")
     assert "Do not run git: the engine commits your changes" in prompt
+
+
+# ── lot 4: review, judge, fix, and the verdict (spec §3.5, §3.7) ───────────────
+
+CONTRACT = (
+    "List each finding with its severity (critical, high, medium or low) and its `file:line`.\n"
+    "End your answer with exactly one line: `VERDICT: APPROVE` if the change can be merged as\n"
+    "it is, or `VERDICT: CHANGES` if anything must change first.\n"
+)
+
+GOLDEN_REVIEW = (
+    """\
+Review the change below. Your workspace holds the repository at the reviewed commit, \
+read-only: read the changed files in full where the diff is not enough.
+
+<task>
+Review this change.
+</task>
+
+<diff>
++print('v2')
+</diff>
+
+"""
+    + CONTRACT
+)
+
+GOLDEN_FIX = """\
+Implement the task below in the repository of your workspace, addressing the findings \
+of a review of this branch.
+
+<task>
+Keep the API stable.
+</task>
+
+<findings>
+src/app.py:3 high: the flag is ignored
+</findings>
+
+Address each finding, or say why you do not.
+
+Do not run git: the engine commits your changes when you finish. A commit, a checkout, \
+a reset or any other move of HEAD or of a branch fails the run.
+"""
+
+
+def test_the_output_contract_is_the_specs_verbatim() -> None:
+    assert OUTPUT_CONTRACT == CONTRACT
+
+
+def test_the_review_template_is_pinned() -> None:
+    assert review_prompt("Review this change.", "+print('v2')\n") == GOLDEN_REVIEW
+
+
+def test_the_judge_template_ends_with_the_contract() -> None:
+    reviews = [ReviewText(role="r", provider="codex", model="m", text="VERDICT: APPROVE")]
+    assert judge_prompt("t", "d", reviews).endswith(CONTRACT)
+
+
+def test_the_judge_gets_every_review_labelled_in_order_with_escaped_attributes() -> None:
+    reviews = [
+        ReviewText(
+            role="reviewer-codex", provider="codex", model="gpt-6", text="A\nVERDICT: CHANGES"
+        ),
+        ReviewText(role='odd"role', provider="agy", model="<m>", text="VERDICT: APPROVE"),
+    ]
+    prompt = judge_prompt("Review this change.", "+x\n", reviews)
+    labelled = '<review role="reviewer-codex" provider="codex" model="gpt-6">\nA\nVERDICT: CHANGES\n</review>'
+    assert labelled in prompt
+    assert '<review role="odd&quot;role" provider="agy" model="&lt;m&gt;">' in prompt
+    assert prompt.index("reviewer-codex") < prompt.index("odd&quot;role")
+    assert "<diff>\n+x\n</diff>" in prompt
+
+
+def test_the_fix_template_is_pinned() -> None:
+    assert (
+        fix_prompt("Keep the API stable.", "src/app.py:3 high: the flag is ignored") == GOLDEN_FIX
+    )
+
+
+def test_the_fix_template_without_a_task_asks_for_the_findings_only() -> None:
+    assert f"<task>\n{FIX_DEFAULT_TASK}\n</task>" in fix_prompt("", "a finding")
+    assert f"<task>\n{FIX_DEFAULT_TASK}\n</task>" in fix_prompt("  \n", "a finding")
+
+
+def test_blocks_carry_their_content_verbatim() -> None:
+    diff = "+x = {'a': 1}\n+print('{0}')\n"
+    assert f"<diff>\n{diff}</diff>" in review_prompt("t", diff)
+
+
+@pytest.mark.parametrize(
+    ("text", "verdict"),
+    [
+        ("All good.\nVERDICT: APPROVE", "approve"),
+        ("Fix it.\nVERDICT: CHANGES\n\n  \n", "changes"),
+        ("**VERDICT: APPROVE**", "approve"),
+        ("`VERDICT: CHANGES`", "changes"),
+        ("_verdict: approve_", "approve"),
+        ("x\n  VERDICT:   CHANGES  ", "changes"),
+    ],
+)
+def test_the_verdict_is_read_from_the_last_non_empty_line(text: str, verdict: str) -> None:
+    assert read_verdict(text) == verdict
+
+
+@pytest.mark.parametrize(
+    "text",
+    [
+        "",
+        "   \n\n",
+        "VERDICT: APPROVE\nThat is all.",
+        "VERDICT: MAYBE",
+        "VERDICT: APPROVE or CHANGES",
+        "Verdict - approve",
+        "I would say VERDICT: APPROVE",
+        None,
+    ],
+)
+def test_anything_else_is_an_unreadable_verdict_never_an_approval(text: str | None) -> None:
+    assert read_verdict(text) is None
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_templates.py -q -p no:cacheprovider`
Expected: `test_templates.py: 1 error` — every failure is the missing feature:
  - `test_templates.py -- (collection) ImportError: cannot import name 'FIX_DEFAULT_TASK' from 'headless_agents.templates' (packages/headless-agents/src/headless_agents/templates.py)`

- [ ] **Step 3: Implement.** Apply:

```diff
diff --git a/packages/headless-agents/src/headless_agents/templates.py b/packages/headless-agents/src/headless_agents/templates.py
index ae309bd4..4bad1462 100644
--- a/packages/headless-agents/src/headless_agents/templates.py
+++ b/packages/headless-agents/src/headless_agents/templates.py
@@ -1,26 +1,116 @@
-"""The prompts the engine composes (spec 0.5.0 §3.7).
+"""The prompts the engine composes, and the verdict a review ends with (spec 0.5.0 §3.5, §3.7).
 
 Written in English and versioned with the package; configuration never edits
 them -- a role shapes behaviour through its instructions, which travel in the
-preamble. Each template delimits what it carries in blocks. Lot 3 ships the
-``implement`` template; the ``fix``, ``review`` and ``judge`` ones come with the
-``review`` shape (lot 4).
+preamble. Each template delimits what it carries in blocks -- ``<task>``,
+``<diff>``, ``<review role="…" provider="…" model="…">``, ``<findings>`` -- whose
+attributes go through :func:`headless_agents.context.xml_attribute`. A block's
+content travels verbatim: a diff can try to steer a judge, which is why the
+verdict is advisory and ``ha`` never merges nor pushes (§3.3).
 """
 
 from __future__ import annotations
 
-from typing import Final
+import re
+from collections.abc import Sequence
+from dataclasses import dataclass
+from typing import Final, Literal
 
-IMPLEMENT_TEMPLATE: Final = """\
+from .context import xml_attribute
+
+_NO_GIT: Final = (
+    "Do not run git: the engine commits your changes when you finish. A commit, a checkout, "
+    "a reset or any other move of HEAD or of a branch fails the run.\n"
+)
+
+IMPLEMENT_TEMPLATE: Final = (
+    """\
 Implement the task below in the repository of your workspace.
 
 <task>
 {task}
 </task>
 
-Do not run git: the engine commits your changes when you finish. A commit, a checkout, \
-a reset or any other move of HEAD or of a branch fails the run.
 """
+    + _NO_GIT
+)
+
+#: §3.7: the review and judge templates end with it, verbatim.
+OUTPUT_CONTRACT: Final = (
+    "List each finding with its severity (critical, high, medium or low) and its `file:line`.\n"
+    "End your answer with exactly one line: `VERDICT: APPROVE` if the change can be merged as\n"
+    "it is, or `VERDICT: CHANGES` if anything must change first.\n"
+)
+
+REVIEW_TEMPLATE: Final = (
+    """\
+Review the change below. Your workspace holds the repository at the reviewed commit, \
+read-only: read the changed files in full where the diff is not enough.
+
+<task>
+{task}
+</task>
+
+<diff>
+{diff}</diff>
+
+"""
+    + OUTPUT_CONTRACT
+)
+
+JUDGE_TEMPLATE: Final = (
+    """\
+Judge the reviews below of the change below. Your workspace holds the repository at the \
+reviewed commit, read-only. Check every finding against the code, merge the duplicates, \
+discard the unfounded, and keep what is real.
+
+<task>
+{task}
+</task>
+
+<diff>
+{diff}</diff>
+
+{reviews}
+"""
+    + OUTPUT_CONTRACT
+)
+
+#: The task of a fix when ``--findings`` is given without one (§3.6: optional guidance).
+FIX_DEFAULT_TASK: Final = "Address the findings below."
+
+FIX_TEMPLATE: Final = (
+    """\
+Implement the task below in the repository of your workspace, addressing the findings \
+of a review of this branch.
+
+<task>
+{task}
+</task>
+
+<findings>
+{findings}
+</findings>
+
+Address each finding, or say why you do not.
+
+"""
+    + _NO_GIT
+)
+
+
+@dataclass(frozen=True)
+class ReviewText:
+    """One reviewer's answer, as the judge receives it (§3.5 step 4)."""
+
+    role: str
+    provider: str
+    model: str
+    text: str
+
+
+def _with_newline(text: str) -> str:
+    return text if text.endswith("\n") or not text else text + "\n"
 
 
 def implement_prompt(task: str) -> str:
@@ -32,4 +122,61 @@ def implement_prompt(task: str) -> str:
     return IMPLEMENT_TEMPLATE.format(task=task.strip())
 
 
-__all__ = ["IMPLEMENT_TEMPLATE", "implement_prompt"]
+def fix_prompt(task: str, findings: str) -> str:
+    """The implement template plus the ``<findings>`` block of a review (§3.6, §3.7)."""
+    return FIX_TEMPLATE.format(task=task.strip() or FIX_DEFAULT_TASK, findings=findings.strip())
+
+
+def review_prompt(task: str, diff: str) -> str:
+    """What each reviewer gets: the task, the diff and the output contract (§3.5 step 2)."""
+    return REVIEW_TEMPLATE.format(task=task.strip(), diff=_with_newline(diff))
+
+
+def judge_prompt(task: str, diff: str, reviews: Sequence[ReviewText]) -> str:
+    """What the judge gets: the task, the diff, and every review labelled (§3.5 step 4)."""
+    blocks = "\n".join(
+        f'<review role="{xml_attribute(review.role)}" provider="{xml_attribute(review.provider)}"'
+        f' model="{xml_attribute(review.model)}">\n{_with_newline(review.text.strip())}</review>\n'
+        for review in reviews
+    )
+    return JUDGE_TEMPLATE.format(task=task.strip(), diff=_with_newline(diff), reviews=blocks)
+
+
+Verdict = Literal["approve", "changes"]
+
+_VERDICT: Final = re.compile(r"VERDICT:\s*(APPROVE|CHANGES)", re.IGNORECASE)
+
+
+def read_verdict(text: str | None) -> Verdict | None:
+    """The verdict of ``text``: its last non-empty line, and nowhere else (§3.5 step 5).
+
+    The ``*``, ``_`` and backtick characters around the line are stripped; what
+    remains must be ``VERDICT: APPROVE`` or ``VERDICT: CHANGES``, whatever the
+    case. Anything else is unreadable -- ``None``, a failure, never an approval.
+    """
+    if not text:
+        return None
+    lines = [line.strip() for line in text.splitlines() if line.strip()]
+    if not lines:
+        return None
+    found = _VERDICT.fullmatch(lines[-1].strip("*_`").strip())
+    if found is None:
+        return None
+    return "approve" if found.group(1).upper() == "APPROVE" else "changes"
+
+
+__all__ = [
+    "FIX_DEFAULT_TASK",
+    "FIX_TEMPLATE",
+    "IMPLEMENT_TEMPLATE",
+    "JUDGE_TEMPLATE",
+    "OUTPUT_CONTRACT",
+    "REVIEW_TEMPLATE",
+    "ReviewText",
+    "Verdict",
+    "fix_prompt",
+    "implement_prompt",
+    "judge_prompt",
+    "read_verdict",
+    "review_prompt",
+]
```

- [ ] **Step 4: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_templates.py -q -p no:cacheprovider`
Expected: `test_templates.py: 24 passed`. Then the gates of the Global Constraints.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/templates.py tests/unit/headless_agents/test_templates.py
git commit -m "feat(headless-agents): the review, judge and fix templates, and the verdict reader"
```

### Task 2: the vendor rule: every commit of a range attributed, reviewers independent

**Files:**
- Create: `packages/headless-agents/src/headless_agents/vendor.py`
- Create: `tests/unit/headless_agents/test_vendor.py`

**Interfaces:**
- Consumes: `provenance.lookup`, `state.read_optional`, `write_flow.UNCONFINED_WRITERS`.
- Produces: `AttributedCommit(sha, run_id, made_by, providers)`;
  `VendorCheck(commits, authors, reviewers)` with `to_document()` and
  `from_document(document, *, where)` (`Unknown` when malformed); `VendorRefused`;
  `attribute(state, commits: Sequence[tuple[sha, subject]]) -> tuple[AttributedCommit, ...]`;
  `check_independence(commits, reviewers: Mapping[role, providers]) -> VendorCheck` (P2).

- [ ] **Step 1: Write the failing tests.** Apply (`git apply`):

```diff
diff --git a/tests/unit/headless_agents/test_vendor.py b/tests/unit/headless_agents/test_vendor.py
new file mode 100644
index 00000000..20ad8e84
--- /dev/null
+++ b/tests/unit/headless_agents/test_vendor.py
@@ -0,0 +1,137 @@
+"""The vendor rule: who wrote each commit of a range, and whether a reviewer shares a vendor
+with them (spec 0.5.0 §3.8.4 steps 4-5, §3.8.6)."""
+
+from __future__ import annotations
+
+from pathlib import Path
+
+import pytest
+
+from headless_agents import provenance
+from headless_agents.state import Unknown, publish
+from headless_agents.vendor import (
+    AttributedCommit,
+    VendorCheck,
+    VendorRefused,
+    attribute,
+    check_independence,
+)
+
+SHA_1 = "1" * 40
+SHA_2 = "2" * 40
+SHA_3 = "3" * 40
+RUN = "20260926T100000-aaaaaaaa"
+OTHER = "20260926T110000-bbbbbbbb"
+
+
+def _record(state: Path, sha: str, providers: list[str], made_by: str = "engine") -> None:
+    provenance.record(state, sha, run_id=RUN, lineage=RUN, made_by=made_by, providers=providers)  # type: ignore[arg-type]
+
+
+def _writers(state: Path, *providers: list[str]) -> None:
+    publish(
+        state / "unconfined-writers.json",
+        {"writers": [{"run_id": OTHER, "repository": "/r", "providers": p} for p in providers]},
+    )
+
+
+def test_a_recorded_commit_takes_its_recorded_providers(tmp_path: Path) -> None:
+    _record(tmp_path, SHA_1, ["opencode", "codex"])
+    (commit,) = attribute(tmp_path, [(SHA_1, "chore(ha): x implement via opencode/m")])
+    assert commit == AttributedCommit(
+        sha=SHA_1, run_id=RUN, made_by="engine", providers=("opencode", "codex")
+    )
+
+
+def test_a_commit_without_provenance_is_hand_written_when_no_unconfined_write_ran(
+    tmp_path: Path,
+) -> None:
+    (commit,) = attribute(tmp_path, [(SHA_2, "fix: a typo")])
+    assert commit == AttributedCommit(sha=SHA_2, run_id=None, made_by="hand", providers=())
+
+
+def test_a_commit_without_provenance_is_attributed_to_every_unconfined_writer(
+    tmp_path: Path,
+) -> None:
+    """§3.8.4 step 4: once an unconfined write ran, nothing is presumed hand-written."""
+    _writers(tmp_path, ["claude"], ["opencode", "claude"])
+    (commit,) = attribute(tmp_path, [(SHA_2, "fix: a typo")])
+    assert commit == AttributedCommit(
+        sha=SHA_2, run_id=None, made_by="unknown", providers=("claude", "opencode")
+    )
+
+
+@pytest.mark.parametrize("subject", ["chore(ha): 2026 implement via codex/m", "chore(ha):"])
+def test_a_chore_ha_commit_without_provenance_refuses(tmp_path: Path, subject: str) -> None:
+    """A 0.4.0 write run or a lost state directory: no legacy fallback (§3.8.4 step 4)."""
+    with pytest.raises(VendorRefused, match=rf"{SHA_3[:12]}.*chore\(ha\).*no provenance"):
+        attribute(tmp_path, [(SHA_3, subject)])
+
+
+def test_an_unreadable_provenance_record_refuses(tmp_path: Path) -> None:
+    (tmp_path / "provenance").mkdir()
+    (tmp_path / "provenance" / f"{SHA_1}.json").write_text("{not json")
+    with pytest.raises(VendorRefused, match="unknown"):
+        attribute(tmp_path, [(SHA_1, "anything")])
+
+
+def test_an_unreadable_writers_list_refuses(tmp_path: Path) -> None:
+    """Unknown is never empty (§3.8.1): an unreadable list cannot presume a hand."""
+    (tmp_path / "unconfined-writers.json").write_text("{not json")
+    with pytest.raises(VendorRefused, match="unconfined-writers.json"):
+        attribute(tmp_path, [(SHA_2, "fix: a typo")])
+
+
+def test_independent_reviewers_pass_and_the_check_says_who_wrote_what(tmp_path: Path) -> None:
+    _record(tmp_path, SHA_1, ["opencode", "codex"])
+    commits = attribute(tmp_path, [(SHA_1, "chore(ha): x"), (SHA_2, "docs: by hand")])
+    check = check_independence(commits, {"reviewer-agy": ("agy",), "reviewer-claude": ("claude",)})
+    assert check.authors == ("codex", "opencode")
+    assert check.to_document() == {
+        "commits": [
+            {"sha": SHA_1, "run_id": RUN, "made_by": "engine", "providers": ["opencode", "codex"]},
+            {"sha": SHA_2, "run_id": None, "made_by": "hand", "providers": []},
+        ],
+        "authors": ["codex", "opencode"],
+        "reviewers": {"reviewer-agy": ["agy"], "reviewer-claude": ["claude"]},
+    }
+
+
+def test_a_reviewer_sharing_any_link_with_an_author_is_refused_naming_all_three(
+    tmp_path: Path,
+) -> None:
+    """Every link of the reviewer's chain counts (§3.8.4 step 5)."""
+    _record(tmp_path, SHA_1, ["opencode", "codex"])
+    commits = attribute(tmp_path, [(SHA_1, "chore(ha): x")])
+    with pytest.raises(VendorRefused, match=rf"reviewer-pair.*codex.*{SHA_1[:12]}"):
+        check_independence(commits, {"reviewer-pair": ("agy", "codex")})
+
+
+def test_a_hand_written_commit_constrains_nothing(tmp_path: Path) -> None:
+    commits = attribute(tmp_path, [(SHA_2, "docs: by hand")])
+    check = check_independence(commits, {"reviewer-codex": ("codex",)})
+    assert check.authors == ()
+
+
+def test_a_check_round_trips_through_its_document() -> None:
+    check = VendorCheck(
+        commits=(AttributedCommit(sha=SHA_1, run_id=RUN, made_by="agent", providers=("claude",)),),
+        authors=("claude",),
+        reviewers={"r": ("codex",)},
+    )
+    assert VendorCheck.from_document(check.to_document(), where="x") == check
+
+
+@pytest.mark.parametrize(
+    "document",
+    [
+        {},
+        {"commits": "x", "authors": [], "reviewers": {}},
+        {"commits": [{"sha": "nope"}], "authors": [], "reviewers": {}},
+        {"commits": [], "authors": [1], "reviewers": {}},
+        {"commits": [], "authors": [], "reviewers": {"r": "codex"}},
+    ],
+)
+def test_a_malformed_check_is_unknown(document: dict[str, object]) -> None:
+    with pytest.raises(Unknown, match="where"):
+        VendorCheck.from_document(document, where="where")
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_vendor.py -q -p no:cacheprovider`
Expected: `test_vendor.py: 1 error` — every failure is the missing feature:
  - `test_vendor.py -- (collection) ModuleNotFoundError: No module named 'headless_agents.vendor'`

- [ ] **Step 3: Implement.** Apply:

```diff
diff --git a/packages/headless-agents/src/headless_agents/vendor.py b/packages/headless-agents/src/headless_agents/vendor.py
new file mode 100644
index 00000000..756f81f6
--- /dev/null
+++ b/packages/headless-agents/src/headless_agents/vendor.py
@@ -0,0 +1,204 @@
+"""The vendor rule: no reviewer shares a vendor with the code's author (spec 0.5.0 §3.8.4).
+
+Pure decisions over the state directory, no git: the review flow lists the
+commits of ``<merge-base>..<head>`` and their subjects, then calls
+:func:`attribute` and :func:`check_independence` under its locks.
+
+- A commit with provenance takes its recorded providers.
+- A ``chore(ha):`` commit without provenance -- a 0.4.0 write run, a lost state
+  directory, a subject typed by hand -- refuses the review: a 0.4.0 chain that
+  succeeded on its first link leaves no trace of the links it declared.
+- Any other commit without provenance is hand-written (``made_by: hand``, no
+  provider) only while ``unconfined-writers.json`` records no unconfined write;
+  otherwise it is attributed to the union of their providers (``made_by:
+  unknown``), because ``ha`` cannot see everything such a write did (§3.8.0).
+"""
+
+from __future__ import annotations
+
+import re
+from collections.abc import Mapping, Sequence
+from dataclasses import dataclass, field
+from pathlib import Path
+from typing import Final
+
+from . import provenance
+from .state import Unknown, read_optional
+from .write_flow import UNCONFINED_WRITERS
+
+#: The subject prefix of every commit ``ha`` makes (§3.6).
+HA_SUBJECT: Final = "chore(ha):"
+#: What a check records for each commit: the three of provenance, ``unknown`` for an
+#: unconfined writer's possible commit, ``hand`` for a presumed hand-written one.
+MADE_BY: Final = frozenset({"engine", "agent", "hook", "unknown", "hand"})
+_SHA: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
+
+
+class VendorRefused(Exception):  # noqa: N818 - a refusal, not a crash
+    """The review cannot prove independence: exit ``2``, naming why (§3.8.4)."""
+
+
+@dataclass(frozen=True)
+class AttributedCommit:
+    sha: str
+    #: The run that recorded it; ``None`` without provenance.
+    run_id: str | None
+    made_by: str
+    providers: tuple[str, ...]
+
+    def to_document(self) -> dict[str, object]:
+        return {
+            "sha": self.sha,
+            "run_id": self.run_id,
+            "made_by": self.made_by,
+            "providers": list(self.providers),
+        }
+
+
+@dataclass(frozen=True)
+class VendorCheck:
+    """What a review proved before its reviewers started (§3.8.6), ``vendor_check`` in reports."""
+
+    commits: tuple[AttributedCommit, ...]
+    #: The union of the providers attributed to the range, sorted.
+    authors: tuple[str, ...]
+    #: Every reviewer role with the providers of every link of its chain.
+    reviewers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
+
+    def to_document(self) -> dict[str, object]:
+        return {
+            "commits": [commit.to_document() for commit in self.commits],
+            "authors": list(self.authors),
+            "reviewers": {role: list(providers) for role, providers in self.reviewers.items()},
+        }
+
+    @classmethod
+    def from_document(cls, document: Mapping[str, object], *, where: str) -> VendorCheck:
+        """The check ``document`` states; :class:`Unknown` when it is not one ``ha`` writes."""
+
+        def texts(value: object) -> tuple[str, ...]:
+            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
+                raise Unknown(f"{where}: vendor check is malformed")
+            return tuple(value)
+
+        raw_commits, raw_reviewers = document.get("commits"), document.get("reviewers")
+        if not isinstance(raw_commits, list) or not isinstance(raw_reviewers, dict):
+            raise Unknown(f"{where}: vendor check is malformed")
+        commits = []
+        for raw in raw_commits:
+            if not isinstance(raw, dict):
+                raise Unknown(f"{where}: vendor check is malformed")
+            sha, run_id, made_by = raw.get("sha"), raw.get("run_id"), raw.get("made_by")
+            if (
+                not isinstance(sha, str)
+                or not _SHA.fullmatch(sha)
+                or not (run_id is None or isinstance(run_id, str))
+                or made_by not in MADE_BY
+            ):
+                raise Unknown(f"{where}: vendor check is malformed")
+            commits.append(
+                AttributedCommit(
+                    sha=sha,
+                    run_id=run_id,
+                    made_by=str(made_by),
+                    providers=texts(raw.get("providers")),
+                )
+            )
+        authors = texts(document.get("authors"))
+        reviewers = {str(role): texts(providers) for role, providers in raw_reviewers.items()}
+        return cls(commits=tuple(commits), authors=authors, reviewers=reviewers)
+
+
+def _unconfined_providers(state: Path) -> tuple[str, ...]:
+    """The union of the providers of every recorded unconfined write; ``()`` for none."""
+    path = state / UNCONFINED_WRITERS
+    try:
+        document = read_optional(path)
+    except Unknown as exc:
+        raise VendorRefused(
+            f"{path} is unknown ({exc}): a commit without provenance cannot be presumed "
+            "hand-written; recover the file by hand"
+        ) from None
+    if document is None:
+        return ()
+    writers = document.get("writers")
+    if not isinstance(writers, list):
+        raise VendorRefused(f"{path} is malformed: recover it by hand")
+    found: list[str] = []
+    for writer in writers:
+        providers = writer.get("providers") if isinstance(writer, dict) else None
+        if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
+            raise VendorRefused(f"{path} is malformed: recover it by hand")
+        found.extend(p for p in providers if p not in found)
+    return tuple(found)
+
+
+def attribute(state: Path, commits: Sequence[tuple[str, str]]) -> tuple[AttributedCommit, ...]:
+    """Every ``(sha, subject)`` of the range attributed (§3.8.4 step 4), in the given order."""
+    writers: tuple[str, ...] | None = None
+    attributed = []
+    for sha, subject in commits:
+        try:
+            record = provenance.lookup(state, sha)
+        except Unknown as exc:
+            raise VendorRefused(f"the provenance of {sha[:12]} is unknown ({exc})") from None
+        if record is not None:
+            providers = record.get("providers")
+            run_id = record.get("run_id")
+            attributed.append(
+                AttributedCommit(
+                    sha=sha,
+                    run_id=run_id if isinstance(run_id, str) else None,
+                    made_by=str(record.get("made_by")),
+                    providers=tuple(providers) if isinstance(providers, list) else (),
+                )
+            )
+            continue
+        if subject.startswith(HA_SUBJECT):
+            raise VendorRefused(
+                f"commit {sha[:12]} says {HA_SUBJECT} but has no provenance (a 0.4.0 write run, "
+                "a lost state directory, or a subject typed by hand): its authors cannot be proven"
+            )
+        if writers is None:
+            writers = _unconfined_providers(state)
+        made_by = "unknown" if writers else "hand"
+        attributed.append(
+            AttributedCommit(sha=sha, run_id=None, made_by=made_by, providers=writers)
+        )
+    return tuple(attributed)
+
+
+def check_independence(
+    commits: Sequence[AttributedCommit], reviewers: Mapping[str, Sequence[str]]
+) -> VendorCheck:
+    """The check, or :class:`VendorRefused` naming the reviewer, provider and commit (step 5).
+
+    The judge is not constrained: only the reviewers are passed here.
+    """
+    authors: list[str] = []
+    for commit in commits:
+        authors.extend(p for p in commit.providers if p not in authors)
+    for role, providers in reviewers.items():
+        for provider in providers:
+            if provider in authors:
+                sha = next(c.sha for c in commits if provider in c.providers)
+                raise VendorRefused(
+                    f"reviewer {role} runs {provider}, which wrote commit {sha[:12]} of the "
+                    "reviewed range: a reviewer must not share a vendor with the code's author"
+                )
+    return VendorCheck(
+        commits=tuple(commits),
+        authors=tuple(sorted(authors)),
+        reviewers={role: tuple(providers) for role, providers in reviewers.items()},
+    )
+
+
+__all__ = [
+    "AttributedCommit",
+    "HA_SUBJECT",
+    "MADE_BY",
+    "VendorCheck",
+    "VendorRefused",
+    "attribute",
+    "check_independence",
+]
```

- [ ] **Step 4: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_vendor.py -q -p no:cacheprovider`
Expected: `test_vendor.py: 16 passed`. Then the gates of the Global Constraints.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/vendor.py tests/unit/headless_agents/test_vendor.py
git commit -m "feat(headless-agents): the vendor rule -- every commit of a range attributed, reviewers independent"
```

### Task 3: a review's check and result, each written once

**Files:**
- Create: `packages/headless-agents/src/headless_agents/reviews.py`
- Create: `tests/unit/headless_agents/test_reviews.py`

**Interfaces:**
- Consumes: `VendorCheck` (Task 2), `Verdict` (Task 1), `state.create_once`, `state.read`.
- Produces: `check_path`, `result_path`, `write_check(state, run_id, check)`,
  `load_check(state, run_id) -> VendorCheck | None`, `write_result(state, run_id, *, head,
  verdict, text, check)`, `load_result(state, run_id) -> ReviewResult` (`Unknown` when missing
  or malformed); `ReviewResult(run_id, head, verdict, text, check)`.

- [ ] **Step 1: Write the failing tests.** Apply (`git apply`):

```diff
diff --git a/tests/unit/headless_agents/test_reviews.py b/tests/unit/headless_agents/test_reviews.py
new file mode 100644
index 00000000..c94d6a3c
--- /dev/null
+++ b/tests/unit/headless_agents/test_reviews.py
@@ -0,0 +1,82 @@
+"""A review's records in the state directory: its check, then its result, each written once
+(spec 0.5.0 §3.8.1, §3.8.4 step 6, §3.8.6)."""
+
+from __future__ import annotations
+
+import json
+from pathlib import Path
+
+import pytest
+
+from headless_agents import reviews
+from headless_agents.state import Unknown
+from headless_agents.vendor import AttributedCommit, VendorCheck
+
+RUN = "20260926T120000-cccccccc"
+HEAD = "a" * 40
+CHECK = VendorCheck(
+    commits=(AttributedCommit(sha="1" * 40, run_id=None, made_by="hand", providers=()),),
+    authors=(),
+    reviewers={"reviewer-codex": ("codex",)},
+)
+
+
+def test_a_check_is_written_once_and_read_back(tmp_path: Path) -> None:
+    reviews.write_check(tmp_path, RUN, CHECK)
+    assert reviews.load_check(tmp_path, RUN) == CHECK
+    with pytest.raises(FileExistsError):
+        reviews.write_check(tmp_path, RUN, CHECK)
+
+
+def test_a_result_is_written_once_and_read_back(tmp_path: Path) -> None:
+    reviews.write_result(
+        tmp_path, RUN, head=HEAD, verdict="changes", text="fix\nVERDICT: CHANGES", check=CHECK
+    )
+    result = reviews.load_result(tmp_path, RUN)
+    assert result == reviews.ReviewResult(
+        run_id=RUN, head=HEAD, verdict="changes", text="fix\nVERDICT: CHANGES", check=CHECK
+    )
+    with pytest.raises(FileExistsError):
+        reviews.write_result(tmp_path, RUN, head=HEAD, verdict="approve", text="x", check=CHECK)
+
+
+def test_a_missing_result_is_unknown_never_empty(tmp_path: Path) -> None:
+    with pytest.raises(Unknown, match="missing"):
+        reviews.load_result(tmp_path, RUN)
+
+
+def test_a_missing_check_is_none(tmp_path: Path) -> None:
+    """A review refused before its check: no check was ever written."""
+    assert reviews.load_check(tmp_path, RUN) is None
+
+
+@pytest.mark.parametrize(
+    ("key", "value"),
+    [
+        ("run_id", "20260926T120000-dddddddd"),
+        ("head", "not-a-sha"),
+        ("verdict", "maybe"),
+        ("verdict", None),
+        ("text", 3),
+        ("vendor_check", {"commits": "x"}),
+    ],
+)
+def test_a_result_ha_never_writes_is_unknown(tmp_path: Path, key: str, value: object) -> None:
+    reviews.write_result(tmp_path, RUN, head=HEAD, verdict="approve", text="ok", check=CHECK)
+    path = reviews.result_path(tmp_path, RUN)
+    document = json.loads(path.read_text())
+    document[key] = value
+    path.chmod(0o600)
+    path.write_text(json.dumps(document))
+    with pytest.raises(Unknown):
+        reviews.load_result(tmp_path, RUN)
+
+
+def test_the_records_live_under_reviews(tmp_path: Path) -> None:
+    assert reviews.check_path(tmp_path, RUN) == tmp_path / "reviews" / f"{RUN}.check.json"
+    assert reviews.result_path(tmp_path, RUN) == tmp_path / "reviews" / f"{RUN}.json"
+
+
+def test_a_malformed_run_id_is_a_programming_error(tmp_path: Path) -> None:
+    with pytest.raises(ValueError, match="run id"):
+        reviews.result_path(tmp_path, "../x")
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_reviews.py -q -p no:cacheprovider`
Expected: `test_reviews.py: 1 error` — every failure is the missing feature:
  - `test_reviews.py -- (collection) ImportError: cannot import name 'reviews' from 'headless_agents' (packages/headless-agents/src/headless_agents/__init__.py)`

- [ ] **Step 3: Implement.** Apply:

```diff
diff --git a/packages/headless-agents/src/headless_agents/reviews.py b/packages/headless-agents/src/headless_agents/reviews.py
new file mode 100644
index 00000000..c17ebc3e
--- /dev/null
+++ b/packages/headless-agents/src/headless_agents/reviews.py
@@ -0,0 +1,125 @@
+"""A review's records in the state directory (spec 0.5.0 §3.8.1, §3.8.4 step 6, §3.8.6).
+
+``<state>/reviews/<run_id>.check.json`` -- the vendor check, written once
+before the reviewers start; ``<state>/reviews/<run_id>.json`` -- the result,
+written once when the verdict is read: the pinned head, the verdict, the
+deciding text and a copy of the check. They are the one authority for a
+review's head, verdict and text: ``run.json`` only copies them, and
+``--findings`` reads the result only, so it still works after ``ha clean``.
+A record that is missing when it should exist, does not parse, or holds what
+``ha`` never writes is :class:`~headless_agents.state.Unknown`.
+"""
+
+from __future__ import annotations
+
+import re
+from dataclasses import dataclass
+from pathlib import Path
+from typing import Final
+
+from .runs import RUN_ID_PATTERN
+from .state import Unknown, create_once, read, read_optional
+from .templates import Verdict
+from .vendor import VendorCheck
+
+REVIEWS_DIR: Final = "reviews"
+VERDICTS: Final = ("approve", "changes")
+_SHA: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
+
+
+@dataclass(frozen=True)
+class ReviewResult:
+    run_id: str
+    #: The exact commit the review read (§3.5).
+    head: str
+    verdict: Verdict
+    #: The deciding text: the judge's, or the only reviewer's.
+    text: str
+    check: VendorCheck
+
+
+def _checked(run_id: str) -> str:
+    if not RUN_ID_PATTERN.fullmatch(run_id):
+        raise ValueError(f"not a run id: {run_id!r}")
+    return run_id
+
+
+def check_path(state: Path, run_id: str) -> Path:
+    return state / REVIEWS_DIR / f"{_checked(run_id)}.check.json"
+
+
+def result_path(state: Path, run_id: str) -> Path:
+    return state / REVIEWS_DIR / f"{_checked(run_id)}.json"
+
+
+def write_check(state: Path, run_id: str, check: VendorCheck) -> None:
+    """Write the check once; :class:`FileExistsError` if it exists."""
+    create_once(check_path(state, run_id), {"run_id": run_id, "vendor_check": check.to_document()})
+
+
+def load_check(state: Path, run_id: str) -> VendorCheck | None:
+    """The check; ``None`` when none was written (a review refused before it)."""
+    path = check_path(state, run_id)
+    document = read_optional(path, expect_id=("run_id", run_id))
+    if document is None:
+        return None
+    raw = document.get("vendor_check")
+    if not isinstance(raw, dict):
+        raise Unknown(f"{path}: vendor_check is malformed")
+    return VendorCheck.from_document(raw, where=str(path))
+
+
+def write_result(
+    state: Path, run_id: str, *, head: str, verdict: Verdict, text: str, check: VendorCheck
+) -> None:
+    """Write the result once, before the cleanup (§3.8.4 step 6); :class:`FileExistsError`."""
+    create_once(
+        result_path(state, run_id),
+        {
+            "run_id": run_id,
+            "head": head,
+            "verdict": verdict,
+            "text": text,
+            "vendor_check": check.to_document(),
+        },
+    )
+
+
+def load_result(state: Path, run_id: str) -> ReviewResult:
+    """The result; :class:`Unknown` when missing or not one ``ha`` writes."""
+    path = result_path(state, run_id)
+    document = read(path, expect_id=("run_id", run_id))
+    head, verdict, text, raw = (
+        document.get("head"),
+        document.get("verdict"),
+        document.get("text"),
+        document.get("vendor_check"),
+    )
+    if not isinstance(head, str) or not _SHA.fullmatch(head):
+        raise Unknown(f"{path}: head is malformed")
+    if verdict not in VERDICTS:
+        raise Unknown(f"{path}: verdict is malformed")
+    if not isinstance(text, str):
+        raise Unknown(f"{path}: text is malformed")
+    if not isinstance(raw, dict):
+        raise Unknown(f"{path}: vendor_check is malformed")
+    return ReviewResult(
+        run_id=run_id,
+        head=head,
+        verdict="approve" if verdict == "approve" else "changes",
+        text=text,
+        check=VendorCheck.from_document(raw, where=str(path)),
+    )
+
+
+__all__ = [
+    "REVIEWS_DIR",
+    "ReviewResult",
+    "VERDICTS",
+    "check_path",
+    "load_check",
+    "load_result",
+    "result_path",
+    "write_check",
+    "write_result",
+]
```

- [ ] **Step 4: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_reviews.py -q -p no:cacheprovider`
Expected: `test_reviews.py: 12 passed`. Then the gates of the Global Constraints.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/reviews.py tests/unit/headless_agents/test_reviews.py
git commit -m "feat(headless-agents): a review's check and result, each written once in the state directory"
```

### Task 4: a review run: planned and executed, with the vendor rule

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/engine.py`
- Modify: `packages/headless-agents/src/headless_agents/report.py`
- Create: `packages/headless-agents/src/headless_agents/review_flow.py`
- Modify: `packages/headless-agents/src/headless_agents/write_flow.py`
- Modify: `tests/unit/headless_agents/test_engine_plan.py`
- Create: `tests/unit/headless_agents/test_review.py`

**Interfaces:**
- Consumes: Tasks 1–3; `write_flow.check_unconfined_intent`, `quarantine.check`,
  `lineage.of_repository`, `lineage.registry_lock`, `lineage.lineage_lock`, `gitops.git`,
  `engine._run_links` (unchanged), `engine._check_isolation`, `engine._check_prompt_size`.
- Produces: `Request.head`, `Request.review_run`; `SlotPlan`; `Plan.panel`, `Plan.reviews`,
  `Plan.reviewed_lineage`; `REVIEW_DEFAULT_TASK`; `review_flow.prepare(...) -> Prepared`,
  `review_flow.finish(...) -> cleanup`, `review_flow.CHANGES_EXIT_CODE = 6`,
  `review_flow.ReviewRefused`; `report.refused_step_entry`; `write_flow.unfinalized` and
  `write_flow.source_lineages` made public (P3–P6).

- [ ] **Step 1: Write the failing tests.** Apply (`git apply`):

```diff
diff --git a/tests/unit/headless_agents/test_engine_plan.py b/tests/unit/headless_agents/test_engine_plan.py
index 2d0970cd..88ed0337 100644
--- a/tests/unit/headless_agents/test_engine_plan.py
+++ b/tests/unit/headless_agents/test_engine_plan.py
@@ -14,7 +14,7 @@ from pathlib import Path
 
 import pytest
 
-from headless_agents.engine import Overrides, Request, UsageError, plan
+from headless_agents.engine import REVIEW_DEFAULT_TASK, Overrides, Request, UsageError, plan
 from headless_agents.registry import max_prompt_bytes
 from headless_agents.runs import Registry
 from headless_agents.templates import implement_prompt
@@ -58,6 +58,8 @@ class Env:
         repo: Path | None = None,
         run_dir: Path | None = None,
         continue_run: str | None = None,
+        head: str | None = None,
+        review_run: str | None = None,
     ) -> Request:
         return Request(
             target=target,
@@ -68,6 +70,8 @@ class Env:
             repo=repo,
             run_dir=run_dir,
             continue_run=continue_run,
+            head=head,
+            review_run=review_run,
             cwd=self.cwd,
             environ={"PATH": "/usr/bin:/bin", "HOME": str(self.home), **self.environ},
             home=self.home,
@@ -301,11 +305,104 @@ def test_a_workflow_target_refuses_every_override(
         plan(env.request("build", "task", overrides=overrides))
 
 
-def test_a_review_workflow_is_refused_until_its_shape_ships(env: Env) -> None:
-    """Spec §5, lot 4: no lot exposes a review that does not enforce the vendor rule."""
-    _workflows(env)
-    with pytest.raises(UsageError, match=r"review shape is not available.*ha run reviewer"):
-        plan(env.request("check", "task"))
+# ── a review target (lot 4) ────────────────────────────────────────────────
+
+
+def _panel(env: Env) -> None:
+    env.roles(
+        '[implementer]\nprovider = "codex"\nwrite = true\n\n'
+        '[reviewer]\nprovider = "claude"\n\n'
+        '[reviewer-oc]\nprovider = "opencode"\n\n'
+        '[judge]\nprovider = "codex"\n'
+    )
+    (env.config / "workflows.toml").write_text(
+        '[build]\nshape = "implement"\nimplement = "implementer"\n\n'
+        '[check]\nshape = "review"\nreview = "reviewer"\n\n'
+        '[panel]\nshape = "review"\nreview = ["reviewer", "reviewer-oc"]\njudge = "judge"\n'
+    )
+
+
+def test_a_review_target_plans_every_slot_of_its_panel_in_order(
+    env: Env, no_subprocess: list[object]
+) -> None:
+    """Spec §3.5: the reviewers, then the judge; plan() runs no git (§3.8.2)."""
+    _panel(env)
+    planned = plan(env.request("panel", "Look at the error paths."))
+    assert planned.workflow is not None and planned.workflow.shape == "review"
+    assert [(s.slot, s.role.name) for s in planned.panel] == [
+        ("review", "reviewer"),
+        ("review", "reviewer-oc"),
+        ("judge", "judge"),
+    ]
+    assert [dict(s.models) for s in planned.panel] == [
+        {"claude": "claude-default"},
+        {"opencode": "oc-default"},
+        {"codex": "codex-default"},
+    ]
+    assert planned.task == "Look at the error paths."
+    assert not planned.role.write
+
+
+@pytest.mark.parametrize("prompt", [None, "", "  \n"])
+def test_a_review_without_a_prompt_reviews_the_change(env: Env, prompt: str | None) -> None:
+    """Spec §3.9: a review's prompt is optional; on a terminal it is not refused."""
+    _panel(env)
+    planned = plan(env.request("check", prompt, stdin_is_tty=True))
+    assert planned.task == REVIEW_DEFAULT_TASK == "Review this change."
+
+
+def test_run_names_an_implement_run_and_its_lineage(env: Env, no_subprocess: list[object]) -> None:
+    _panel(env)
+    env.register(RUN, target=IMPLEMENT, lineage=OWNER)
+    planned = plan(env.request("check", None, review_run=RUN))
+    assert planned.reviews == RUN and planned.reviewed_lineage == OWNER
+
+
+@pytest.mark.parametrize("given", [{"head": "HEAD~1"}, {"base": "main"}])
+def test_run_excludes_head_and_base(env: Env, given: dict[str, str]) -> None:
+    _panel(env)
+    env.register(RUN, target=IMPLEMENT, lineage=OWNER)
+    with pytest.raises(UsageError, match="--run excludes --head and --base"):
+        plan(env.request("check", None, review_run=RUN, **given))  # type: ignore[arg-type]
+
+
+def test_run_refuses_a_run_that_is_not_an_implement_run(env: Env) -> None:
+    _panel(env)
+    env.register(RUN, target={"kind": "provider", "name": "codex"}, lineage=None)
+    with pytest.raises(UsageError, match=f"--run {RUN}: not an implement run"):
+        plan(env.request("check", None, review_run=RUN))
+
+
+@pytest.mark.parametrize(("run_id", "rule"), [("nope", "not a run id"), (RUN, f"no run {RUN}")])
+def test_run_refuses_what_names_no_registered_run(env: Env, run_id: str, rule: str) -> None:
+    _panel(env)
+    with pytest.raises(UsageError, match=rule):
+        plan(env.request("check", None, review_run=run_id))
+
+
+@pytest.mark.parametrize(
+    ("target", "fields", "rule"),
+    [
+        ("build", {"head": "HEAD"}, "--head needs a review workflow"),
+        ("build", {"review_run": RUN}, "--run needs a review workflow"),
+        ("codex", {"head": "HEAD"}, "--head needs a review workflow"),
+        ("codex", {"review_run": RUN}, "--run needs a review workflow"),
+        ("check", {"continue_run": RUN}, "--continue needs an implement workflow"),
+    ],
+)
+def test_each_option_belongs_to_its_shape(
+    env: Env, target: str, fields: dict[str, str], rule: str
+) -> None:
+    """Spec §3.9: --head and --run are the review's; --continue the implement's."""
+    _panel(env)
+    with pytest.raises(UsageError, match=rule):
+        plan(env.request(target, "task", **fields))  # type: ignore[arg-type]
+
+
+def test_a_review_refuses_every_override_too(env: Env) -> None:
+    _panel(env)
+    with pytest.raises(UsageError, match="runs its roles as declared, so -m is refused"):
+        plan(env.request("check", None, overrides=Overrides(model="m")))
 
 
 def test_an_implement_workflow_needs_a_task(env: Env) -> None:
diff --git a/tests/unit/headless_agents/test_review.py b/tests/unit/headless_agents/test_review.py
new file mode 100644
index 00000000..014333d2
--- /dev/null
+++ b/tests/unit/headless_agents/test_review.py
@@ -0,0 +1,599 @@
+"""The shape ``review`` through the engine: the vendor rule, the panel, the verdict, the
+records and the cleanup (spec 0.5.0 §3.5, §3.8.4, §3.8.6, §3.10; lot 4).
+
+The providers are fakes -- a writer edits its workspace, a reader answers a text
+-- over a real git repository and the real engine, state directory and reports.
+"""
+
+from __future__ import annotations
+
+import json
+import os
+import subprocess
+import threading
+from collections.abc import Callable
+from dataclasses import dataclass, field, replace
+from pathlib import Path
+
+import pytest
+
+from headless_agents import engine, lineage, locks, provenance, quarantine, reviews
+from headless_agents.engine import Overrides, Request, UsageError, execute, plan
+from headless_agents.proofs import CLI_RAILS, record_proof
+from headless_agents.registry import Probe
+from headless_agents.result import RunResult
+from headless_agents.run_record import record, run_id_of
+from headless_agents.runs import Registry
+from headless_agents.spec import RunSpec
+from headless_agents.state import publish
+
+GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
+APPROVE = "Looks right.\nVERDICT: APPROVE"
+CHANGES = "src/app.py:2 high: the flag is never read.\nVERDICT: CHANGES"
+
+
+def _git(cwd: Path, *args: str) -> str:
+    return subprocess.run(
+        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV
+    ).stdout
+
+
+@dataclass
+class _Agent:
+    """A fake rail: a writer applies ``edit`` to its writable workspace; a reader answers
+    ``answer`` -- a text, or a function of the spec -- with exit ``code``."""
+
+    name: str
+    edit: Callable[[Path], None] | None = None
+    answer: str | Callable[[RunSpec], str] | None = APPROVE
+    code: int = 0
+    barrier: threading.Barrier | None = None
+    specs: list[RunSpec] = field(default_factory=list)
+
+    def run(self, spec: RunSpec) -> RunResult:
+        self.specs.append(spec)
+        workspace = spec.profile.workspace
+        assert workspace is not None
+        if self.barrier is not None:
+            self.barrier.wait(timeout=5)
+        text: str | None
+        if workspace.write:
+            assert self.edit is not None
+            self.edit(workspace.path)
+            text = "I did it"
+        else:
+            text = self.answer(spec) if callable(self.answer) else self.answer
+        spec = spec.with_run_dir_defaults()
+        return record(
+            spec,
+            RunResult(
+                exit_code=self.code,
+                provider=self.name,
+                model=spec.model,
+                model_reported=f"{self.name}-served",
+                report_path=spec.report_log,
+                events_log=spec.events_log,
+                tokens=None,
+                duration_seconds=0.1,
+                tool_call_completed=False,
+                text=text if self.code == 0 else None,
+                run_id=run_id_of(spec),
+            ),
+        )
+
+
+ROLES = """\
+[implementer]
+provider = "codex"
+write = true
+
+[reviewer]
+provider = "claude"
+
+[reviewer-agy]
+provider = "agy"
+
+[reviewer-oc]
+provider = "opencode"
+
+[reviewer-codex]
+provider = "codex"
+
+[judge]
+provider = "claude"
+"""
+
+WORKFLOWS = """\
+[build]
+shape = "implement"
+implement = "implementer"
+
+[check]
+shape = "review"
+review = "reviewer"
+
+[self-check]
+shape = "review"
+review = "reviewer-codex"
+
+[panel]
+shape = "review"
+review = ["reviewer", "reviewer-agy", "reviewer-oc"]
+judge = "judge"
+"""
+
+
+@dataclass
+class World:
+    home: Path
+    repo: Path
+    agents: dict[str, _Agent]
+    said: list[str] = field(default_factory=list)
+
+    @property
+    def state(self) -> Path:
+        return (self.home / ".local" / "state" / "ha").resolve()
+
+    def registry(self) -> Registry:
+        return Registry(self.state, runs_root=self.home / ".cache" / "ha" / "runs")
+
+    def request(self, target: str, prompt: str | None, **fields: object) -> Request:
+        request = Request(
+            target=target,
+            prompt=prompt,
+            stdin_is_tty=False,
+            overrides=Overrides(),
+            base=None,
+            repo=None,
+            run_dir=None,
+            cwd=self.repo,
+            environ={"PATH": os.environ["PATH"], "HOME": str(self.home)},
+            home=self.home,
+        )
+        return replace(request, **fields)  # type: ignore[arg-type]
+
+    def review(
+        self, target: str = "check", prompt: str | None = None, **fields: object
+    ) -> engine.Outcome:
+        return execute(plan(self.request(target, prompt, **fields)), say=self.said.append)
+
+    def implement(self, task: str = "Add a flag.", **fields: object) -> engine.Outcome:
+        outcome = execute(plan(self.request("build", task, **fields)), say=self.said.append)
+        assert outcome.exit_code == 0, self.said
+        return outcome
+
+    def commit_by_hand(self, text: str = "print('v2')\n", subject: str = "feat: by hand") -> str:
+        (self.repo / "app.py").write_text(text)
+        _git(self.repo, "commit", "-qam", subject)
+        return _git(self.repo, "rev-parse", "HEAD").strip()
+
+
+@pytest.fixture
+def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
+    home = tmp_path / "home"
+    config = home / ".config" / "ha"
+    config.mkdir(parents=True)
+    (config / "models.toml").write_text(
+        'codex = "codex-m"\nclaude = "claude-m"\nopencode = "oc-m"\nagy = "agy-m"\n'
+    )
+    (config / "roles.toml").write_text(ROLES)
+    (config / "workflows.toml").write_text(WORKFLOWS)
+    repo = tmp_path / "repo"
+    repo.mkdir()
+    _git(repo, "init", "-q", "-b", "main")
+    _git(repo, "config", "user.name", "Op")
+    _git(repo, "config", "user.email", "op@example.test")
+    (repo / "app.py").write_text("print('v1')\n")
+    _git(repo, "add", "app.py")
+    _git(repo, "commit", "-q", "-m", "init")
+    # origin/HEAD, the default --base (§3.5), at the first commit.
+    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
+    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
+    agents = {name: _Agent(name) for name in ("codex", "claude", "agy", "opencode")}
+    agents["codex"].edit = lambda root: (root / "app.py").write_text("print('v1')\nFLAG = 1\n")
+    monkeypatch.setattr(engine, "get_provider", lambda name: agents[name])
+    monkeypatch.setattr(
+        engine,
+        "probe",
+        lambda name, **_: Probe(available=True, detail="fake", version=f"{name} 1.0"),
+    )
+    state = (home / ".local" / "state" / "ha").resolve()
+    for rail in CLI_RAILS:
+        record_proof(
+            state, rail, version=f"{rail} 1.0", isolation=True, confinement=True, today="2026-09-26"
+        )
+    return World(home=home, repo=repo, agents=agents)
+
+
+def _report(outcome: engine.Outcome) -> dict[str, object]:
+    return json.loads((outcome.run_dir / "run.json").read_text())
+
+
+# ── one reviewer ────────────────────────────────────────────────────────────
+
+
+def test_an_approving_review_records_its_head_verdict_check_and_cleanup(world: World) -> None:
+    head = world.commit_by_hand()
+    outcome = world.review()
+    assert outcome.exit_code == 0, world.said
+    run_id = outcome.run_id
+    result = reviews.load_result(world.state, run_id)
+    assert (result.head, result.verdict, result.text) == (head, "approve", APPROVE)
+    assert result.check == reviews.load_check(world.state, run_id)
+    report = _report(outcome)
+    assert report["status"] == "approved" and report["verdict"] == "approve"
+    assert report["head"] == head and report["text"] == APPROVE
+    assert report["vendor_check"] == result.check.to_document()
+    assert report["cleanup"] == {"status": "done"}
+    assert report["branch"] is None and report["commits"] is None
+    assert not (outcome.run_dir / "wt").exists()
+    assert "print('v2')" in (outcome.run_dir / "change.patch").read_text()
+    assert world.registry().resolve(run_id).status == "approved"
+    (step,) = report["steps"]  # type: ignore[misc]
+    assert (step["slot"], step["role"], step["verdict"]) == ("review", "reviewer", "approve")
+    assert step["dir"] == "steps/01-review-reviewer"
+
+
+def test_a_review_asking_for_changes_exits_6(world: World) -> None:
+    world.commit_by_hand()
+    world.agents["claude"].answer = CHANGES
+    outcome = world.review()
+    assert outcome.exit_code == 6
+    assert _report(outcome)["status"] == "changes"
+    assert reviews.load_result(world.state, outcome.run_id).verdict == "changes"
+
+
+def test_the_reviewer_reads_the_pinned_commit_read_only_with_the_diff(world: World) -> None:
+    head = world.commit_by_hand()
+    world.review(prompt="Mind the flag.")
+    (spec,) = world.agents["claude"].specs
+    workspace = spec.profile.workspace
+    assert workspace is not None and not workspace.write
+    assert "<task>\nMind the flag.\n</task>" in spec.prompt
+    assert "+print('v2')" in spec.prompt and spec.prompt.endswith("anything must change first.\n")
+    assert workspace.path.name == "wt"
+    assert head  # the worktree was detached at it; it is gone once the review ends
+
+
+def test_an_unreadable_verdict_fails_and_writes_no_result(world: World) -> None:
+    world.commit_by_hand()
+    world.agents["claude"].answer = "I think it is fine."
+    outcome = world.review()
+    assert outcome.exit_code == 1
+    report = _report(outcome)
+    assert report["failure_reason"] == "unreadable_verdict" and report["status"] == "failed"
+    assert not reviews.result_path(world.state, outcome.run_id).exists()
+    assert report["cleanup"] == {"status": "done"}
+
+
+def test_a_failing_reviewer_fails_the_review(world: World) -> None:
+    world.commit_by_hand()
+    world.agents["claude"].code = 3
+    outcome = world.review()
+    assert outcome.exit_code == 1
+    report = _report(outcome)
+    assert report["failure_reason"] == "step_failed"
+    assert report["steps"][0]["exit_code"] == 3  # type: ignore[index]
+
+
+def test_a_reviewer_answering_nothing_fails_the_review(world: World) -> None:
+    world.commit_by_hand()
+    world.agents["claude"].answer = "  \n"
+    outcome = world.review()
+    assert outcome.exit_code == 1 and _report(outcome)["failure_reason"] == "step_failed"
+
+
+def test_an_empty_diff_is_refused_nothing_to_review(world: World) -> None:
+    with pytest.raises(UsageError, match="nothing to review"):
+        world.review()
+    assert world.agents["claude"].specs == []
+
+
+def test_a_base_that_does_not_resolve_is_refused(world: World) -> None:
+    world.commit_by_hand()
+    with pytest.raises(UsageError, match="--base no-such-ref does not resolve"):
+        world.review(base="no-such-ref")
+
+
+def test_head_and_base_name_the_reviewed_range(world: World) -> None:
+    first = world.commit_by_hand("print('v2')\n", "feat: one")
+    world.commit_by_hand("print('v3')\n", "feat: two")
+    outcome = world.review(head=first, base="main~2")
+    report = _report(outcome)
+    assert report["head"] == first
+    assert "print('v3')" not in (outcome.run_dir / "change.patch").read_text()
+
+
+# ── a panel ─────────────────────────────────────────────────────────────────
+
+
+def test_a_panel_runs_its_reviewers_concurrently_then_the_judge_decides(world: World) -> None:
+    world.commit_by_hand()
+    barrier = threading.Barrier(3)
+    for name in ("agy", "opencode"):
+        world.agents[name].barrier = barrier
+    world.agents["agy"].answer = CHANGES
+    judged: list[str] = []
+
+    def judge_or_review(spec: RunSpec) -> str:
+        if "Judge the reviews below" in spec.prompt:
+            judged.append(spec.prompt)
+            return "Only the flag finding stands.\n**VERDICT: CHANGES**"
+        barrier.wait(timeout=5)
+        return APPROVE
+
+    world.agents["claude"].answer = judge_or_review
+    outcome = world.review("panel")
+    assert outcome.exit_code == 6, world.said
+    report = _report(outcome)
+    steps = report["steps"]
+    assert [(s["index"], s["slot"], s["role"], s["verdict"]) for s in steps] == [  # type: ignore[union-attr]
+        (1, "review", "reviewer", "approve"),
+        (2, "review", "reviewer-agy", "changes"),
+        (3, "review", "reviewer-oc", "approve"),
+        (4, "judge", "judge", "changes"),
+    ]
+    (prompt,) = judged
+    assert '<review role="reviewer-agy" provider="agy" model="agy-served">' in prompt
+    assert report["text"] == "Only the flag finding stands.\n**VERDICT: CHANGES**"
+
+
+def test_one_failing_reviewer_stops_the_panel_before_the_judge(world: World) -> None:
+    world.commit_by_hand()
+    world.agents["opencode"].code = 1
+    outcome = world.review("panel")
+    assert outcome.exit_code == 1
+    judged = [s for s in world.agents["claude"].specs if "Judge the reviews" in s.prompt]
+    assert judged == []
+    assert [s["slot"] for s in _report(outcome)["steps"]] == ["review"] * 3  # type: ignore[union-attr]
+
+
+# ── the vendor rule (§3.8.4) ────────────────────────────────────────────────
+
+
+def test_a_reviewer_sharing_a_vendor_with_the_implementer_is_refused(world: World) -> None:
+    built = world.implement()
+    with pytest.raises(UsageError, match=r"reviewer reviewer-codex runs codex.*wrote commit"):
+        world.review("self-check", review_run=built.run_id)
+    assert world.agents["codex"].specs[1:] == []
+
+
+def test_an_independent_reviewer_reviews_an_implement_run(world: World) -> None:
+    built = world.implement()
+    outcome = world.review("check", review_run=built.run_id)
+    assert outcome.exit_code == 0, world.said
+    check = _report(outcome)["vendor_check"]
+    assert check["authors"] == ["codex"]  # type: ignore[index]
+    assert check["reviewers"] == {"reviewer": ["claude"]}  # type: ignore[index]
+    assert check["commits"][0]["run_id"] == built.run_id  # type: ignore[index]
+
+
+def test_a_hand_written_commit_constrains_nothing(world: World) -> None:
+    world.commit_by_hand()
+    outcome = world.review("self-check")
+    assert outcome.exit_code == 0
+    assert _report(outcome)["vendor_check"]["commits"][0]["made_by"] == "hand"  # type: ignore[index]
+
+
+def test_a_chore_ha_commit_without_provenance_is_refused(world: World) -> None:
+    world.commit_by_hand(subject="chore(ha): 20260101T000000-deadbeef implement via codex/m")
+    with pytest.raises(UsageError, match=r"chore\(ha\).*no provenance"):
+        world.review()
+
+
+def test_after_an_unconfined_write_a_commit_without_provenance_is_its_writers(
+    world: World,
+) -> None:
+    publish(
+        world.state / "unconfined-writers.json",
+        {"writers": [{"run_id": "x", "repository": str(world.repo), "providers": ["claude"]}]},
+    )
+    world.commit_by_hand()
+    with pytest.raises(UsageError, match="reviewer reviewer runs claude"):
+        world.review()
+
+
+def test_run_reviews_the_current_tip_after_the_branch_advanced(world: World) -> None:
+    """§3.5: --run stands for the lineage's current tip, later continuations included."""
+    built = world.implement()
+    world.agents["codex"].edit = lambda root: (root / "app.py").write_text(
+        "print('v1')\nFLAG = 2\n"
+    )
+    world.implement("More.", continue_run=built.run_id)
+    tip = _git(world.repo, "rev-parse", f"ha/{built.run_id}").strip()
+    outcome = world.review("check", review_run=built.run_id)
+    report = _report(outcome)
+    assert report["head"] == tip
+    assert "FLAG = 2" in (outcome.run_dir / "change.patch").read_text()
+    assert len(report["vendor_check"]["commits"]) == 2  # type: ignore[index]
+
+
+def test_run_of_a_lineage_whose_branch_is_gone_is_refused(world: World) -> None:
+    built = world.implement()
+    _git(world.repo, "worktree", "remove", "--force", str(built.run_dir / "wt"))
+    _git(world.repo, "branch", "-D", f"ha/{built.run_id}")
+    with pytest.raises(UsageError, match="no longer exists"):
+        world.review("check", review_run=built.run_id)
+
+
+def test_every_lineage_of_the_repository_is_locked_shared_ascending_before_the_first_git(
+    world: World, monkeypatch: pytest.MonkeyPatch
+) -> None:
+    """§4: a review's lock order -- the lineage registry lock shared, then every lineage of
+    its repository shared, in ascending owner order, before its first git command."""
+    from headless_agents import review_flow
+
+    first = world.implement()
+    world.agents["codex"].edit = lambda root: (root / "other.py").write_text("x = 1\n")
+    second = world.implement("Another.")
+    world.commit_by_hand()
+    events: list[str] = []
+    real_held, real_git = locks.held, review_flow.git
+
+    def held(path: Path, *, rank: locks.Rank, **kwargs: object):  # type: ignore[no-untyped-def]
+        mode = "ex" if kwargs.get("exclusive") else "sh"
+        events.append(f"lock {rank.name} {path.name} {mode}")
+        return real_held(path, rank=rank, **kwargs)  # type: ignore[arg-type]
+
+    def git(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
+        events.append("git")
+        return real_git(*args, **kwargs)  # type: ignore[arg-type]
+
+    monkeypatch.setattr(review_flow, "held", held)
+    monkeypatch.setattr(review_flow, "git", git)
+    assert world.review().exit_code == 0
+    before_git = events[: events.index("git")]
+    owners = sorted([first.run_id, second.run_id])
+    assert before_git == [
+        "lock LINEAGE_REGISTRY lineages.lock sh",
+        *(f"lock LINEAGE {owner}.lock sh" for owner in owners),
+    ]
+
+
+# ── uncertainty refuses (§3.8.4 step 2) ─────────────────────────────────────
+
+
+def test_a_compromised_lineage_of_the_repository_refuses_any_review(world: World) -> None:
+    built = world.implement()
+    current = lineage.load(world.state, built.run_id)
+    lineage.save(world.state, replace(current, compromised="tripwire"))
+    world.commit_by_hand()
+    with pytest.raises(UsageError, match=rf"lineage {built.run_id} is compromised \(tripwire\)"):
+        world.review()
+
+
+def test_a_stale_pending_write_refuses_and_quarantines_the_repository(world: World) -> None:
+    built = world.implement()
+    current = lineage.load(world.state, built.run_id)
+    pending = lineage.PendingWrite(
+        run_id="20260926T130000-eeeeeeee",
+        providers=("codex",),
+        unconfined=False,
+        start_tip=None,
+        start_reflog=None,
+    )
+    lineage.save(world.state, replace(current, pending=pending))
+    world.commit_by_hand()
+    with pytest.raises(UsageError, match="unfinished write"):
+        world.review()
+    assert lineage.load(world.state, built.run_id).compromised == "unfinalized_write"
+    assert quarantine.check(world.state, (world.repo / ".git").resolve()) is not None
+
+
+def test_an_unknown_lineage_of_the_repository_refuses(world: World) -> None:
+    built = world.implement()
+    lineage.lineage_path(world.state, built.run_id).write_text("{not json")
+    world.commit_by_hand()
+    with pytest.raises(UsageError, match=f"lineage {built.run_id} is unknown"):
+        world.review()
+
+
+def test_a_repository_quarantine_refuses_before_any_git(world: World) -> None:
+    world.commit_by_hand()
+    quarantine.publish(
+        world.state,
+        "repository",
+        reason="tripwire",
+        run_id="x",
+        paths=[],
+        common_dir=(world.repo / ".git").resolve(),
+    )
+    with pytest.raises(UsageError, match="quarantine"):
+        world.review()
+
+
+def test_a_review_waits_for_a_write_holding_its_lineage_then_is_refused(
+    world: World, monkeypatch: pytest.MonkeyPatch
+) -> None:
+    built = world.implement()
+    world.commit_by_hand()
+    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.2)
+    holder = subprocess.Popen(  # noqa: S603 - a fixed argv holding the lock
+        [
+            "python3",
+            "-c",
+            "import fcntl, os, sys, time\n"
+            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
+            "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(30)\n",
+            str(lineage.lineage_lock(world.state, built.run_id)),
+        ],
+        stdout=subprocess.PIPE,
+        text=True,
+    )
+    try:
+        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
+        with pytest.raises(UsageError, match=f"the lineage lock of {built.run_id}"):
+            world.review()
+    finally:
+        holder.kill()
+        holder.wait()
+
+
+# ── records ─────────────────────────────────────────────────────────────────
+
+
+def test_a_failed_cleanup_keeps_the_worktree_and_the_verdicts_exit(
+    world: World, monkeypatch: pytest.MonkeyPatch
+) -> None:
+    from headless_agents import review_flow
+
+    world.commit_by_hand()
+    real = review_flow._remove_worktree
+
+    def failing(*args: object, **kwargs: object) -> str | None:
+        return "fatal: simulated"
+
+    monkeypatch.setattr(review_flow, "_remove_worktree", failing)
+    outcome = world.review()
+    assert outcome.exit_code == 0
+    report = _report(outcome)
+    assert report["cleanup"] == {"status": "failed", "reason": "fatal: simulated"}
+    assert (outcome.run_dir / "wt").is_dir()
+    assert reviews.load_result(world.state, outcome.run_id).verdict == "approve"
+    assert real is not failing
+
+
+def test_a_prompt_too_large_for_a_reviewer_stops_the_phase_with_the_step_at_2(
+    world: World, monkeypatch: pytest.MonkeyPatch
+) -> None:
+    """§3.4: known only once the diff is -- the phase is refused, the workflow exits 1."""
+    world.commit_by_hand("x = 1\n" * 3000)
+    monkeypatch.setattr(
+        engine,
+        "max_prompt_bytes",
+        lambda provider: 1000 if provider in ("agy", "opencode") else None,
+    )
+    outcome = world.review("panel")
+    assert outcome.exit_code == 1
+    report = _report(outcome)
+    assert report["failure_reason"] == "prompt_too_large"
+    (step,) = report["steps"]  # type: ignore[misc]
+    assert (step["role"], step["exit_code"]) == ("reviewer-agy", 2)
+    assert all(agent.specs == [] for agent in world.agents.values())
+
+
+def test_a_review_registers_no_lineage_and_leaves_implement_records_alone(world: World) -> None:
+    built = world.implement()
+    before = lineage.load(world.state, built.run_id)
+    outcome = world.review("check", review_run=built.run_id)
+    entry = world.registry().resolve(outcome.run_id)
+    assert entry.lineage is None and entry.target == {
+        "kind": "workflow",
+        "name": "check",
+        "shape": "review",
+    }
+    assert lineage.load(world.state, built.run_id) == before
+    tip = _git(world.repo, "rev-parse", f"ha/{built.run_id}").strip()
+    assert provenance.lookup(world.state, tip) is not None
+
+
+def test_every_role_of_the_panel_needs_its_isolation_proof(world: World) -> None:
+    """§3.8.0: a rail that may load the operator's configuration is refused as an executor,
+    whichever slot it fills -- not only the first reviewer's."""
+    world.commit_by_hand()
+    record_proof(
+        world.state, "agy", version="agy 1.0", isolation=False, confinement=True, today="2026-09-26"
+    )
+    with pytest.raises(UsageError, match="agy agy 1.0 has no passing isolation proof"):
+        world.review("panel")
+    assert all(agent.specs == [] for agent in world.agents.values())
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_engine_plan.py tests/unit/headless_agents/test_review.py -q -p no:cacheprovider`
Expected: `test_engine_plan.py: 1 error; test_review.py: 28 failed` — every failure is the missing feature:
  - `test_engine_plan.py -- (collection) ImportError: cannot import name 'REVIEW_DEFAULT_TASK' from 'headless_agents.engine' (packages/headless-agents/src/headless_agents/engine.py)`
  - `test_review.py -- test_an_approving_review_records_its_head_verdict_check_and_cleanup: headless_agents.engine.UsageError: workflow check: the review shape is not ...`
  - `test_review.py -- test_a_review_asking_for_changes_exits_6: headless_agents.engine.UsageError: workflow check: the review shape is not ...`
  - `test_review.py -- test_the_reviewer_reads_the_pinned_commit_read_only_with_the_diff: headless_agents.engine.UsageError: workflow check: the review shape is not ...`
  - `test_review.py -- test_an_unreadable_verdict_fails_and_writes_no_result: headless_agents.engine.UsageError: workflow check: the review shape is not ...`
  - `test_review.py -- test_a_failing_reviewer_fails_the_review: headless_agents.engine.UsageError: workflow check: the review shape is not ...`
  - `test_review.py -- test_a_reviewer_answering_nothing_fails_the_review: headless_agents.engine.UsageError: workflow check: the review shape is not ...`
  - `test_review.py -- test_an_empty_diff_is_refused_nothing_to_review: AssertionError: Regex pattern did not match.`
  - `test_review.py -- test_a_base_that_does_not_resolve_is_refused: AssertionError: Regex pattern did not match.`
  - `test_review.py -- test_head_and_base_name_the_reviewed_range: TypeError: Request.__init__() got an unexpected keyword argument 'head'`
  - `test_review.py -- test_a_panel_runs_its_reviewers_concurrently_then_the_judge_decides: headless_agents.engine.UsageError: workflow panel: the review shape is not ...`
  - `test_review.py -- test_one_failing_reviewer_stops_the_panel_before_the_judge: headless_agents.engine.UsageError: workflow panel: the review shape is not ...`
  - … and 17 more

- [ ] **Step 3: Implement.** Apply:

```diff
diff --git a/packages/headless-agents/src/headless_agents/engine.py b/packages/headless-agents/src/headless_agents/engine.py
index 78e17d1a..dd59ffaa 100644
--- a/packages/headless-agents/src/headless_agents/engine.py
+++ b/packages/headless-agents/src/headless_agents/engine.py
@@ -19,12 +19,13 @@ import os
 import shutil
 import time
 from collections.abc import Callable, Mapping, Sequence
+from concurrent.futures import ThreadPoolExecutor
 from contextlib import ExitStack
 from dataclasses import dataclass, replace
 from pathlib import Path
 from typing import Final
 
-from . import locks, write_flow
+from . import locks, review_flow, write_flow
 from .capability import scoped_environment
 from .chain import run_chain
 from .cli_models import ModelsError, models_for
@@ -49,6 +50,7 @@ from .report import (
     PROMPT_FILE,
     RUN_JSON,
     new_report,
+    refused_step_entry,
     step_dir_name,
     step_entry,
     with_step,
@@ -60,7 +62,14 @@ from .run_record import RESULT_FILE_NAME
 from .runs import MINT_ATTEMPTS, Entry, Registry, RegistryError, make_run_dir
 from .spec import RunSpec
 from .state import Unknown
-from .templates import implement_prompt
+from .templates import (
+    ReviewText,
+    Verdict,
+    implement_prompt,
+    judge_prompt,
+    read_verdict,
+    review_prompt,
+)
 from .workflows import Workflow, WorkflowsError, load_workflows
 from .workspace import prepend
 
@@ -112,6 +121,21 @@ class Request:
     home: Path
     #: ``--continue RUN_ID``: an implement run whose lineage this run joins (§3.6).
     continue_run: str | None = None
+    #: ``--head REF`` of a review (§3.5); ``None`` means ``HEAD``.
+    head: str | None = None
+    #: ``--run RUN_ID`` of a review: an implement run whose lineage's tip is reviewed.
+    review_run: str | None = None
+
+
+@dataclass(frozen=True)
+class SlotPlan:
+    """One slot of a review's panel, planned like a role run (§3.5)."""
+
+    slot: str
+    role: Role
+    models: Mapping[str, str]
+    mcp: McpServer | None
+    environment: dict[str, str]
 
 
 @dataclass(frozen=True)
@@ -133,6 +157,11 @@ class Plan:
     #: both read from the registry; ``execute`` checks them again under the lock.
     continues: str | None = None
     joins: str | None = None
+    #: A review's panel -- its reviewers, then its judge -- in launch order.
+    panel: tuple[SlotPlan, ...] = ()
+    #: The run ``--run`` names, and the owner of its lineage, read from the registry.
+    reviews: str | None = None
+    reviewed_lineage: str | None = None
 
 
 def operator_environment(environ: Mapping[str, str]) -> dict[str, str]:
@@ -390,34 +419,104 @@ _OVERRIDE_FLAGS: Final[Mapping[str, str]] = {
 }
 
 
-def _continued_lineage(run_id: str, *, state: Path, home: Path) -> str:
-    """The owner of the lineage ``--continue RUN_ID`` joins, read from the registry.
+def _continued_lineage(run_id: str, *, state: Path, home: Path, option: str = "--continue") -> str:
+    """The owner of the implement lineage ``--continue`` or ``--run`` names, from the registry.
 
     No git and no lineage state here (§3.8.2): only the run's registry entry,
     which must name an ``implement`` run -- a member of an ``implement``
-    lineage (§3.6). ``execute`` checks it again, then the lineage itself,
+    lineage (§3.5, §3.6). ``execute`` checks it again, then the lineage itself,
     under the lineage lock.
     """
     registry = Registry(state, runs_root=runs_root(home))
     try:
         entry = registry.resolve(run_id)
     except RegistryError as exc:
-        raise UsageError(f"--continue: {exc}") from None
+        raise UsageError(f"{option}: {exc}") from None
     except Unknown as exc:
-        raise UsageError(f"--continue {run_id}: {exc}; recover it by hand") from None
+        raise UsageError(f"{option} {run_id}: {exc}; recover it by hand") from None
     target = entry.target
     if (
         entry.lineage is None
         or target.get("kind") != "workflow"
         or target.get("shape") != "implement"
     ):
+        verb = "continued" if option == "--continue" else "reviewed with --run"
         raise UsageError(
-            f"--continue {run_id}: not an implement run; only an implement run's lineage "
-            "can be continued"
+            f"{option} {run_id}: not an implement run; only an implement run's lineage "
+            f"can be {verb}"
         )
     return entry.lineage
 
 
+#: A review's task when none is given (§3.5): its prompt is optional.
+REVIEW_DEFAULT_TASK: Final = "Review this change."
+
+
+def _refuse_options_of_other_shapes(request: Request, shape: str | None) -> None:
+    """Spec §3.9: ``--head`` and ``--run`` belong to a review; ``--continue`` to an implement."""
+    if shape != "review":
+        for flag, value in (("--head", request.head), ("--run", request.review_run)):
+            if value is not None:
+                raise UsageError(f"{flag} needs a review workflow as the target")
+    if shape != "implement" and request.continue_run is not None:
+        raise UsageError("--continue needs an implement workflow as the target")
+
+
+def _slot_plan(slot: str, role: Role, request: Request, config: Config, name: str) -> SlotPlan:
+    rule = capability_rule(role, config.profiles)
+    if rule is not None:
+        # Validated when workflows.toml was read; the engine checks again (§3.4).
+        raise UsageError(f"{name}: {rule}")
+    mcp = _mcp(role, request)
+    return SlotPlan(
+        slot=slot,
+        role=role,
+        models=_models(role, request),
+        mcp=mcp,
+        environment=_environment(request.environ, mcp),
+    )
+
+
+def _plan_review(request: Request, workflow: Workflow, config: Config) -> Plan:
+    """A review target: every slot planned; the head, base and diff wait for ``execute``.
+
+    The prompt is optional (§3.9) -- absent, it reviews the change -- and its
+    size is checked when each step starts, once the diff is known (§3.4).
+    ``--run`` is resolved through the registry only (§3.8.2).
+    """
+    if request.review_run is not None and (request.head is not None or request.base is not None):
+        raise UsageError(
+            "--run excludes --head and --base: it stands for the lineage's tip and its base"
+        )
+    panel = tuple(
+        _slot_plan(slot, resolve_role(name, config.roles), request, config, workflow.name)
+        for slot, name in workflow.slot_roles()
+    )
+    state = state_dir(request.environ, home=request.home)
+    lineage = (
+        _continued_lineage(request.review_run, state=state, home=request.home, option="--run")
+        if request.review_run is not None
+        else None
+    )
+    task = (request.prompt or "").strip() or REVIEW_DEFAULT_TASK
+    first = panel[0]
+    return Plan(
+        request=request,
+        role=first.role,
+        models=first.models,
+        prompt=task,
+        mcp=first.mcp,
+        environment=first.environment,
+        run_dir=_run_dir(request),
+        state=state,
+        workflow=workflow,
+        task=task,
+        panel=panel,
+        reviews=request.review_run,
+        reviewed_lineage=lineage,
+    )
+
+
 def _plan_workflow(request: Request, workflow: Workflow, config: Config) -> Plan:
     """A workflow target: its roles run as declared (§3.3), its prompt is a template (§3.7)."""
     given = [
@@ -426,16 +525,15 @@ def _plan_workflow(request: Request, workflow: Workflow, config: Config) -> Plan
         if getattr(request.overrides, field) is not None
     ]
     if given:
+        own = "--head and --run" if workflow.shape == "review" else "--continue"
         raise UsageError(
             f"workflow {workflow.name}: a workflow runs its roles as declared, so "
             f"{', '.join(given)} is refused; its options are --base, --repo, --json, --run-dir "
-            "and --continue"
+            f"and {own}"
         )
+    _refuse_options_of_other_shapes(request, workflow.shape)
     if workflow.implement is None:
-        raise UsageError(
-            f"workflow {workflow.name}: the review shape is not available in this version of ha; "
-            f"run a reviewer role directly (ha run {workflow.review[0]} ...)"
-        )
+        return _plan_review(request, workflow, config)
     if request.continue_run is not None and request.base is not None:
         raise UsageError(
             "--base and --continue exclude each other: a continuation works from its lineage's base"
@@ -479,8 +577,7 @@ def plan(request: Request) -> Plan:
     workflow = config.workflows.get(request.target)
     if workflow is not None:
         return _plan_workflow(request, workflow, config)
-    if request.continue_run is not None:
-        raise UsageError("--continue needs an implement workflow as the target")
+    _refuse_options_of_other_shapes(request, None)
     declared, profiles = config.roles, config.profiles
     if request.target not in declared and request.target not in PROVIDER_NAMES:
         known = sorted({*config.workflows, *declared, *PROVIDER_NAMES})
@@ -836,6 +933,204 @@ def _execute_write(
     )
 
 
+def _slot_run_plan(plan: Plan, slot: SlotPlan, prompt: str) -> Plan:
+    """The plan of one panel step: its own role, models, MCP and environment (§3.5)."""
+    return replace(
+        plan,
+        role=slot.role,
+        models=slot.models,
+        mcp=slot.mcp,
+        environment=slot.environment,
+        prompt=prompt,
+    )
+
+
+@dataclass(frozen=True)
+class _Phase:
+    """A phase's steps once they ran: entries for the report, and the texts."""
+
+    entries: list[dict[str, object]]
+    results: list[RunResult | None]
+    failure_reason: str | None
+
+
+def _run_phase(
+    plan: Plan,
+    steps: Sequence[tuple[int, SlotPlan, str]],
+    *,
+    run_id: str,
+    run_dir: Path,
+    worktree: Path,
+    say: Callable[[str], None],
+) -> _Phase:
+    """Run ``(index, slot, prompt)`` steps in parallel, each read-only on the worktree.
+
+    Every prompt is size-checked first, now that the diff is known (§3.4): one
+    too large refuses the whole phase before anything starts, its step recorded
+    with code ``2``. Each step must answer -- exit ``0`` with a non-empty text --
+    or the phase fails (§3.5 step 3).
+    """
+    bundles = {index: _bundle(slot.role, plan.request, worktree) for index, slot, _ in steps}
+    for index, slot, prompt in steps:
+        try:
+            _check_prompt_size(slot.role, prompt, bundles[index])
+        except UsageError as exc:
+            say(f"step {index} {slot.slot} {slot.role.name}: refused ({exc})")
+            name = step_dir_name(index, slot.slot, slot.role.name)
+            entry = refused_step_entry(
+                index=index, slot=slot.slot, role=slot.role.name, step_dir=f"steps/{name}"
+            )
+            return _Phase(entries=[entry], results=[None], failure_reason="prompt_too_large")
+
+    def run_one(step: tuple[int, SlotPlan, str]) -> RunResult:
+        index, slot, prompt = step
+        name = step_dir_name(index, slot.slot, slot.role.name)
+        say(f"step {index} {slot.slot} {slot.role.name}: started")
+        final = _run_links(
+            _slot_run_plan(plan, slot, prompt),
+            bundles[index],
+            run_id=run_id,
+            step_dir=run_dir / "steps" / name,
+            workspace=Workspace(path=worktree),
+            say=say,
+        )
+        say(f"step {index} {slot.slot} {slot.role.name}: exit {final.exit_code}")
+        return final
+
+    with ThreadPoolExecutor(max_workers=len(steps)) as pool:
+        results = list(pool.map(run_one, steps))
+    entries = []
+    failed = False
+    for (index, slot, _), result in zip(steps, results, strict=True):
+        name = step_dir_name(index, slot.slot, slot.role.name)
+        entry = step_entry(
+            index=index,
+            slot=slot.slot,
+            role=slot.role.name,
+            step_dir=f"steps/{name}",
+            result=result,
+            tools=tool_counts(result),
+        )
+        entry["verdict"] = read_verdict(result.text)
+        entries.append(entry)
+        failed = failed or result.exit_code != 0 or not (result.text or "").strip()
+    return _Phase(
+        entries=entries, results=list(results), failure_reason="step_failed" if failed else None
+    )
+
+
+def _execute_review(
+    plan: Plan,
+    *,
+    registry: Registry,
+    entry: Entry,
+    identity: RepoIdentity,
+    start: Path,
+    report: dict[str, object],
+    started: float,
+    say: Callable[[str], None],
+) -> Outcome:
+    """A review run: the vendor rule and the pinned change, the reviewers in parallel,
+    then the judge; the verdict, the result and the cleanup (§3.5, §3.8.4)."""
+    request, run_dir = plan.request, entry.run_dir
+    reviewers = [(i, s) for i, s in enumerate(plan.panel, 1) if s.slot == "review"]
+    judge = next(((i, s) for i, s in enumerate(plan.panel, 1) if s.slot == "judge"), None)
+    try:
+        prepared = review_flow.prepare(
+            run_id=entry.run_id,
+            run_dir=run_dir,
+            state=plan.state,
+            identity=identity,
+            start=start,
+            environ=plan.environment,
+            head_ref=request.head,
+            base_ref=request.base,
+            reviewed_lineage=plan.reviewed_lineage,
+            reviewers={slot.role.name: slot.role.providers for _, slot in reviewers},
+        )
+    except review_flow.ReviewRefused as exc:
+        _refused(registry, entry)
+        raise UsageError(str(exc)) from None
+    task = plan.task or REVIEW_DEFAULT_TASK
+    report.update(
+        head=prepared.head, base=prepared.merge_base, vendor_check=prepared.check.to_document()
+    )
+    write_report(run_dir, report)
+
+    phase = _run_phase(
+        plan,
+        [(index, slot, review_prompt(task, prepared.patch)) for index, slot in reviewers],
+        run_id=entry.run_id,
+        run_dir=run_dir,
+        worktree=prepared.worktree,
+        say=say,
+    )
+    entries, failure = list(phase.entries), phase.failure_reason
+    deciding: RunResult | None = phase.results[0] if len(phase.results) == 1 else None
+    if failure is None and judge is not None:
+        index, slot = judge
+        answers = [
+            ReviewText(
+                role=slot_.role.name,
+                provider=result.provider,
+                model=result.model_reported or result.model or "",
+                text=result.text or "",
+            )
+            for (_, slot_), result in zip(reviewers, phase.results, strict=True)
+            if result is not None
+        ]
+        judged = _run_phase(
+            plan,
+            [(index, slot, judge_prompt(task, prepared.patch, answers))],
+            run_id=entry.run_id,
+            run_dir=run_dir,
+            worktree=prepared.worktree,
+            say=say,
+        )
+        entries.extend(judged.entries)
+        failure = judged.failure_reason
+        deciding = judged.results[0]
+    verdict: Verdict | None = None
+    text = deciding.text if deciding is not None else None
+    if failure is None:
+        verdict = read_verdict(text)
+        if verdict is None:
+            failure = "unreadable_verdict"
+    cleanup = review_flow.finish(
+        run_id=entry.run_id,
+        state=plan.state,
+        identity=identity,
+        environ=plan.environment,
+        prepared=prepared,
+        verdict=verdict,
+        text=text,
+    )
+    if cleanup["status"] != "done":
+        say(f"the review's worktree was kept ({cleanup.get('reason')}): ha clean retries")
+    if verdict == "approve":
+        status, code = "approved", 0
+    elif verdict == "changes":
+        status, code = "changes", review_flow.CHANGES_EXIT_CODE
+    else:
+        status, code = "failed", 1
+    for step in entries:
+        report = with_step(report, step)
+    report.update(
+        status=status,
+        exit_code=code,
+        verdict=verdict,
+        text=text,
+        failure_reason=failure,
+        cleanup=cleanup,
+        duration_seconds=round(time.monotonic() - started, 3),
+    )
+    write_report(run_dir, report)
+    registry.set_status(entry.run_id, status)
+    return Outcome(
+        exit_code=code, run_id=entry.run_id, run_dir=run_dir, report=report, final=deciding
+    )
+
+
 def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
     """Run a planned one-step run -- a role's, or an ``implement`` workflow's -- under its
     locks, and record it.
@@ -857,11 +1152,19 @@ def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
     repository = identity.work_tree if identity is not None else start
     if role.write and identity is None:
         raise UsageError(f"a write run needs a git repository; {start} is not in one")
+    if plan.panel and identity is None:
+        raise UsageError(f"a review needs a git repository; {start} is not in one")
     if plan.continues is not None:
         # §3.8.2: what plan() read from the registry is read again, before anything
         # is created; the lineage itself is checked under its lock (write_flow).
         if _continued_lineage(plan.continues, state=plan.state, home=request.home) != plan.joins:
             raise UsageError(f"--continue {plan.continues}: its lineage changed since the plan")
+    if plan.reviews is not None:
+        lineage = _continued_lineage(
+            plan.reviews, state=plan.state, home=request.home, option="--run"
+        )
+        if lineage != plan.reviewed_lineage:
+            raise UsageError(f"--run {plan.reviews}: its lineage changed since the plan")
 
     runs = runs_root(request.home)
     registry = Registry(plan.state, runs_root=runs)
@@ -892,7 +1195,8 @@ def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
                 write=role.write,
                 joins=plan.joins,
                 continues=plan.continues,
-                providers=role.providers,
+                # A write run's record (§3.10); a review writes nothing.
+                providers=() if plan.panel else role.providers,
             )
         except (LockTimeout, RegistryError) as exc:
             # A run that never started leaves nothing behind (codex review of #207).
@@ -925,6 +1229,9 @@ def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
 
         try:
             _check_isolation(plan)
+            for member in plan.panel:
+                # Every role of a review's panel runs a rail (§3.8.0), not only the first.
+                _check_isolation(replace(plan, role=member.role, environment=member.environment))
         except UsageError:
             _refused(registry, entry)
             raise
@@ -949,6 +1256,18 @@ def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
             started_at=_utc_now(),
         )
         write_report(run_dir, report)
+        if plan.panel:
+            assert identity is not None
+            return _execute_review(
+                plan,
+                registry=registry,
+                entry=entry,
+                identity=identity,
+                start=start,
+                report=report,
+                started=started,
+                say=say,
+            )
         step_name = step_dir_name(1, slot, role.name)
         step_dir = run_dir / "steps" / step_name
         if role.write:
@@ -1092,6 +1411,8 @@ __all__ = [
     "runs_root",
     "Overrides",
     "Plan",
+    "REVIEW_DEFAULT_TASK",
+    "SlotPlan",
     "Request",
     "UsageError",
     "operator_environment",
diff --git a/packages/headless-agents/src/headless_agents/report.py b/packages/headless-agents/src/headless_agents/report.py
index e5d9d901..5cea7d78 100644
--- a/packages/headless-agents/src/headless_agents/report.py
+++ b/packages/headless-agents/src/headless_agents/report.py
@@ -88,6 +88,14 @@ def step_entry(
     }
 
 
+def refused_step_entry(*, index: int, slot: str, role: str, step_dir: str) -> dict[str, object]:
+    """A step refused at its start -- its prompt too large once the diff is known (§3.4):
+    recorded with code ``2``, nothing measured because nothing ran."""
+    entry: dict[str, object] = dict.fromkeys(STEP_KEYS)
+    entry.update(index=index, slot=slot, role=role, dir=step_dir, exit_code=2)
+    return entry
+
+
 def with_step(report: Mapping[str, object], step: Mapping[str, object]) -> dict[str, object]:
     """A copy of ``report`` with ``step`` appended and the cost recomputed."""
     previous = report.get("steps")
@@ -112,6 +120,7 @@ __all__ = [
     "SCHEMA",
     "STEP_KEYS",
     "new_report",
+    "refused_step_entry",
     "step_dir_name",
     "step_entry",
     "with_step",
diff --git a/packages/headless-agents/src/headless_agents/review_flow.py b/packages/headless-agents/src/headless_agents/review_flow.py
new file mode 100644
index 00000000..44411c75
--- /dev/null
+++ b/packages/headless-agents/src/headless_agents/review_flow.py
@@ -0,0 +1,292 @@
+"""A review's state and git, from its locks to its cleanup (spec 0.5.0 §3.5, §3.8.4, §3.8.6).
+
+:func:`prepare` runs steps 1-6 of §3.8.4 and step 1 of §3.5, in this order:
+
+1. locks, without git -- the lineage registry lock shared, then the lock of
+   every lineage of the repository (and of any lineage whose worktree holds
+   ``--repo``) shared, in ascending owner order; the quarantines and a stale
+   unconfined intent checked first;
+2. uncertainty refuses -- a lineage unknown, compromised, or holding a pending
+   write; a pending write seen under its lock held shared has lost its writer
+   (a live writer holds it exclusively), so it is handled as §3.8.5 says;
+3. the head pinned -- ``--run``'s lineage tip and base, or ``--head`` and
+   ``--base`` (default ``origin/HEAD``); then the merge base;
+4. every commit of ``<merge-base>..<head>`` attributed (:mod:`.vendor`);
+5. independence checked for every reviewer role;
+6. the check written once; the lineage and registry locks released -- the
+   caller keeps the lifecycle and unconfined locks to the end (§3.8.2) --
+   then ``change.patch`` and the detached worktree on the pinned head.
+
+:func:`finish` writes the result once, when a verdict was read, **before**
+the cleanup, so a failed cleanup never loses a verdict (§3.8.4 step 6).
+Every git command runs through :func:`headless_agents.gitops.git`, hooks off.
+"""
+
+from __future__ import annotations
+
+from collections.abc import Mapping, Sequence
+from contextlib import ExitStack
+from dataclasses import dataclass
+from pathlib import Path
+from typing import Final
+
+from . import lineage as lineages
+from . import locks, quarantine, reviews
+from .git_tripwire import GitTampered
+from .gitops import git
+from .lineage import LineageState
+from .locks import LockTimeout, Rank, held
+from .repo import RepoIdentity
+from .state import Unknown
+from .templates import Verdict
+from .vendor import VendorCheck, VendorRefused, attribute, check_independence
+from .write_flow import (
+    PATCH_FILE,
+    WriteRefused,
+    check_unconfined_intent,
+    source_lineages,
+    unfinalized,
+)
+
+#: The exit code of a review whose verdict asks for changes (§3.5, §3.9).
+CHANGES_EXIT_CODE: Final = 6
+#: A review's base when neither ``--base`` nor ``--run`` names one (§3.5).
+DEFAULT_BASE: Final = "origin/HEAD"
+WORKTREE: Final = "wt"
+
+
+class ReviewRefused(Exception):  # noqa: N818 - a refusal, not a crash
+    """The review cannot start: exit ``2``, no agent ran."""
+
+
+@dataclass(frozen=True)
+class Prepared:
+    """What a review reads, pinned before its reviewers start."""
+
+    head: str
+    merge_base: str
+    patch: str
+    check: VendorCheck
+    worktree: Path
+
+
+def _git(
+    identity: RepoIdentity, args: Sequence[str], environ: Mapping[str, str], state: Path
+) -> tuple[int, str, str]:
+    try:
+        result = git(identity.work_tree, args, environ, state=state)
+    except GitTampered as exc:
+        raise ReviewRefused(f"{exc}; nothing ran") from None
+    return result.returncode, result.stdout, result.stderr
+
+
+def _resolve(
+    identity: RepoIdentity, ref: str, environ: Mapping[str, str], state: Path
+) -> str | None:
+    code, out, _ = _git(
+        identity, ["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], environ, state
+    )
+    return out.strip() if code == 0 and out.strip() else None
+
+
+def _admitted(state: Path, owners: Sequence[str]) -> dict[str, LineageState]:
+    """Every lineage, known, not compromised, holding no pending write (§3.8.4 step 2)."""
+    found: dict[str, LineageState] = {}
+    for owner in owners:
+        try:
+            current = lineages.load(state, owner)
+        except Unknown as exc:
+            raise ReviewRefused(f"lineage {owner} is unknown ({exc}); nothing ran") from None
+        if current.compromised is not None:
+            raise ReviewRefused(
+                f"lineage {owner} is compromised ({current.compromised}): no review of its "
+                "repository can prove who wrote what; nothing ran"
+            )
+        if current.pending is not None:
+            # Held shared by us, the lock is free of any writer: this write is dead.
+            raise ReviewRefused(str(unfinalized(state, current, current.common_dir))) from None
+        found[owner] = current
+    return found
+
+
+def _range(
+    identity: RepoIdentity,
+    environ: Mapping[str, str],
+    state: Path,
+    *,
+    head_ref: str | None,
+    base_ref: str | None,
+    reviewed: LineageState | None,
+) -> tuple[str, str]:
+    """``(head, base)`` as commits: pinned once, and only now (§3.8.4 step 3)."""
+    if reviewed is not None:
+        head = _resolve(identity, f"refs/heads/{reviewed.branch}", environ, state)
+        if head is None:
+            raise ReviewRefused(
+                f"the branch {reviewed.branch} of lineage {reviewed.owner} no longer exists: "
+                "nothing to review; nothing ran"
+            )
+        if reviewed.base is None:
+            raise ReviewRefused(f"lineage {reviewed.owner} records no base; nothing ran")
+        return head, reviewed.base
+    head_name = head_ref or "HEAD"
+    head = _resolve(identity, head_name, environ, state)
+    if head is None:
+        raise ReviewRefused(f"--head {head_name} does not resolve to a commit; nothing ran")
+    base_name = base_ref or DEFAULT_BASE
+    base = _resolve(identity, base_name, environ, state)
+    if base is None:
+        raise ReviewRefused(
+            f"--base {base_name} does not resolve to a commit; name the base with --base; "
+            "nothing ran"
+        )
+    return head, base
+
+
+def prepare(
+    *,
+    run_id: str,
+    run_dir: Path,
+    state: Path,
+    identity: RepoIdentity,
+    start: Path,
+    environ: Mapping[str, str],
+    head_ref: str | None,
+    base_ref: str | None,
+    reviewed_lineage: str | None,
+    reviewers: Mapping[str, Sequence[str]],
+) -> Prepared:
+    """Steps 1-6 of §3.8.4, then the patch and the detached worktree; :class:`ReviewRefused`."""
+    common = identity.common_dir
+    refusal = quarantine.check(state, common)
+    if refusal is not None:
+        raise ReviewRefused(f"{refusal}; nothing ran")
+    try:
+        check_unconfined_intent(state, run_id)
+    except WriteRefused as exc:
+        raise ReviewRefused(str(exc)) from None
+    with ExitStack() as stack:
+        try:
+            stack.enter_context(
+                held(
+                    lineages.registry_lock(state),
+                    rank=Rank.LINEAGE_REGISTRY,
+                    exclusive=False,
+                    wait=locks.LOCK_WAIT_SECONDS,
+                    what="the lineage registry lock",
+                )
+            )
+            owners = sorted(
+                set(lineages.of_repository(state, common)) | set(source_lineages(state, start))
+            )
+            for owner in owners:
+                stack.enter_context(
+                    held(
+                        lineages.lineage_lock(state, owner),
+                        rank=Rank.LINEAGE,
+                        key=owner,
+                        exclusive=False,
+                        wait=locks.LOCK_WAIT_SECONDS,
+                        what=f"the lineage lock of {owner}",
+                    )
+                )
+        except LockTimeout as exc:
+            raise ReviewRefused(f"{exc}: a write holds it; nothing ran") from None
+        admitted = _admitted(state, owners)
+        reviewed = None
+        if reviewed_lineage is not None:
+            reviewed = admitted.get(reviewed_lineage)
+            if reviewed is None:
+                raise ReviewRefused(
+                    f"--run: lineage {reviewed_lineage} does not belong to "
+                    f"{identity.work_tree}; nothing ran"
+                )
+        head, base = _range(
+            identity, environ, state, head_ref=head_ref, base_ref=base_ref, reviewed=reviewed
+        )
+        code, out, err = _git(identity, ["merge-base", base, head], environ, state)
+        if code != 0 or not out.strip():
+            raise ReviewRefused(f"{base[:12]} and {head[:12]} have no merge base; nothing ran")
+        merge_base = out.strip()
+        code, patch, err = _git(identity, ["diff", "--binary", merge_base, head], environ, state)
+        if code != 0:
+            raise ReviewRefused(f"git diff failed: {err.strip()}; nothing ran")
+        if not patch.strip():
+            raise ReviewRefused(
+                f"the diff from {merge_base[:12]} to {head[:12]} is empty: nothing to review"
+            )
+        code, log, err = _git(
+            identity,
+            ["log", "--reverse", "--format=%H%x00%s", f"{merge_base}..{head}"],
+            environ,
+            state,
+        )
+        if code != 0:
+            raise ReviewRefused(f"git log failed: {err.strip()}; nothing ran")
+        commits = [tuple(line.split("\x00", 1)) for line in log.splitlines() if line]
+        try:
+            check = check_independence(
+                attribute(state, [(sha, subject) for sha, subject in commits]), reviewers
+            )
+        except VendorRefused as exc:
+            raise ReviewRefused(f"{exc}; nothing ran") from None
+        reviews.write_check(state, run_id, check)
+    # The lineage and registry locks are released: the pinned commit no write can change.
+    (run_dir / PATCH_FILE).write_text(patch, encoding="utf-8", errors="replace")
+    worktree = run_dir / WORKTREE
+    code, _, err = _git(
+        identity, ["worktree", "add", "-q", "--detach", str(worktree), head], environ, state
+    )
+    if code != 0:
+        raise ReviewRefused(f"git worktree add failed: {err.strip()}; nothing ran")
+    return Prepared(head=head, merge_base=merge_base, patch=patch, check=check, worktree=worktree)
+
+
+def _remove_worktree(
+    worktree: Path, identity: RepoIdentity, environ: Mapping[str, str], state: Path
+) -> str | None:
+    """Remove the detached worktree through git; the reason when it cannot."""
+    refusal = quarantine.check(state, identity.common_dir)
+    if refusal is not None:
+        return refusal
+    try:
+        code, _, err = _git(
+            identity, ["worktree", "remove", "--force", str(worktree)], environ, state
+        )
+    except ReviewRefused as exc:
+        return str(exc)
+    return None if code == 0 else (err.strip() or "git worktree remove failed")
+
+
+def finish(
+    *,
+    run_id: str,
+    state: Path,
+    identity: RepoIdentity,
+    environ: Mapping[str, str],
+    prepared: Prepared,
+    verdict: Verdict | None,
+    text: str | None,
+) -> dict[str, object]:
+    """The result, written once when a verdict was read, then the cleanup (§3.8.4 step 6)."""
+    if verdict is not None:
+        reviews.write_result(
+            state,
+            run_id,
+            head=prepared.head,
+            verdict=verdict,
+            text=text or "",
+            check=prepared.check,
+        )
+    reason = _remove_worktree(prepared.worktree, identity, environ, state)
+    return {"status": "done"} if reason is None else {"status": "failed", "reason": reason}
+
+
+__all__ = [
+    "CHANGES_EXIT_CODE",
+    "DEFAULT_BASE",
+    "Prepared",
+    "ReviewRefused",
+    "finish",
+    "prepare",
+]
diff --git a/packages/headless-agents/src/headless_agents/write_flow.py b/packages/headless-agents/src/headless_agents/write_flow.py
index dfbc4cc6..b52ed88c 100644
--- a/packages/headless-agents/src/headless_agents/write_flow.py
+++ b/packages/headless-agents/src/headless_agents/write_flow.py
@@ -144,8 +144,8 @@ def _inside(path: Path, root: Path) -> bool:
     return normal == top or normal.startswith(top.rstrip(os.sep) + os.sep)
 
 
-def _sources(state: Path, start: Path) -> list[str]:
-    """Every lineage whose recorded worktree contains ``start`` (§3.8.2)."""
+def source_lineages(state: Path, start: Path) -> list[str]:
+    """Every lineage whose recorded worktree contains ``start`` (§3.8.2); a review uses it too."""
     found = []
     for owner in lineages.owners(state):
         try:
@@ -196,12 +196,16 @@ def check_repository(state: Path, common: Path, *, own: str) -> None:
         except Unknown as exc:
             raise WriteRefused(f"lineage {owner} is unknown ({exc}); nothing ran") from None
         if other.pending is not None and is_free(lineages.lineage_lock(state, owner)):
-            raise _unfinalized(state, other, common)
+            raise unfinalized(state, other, common)
 
 
-def _unfinalized(state: Path, lineage: LineageState, common: Path) -> WriteRefused:
+def unfinalized(state: Path, lineage: LineageState, common: Path) -> WriteRefused:
     """A stale pending write: its lineage compromised (``unfinalized_write``), the
-    repository quarantined -- the operator too when that write was unconfined."""
+    repository quarantined -- the operator too when that write was unconfined.
+
+    Public: a review that finds a pending write under its shared lineage lock
+    found a stale one (a live writer holds that lock exclusively) and applies
+    the same (§3.8.5)."""
     pending = lineage.pending
     assert pending is not None, "only a pending write can be stale"
     lineages.save(state, replace(lineage, compromised="unfinalized_write"))
@@ -270,7 +274,7 @@ def _check_continued(write: _Write) -> None:
             f"{write.identity.work_tree}; nothing ran"
         )
     if current.pending is not None:
-        raise _unfinalized(state, current, current.common_dir)
+        raise unfinalized(state, current, current.common_dir)
     if current.compromised is not None:
         raise WriteRefused(f"lineage {owner} is compromised ({current.compromised}); nothing ran")
     if write.named not in current.members:
@@ -302,7 +306,7 @@ def _admit(write: _Write, registry: ExitStack) -> None:
             what="the lineage registry lock",
         )
     )
-    sources = _sources(state, write.start)
+    sources = source_lineages(state, write.start)
     for owner in sorted({write.owner, *sources}):
         own = owner == write.owner
         write.locks.enter_context(
@@ -960,4 +964,6 @@ __all__ = [
     "check_unconfined_intent",
     "clean_write",
     "run_write_step",
+    "source_lineages",
+    "unfinalized",
 ]
```

- [ ] **Step 4: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_engine_plan.py tests/unit/headless_agents/test_review.py -q -p no:cacheprovider`
Expected: `test_engine_plan.py: 65 passed; test_review.py: 28 passed`. Then the gates of the Global Constraints.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/engine.py packages/headless-agents/src/headless_agents/report.py packages/headless-agents/src/headless_agents/review_flow.py packages/headless-agents/src/headless_agents/write_flow.py tests/unit/headless_agents/test_engine_plan.py tests/unit/headless_agents/test_review.py
git commit -m "feat(headless-agents): a review run -- the vendor rule, the panel, the verdict, the records and the cleanup"
```

### Task 5: `ha show` of a review, and `ha show --dir`

**Files:**
- Modify: `packages/headless-agents/README.md`
- Modify: `packages/headless-agents/src/headless_agents/cli.py`
- Modify: `packages/headless-agents/src/headless_agents/show.py`
- Modify: `tests/unit/headless_agents/test_cli.py`
- Modify: `tests/unit/headless_agents/test_show.py`

**Interfaces:**
- Consumes: `reviews.load_result`, `reviews.load_check` (Task 3).
- Produces: `show.is_review(entry)`, `show.vendors_line(check_document) -> str`,
  `show.from_dir(run_dir) -> Shown` (`NotShown` when the directory holds no report of `ha`);
  `ha show --dir PATH [--json]` (P7).

- [ ] **Step 1: Write the failing tests.** Apply (`git apply`):

```diff
diff --git a/tests/unit/headless_agents/test_cli.py b/tests/unit/headless_agents/test_cli.py
index 25d81de7..8c56b111 100644
--- a/tests/unit/headless_agents/test_cli.py
+++ b/tests/unit/headless_agents/test_cli.py
@@ -11,6 +11,7 @@ from __future__ import annotations
 import io
 import json
 import os
+import shutil
 import signal
 import subprocess
 import sys
@@ -1172,6 +1173,29 @@ def test_show_of_a_cleaned_run_says_so_and_exits_0(world: _World) -> None:
     assert out.splitlines()[0] == f"{run_id}  codex  exit -  answered"
 
 
+def test_show_dir_renders_a_run_directory_for_display_only(world: _World) -> None:
+    """Spec §3.8.6: readable even after the state directory is lost."""
+    code, out, _ = world.run("run", "codex", "--json", "go")
+    run_dir = world.home / ".cache" / "ha" / "runs" / json.loads(out)["run_id"]
+    shutil.rmtree(world.home / ".local" / "state" / "ha")
+    code, out, err = world.run("show", "--dir", str(run_dir))
+    assert code == 0 and "display only" in err
+    assert out.splitlines()[0].endswith("codex  exit 0  answered")
+    code, out, _ = world.run("show", "--dir", str(run_dir), "--json")
+    assert code == 0 and json.loads(out)["status"] == "answered"
+
+
+@pytest.mark.parametrize("argv", [("show",), ("show", "20260925T000000-00000000", "--dir", "/tmp")])
+def test_show_takes_a_run_id_or_a_dir_exactly(world: _World, argv: tuple[str, ...]) -> None:
+    code, _, err = world.run(*argv)
+    assert code == 2 and "a run id or --dir" in err
+
+
+def test_show_dir_refuses_a_directory_that_holds_no_report(world: _World, tmp_path: Path) -> None:
+    code, _, err = world.run("show", "--dir", str(tmp_path))
+    assert code == 2 and "run.json" in err
+
+
 def test_show_refuses_what_is_not_a_registered_run(world: _World) -> None:
     code, _, err = world.run("show", "20260925T000000-00000000")
     assert code == 2 and "no run 20260925T000000-00000000" in err
diff --git a/tests/unit/headless_agents/test_show.py b/tests/unit/headless_agents/test_show.py
index ed816bc2..99fe2e4b 100644
--- a/tests/unit/headless_agents/test_show.py
+++ b/tests/unit/headless_agents/test_show.py
@@ -10,9 +10,10 @@ from pathlib import Path
 import pytest
 
 from headless_agents import lineage as lineages
-from headless_agents import locks, provenance, show
+from headless_agents import locks, provenance, reviews, show
 from headless_agents.report import RUN_KEYS
 from headless_agents.runs import Registry
+from headless_agents.vendor import AttributedCommit, VendorCheck
 
 RUN = "20260925T120000-ab12cd34"
 OTHER = "20260925T130000-cd34ef56"
@@ -622,3 +623,166 @@ def test_format_tools_and_tokens() -> None:
     assert show.format_tokens(None) == "-"
     assert show.format_tokens({"input": 999, "output": None}) == "in 999 out -"
     assert show.format_tokens({"input": 1_500_000, "output": 3000}) == "in 1.5M out 3k"
+
+
+# ── a review (lot 4, spec §3.8.6) ───────────────────────────────────────────
+
+REVIEW = "20260925T150000-9f8e7d6c"
+PINNED = "c" * 40
+REVIEW_TARGET = {"kind": "workflow", "name": "panel", "shape": "review"}
+CHECK = VendorCheck(
+    commits=(
+        AttributedCommit(sha=SHA_1, run_id=RUN, made_by="engine", providers=("opencode", "codex")),
+        AttributedCommit(sha=SHA_2, run_id=None, made_by="hand", providers=()),
+    ),
+    authors=("codex", "opencode"),
+    reviewers={"reviewer-agy": ("agy",), "reviewer-claude": ("claude",)},
+)
+
+
+def _review(
+    home: Home, *, verdict: str | None = "changes", status: str = "changes", **report: object
+) -> Path:
+    registry = home.registry()
+    entry = registry.create(
+        REVIEW, run_dir=None, target=REVIEW_TARGET, repository=home.root / "repo", lineage=None
+    )
+    entry.run_dir.mkdir(parents=True)
+    (entry.run_dir / "prompt.md").write_text("Review this change.\n")
+    document = home.report(
+        run_id=REVIEW,
+        target=REVIEW_TARGET,
+        status=status,
+        text="report text\nVERDICT: APPROVE",
+        verdict="approve",
+        head="d" * 40,
+        cleanup={"status": "done"},
+        **report,
+    )
+    (entry.run_dir / "run.json").write_text(json.dumps(document))
+    reviews.write_check(home.state, REVIEW, CHECK)
+    if verdict is not None:
+        reviews.write_result(
+            home.state,
+            REVIEW,
+            head=PINNED,
+            verdict=verdict,  # type: ignore[arg-type]
+            text="Only one finding stands.\nVERDICT: CHANGES",
+            check=CHECK,
+        )
+    registry.set_status(REVIEW, status)
+    return entry.run_dir
+
+
+def test_a_reviews_head_verdict_text_and_check_come_from_its_result(home: Home) -> None:
+    _review(home)
+    report = home.rebuild(REVIEW).report
+    assert report["verdict"] == "changes" and report["head"] == PINNED
+    assert report["text"] == "Only one finding stands.\nVERDICT: CHANGES"
+    assert report["vendor_check"] == CHECK.to_document()
+    assert report["cleanup"] == {"status": "done"}
+    assert report["branch"] is None and report["commits"] is None
+
+
+def test_a_review_without_a_result_shows_its_check_and_no_verdict(home: Home) -> None:
+    _review(home, verdict=None, status="failed", failure_reason="unreadable_verdict")
+    report = home.rebuild(REVIEW).report
+    assert report["verdict"] is None and report["head"] is None
+    assert report["vendor_check"] == CHECK.to_document()
+
+
+def test_a_cleaned_review_is_shown_from_the_state(home: Home) -> None:
+    run_dir = _review(home)
+    shutil.rmtree(run_dir)
+    home.registry().set_cleaned(REVIEW, "2026-09-25T16:00:00Z")
+    report = home.rebuild(REVIEW).report
+    assert report["verdict"] == "changes" and report["vendor_check"] == CHECK.to_document()
+
+
+def test_an_unreadable_review_result_is_unknown(home: Home) -> None:
+    _review(home)
+    path = reviews.result_path(home.state, REVIEW)
+    path.chmod(0o600)
+    path.write_text("{not json")
+    shown = home.rebuild(REVIEW)
+    assert shown.unknown and shown.report["verdict"] is None
+    assert any("cannot be read" in note for note in shown.notes)
+
+
+GOLDEN_REVIEW = """\
+20260925T150000-9f8e7d6c  panel  exit 6  changes requested  cleanup failed: fatal: busy
+task    Review this change.
+head    ccccccc  2 commits  +120 -14  5 files
+vendors authors codex, opencode (1 recorded commit, 1 hand-written) — reviewers agy, claude: independent
+  review  reviewer-agy     agy     (auto)  0  2m40s  -  -  view_file 9  CHANGES
+  judge   reviewer-claude  claude  opus    0  1m10s  -  -  Read 14      CHANGES
+--- judge ---
+Only one finding stands.
+VERDICT: CHANGES
+"""
+
+
+def test_a_review_renders_as_the_golden_text(home: Home) -> None:
+    steps = [
+        dict(_STEP, index=1, slot="review", role="reviewer-agy", provider="agy", model=None,
+             duration_seconds=160.0, tokens=None, cost_usd=None, tools={"view_file": 9},
+             verdict="changes"),
+        dict(_STEP, index=2, slot="judge", role="reviewer-claude", provider="claude",
+             model="opus", duration_seconds=70.0, tokens=None, cost_usd=None,
+             tools={"Read": 14}, verdict="changes"),
+    ]  # fmt: skip
+    report = home.report(
+        run_id=REVIEW,
+        target=REVIEW_TARGET,
+        status="changes",
+        exit_code=6,
+        verdict="changes",
+        head=PINNED,
+        text="Only one finding stands.\nVERDICT: CHANGES\n",
+        vendor_check=CHECK.to_document(),
+        cleanup={"status": "failed", "reason": "fatal: busy"},
+        steps=steps,
+    )
+    stat = show.Diffstat(insertions=120, deletions=14, files=5)
+    rendered = show.render(_shown(report, task="Review this change.", diffstat=stat))
+    assert rendered == GOLDEN_REVIEW
+
+
+def test_the_vendors_line_names_attributed_commits_and_none_authors() -> None:
+    check = VendorCheck(
+        commits=(
+            AttributedCommit(sha=SHA_1, run_id=None, made_by="unknown", providers=("claude",)),
+        ),
+        authors=("claude",),
+        reviewers={"r": ("codex",)},
+    )
+    assert show.vendors_line(check.to_document()) == (
+        "vendors authors claude (1 attributed to unconfined writers) — reviewers codex: independent"
+    )
+    bare = VendorCheck(commits=(), authors=(), reviewers={"r": ("codex",)})
+    assert show.vendors_line(bare.to_document()) == (
+        "vendors authors none — reviewers codex: independent"
+    )
+
+
+# ── ha show --dir (spec §3.8.6: display only) ───────────────────────────────
+
+
+def test_a_run_directory_is_shown_for_display_after_the_state_is_lost(home: Home) -> None:
+    run_dir = _review(home)
+    shutil.rmtree(home.state)
+    shown = show.from_dir(run_dir)
+    assert shown.report["run_id"] == REVIEW and shown.report["verdict"] == "approve"
+    assert shown.task == "Review this change."
+    assert any("display only" in note for note in shown.notes)
+    assert list(shown.report) == list(RUN_KEYS)
+
+
+@pytest.mark.parametrize("content", [None, "{not json", '["a list"]', '{"run_id": "../x"}'])
+def test_a_directory_without_a_report_of_ha_is_not_shown(
+    tmp_path: Path, content: str | None
+) -> None:
+    if content is not None:
+        (tmp_path / "run.json").write_text(content)
+    with pytest.raises(show.NotShown, match="run.json"):
+        show.from_dir(tmp_path)
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_cli.py tests/unit/headless_agents/test_show.py -q -p no:cacheprovider`
Expected: `test_cli.py: 4 failed, 98 passed; test_show.py: 11 failed, 46 passed` — every failure is the missing feature:
  - `test_cli.py -- test_show_dir_renders_a_run_directory_for_display_only: assert (2 == 0)`
  - `test_cli.py -- test_show_takes_a_run_id_or_a_dir_exactly[argv0]: AssertionError: assert (2 == 2 and 'a run id or --dir' in 'ha: the followin...`
  - `test_cli.py -- test_show_takes_a_run_id_or_a_dir_exactly[argv1]: AssertionError: assert (2 == 2 and 'a run id or --dir' in 'ha: unrecognized...`
  - `test_cli.py -- test_show_dir_refuses_a_directory_that_holds_no_report: AssertionError: assert (2 == 2 and 'run.json' in 'ha: unrecognized argument...`
  - `test_show.py -- test_a_reviews_head_verdict_text_and_check_come_from_its_result: AssertionError: assert (None == 'changes')`
  - `test_show.py -- test_a_review_without_a_result_shows_its_check_and_no_verdict: AssertionError: assert None == {'commits': [{'sha': '1111111111111111111111...`
  - `test_show.py -- test_a_cleaned_review_is_shown_from_the_state: AssertionError: assert (None == 'changes')`
  - `test_show.py -- test_an_unreadable_review_result_is_unknown: AssertionError: assert (False)`
  - `test_show.py -- test_a_review_renders_as_the_golden_text: AssertionError: assert '20260925T150...CT: CHANGES\n' == '20260925T150...CT...`
  - `test_show.py -- test_the_vendors_line_names_attributed_commits_and_none_authors: AttributeError: module 'headless_agents.show' has no attribute 'vendors_line'`
  - `test_show.py -- test_a_run_directory_is_shown_for_display_after_the_state_is_lost: AttributeError: module 'headless_agents.show' has no attribute 'from_dir'`
  - `test_show.py -- test_a_directory_without_a_report_of_ha_is_not_shown[None]: AttributeError: module 'headless_agents.show' has no attribute 'from_dir'`

- [ ] **Step 3: Implement.** Apply:

```diff
diff --git a/packages/headless-agents/README.md b/packages/headless-agents/README.md
index cbee511a..78277ff5 100644
--- a/packages/headless-agents/README.md
+++ b/packages/headless-agents/README.md
@@ -375,6 +375,7 @@ ha workflows [--json]
 ha providers [--json]
 ha runs [--limit N] [--json]
 ha show RUN_ID [--json]
+ha show --dir PATH [--json]
 ha clean RUN_ID
 ha --version
 ```
diff --git a/packages/headless-agents/src/headless_agents/cli.py b/packages/headless-agents/src/headless_agents/cli.py
index dccf87ee..fbd4c12e 100644
--- a/packages/headless-agents/src/headless_agents/cli.py
+++ b/packages/headless-agents/src/headless_agents/cli.py
@@ -8,6 +8,7 @@
     ha providers [--json]
     ha runs [--limit N] [--json]
     ha show RUN_ID [--json]
+    ha show --dir PATH [--json]               display only
     ha clean RUN_ID
     ha --version
 
@@ -182,7 +183,15 @@ def _parser() -> argparse.ArgumentParser:
     runs.add_argument("--json", action="store_true", help="print the list as JSON")
 
     show_parser = commands.add_parser("show", help="show one run, rebuilt from the state")
-    show_parser.add_argument("run_id", help="the run id, as ha run or ha runs printed it")
+    show_parser.add_argument(
+        "run_id", nargs="?", help="the run id, as ha run or ha runs printed it"
+    )
+    show_parser.add_argument(
+        "--dir",
+        type=Path,
+        metavar="PATH",
+        help="render a run directory's run.json instead, for display only",
+    )
     show_parser.add_argument("--json", action="store_true", help="print the rebuilt run.json")
 
     clean_parser = commands.add_parser("clean", help="remove one run's directory")
@@ -514,11 +523,19 @@ def _clean(args: argparse.Namespace, io: Io) -> int:
 
 
 def _show(args: argparse.Namespace, io: Io) -> int:
-    """``ha show RUN_ID``: rebuilt from the state (plan P6); 1 when part of it is unreadable."""
+    """``ha show RUN_ID``: rebuilt from the state (plan P6); 1 when part of it is unreadable.
+    ``ha show --dir PATH``: a run directory's report, for display only (§3.8.6)."""
+    if (args.run_id is None) == (args.dir is None):
+        raise UsageError("ha show takes a run id or --dir PATH, exactly one of them")
     try:
-        shown = show.rebuild(
-            args.run_id, state=state_dir(io.environ, home=io.home), runs_root=runs_root(io.home)
-        )
+        if args.dir is not None:
+            shown = show.from_dir(Path(os.path.abspath(io.cwd / args.dir)))
+        else:
+            shown = show.rebuild(
+                args.run_id,
+                state=state_dir(io.environ, home=io.home),
+                runs_root=runs_root(io.home),
+            )
     except show.NotShown as exc:
         raise UsageError(str(exc)) from None
     for note in shown.notes:
diff --git a/packages/headless-agents/src/headless_agents/show.py b/packages/headless-agents/src/headless_agents/show.py
index 3e284807..4f286229 100644
--- a/packages/headless-agents/src/headless_agents/show.py
+++ b/packages/headless-agents/src/headless_agents/show.py
@@ -22,16 +22,16 @@ from pathlib import Path
 from typing import Final
 
 from . import lineage as lineages
-from . import provenance
+from . import provenance, reviews
 from .report import PROMPT_FILE, RUN_JSON, RUN_KEYS, SCHEMA
 from .run_record import RESULT_FILE_NAME
 from .runs import RUN_ID_PATTERN, Entry, Registry, RegistryError
 from .state import Unknown
 from .write_flow import PATCH_FILE
 
-#: Fields whose one authority is a state record a later lot introduces (lot 2 plan P6):
-#: a review's result (spec §3.8.1, §3.8.6), and the findings a fix reads (§3.6). ``ha
-#: show`` never takes them from ``run.json``; lot 4 fills them from the state.
+#: Fields a report never supplies for a run they do not belong to (lot 2 plan P6): a
+#: review's verdict and check come from its records in the state (§3.8.1, §3.8.6), its
+#: ``cleanup`` from its own report, and ``findings_from`` from a fix's registry entry.
 LATER_AUTHORITIES: Final = ("verdict", "vendor_check", "cleanup", "findings_from")
 #: What the engine writes for a write run only; a run outside any lineage has none of them.
 _WRITE_FIELDS: Final = ("lineage", "branch", "base", "head", "commits")
@@ -241,6 +241,8 @@ def rebuild(run_id: str, *, state: Path, runs_root: Path) -> Shown:
         document["commits"] = _commits(document.get("commits"), recorded)
     else:
         document.update(dict.fromkeys(_WRITE_FIELDS))
+    if is_review(entry):
+        unknown = _review_records(document, report, state, run_id, notes) or unknown
     if status is None:
         status = registry.effective_status(entry, lineage_status)
     if report is not None and report.get("status") != status:
@@ -248,16 +250,81 @@ def rebuild(run_id: str, *, state: Path, runs_root: Path) -> Shown:
             f"run.json says {report.get('status')}; the state says {status}: shown from the state"
         )
     document["status"] = status
+    patched = entry.lineage is not None or is_review(entry)
     return Shown(
         report=document,
         task=read_task(entry.run_dir) if own else None,
-        diffstat=read_diffstat(entry.run_dir) if own and entry.lineage is not None else None,
+        diffstat=read_diffstat(entry.run_dir) if own and patched else None,
         notes=tuple(notes),
         warnings=tuple(warnings),
         unknown=unknown,
     )
 
 
+def is_review(entry: Entry) -> bool:
+    return entry.target.get("kind") == "workflow" and entry.target.get("shape") == "review"
+
+
+def _review_records(
+    document: dict[str, object],
+    report: Mapping[str, object] | None,
+    state: Path,
+    run_id: str,
+    notes: list[str],
+) -> bool:
+    """A review's head, verdict, text and check from its records (§3.8.6); ``True`` when one
+    cannot be read. Its ``cleanup`` has no record in the state: the report's, for display."""
+    unknown = False
+    result = None
+    if reviews.result_path(state, run_id).exists():
+        try:
+            result = reviews.load_result(state, run_id)
+        except Unknown as exc:
+            notes.append(f"{exc}: the review result cannot be read")
+            unknown = True
+    check = result.check if result is not None else None
+    if result is None:
+        try:
+            check = reviews.load_check(state, run_id)
+        except Unknown as exc:
+            notes.append(f"{exc}: the vendor check cannot be read")
+            unknown = True
+    document.update(
+        vendor_check=check.to_document() if check is not None else None,
+        cleanup=report.get("cleanup") if report is not None else None,
+        base=report.get("base") if report is not None else None,
+    )
+    if result is not None:
+        document.update(verdict=result.verdict, head=result.head, text=result.text)
+    return unknown
+
+
+def from_dir(run_dir: Path) -> Shown:
+    """``ha show --dir PATH``: a run directory's ``run.json``, for display only (§3.8.6).
+
+    A review kept with ``--run-dir`` stays readable after the state directory is
+    lost; nothing reads a report found by path to decide anything.
+    """
+    path = run_dir / RUN_JSON
+    try:
+        document = load_json(path.read_text(encoding="utf-8"))
+    except (OSError, UnicodeDecodeError, ValueError):
+        raise NotShown(f"{path} cannot be read: not a run directory of ha") from None
+    run_id = document.get("run_id") if isinstance(document, dict) else None
+    if not isinstance(document, dict) or not (
+        isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id)
+    ):
+        raise NotShown(f"{path} is not a run.json of ha")
+    return Shown(
+        report={key: document.get(key) for key in RUN_KEYS},
+        task=read_task(run_dir),
+        diffstat=read_diffstat(run_dir),
+        notes=(f"display only: {path}, not the state directory",),
+        warnings=(),
+        unknown=False,
+    )
+
+
 _STATUS_WORDS: Final[Mapping[str, str]] = {
     "no_change": "no change",
     "changes": "changes requested",
@@ -349,8 +416,48 @@ def _step_cells(step: Mapping[str, object]) -> list[str]:
         format_tokens(step.get("tokens")),
         format_cost(step.get("cost_usd")),
         format_tools(step.get("tools")),
-        verdict if isinstance(verdict, str) else "",
+        verdict.upper() if isinstance(verdict, str) else "",
+    ]
+
+
+def _plural(count: int, word: str) -> str:
+    return f"{count} {word}{'' if count == 1 else 's'}"
+
+
+def vendors_line(check: Mapping[str, object]) -> str:
+    """``vendors authors codex, opencode (2 recorded commits) — reviewers agy, claude:
+    independent``: the check a review passed before its reviewers started (§3.8.6)."""
+    raw_commits, raw_authors = check.get("commits"), check.get("authors")
+    raw_reviewers = check.get("reviewers")
+    commits = (
+        [c for c in raw_commits if isinstance(c, dict)] if isinstance(raw_commits, list) else []
+    )
+    authors = [str(a) for a in raw_authors] if isinstance(raw_authors, list) else []
+    reviewers = sorted(
+        {
+            str(provider)
+            for providers in (raw_reviewers.values() if isinstance(raw_reviewers, dict) else [])
+            if isinstance(providers, list)
+            for provider in providers
+        }
+    )
+    recorded = sum(1 for c in commits if c.get("run_id") is not None)
+    hand = sum(1 for c in commits if c.get("made_by") == "hand")
+    attributed = sum(1 for c in commits if c.get("made_by") == "unknown")
+    parts = [
+        text
+        for count, text in (
+            (recorded, _plural(recorded, "recorded commit")),
+            (hand, f"{hand} hand-written"),
+            (attributed, f"{attributed} attributed to unconfined writers"),
+        )
+        if count
     ]
+    detail = f" ({', '.join(parts)})" if parts else ""
+    return (
+        f"vendors authors {', '.join(authors) or 'none'}{detail} — reviewers "
+        f"{', '.join(reviewers) or 'none'}: independent"
+    )
 
 
 def _table(rows: Sequence[Sequence[str]]) -> list[str]:
@@ -379,19 +486,30 @@ def render(shown: Shown) -> str:
     reason = report.get("failure_reason")
     if isinstance(reason, str):
         header += f" ({reason})"
+    cleanup = report.get("cleanup")
+    if isinstance(cleanup, dict) and cleanup.get("status") == "failed":
+        # §3.10: a review whose worktree could not be removed says so on its header.
+        header += f"  cleanup failed: {cleanup.get('reason')}"
     lines = [header, f"task    {shown.task or '-'}"]
-    branch = report.get("branch")
+    branch, head, check = report.get("branch"), report.get("head"), report.get("vendor_check")
+    line = None
     if isinstance(branch, str):
         commits = report.get("commits")
         count = len(commits) if isinstance(commits, list) else 0
-        head = report.get("head")
         line = (
             f"head    {branch} @ {head[:7] if isinstance(head, str) else '-'}  "
-            f"{count} commit{'' if count == 1 else 's'}"
+            f"{_plural(count, 'commit')}"
         )
+    elif isinstance(head, str) and isinstance(check, dict):
+        commits = check.get("commits")
+        count = len(commits) if isinstance(commits, list) else 0
+        line = f"head    {head[:7]}  {_plural(count, 'commit')}"
+    if line is not None:
         if shown.diffstat is not None:
             line += f"  {format_diffstat(shown.diffstat)}"
         lines.append(line)
+    if isinstance(check, dict):
+        lines.append(vendors_line(check))
     lines.extend(f"warning {warning}" for warning in shown.warnings)
     steps = report.get("steps")
     rows = (
@@ -417,10 +535,13 @@ __all__ = [
     "format_duration",
     "format_tokens",
     "format_tools",
+    "from_dir",
+    "is_review",
     "load_json",
     "read_diffstat",
     "read_run_dir",
     "read_task",
     "rebuild",
     "render",
+    "vendors_line",
 ]
```

- [ ] **Step 4: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_cli.py tests/unit/headless_agents/test_show.py -q -p no:cacheprovider`
Expected: `test_cli.py: 102 passed; test_show.py: 57 passed`. Then the gates of the Global Constraints.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/README.md packages/headless-agents/src/headless_agents/cli.py packages/headless-agents/src/headless_agents/show.py tests/unit/headless_agents/test_cli.py tests/unit/headless_agents/test_show.py
git commit -m "feat(headless-agents): ha show renders a review from its records, and ha show --dir a run directory"
```

### Task 6: `ha clean` of a review

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/engine.py`
- Modify: `packages/headless-agents/src/headless_agents/review_flow.py`
- Modify: `tests/unit/headless_agents/test_review.py`

**Interfaces:**
- Consumes: `review_flow.WORKTREE`, `review_flow.remove_worktree` (made public here).
- Produces: `engine._remove_review_worktree`; `ha clean` removes a review's kept worktree
  through git, and runs no git under a quarantine (P9).

- [ ] **Step 1: Write the failing tests.** Apply (`git apply`):

```diff
diff --git a/tests/unit/headless_agents/test_review.py b/tests/unit/headless_agents/test_review.py
index 014333d2..459476dd 100644
--- a/tests/unit/headless_agents/test_review.py
+++ b/tests/unit/headless_agents/test_review.py
@@ -538,12 +538,12 @@ def test_a_failed_cleanup_keeps_the_worktree_and_the_verdicts_exit(
     from headless_agents import review_flow
 
     world.commit_by_hand()
-    real = review_flow._remove_worktree
+    real = review_flow.remove_worktree
 
     def failing(*args: object, **kwargs: object) -> str | None:
         return "fatal: simulated"
 
-    monkeypatch.setattr(review_flow, "_remove_worktree", failing)
+    monkeypatch.setattr(review_flow, "remove_worktree", failing)
     outcome = world.review()
     assert outcome.exit_code == 0
     report = _report(outcome)
@@ -597,3 +597,63 @@ def test_every_role_of_the_panel_needs_its_isolation_proof(world: World) -> None
     with pytest.raises(UsageError, match="agy agy 1.0 has no passing isolation proof"):
         world.review("panel")
     assert all(agent.specs == [] for agent in world.agents.values())
+
+
+# ── ha clean of a review (§3.9) ─────────────────────────────────────────────
+
+
+def _kept_worktree(world: World, monkeypatch: pytest.MonkeyPatch) -> engine.Outcome:
+    from headless_agents import review_flow
+
+    world.commit_by_hand()
+    monkeypatch.setattr(review_flow, "remove_worktree", lambda *a, **k: "fatal: simulated")
+    outcome = world.review()
+    monkeypatch.undo()
+    assert (outcome.run_dir / "wt").is_dir()
+    return outcome
+
+
+def _clean(world: World, run_id: str) -> int:
+    return engine.clean(
+        run_id,
+        environ={"PATH": os.environ["PATH"], "HOME": str(world.home)},
+        home=world.home,
+        say=world.said.append,
+    )
+
+
+def test_ha_clean_removes_a_kept_review_worktree_through_git(
+    world: World, monkeypatch: pytest.MonkeyPatch
+) -> None:
+    outcome = _kept_worktree(world, monkeypatch)
+    assert _clean(world, outcome.run_id) == 0, world.said
+    assert not outcome.run_dir.exists()
+    assert str(outcome.run_dir / "wt") not in _git(world.repo, "worktree", "list")
+    entry = world.registry().resolve(outcome.run_id)
+    assert entry.cleaned_at is not None and entry.status == "approved"
+    assert reviews.load_result(world.state, outcome.run_id).verdict == "approve"
+
+
+def test_ha_clean_of_a_review_runs_no_git_under_a_quarantine(
+    world: World, monkeypatch: pytest.MonkeyPatch
+) -> None:
+    outcome = _kept_worktree(world, monkeypatch)
+    quarantine.publish(
+        world.state,
+        "repository",
+        reason="tripwire",
+        run_id="x",
+        paths=[],
+        common_dir=(world.repo / ".git").resolve(),
+    )
+    assert _clean(world, outcome.run_id) == 1
+    assert (outcome.run_dir / "wt").is_dir()
+    assert any("quarantine" in line for line in world.said)
+    assert world.registry().resolve(outcome.run_id).cleaned_at is None
+
+
+def test_ha_clean_of_a_finished_review_removes_its_directory(world: World) -> None:
+    world.commit_by_hand()
+    outcome = world.review()
+    assert _clean(world, outcome.run_id) == 0
+    assert not outcome.run_dir.exists()
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_review.py -q -p no:cacheprovider`
Expected: `test_review.py: 3 failed, 28 passed` — every failure is the missing feature:
  - `test_review.py -- test_a_failed_cleanup_keeps_the_worktree_and_the_verdicts_exit: AttributeError: module 'headless_agents.review_flow' has no attribute 'remo...`
  - `test_review.py -- test_ha_clean_removes_a_kept_review_worktree_through_git: AttributeError: <module 'headless_agents.review_flow' from '/home/hawixs/ha...`
  - `test_review.py -- test_ha_clean_of_a_review_runs_no_git_under_a_quarantine: AttributeError: <module 'headless_agents.review_flow' from '/home/hawixs/ha...`

- [ ] **Step 3: Implement.** Apply:

```diff
diff --git a/packages/headless-agents/src/headless_agents/engine.py b/packages/headless-agents/src/headless_agents/engine.py
index dd59ffaa..85fe6044 100644
--- a/packages/headless-agents/src/headless_agents/engine.py
+++ b/packages/headless-agents/src/headless_agents/engine.py
@@ -1325,6 +1325,19 @@ def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
         )
 
 
+def _remove_review_worktree(
+    worktree: Path, repository: Path, state: Path, environ: Mapping[str, str]
+) -> str | None:
+    """``git worktree remove`` of a review's kept worktree; the reason when it cannot."""
+    try:
+        identity = discover(repository)
+    except RepoError as exc:
+        return str(exc)
+    if identity is None:
+        return f"{repository} is no longer a git repository"
+    return review_flow.remove_worktree(worktree, identity, operator_environment(environ), state)
+
+
 def clean(
     run_id: str, *, environ: Mapping[str, str], home: Path, say: Callable[[str], None]
 ) -> int:
@@ -1385,6 +1398,14 @@ def clean(
             except LockTimeout as exc:
                 raise UsageError(f"{exc}: the lineage is in use; nothing cleaned") from None
         started = (entry.run_dir / RUN_JSON).is_file()
+        worktree = entry.run_dir / review_flow.WORKTREE
+        if worktree.is_dir() and entry.repository is not None:
+            # A review whose cleanup failed kept its detached worktree: removed through
+            # git, and no git at all under a quarantine (§3.9).
+            reason = _remove_review_worktree(worktree, entry.repository, state, environ)
+            if reason is not None:
+                say(f"{run_id}: its worktree {worktree} is kept ({reason}); nothing cleaned")
+                return 1
         if entry.run_dir.is_dir():
             shutil.rmtree(entry.run_dir)
         if not started:
diff --git a/packages/headless-agents/src/headless_agents/review_flow.py b/packages/headless-agents/src/headless_agents/review_flow.py
index 44411c75..8f7680a9 100644
--- a/packages/headless-agents/src/headless_agents/review_flow.py
+++ b/packages/headless-agents/src/headless_agents/review_flow.py
@@ -242,7 +242,7 @@ def prepare(
     return Prepared(head=head, merge_base=merge_base, patch=patch, check=check, worktree=worktree)
 
 
-def _remove_worktree(
+def remove_worktree(
     worktree: Path, identity: RepoIdentity, environ: Mapping[str, str], state: Path
 ) -> str | None:
     """Remove the detached worktree through git; the reason when it cannot."""
@@ -278,7 +278,7 @@ def finish(
             text=text or "",
             check=prepared.check,
         )
-    reason = _remove_worktree(prepared.worktree, identity, environ, state)
+    reason = remove_worktree(prepared.worktree, identity, environ, state)
     return {"status": "done"} if reason is None else {"status": "failed", "reason": reason}
 
 
@@ -287,6 +287,8 @@ __all__ = [
     "DEFAULT_BASE",
     "Prepared",
     "ReviewRefused",
+    "WORKTREE",
     "finish",
     "prepare",
+    "remove_worktree",
 ]
```

- [ ] **Step 4: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_review.py -q -p no:cacheprovider`
Expected: `test_review.py: 31 passed`. Then the gates of the Global Constraints.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/engine.py packages/headless-agents/src/headless_agents/review_flow.py tests/unit/headless_agents/test_review.py
git commit -m "feat(headless-agents): ha clean removes a review's kept worktree through git, and runs no git under a quarantine"
```

### Task 7: `implement --findings`

**Files:**
- Modify: `packages/headless-agents/src/headless_agents/engine.py`
- Modify: `packages/headless-agents/src/headless_agents/runs.py`
- Modify: `packages/headless-agents/src/headless_agents/show.py`
- Modify: `packages/headless-agents/src/headless_agents/write_flow.py`
- Modify: `tests/unit/headless_agents/test_review.py`
- Modify: `tests/unit/headless_agents/test_runs.py`
- Modify: `tests/unit/headless_agents/test_show.py`

**Interfaces:**
- Consumes: `reviews.load_result` (Task 3), `templates.fix_prompt` (Task 1).
- Produces: `Request.findings_run`; `Plan.findings_from`; `Entry.findings_from` and
  `Registry.create(..., findings_from=None)`; `write_flow.run_write_step(...,
  findings_head=None)`; the commit subject `chore(ha): <run_id> fix via …`; `run.json` and
  `ha show` copy `findings_from` from the entry (P8).

- [ ] **Step 1: Write the failing tests.** Apply (`git apply`):

```diff
diff --git a/tests/unit/headless_agents/test_review.py b/tests/unit/headless_agents/test_review.py
index 459476dd..d8c86ed4 100644
--- a/tests/unit/headless_agents/test_review.py
+++ b/tests/unit/headless_agents/test_review.py
@@ -657,3 +657,123 @@ def test_ha_clean_of_a_finished_review_removes_its_directory(world: World) -> No
     outcome = world.review()
     assert _clean(world, outcome.run_id) == 0
     assert not outcome.run_dir.exists()
+
+
+# ── implement --findings (§3.6) ─────────────────────────────────────────────
+
+
+def _fix(root: Path) -> None:
+    (root / "app.py").write_text("print('v1')\nFLAG = 1\nprint(FLAG)\n")
+
+
+def test_the_session_loop_implement_review_fix_review(world: World) -> None:
+    """§4: implement, review --run (CHANGES), implement --continue --findings committing a
+    fix on the same branch, review --run (APPROVE)."""
+    built = world.implement()
+    world.agents["claude"].answer = CHANGES
+    first = world.review("check", review_run=built.run_id)
+    assert first.exit_code == 6
+    world.agents["codex"].edit = _fix
+    fixed = world.implement("Keep it short.", continue_run=built.run_id, findings_run=first.run_id)
+    subjects = _git(world.repo, "log", "--format=%s", f"main..ha/{built.run_id}").splitlines()
+    assert subjects[0] == f"chore(ha): {fixed.run_id} fix via codex/codex-served"
+    prompt = world.agents["codex"].specs[-1].prompt
+    assert f"<findings>\n{CHANGES}\n</findings>" in prompt
+    assert "<task>\nKeep it short.\n</task>" in prompt
+    report = _report(fixed)
+    assert report["findings_from"] == first.run_id
+    assert world.registry().resolve(fixed.run_id).findings_from == first.run_id
+    world.agents["claude"].answer = APPROVE
+    second = world.review("check", review_run=built.run_id)
+    assert second.exit_code == 0
+    assert len(_report(second)["vendor_check"]["commits"]) == 2  # type: ignore[index]
+
+
+def test_findings_take_no_task_and_never_read_stdin(world: World) -> None:
+    built = world.implement()
+    world.agents["claude"].answer = CHANGES
+    review = world.review("check", review_run=built.run_id)
+    world.agents["codex"].edit = _fix
+    world.implement(None, continue_run=built.run_id, findings_run=review.run_id, stdin_is_tty=True)  # type: ignore[arg-type]
+    assert "<task>\nAddress the findings below.\n</task>" in world.agents["codex"].specs[-1].prompt
+
+
+def test_findings_still_read_after_ha_clean_of_the_review(world: World) -> None:
+    """§3.6: read from the review result in the state, never from its report."""
+    built = world.implement()
+    world.agents["claude"].answer = CHANGES
+    review = world.review("check", review_run=built.run_id)
+    assert _clean(world, review.run_id) == 0
+    world.agents["codex"].edit = _fix
+    fixed = world.implement(continue_run=built.run_id, findings_run=review.run_id)
+    assert fixed.exit_code == 0
+
+
+def test_findings_of_a_review_without_a_verdict_are_refused(world: World) -> None:
+    built = world.implement()
+    world.agents["claude"].answer = "no verdict here"
+    review = world.review("check", review_run=built.run_id)
+    assert review.exit_code == 1
+    with pytest.raises(UsageError, match=f"--findings {review.run_id}: .*review result"):
+        world.implement(continue_run=built.run_id, findings_run=review.run_id)
+
+
+def test_stale_findings_are_refused_and_the_lineage_left_as_it_was(world: World) -> None:
+    """§3.6: findings about another revision would steer the implementer against code it
+    cannot see -- the lineage's tip moved since the review pinned its head."""
+    built = world.implement()
+    world.agents["claude"].answer = CHANGES
+    review = world.review("check", review_run=built.run_id)
+    world.agents["codex"].edit = lambda root: (root / "app.py").write_text("moved\n")
+    world.implement("Move on.", continue_run=built.run_id)
+    before = lineage.load(world.state, built.run_id)
+    runs = sorted(world.registry().run_ids())
+    with pytest.raises(UsageError, match="another revision"):
+        world.implement(continue_run=built.run_id, findings_run=review.run_id)
+    assert lineage.load(world.state, built.run_id) == before
+    assert sorted(world.registry().run_ids()) == runs
+
+
+def test_a_new_run_takes_findings_about_its_base(world: World) -> None:
+    head = world.commit_by_hand()
+    world.agents["claude"].answer = CHANGES
+    review = world.review()
+    assert reviews.load_result(world.state, review.run_id).head == head
+    world.agents["codex"].edit = _fix
+    fixed = world.implement(findings_run=review.run_id)
+    assert _git(world.repo, "log", "-1", "--format=%s", f"ha/{fixed.run_id}").startswith(
+        f"chore(ha): {fixed.run_id} fix via"
+    )
+
+
+def test_a_new_run_from_another_base_refuses_the_findings_and_leaves_no_lineage(
+    world: World,
+) -> None:
+    world.commit_by_hand()
+    world.agents["claude"].answer = CHANGES
+    review = world.review()
+    owners = lineage.owners(world.state)
+    runs = sorted(world.registry().run_ids())
+    with pytest.raises(UsageError, match="another revision"):
+        world.implement(findings_run=review.run_id, base="main~1")
+    assert lineage.owners(world.state) == owners
+    assert sorted(world.registry().run_ids()) == runs
+    assert _git(world.repo, "worktree", "list").count("\n") == 1
+
+
+@pytest.mark.parametrize(
+    ("target", "rule"),
+    [
+        ("check", "--findings needs an implement workflow"),
+        ("codex", "--findings needs an implement workflow"),
+    ],
+)
+def test_findings_belong_to_an_implement_workflow(world: World, target: str, rule: str) -> None:
+    with pytest.raises(UsageError, match=rule):
+        plan(world.request(target, "x", findings_run="20260926T000000-aaaaaaaa"))
+
+
+def test_findings_must_name_a_review_run(world: World) -> None:
+    built = world.implement()
+    with pytest.raises(UsageError, match=f"--findings {built.run_id}: not a review run"):
+        plan(world.request("build", "x", findings_run=built.run_id))
diff --git a/tests/unit/headless_agents/test_runs.py b/tests/unit/headless_agents/test_runs.py
index a91dac75..50bffbac 100644
--- a/tests/unit/headless_agents/test_runs.py
+++ b/tests/unit/headless_agents/test_runs.py
@@ -338,9 +338,34 @@ def test_create_writes_the_continuation_records(tmp_path: Path) -> None:
     )
     entry = registry.resolve(run_id)
     assert entry.continues == owner and entry.providers == ("opencode", "codex")
+    assert entry.findings_from is None
 
 
-@pytest.mark.parametrize("key", ["continues", "providers"])
+def test_create_writes_the_review_a_fix_takes_its_findings_from(tmp_path: Path) -> None:
+    """Lot 4: findings_from is a write run's record, like continues (§3.6, §3.10)."""
+    registry = Registry(tmp_path / "state", runs_root=tmp_path / "runs")
+    run_id, review = "20260926T100000-cccccccc", "20260926T095000-eeeeeeee"
+    registry.create(
+        run_id,
+        run_dir=None,
+        target={"kind": "workflow", "name": "build", "shape": "implement"},
+        repository=None,
+        lineage=run_id,
+        findings_from=review,
+    )
+    assert registry.resolve(run_id).findings_from == review
+
+
+def test_findings_outside_any_lineage_are_unknown(tmp_path: Path) -> None:
+    """A fix is a write: an entry with findings_from and no lineage is not ha's writing."""
+    registry, run_id, path, document = _entry_document(tmp_path)
+    document["findings_from"] = "20260926T090000-dddddddd"
+    path.write_text(json.dumps(document))
+    with pytest.raises(Unknown, match="findings_from names a review, but the entry has no lineage"):
+        registry.resolve(run_id)
+
+
+@pytest.mark.parametrize("key", ["continues", "providers", "findings_from"])
 def test_an_entry_missing_a_continuation_record_is_unknown(tmp_path: Path, key: str) -> None:
     registry, run_id, path, document = _entry_document(tmp_path)
     del document[key]
@@ -354,6 +379,8 @@ def test_an_entry_missing_a_continuation_record_is_unknown(tmp_path: Path, key:
     [
         ("continues", "nope"),
         ("continues", 7),
+        ("findings_from", "nope"),
+        ("findings_from", 7),
         ("providers", None),
         ("providers", "codex"),
         ("providers", [""]),
diff --git a/tests/unit/headless_agents/test_show.py b/tests/unit/headless_agents/test_show.py
index 99fe2e4b..8ecece1a 100644
--- a/tests/unit/headless_agents/test_show.py
+++ b/tests/unit/headless_agents/test_show.py
@@ -674,6 +674,26 @@ def _review(
     return entry.run_dir
 
 
+def test_a_fix_shows_the_review_it_took_its_findings_from_its_entry(home: Home) -> None:
+    """Lot 4: findings_from is a write run's record in its registry entry (§3.10)."""
+    fix = "20260925T160000-aabbccdd"
+    home.write_run(findings_from=OTHER)
+    home.registry().create(
+        fix,
+        run_dir=None,
+        target={"kind": "workflow", "name": "build", "shape": "implement"},
+        repository=home.root / "repo",
+        lineage=RUN,
+        continues=RUN,
+        providers=("codex",),
+        findings_from=REVIEW,
+    )
+    current = lineages.load(home.state, RUN)
+    lineages.save(home.state, replace(current, members={**current.members, fix: "committed"}))
+    assert home.rebuild(fix).report["findings_from"] == REVIEW
+    assert home.rebuild().report["findings_from"] is None
+
+
 def test_a_reviews_head_verdict_text_and_check_come_from_its_result(home: Home) -> None:
     _review(home)
     report = home.rebuild(REVIEW).report
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_review.py tests/unit/headless_agents/test_runs.py tests/unit/headless_agents/test_show.py -q -p no:cacheprovider`
Expected: `test_review.py: 10 failed, 31 passed; test_runs.py: 6 failed, 58 passed; test_show.py: 1 failed, 57 passed` — every failure is the missing feature:
  - `test_review.py -- test_the_session_loop_implement_review_fix_review: TypeError: Request.__init__() got an unexpected keyword argument 'findings_...`
  - `test_review.py -- test_findings_take_no_task_and_never_read_stdin: TypeError: Request.__init__() got an unexpected keyword argument 'findings_...`
  - `test_review.py -- test_findings_still_read_after_ha_clean_of_the_review: TypeError: Request.__init__() got an unexpected keyword argument 'findings_...`
  - `test_review.py -- test_findings_of_a_review_without_a_verdict_are_refused: TypeError: Request.__init__() got an unexpected keyword argument 'findings_...`
  - `test_review.py -- test_stale_findings_are_refused_and_the_lineage_left_as_it_was: TypeError: Request.__init__() got an unexpected keyword argument 'findings_...`
  - `test_review.py -- test_a_new_run_takes_findings_about_its_base: TypeError: Request.__init__() got an unexpected keyword argument 'findings_...`
  - `test_review.py -- test_a_new_run_from_another_base_refuses_the_findings_and_leaves_no_lineage: TypeError: Request.__init__() got an unexpected keyword argument 'findings_...`
  - `test_review.py -- test_findings_must_name_a_review_run: TypeError: Request.__init__() got an unexpected keyword argument 'findings_...`
  - `test_runs.py -- test_create_writes_the_continuation_records: AttributeError: 'Entry' object has no attribute 'findings_from'`
  - `test_runs.py -- test_create_writes_the_review_a_fix_takes_its_findings_from: TypeError: Registry.create() got an unexpected keyword argument 'findings_f...`
  - `test_runs.py -- test_findings_outside_any_lineage_are_unknown: Failed: DID NOT RAISE Unknown`
  - `test_runs.py -- test_an_entry_missing_a_continuation_record_is_unknown[findings_from]: KeyError: 'findings_from'`
  - … and 3 more

- [ ] **Step 3: Implement.** Apply:

```diff
diff --git a/packages/headless-agents/src/headless_agents/engine.py b/packages/headless-agents/src/headless_agents/engine.py
index 85fe6044..3c7628e4 100644
--- a/packages/headless-agents/src/headless_agents/engine.py
+++ b/packages/headless-agents/src/headless_agents/engine.py
@@ -25,7 +25,7 @@ from dataclasses import dataclass, replace
 from pathlib import Path
 from typing import Final
 
-from . import locks, review_flow, write_flow
+from . import locks, review_flow, reviews, write_flow
 from .capability import scoped_environment
 from .chain import run_chain
 from .cli_models import ModelsError, models_for
@@ -65,6 +65,7 @@ from .state import Unknown
 from .templates import (
     ReviewText,
     Verdict,
+    fix_prompt,
     implement_prompt,
     judge_prompt,
     read_verdict,
@@ -125,6 +126,8 @@ class Request:
     head: str | None = None
     #: ``--run RUN_ID`` of a review: an implement run whose lineage's tip is reviewed.
     review_run: str | None = None
+    #: ``--findings RUN_ID`` of an implement run: a review whose verdict was read (§3.6).
+    findings_run: str | None = None
 
 
 @dataclass(frozen=True)
@@ -162,6 +165,9 @@ class Plan:
     #: The run ``--run`` names, and the owner of its lineage, read from the registry.
     reviews: str | None = None
     reviewed_lineage: str | None = None
+    #: The review ``--findings`` names, read from the registry; its result is read by
+    #: ``execute``, which composes the fix prompt from it (§3.6).
+    findings_from: str | None = None
 
 
 def operator_environment(environ: Mapping[str, str]) -> dict[str, str]:
@@ -458,8 +464,48 @@ def _refuse_options_of_other_shapes(request: Request, shape: str | None) -> None
         for flag, value in (("--head", request.head), ("--run", request.review_run)):
             if value is not None:
                 raise UsageError(f"{flag} needs a review workflow as the target")
-    if shape != "implement" and request.continue_run is not None:
-        raise UsageError("--continue needs an implement workflow as the target")
+    if shape != "implement":
+        for flag, value in (
+            ("--continue", request.continue_run),
+            ("--findings", request.findings_run),
+        ):
+            if value is not None:
+                raise UsageError(f"{flag} needs an implement workflow as the target")
+
+
+def _findings_review(run_id: str, *, state: Path, home: Path) -> str:
+    """The review ``--findings`` names, from the registry only (§3.8.2): a review run."""
+    registry = Registry(state, runs_root=runs_root(home))
+    try:
+        entry = registry.resolve(run_id)
+    except RegistryError as exc:
+        raise UsageError(f"--findings: {exc}") from None
+    except Unknown as exc:
+        raise UsageError(f"--findings {run_id}: {exc}; recover it by hand") from None
+    target = entry.target
+    if (
+        entry.lineage is not None
+        or target.get("kind") != "workflow"
+        or target.get("shape") != "review"
+    ):
+        raise UsageError(f"--findings {run_id}: not a review run; findings come from a review")
+    return run_id
+
+
+def _findings(plan: Plan) -> reviews.ReviewResult | None:
+    """The result of the review ``--findings`` names, read from the state (§3.6), never its
+    report -- so it still reads after ``ha clean`` of that review."""
+    if plan.findings_from is None:
+        return None
+    request = plan.request
+    _findings_review(plan.findings_from, state=plan.state, home=request.home)
+    try:
+        return reviews.load_result(plan.state, plan.findings_from)
+    except Unknown as exc:
+        raise UsageError(
+            f"--findings {plan.findings_from}: the review result is missing or unknown ({exc}): "
+            "only a review whose verdict was read has findings"
+        ) from None
 
 
 def _slot_plan(slot: str, role: Role, request: Request, config: Config, name: str) -> SlotPlan:
@@ -544,12 +590,22 @@ def _plan_workflow(request: Request, workflow: Workflow, config: Config) -> Plan
     if rule is not None:
         raise UsageError(f"{workflow.name}: {rule}")
     models = _models(role, request)
-    task = _prompt(request)
-    prompt = implement_prompt(task)
+    state = state_dir(request.environ, home=request.home)
+    findings_from = (
+        _findings_review(request.findings_run, state=state, home=request.home)
+        if request.findings_run is not None
+        else None
+    )
+    if findings_from is not None:
+        # §3.9: with --findings the task is optional guidance; stdin is never read.
+        task = (request.prompt or "").strip()
+        prompt = fix_prompt(task, "")
+    else:
+        task = _prompt(request)
+        prompt = implement_prompt(task)
     _check_prompt_size(role, prompt, _bundle(role, request, None))
     mcp = _mcp(role, request)
     run_dir = _run_dir(request)
-    state = state_dir(request.environ, home=request.home)
     joins = (
         _continued_lineage(request.continue_run, state=state, home=request.home)
         if request.continue_run is not None
@@ -568,6 +624,7 @@ def _plan_workflow(request: Request, workflow: Workflow, config: Config) -> Plan
         task=task,
         continues=request.continue_run,
         joins=joins,
+        findings_from=findings_from,
     )
 
 
@@ -765,6 +822,7 @@ def _admit(
     joins: str | None = None,
     continues: str | None = None,
     providers: Sequence[str] = (),
+    findings_from: str | None = None,
 ) -> Entry:
     """Mint an id, take its lifecycle lock, then publish its entry (§3.8.3 step 1).
 
@@ -805,6 +863,7 @@ def _admit(
                     lineage=joins if joins is not None else (run_id if write else None),
                     continues=continues,
                     providers=providers,
+                    findings_from=findings_from,
                 )
             except FileExistsError:
                 continue
@@ -866,6 +925,7 @@ def _execute_write(
     started: float,
     unconfined: bool,
     say: Callable[[str], None],
+    findings_head: str | None = None,
 ) -> Outcome:
     """A write run -- a role's, or an ``implement`` workflow's: §3.8.3, then its report."""
     role, run_dir = plan.role, entry.run_dir
@@ -893,6 +953,7 @@ def _execute_write(
             unconfined=unconfined,
             joins=plan.joins,
             named=plan.continues,
+            findings_head=findings_head,
         )
     except write_flow.WriteRefused as exc:
         _refused(registry, entry)
@@ -918,6 +979,7 @@ def _execute_write(
         head=outcome.head,
         lineage=entry.lineage,
         continues=entry.continues,
+        findings_from=entry.findings_from,
         implement_providers=list(entry.providers),
         commits=[{"sha": sha, "made_by": made_by} for sha, made_by in outcome.commits],
         failure_reason=outcome.failure_reason,
@@ -1165,6 +1227,9 @@ def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
         )
         if lineage != plan.reviewed_lineage:
             raise UsageError(f"--run {plan.reviews}: its lineage changed since the plan")
+    findings = _findings(plan)
+    if findings is not None:
+        plan = replace(plan, prompt=fix_prompt(plan.task or "", findings.text))
 
     runs = runs_root(request.home)
     registry = Registry(plan.state, runs_root=runs)
@@ -1197,6 +1262,7 @@ def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
                 continues=plan.continues,
                 # A write run's record (§3.10); a review writes nothing.
                 providers=() if plan.panel else role.providers,
+                findings_from=plan.findings_from,
             )
         except (LockTimeout, RegistryError) as exc:
             # A run that never started leaves nothing behind (codex review of #207).
@@ -1285,6 +1351,7 @@ def execute(plan: Plan, *, say: Callable[[str], None]) -> Outcome:
                 step_name=step_name,
                 started=started,
                 say=say,
+                findings_head=findings.head if findings is not None else None,
             )
         say(f"step 1 run {role.name}: started")
         final = _run_links(
diff --git a/packages/headless-agents/src/headless_agents/runs.py b/packages/headless-agents/src/headless_agents/runs.py
index 000bfc46..ea8a53b6 100644
--- a/packages/headless-agents/src/headless_agents/runs.py
+++ b/packages/headless-agents/src/headless_agents/runs.py
@@ -52,6 +52,8 @@ class Entry:
     #: The providers of every link of the run's role, as it ran: a write run's
     #: ``implement_providers`` in its report (§3.10).
     providers: tuple[str, ...]
+    #: The review ``--findings`` named, for a fix; ``None`` otherwise (§3.6, §3.10).
+    findings_from: str | None = None
 
 
 def _optional_path(value: object) -> Path | None:
@@ -98,13 +100,14 @@ class Registry:
         lineage: str | None,
         continues: str | None = None,
         providers: Sequence[str] = (),
+        findings_from: str | None = None,
     ) -> Entry:
         """Create the entry of ``run_id`` once; ``FileExistsError`` when it is taken.
 
         The engine calls this while holding the id's lifecycle lock, so no
         registered run is ever seen with a free lock before it starts (§3.8.3).
-        ``continues`` and ``providers`` are the continuation records of §3.10:
-        their one authority is this entry, written once, never the report.
+        ``continues``, ``providers`` and ``findings_from`` are a write run's records
+        (§3.10): their one authority is this entry, written once, never the report.
         """
         path = run_dir if run_dir is not None else self.runs_root / run_id
         document: dict[str, object] = {
@@ -118,6 +121,7 @@ class Registry:
             "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "continues": continues,
             "providers": list(providers),
+            "findings_from": findings_from,
         }
         create_once(self._path(run_id), document)
         return self._entry(document, self._path(run_id))
@@ -179,6 +183,14 @@ class Registry:
         if continues is not None and document.get("lineage") is None:
             # A continuation joins a lineage by definition (§3.6).
             raise Unknown(f"{path}: continues names a run, but the entry has no lineage")
+        findings_from = document.get("findings_from", _MISSING)
+        if findings_from is not None and (
+            not isinstance(findings_from, str) or not RUN_ID_PATTERN.fullmatch(findings_from)
+        ):
+            raise Unknown(f"{path}: findings_from is malformed")
+        if findings_from is not None and document.get("lineage") is None:
+            # A fix is a write, and a write belongs to a lineage (§3.6).
+            raise Unknown(f"{path}: findings_from names a review, but the entry has no lineage")
         providers = document.get("providers")
         if not isinstance(providers, list) or not all(isinstance(p, str) and p for p in providers):
             raise Unknown(f"{path}: providers is malformed")
@@ -192,6 +204,7 @@ class Registry:
             cleaned_at=_optional_str(document.get("cleaned_at")),
             continues=continues,
             providers=tuple(providers),
+            findings_from=findings_from,
         )
 
     def run_ids(self) -> list[str]:
diff --git a/packages/headless-agents/src/headless_agents/show.py b/packages/headless-agents/src/headless_agents/show.py
index 4f286229..fe6b339b 100644
--- a/packages/headless-agents/src/headless_agents/show.py
+++ b/packages/headless-agents/src/headless_agents/show.py
@@ -208,6 +208,7 @@ def rebuild(run_id: str, *, state: Path, runs_root: Path) -> Shown:
     # §3.10: a write run's continuation records, copied from its registry entry.
     document.update(
         continues=entry.continues,
+        findings_from=entry.findings_from,
         implement_providers=list(entry.providers) if entry.lineage is not None else None,
     )
     status: str | None = None
diff --git a/packages/headless-agents/src/headless_agents/write_flow.py b/packages/headless-agents/src/headless_agents/write_flow.py
index b52ed88c..65fb0ba0 100644
--- a/packages/headless-agents/src/headless_agents/write_flow.py
+++ b/packages/headless-agents/src/headless_agents/write_flow.py
@@ -106,6 +106,8 @@ class _Write:
     lineage: LineageState | None = None
     #: A continuation's lineage as admission read it: restored if preparation refuses.
     before: LineageState | None = None
+    #: The head a review pinned, for a run taking its findings (``--findings``, §3.6).
+    findings_head: str | None = None
 
     def __post_init__(self) -> None:
         self.owner = self.owner or self.run_id
@@ -395,8 +397,9 @@ class _PreparationFailed(Exception):
     pass
 
 
-class _ContinuationRefused(Exception):  # noqa: N818 - a refusal, not a crash
-    """A continuation's worktree cannot be continued: nothing ran (§3.8.3 step 3)."""
+class _PreparationRefused(Exception):  # noqa: N818 - a refusal, not a crash
+    """Preparation refuses the write: nothing ran (§3.8.3 step 3) -- a continuation's
+    worktree that cannot be continued, or findings about another revision (§3.6)."""
 
 
 def _prepare_continued(write: _Write) -> str:
@@ -408,27 +411,48 @@ def _prepare_continued(write: _Write) -> str:
     worktree, branch = write.worktree, write.branch
     code, out, err = write.git(worktree, ["status", "--porcelain"])
     if code != 0:
-        raise _ContinuationRefused(f"git status failed in {worktree}: {err.strip()}")
+        raise _PreparationRefused(f"git status failed in {worktree}: {err.strip()}")
     if out.strip():
-        raise _ContinuationRefused(
+        raise _PreparationRefused(
             f"the worktree {worktree} has uncommitted changes: commit or discard them first"
         )
     code, out, _ = write.git(worktree, ["symbolic-ref", "-q", "HEAD"])
     if code != 0 or out.strip() != f"refs/heads/{branch}":
-        raise _ContinuationRefused(
+        raise _PreparationRefused(
             f"the worktree {worktree} is not on its branch {branch}: check it out again first"
         )
-    base = write.current.base
-    if _tip(write) is None or base is None:
-        raise _ContinuationRefused(f"the branch {branch} or the lineage's base is missing")
+    base, tip = write.current.base, _tip(write)
+    if tip is None or base is None:
+        raise _PreparationRefused(f"the branch {branch} or the lineage's base is missing")
+    _check_findings(write, tip)
     write.git_dir = resolve_git_dir(worktree)
     return base
 
 
+def _check_findings(write: _Write, start: str) -> None:
+    """Findings are about the commit this run starts from, or they are refused (§3.6).
+
+    Findings about another revision would steer the implementer against code it
+    cannot see; a session that wants them anyway passes them in the task.
+    """
+    head = write.findings_head
+    if head is not None and head != start:
+        raise _PreparationRefused(
+            f"--findings: the review read {head[:12]}, this run starts from {start[:12]}: "
+            "findings about another revision are refused; pass them in the task instead"
+        )
+
+
 def _withdraw(write: _Write) -> None:
-    """A refused continuation leaves its lineage as admission found it: nothing ran."""
-    assert write.before is not None, "admission read the lineage"
-    write.save(write.before)
+    """A refused preparation leaves the lineages as admission found them: nothing ran.
+
+    A continuation's lineage is restored; a new lineage, whose state its intent
+    created and nothing else touched yet (no worktree, no branch), is removed.
+    """
+    if write.before is not None:
+        write.save(write.before)
+    else:
+        lineages.lineage_path(write.state, write.owner).unlink(missing_ok=True)
     if write.unconfined:
         # This run never ran: it names no unconfined writer. Left listed, it would make
         # every later commit without provenance its providers' (Opus review of lot 3).
@@ -458,6 +482,7 @@ def _prepare(write: _Write) -> str:
     if code != 0:
         raise _PreparationFailed(f"cannot resolve --base {base_ref!r}: {err.strip()}")
     base = out.strip()
+    _check_findings(write, base)
     code, _, err = write.git(
         repository, ["worktree", "add", "-q", "-b", write.branch, str(write.worktree), base]
     )
@@ -696,12 +721,14 @@ def run_write_step(
     unconfined: bool = False,
     joins: str | None = None,
     named: str | None = None,
+    findings_head: str | None = None,
 ) -> WriteOutcome:
     """§3.8.3 for one write run; :class:`WriteRefused` when admission refuses.
 
     ``joins`` names the lineage a continuation joins (its owner) and ``named``
     the member ``--continue`` named (§3.6); without them the write starts a
-    lineage of its own.
+    lineage of its own. ``findings_head`` is the head a review pinned, for a
+    run taking its findings: preparation refuses unless the run starts there.
     """
     with ExitStack() as locks:
         write = _Write(
@@ -716,6 +743,7 @@ def run_write_step(
             locks=locks,
             owner=joins or run_id,
             named=named,
+            findings_head=findings_head,
         )
         with ExitStack() as registry:
             try:
@@ -727,7 +755,7 @@ def run_write_step(
         # The registry lock is released: the lineage exists with its intent.
         try:
             base = _prepare(write)
-        except _ContinuationRefused as exc:
+        except _PreparationRefused as exc:
             _withdraw(write)
             raise WriteRefused(f"{exc}; nothing ran") from None
         except _PreparationFailed as exc:
@@ -840,7 +868,8 @@ def run_write_step(
             return _outcome(NO_CHANGE_EXIT_CODE, "no_change", None, write, head=head, final=final)
 
         model = final.model_reported or final.model or "unknown"
-        verb = "residue" if failed_step else "implement"
+        # §3.6: "fix" instead of "implement" for a run taking a review's findings.
+        verb = "residue" if failed_step else ("fix" if findings_head is not None else "implement")
         message = f"chore(ha): {run_id} {verb} via {final.provider}/{model}"
         reason, commits, head = _commit(write, message, tip, step_dir)
         _crash_after("commit")
```

- [ ] **Step 4: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_review.py tests/unit/headless_agents/test_runs.py tests/unit/headless_agents/test_show.py -q -p no:cacheprovider`
Expected: `test_review.py: 41 passed; test_runs.py: 64 passed; test_show.py: 58 passed`. Then the gates of the Global Constraints.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/src/headless_agents/engine.py packages/headless-agents/src/headless_agents/runs.py packages/headless-agents/src/headless_agents/show.py packages/headless-agents/src/headless_agents/write_flow.py tests/unit/headless_agents/test_review.py tests/unit/headless_agents/test_runs.py tests/unit/headless_agents/test_show.py
git commit -m "feat(headless-agents): implement --findings takes a review's deciding text, about the commit it starts from"
```

### Task 8: the CLI: `--head`, `--run`, `--findings`, the review's output, the help

**Files:**
- Modify: `packages/headless-agents/README.md`
- Modify: `packages/headless-agents/src/headless_agents/cli.py`
- Modify: `packages/headless-agents/src/headless_agents/engine.py`
- Modify: `tests/unit/headless_agents/test_cli.py`
- Modify: `tests/unit/headless_agents/test_review.py`

**Interfaces:**
- Consumes: every earlier task.
- Produces: `engine.prompt_is_optional(target, *, findings, environ, home) -> bool`; the
  `ha run` options `--findings RUN_ID`, `--head REF`, `--run RUN_ID`; a review's stdout
  `run: <id>` / `head: <sha>` / its deciding text; the help's workflow exit codes; the
  README (P10).

- [ ] **Step 1: Write the failing tests.** Apply (`git apply`):

```diff
diff --git a/tests/unit/headless_agents/test_cli.py b/tests/unit/headless_agents/test_cli.py
index 8c56b111..d71c7480 100644
--- a/tests/unit/headless_agents/test_cli.py
+++ b/tests/unit/headless_agents/test_cli.py
@@ -588,6 +588,93 @@ def test_the_readme_synopsis_lists_ha_workflows() -> None:
     assert "ha workflows [--json]" in readme
 
 
+# ── a review and --findings through the CLI (lot 4) ─────────────────────────
+
+
+class _UnreadableStdin(io.StringIO):
+    """A piped stdin a target whose prompt is optional must never read (§3.9)."""
+
+    def read(self, *args: object) -> str:  # type: ignore[override]
+        raise AssertionError("stdin was read")
+
+
+def _review_workflows(world: _World) -> None:
+    world.roles(
+        '[reviewer]\nprovider = "claude"\n\n[implementer]\nprovider = "codex"\nwrite = true\n'
+    )
+    _workflows(
+        world,
+        '[check]\nshape = "review"\nreview = "reviewer"\n\n'
+        '[build]\nshape = "implement"\nimplement = "implementer"\n',
+    )
+
+
+def _run_with_stdin(world: _World, stdin: io.StringIO, *argv: str) -> tuple[int, str, str]:
+    out, err = io.StringIO(), io.StringIO()
+    code = cli.main(
+        list(argv),
+        environ=world.environ,
+        stdin=stdin,
+        stdout=out,
+        stderr=err,
+        cwd=world.repo,
+        home=world.home,
+    )
+    return code, out.getvalue(), err.getvalue()
+
+
+def test_a_review_without_a_prompt_never_reads_a_piped_stdin(world: _World) -> None:
+    _review_workflows(world)
+    code, _, err = _run_with_stdin(world, _UnreadableStdin("piped"), "run", "check")
+    # The engine goes on to the review, which the test repository cannot give a base to.
+    assert code == 2 and "stdin was read" not in err
+
+
+def test_findings_without_a_prompt_never_read_a_piped_stdin(world: _World) -> None:
+    _review_workflows(world)
+    run_id = "20260926T000000-aaaaaaaa"
+    code, _, err = _run_with_stdin(
+        world, _UnreadableStdin("piped"), "run", "build", "--findings", run_id
+    )
+    assert code == 2 and f"no run {run_id}" in err
+
+
+def test_a_dash_still_reads_stdin_for_a_review(world: _World) -> None:
+    _review_workflows(world)
+    code, _, err = _run_with_stdin(world, io.StringIO("Mind the errors."), "run", "check", "-")
+    assert code == 2 and "stdin was read" not in err
+
+
+@pytest.mark.parametrize(
+    ("argv", "rule"),
+    [
+        (("run", "check", "--run", "20260926T000000-aaaaaaaa", "--head", "HEAD"), "--run excludes"),
+        (("run", "build", "--head", "HEAD", "task"), "--head needs a review workflow"),
+        (
+            ("run", "check", "--findings", "20260926T000000-aaaaaaaa"),
+            "--findings needs an implement",
+        ),
+    ],
+)
+def test_the_cli_passes_head_run_and_findings_to_the_engine(
+    world: _World, argv: tuple[str, ...], rule: str
+) -> None:
+    _review_workflows(world)
+    code, _, err = world.run(*argv)
+    assert code == 2 and rule in err
+
+
+def test_run_help_names_the_review_exit_codes_and_a_review_example(
+    capsys: pytest.CaptureFixture[str],
+) -> None:
+    with pytest.raises(SystemExit):
+        cli.main(["run", "--help"])
+    text = capsys.readouterr().out
+    assert "  6 changes requested" in text
+    assert "--run RUN_ID" in text and "--findings RUN_ID" in text and "--head REF" in text
+    assert "ha run multi-review --run" in text
+
+
 # ── ha runs / ha clean ─────────────────────────────────────────────────────
 
 
diff --git a/tests/unit/headless_agents/test_review.py b/tests/unit/headless_agents/test_review.py
index d8c86ed4..d96863d0 100644
--- a/tests/unit/headless_agents/test_review.py
+++ b/tests/unit/headless_agents/test_review.py
@@ -659,6 +659,47 @@ def test_ha_clean_of_a_finished_review_removes_its_directory(world: World) -> No
     assert not outcome.run_dir.exists()
 
 
+# ── the CLI's output (§3.9) ─────────────────────────────────────────────────
+
+
+def _cli(world: World, *argv: str) -> tuple[int, str, str]:
+    import io
+
+    from headless_agents import cli
+
+    out, err = io.StringIO(), io.StringIO()
+    code = cli.main(
+        list(argv),
+        environ={"PATH": os.environ["PATH"], "HOME": str(world.home)},
+        stdin=io.StringIO(""),
+        stdout=out,
+        stderr=err,
+        cwd=world.repo,
+        home=world.home,
+    )
+    return code, out.getvalue(), err.getvalue()
+
+
+@pytest.mark.parametrize(("answer", "code"), [(APPROVE, 0), (CHANGES, 6)])
+def test_the_cli_prints_the_deciding_text_of_a_review_whatever_its_verdict(
+    world: World, answer: str, code: int
+) -> None:
+    """A verdict is an answer: exit 6 prints the findings a session feeds to --findings."""
+    world.commit_by_hand()
+    world.agents["claude"].answer = answer
+    exit_code, out, err = _cli(world, "run", "check")
+    assert exit_code == code
+    assert out.startswith("run: ") and out.endswith(answer + "\n")
+    assert "exited" not in err
+
+
+def test_the_cli_says_why_a_review_failed(world: World) -> None:
+    world.commit_by_hand()
+    world.agents["claude"].answer = "no verdict"
+    exit_code, out, err = _cli(world, "run", "check")
+    assert exit_code == 1 and "unreadable_verdict" in err
+
+
 # ── implement --findings (§3.6) ─────────────────────────────────────────────
```

- [ ] **Step 2: Run them.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_cli.py tests/unit/headless_agents/test_review.py -q -p no:cacheprovider`
Expected: `test_cli.py: 6 failed, 103 passed; test_review.py: 3 failed, 41 passed` — every failure is the missing feature:
  - `test_cli.py -- test_a_review_without_a_prompt_never_reads_a_piped_stdin: AssertionError: stdin was read`
  - `test_cli.py -- test_findings_without_a_prompt_never_read_a_piped_stdin: AssertionError: assert (2 == 2 and 'no run 20260926T000000-aaaaaaaa' in 'ha...`
  - `test_cli.py -- test_run_help_names_the_review_exit_codes_and_a_review_example: assert '  6 changes requested' in 'usage: ha run [-h] [-m MODEL] [--effort ...`
  - `test_review.py -- test_the_cli_says_why_a_review_failed: AssertionError: assert (1 == 1 and 'unreadable_verdict' in 'ha: step 1 revi...`

- [ ] **Step 3: Implement.** Apply:

```diff
diff --git a/packages/headless-agents/README.md b/packages/headless-agents/README.md
index 78277ff5..e6f9ed92 100644
--- a/packages/headless-agents/README.md
+++ b/packages/headless-agents/README.md
@@ -370,6 +370,7 @@ ha run TARGET [PROMPT | -] [-m MODEL] [--effort E] [--timeout SECONDS]
        [--context full|global|none] [--context-parents] [--mcp PROFILE]
        [--base-url URL --key-env VAR] [--repo PATH] [--json] [--run-dir DIR]
        [--write [--shell] [--base REF]]
+       [--continue RUN_ID] [--findings RUN_ID] [--head REF] [--run RUN_ID]
 ha roles [--json]
 ha workflows [--json]
 ha providers [--json]
@@ -394,8 +395,18 @@ section is being rewritten with the 0.5.0 lots.
   instead: the next run works in the same worktree, on the same branch, from its tip --
   commits made there by hand included -- once the worktree is clean. A workflow runs its
   roles as declared: `-m`, `--write` and the other role options are refused. `ha workflows`
-  lists what `workflows.toml` declares; `shape = "review"` is validated there and arrives
-  with the vendor rule in a later 0.5.0 lot.
+  lists what `workflows.toml` declares.
+- **A review** (`shape = "review"`: one reviewer role or more, and a judge with two or
+  more) reads a change read-only: `ha run multi-review --run RUN_ID` reviews the current
+  tip of an implement run's lineage from its base; `--head REF` and `--base REF` (default
+  `origin/HEAD`) name any other range. Before anything runs, the vendor rule proves that no
+  reviewer shares a provider with whoever wrote a commit of the range -- from the
+  provenance `ha` records for its own commits -- and refuses the review otherwise (exit
+  `2`). The reviewers run in parallel, the judge weighs their findings, and the last line
+  of the deciding text is the verdict: exit `0` APPROVE, `6` CHANGES. `ha run build
+  --continue RUN_ID --findings REVIEW_ID` then hands those findings to the implementer, on
+  the commit the review read; `ha show REVIEW_ID` renders the review, its vendor check
+  included, from the state directory, and `ha show --dir PATH` a run directory's report.
 
 - **Read-only** (default): a CLI rail reads the current repository through a read-only
   workspace (no write tool; no shell, except codex, whose shell is its only read tool and
diff --git a/packages/headless-agents/src/headless_agents/cli.py b/packages/headless-agents/src/headless_agents/cli.py
index fbd4c12e..22bc82aa 100644
--- a/packages/headless-agents/src/headless_agents/cli.py
+++ b/packages/headless-agents/src/headless_agents/cli.py
@@ -44,6 +44,7 @@ from .engine import (
     executable_for,
     execute,
     plan,
+    prompt_is_optional,
     runs_root,
 )
 from .registry import PROVIDER_NAMES, Probe, UnknownProvider, max_prompt_bytes, probe
@@ -79,19 +80,21 @@ exit codes of a run (a role or a provider):
   124 timeout
   130 interrupted (Ctrl-C); the run reads incomplete
 
-exit codes of a workflow (implement):
-  0 committed
-  5 the implementation changed nothing
-  1 a step failed (its residue committed), the tripwire fired, HEAD moved or a hook
-    refused; the step's own code is in run.json
-  2 invalid usage or configuration, a refused --continue, or its lineage in use for
-    more than 10 s; nothing ran
+exit codes of a workflow (implement, review):
+  0 committed (implement), approved (review)
+  6 changes requested (review)
+  5 the implementation changed nothing (implement)
+  1 a step failed (its residue committed), the tripwire fired, HEAD moved, a hook
+    refused, or the verdict is unreadable; the step's own code is in run.json
+  2 invalid usage or configuration, a refused --run, --continue or --findings, a
+    lineage in use for more than 10 s, or the vendor rule; nothing ran
   130 interrupted (Ctrl-C); the run reads incomplete
 
 examples:
   ha run codex -m gpt-6-luna "Explain what this repository does."
   ha run build "Add a --verbose flag to the CLI."
-  ha run build --continue 20260926T101500-ab12cd34 "Also document the flag."
+  ha run multi-review --run 20260926T101500-ab12cd34
+  ha run build --continue 20260926T101500-ab12cd34 --findings 20260926T104000-9f8e7d6c
 """
 
 
@@ -166,6 +169,25 @@ def _parser() -> argparse.ArgumentParser:
         help="an implement workflow only: join the lineage of RUN_ID, an implement run, "
         "and work on its branch in its worktree",
     )
+    run.add_argument(
+        "--findings",
+        dest="findings_run",
+        metavar="RUN_ID",
+        help="an implement workflow only: address the findings of RUN_ID, a review of the "
+        "commit this run starts from; the prompt becomes optional",
+    )
+    run.add_argument(
+        "--head",
+        metavar="REF",
+        help="a review workflow only: the commit reviewed (default HEAD)",
+    )
+    run.add_argument(
+        "--run",
+        dest="review_run",
+        metavar="RUN_ID",
+        help="a review workflow only: review the current tip of RUN_ID's lineage, from its "
+        "base; excludes --head and --base",
+    )
     # Removed in 0.5.0: kept hidden so their use gets a message, not argparse's guess.
     run.add_argument("-p", "--provider", help=argparse.SUPPRESS)
     run.add_argument("--chain", help=argparse.SUPPRESS)
@@ -241,12 +263,20 @@ def _providers(args: argparse.Namespace, io: Io) -> int:
 
 
 def _prompt(args: argparse.Namespace, io: Io) -> tuple[str | None, bool]:
-    """The task text and whether stdin is a terminal (§3.9: never wait on one)."""
+    """The task text and whether stdin is a terminal (§3.9: never wait on one).
+
+    A target whose prompt is optional -- a review, or an implement taking
+    findings -- never reads stdin unless given ``-``.
+    """
     is_tty = bool(getattr(io.stdin, "isatty", lambda: False)())
     if args.prompt == "-":
         return io.stdin.read(), is_tty
     if args.prompt is not None:
         return args.prompt, is_tty
+    if prompt_is_optional(
+        args.target, findings=args.findings_run is not None, environ=io.environ, home=io.home
+    ):
+        return None, is_tty
     return (None if is_tty else io.stdin.read()), is_tty
 
 
@@ -298,21 +328,34 @@ def _run(args: argparse.Namespace, io: Io) -> int:
         environ=io.environ,
         home=io.home,
         continue_run=args.continue_run,
+        head=args.head,
+        review_run=args.review_run,
+        findings_run=args.findings_run,
     )
     outcome = execute(plan(request), say=io.say)
-    branch = outcome.report.get("branch")
+    report = outcome.report
+    branch, verdict = report.get("branch"), report.get("verdict")
     if args.json:
-        io.stdout.write(json.dumps(outcome.report, ensure_ascii=False, indent=2) + "\n")
+        io.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
+    elif isinstance(verdict, str):
+        # A review's verdict is its answer, APPROVE or CHANGES alike: the deciding text is
+        # what a session reads, and feeds back with --findings RUN_ID (§3.9).
+        io.stdout.write(f"run: {outcome.run_id}\nhead: {report.get('head')}\n\n")
+        text = report.get("text")
+        if isinstance(text, str) and text:
+            io.stdout.write(text if text.endswith("\n") else text + "\n")
     elif outcome.exit_code == 0 and outcome.final is not None:
         if isinstance(branch, str):
             io.stdout.write(_write_header(outcome, branch))
         text = outcome.final.text
         if text:
             io.stdout.write(text if text.endswith("\n") else text + "\n")
-    if outcome.exit_code != 0:
+    if outcome.exit_code != 0 and not isinstance(verdict, str):
         provider = outcome.final.provider if outcome.final is not None else args.target
+        reason = report.get("failure_reason")
+        because = f" ({reason})" if isinstance(reason, str) else ""
         io.say(
-            f"{provider} exited {outcome.exit_code}; run {outcome.run_id}, "
+            f"{provider} exited {outcome.exit_code}{because}; run {outcome.run_id}, "
             f"logs in {outcome.run_dir}"
         )
     return outcome.exit_code
diff --git a/packages/headless-agents/src/headless_agents/engine.py b/packages/headless-agents/src/headless_agents/engine.py
index 3c7628e4..fc3167fb 100644
--- a/packages/headless-agents/src/headless_agents/engine.py
+++ b/packages/headless-agents/src/headless_agents/engine.py
@@ -458,6 +458,21 @@ def _continued_lineage(run_id: str, *, state: Path, home: Path, option: str = "-
 REVIEW_DEFAULT_TASK: Final = "Review this change."
 
 
+def prompt_is_optional(
+    target: str, *, findings: bool, environ: Mapping[str, str], home: Path
+) -> bool:
+    """Whether ``target`` runs without a prompt: a review, or an implement taking findings.
+
+    §3.9: such a target never reads stdin unless given ``-``. The CLI asks before
+    reading; the configuration is read here, and an invalid one refuses (exit ``2``),
+    as :func:`plan` would.
+    """
+    workflow = load_config(environ, home).workflows.get(target)
+    if workflow is None:
+        return False
+    return workflow.shape == "review" or findings
+
+
 def _refuse_options_of_other_shapes(request: Request, shape: str | None) -> None:
     """Spec §3.9: ``--head`` and ``--run`` belong to a review; ``--continue`` to an implement."""
     if shape != "review":
@@ -1505,4 +1520,5 @@ __all__ = [
     "UsageError",
     "operator_environment",
     "plan",
+    "prompt_is_optional",
 ]
```

- [ ] **Step 4: Run them again, then the gates.**

Run: `.venv/bin/pytest tests/unit/headless_agents/test_cli.py tests/unit/headless_agents/test_review.py -q -p no:cacheprovider`
Expected: `test_cli.py: 109 passed; test_review.py: 44 passed`. Then the gates of the Global Constraints.

- [ ] **Step 5: Commit.**

```bash
git add packages/headless-agents/README.md packages/headless-agents/src/headless_agents/cli.py packages/headless-agents/src/headless_agents/engine.py tests/unit/headless_agents/test_cli.py tests/unit/headless_agents/test_review.py
git commit -m "feat(headless-agents): ha run takes --head, --run and --findings, prints a review's verdict text, and documents both shapes"
```

---

## Spec coverage (lot 4)

| Spec | Where |
|---|---|
| §3.2 the shape `review`: slots, a judge with two reviewers or more | lot 3 (validation); Task 4 (the panel planned in launch order) |
| §3.3 a workflow runs its roles as declared; the verdict advisory | Task 4 (every override refused on a review); Task 1 (P1) |
| §3.4 the prompt size once the diff is known, the phase refused with its step at `2`; parallel steps in threads | Task 4 (P6) |
| §3.5 inputs (`--head`, `--base` default `origin/HEAD`, `--run`), the change, reviewers in parallel, every reviewer must answer, the judge, the verdict, the cleanup, exit codes | Tasks 4, 8 |
| §3.6 `--findings`: the result read from the state, the head check, the fix template and subject | Task 7 (P8) |
| §3.7 the review, judge and fix templates, the output contract verbatim, the escaped attributes | Task 1 |
| §3.8.1 `reviews/<run_id>.check.json` and `reviews/<run_id>.json`, each written once | Task 3 |
| §3.8.2 a review's locks: unconfined shared to its end, every lineage of its repository shared, ascending, before its first git | Task 4 (P5; the lock-order test) |
| §3.8.4 steps 1–6: locks without git, uncertainty refuses, the head pinned, every commit attributed, independence, the check then the result before the cleanup | Tasks 2, 3, 4 |
| §3.8.5 a stale pending write found by a review: lineage compromised, repository quarantined | Task 4 (`write_flow.unfinalized`) |
| §3.8.6 the proof travels with the review: `ha show` from the state, `ha show --dir` for display | Task 5 |
| §3.9 options of a workflow target; the prompt optional for a review and with `--findings`; output; help; `ha clean` of a review | Tasks 4, 6, 8 |
| §3.10 a review's report: `head`, `verdict`, `text`, `vendor_check`, `cleanup`, the write fields `null`, each step's verdict; `findings_from` | Tasks 4, 5, 7 |
| §4 unit: the verdict reader, the vendor rule, `--run` and `--findings` refusals, the review lock order, check and result written once | Tasks 1–4, 7 |
| §4 engine: one and three reviewers; the session loop implement → review CHANGES → fix → review APPROVE; `--run` after the branch advanced; the vendor rule cases; stale findings; `--findings` after `ha clean` and with a missing result; a review waiting on a write's lineage then refused; a crash after an intent then a review refused; a corrupt lineage; a repository quarantine; a failing reviewer; an unreadable verdict; reviewers really concurrent; a failed cleanup; `ha show --dir` after the state is lost | Tasks 4–7 |

Out of this lot, by the spec's own order (§5): the `live` suite (a real review with two
providers and a judge; a real implement, review, fix sequence), the README rewrite, the
CHANGELOG, then the tag (lot 5).
