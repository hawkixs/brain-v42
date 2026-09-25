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
