"""The prompts the engine composes (spec 0.5.0 §3.7), and the verdict a review ends with (§3.5)."""

from __future__ import annotations

import pytest

from headless_agents.templates import (
    FIX_DEFAULT_TASK,
    OUTPUT_CONTRACT,
    ReviewText,
    fix_prompt,
    implement_prompt,
    judge_prompt,
    read_verdict,
    review_prompt,
)

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


# ── lot 4: review, judge, fix, and the verdict (spec §3.5, §3.7) ───────────────

CONTRACT = (
    "List each finding with its severity (critical, high, medium or low) and its `file:line`.\n"
    "End your answer with exactly one line: `VERDICT: APPROVE` if the change can be merged as\n"
    "it is, or `VERDICT: CHANGES` if anything must change first.\n"
)

GOLDEN_REVIEW = (
    """\
Review the change below. Your workspace holds the repository at the reviewed commit, \
read-only: read the changed files in full where the diff is not enough.

<task>
Review this change.
</task>

<diff>
+print('v2')
</diff>

"""
    + CONTRACT
)

GOLDEN_FIX = """\
Implement the task below in the repository of your workspace, addressing the findings \
of a review of this branch.

<task>
Keep the API stable.
</task>

<findings>
src/app.py:3 high: the flag is ignored
</findings>

Address each finding, or say why you do not.

Do not run git: the engine commits your changes when you finish. A commit, a checkout, \
a reset or any other move of HEAD or of a branch fails the run.
"""


def test_the_output_contract_is_the_specs_verbatim() -> None:
    assert OUTPUT_CONTRACT == CONTRACT


def test_the_review_template_is_pinned() -> None:
    assert review_prompt("Review this change.", "+print('v2')\n") == GOLDEN_REVIEW


def test_the_judge_template_ends_with_the_contract() -> None:
    reviews = [ReviewText(role="r", provider="codex", model="m", text="VERDICT: APPROVE")]
    assert judge_prompt("t", "d", reviews).endswith(CONTRACT)


def test_the_judge_gets_every_review_labelled_in_order_with_escaped_attributes() -> None:
    reviews = [
        ReviewText(
            role="reviewer-codex", provider="codex", model="gpt-6", text="A\nVERDICT: CHANGES"
        ),
        ReviewText(role='odd"role', provider="agy", model="<m>", text="VERDICT: APPROVE"),
    ]
    prompt = judge_prompt("Review this change.", "+x\n", reviews)
    labelled = '<review role="reviewer-codex" provider="codex" model="gpt-6">\nA\nVERDICT: CHANGES\n</review>'
    assert labelled in prompt
    assert '<review role="odd&quot;role" provider="agy" model="&lt;m&gt;">' in prompt
    assert prompt.index("reviewer-codex") < prompt.index("odd&quot;role")
    assert "<diff>\n+x\n</diff>" in prompt


def test_the_fix_template_is_pinned() -> None:
    assert (
        fix_prompt("Keep the API stable.", "src/app.py:3 high: the flag is ignored") == GOLDEN_FIX
    )


def test_the_fix_template_without_a_task_asks_for_the_findings_only() -> None:
    assert f"<task>\n{FIX_DEFAULT_TASK}\n</task>" in fix_prompt("", "a finding")
    assert f"<task>\n{FIX_DEFAULT_TASK}\n</task>" in fix_prompt("  \n", "a finding")


def test_blocks_carry_their_content_verbatim() -> None:
    diff = "+x = {'a': 1}\n+print('{0}')\n"
    assert f"<diff>\n{diff}</diff>" in review_prompt("t", diff)


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("All good.\nVERDICT: APPROVE", "approve"),
        ("Fix it.\nVERDICT: CHANGES\n\n  \n", "changes"),
        ("**VERDICT: APPROVE**", "approve"),
        ("`VERDICT: CHANGES`", "changes"),
        ("_verdict: approve_", "approve"),
        ("x\n  VERDICT:   CHANGES  ", "changes"),
    ],
)
def test_the_verdict_is_read_from_the_last_non_empty_line(text: str, verdict: str) -> None:
    assert read_verdict(text) == verdict


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n\n",
        "VERDICT: APPROVE\nThat is all.",
        "VERDICT: MAYBE",
        "VERDICT: APPROVE or CHANGES",
        "Verdict - approve",
        "I would say VERDICT: APPROVE",
        None,
    ],
)
def test_anything_else_is_an_unreadable_verdict_never_an_approval(text: str | None) -> None:
    assert read_verdict(text) is None
