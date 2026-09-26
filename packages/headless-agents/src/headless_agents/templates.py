"""The prompts the engine composes, and the verdict a review ends with (spec 0.5.0 §3.5, §3.7).

Written in English and versioned with the package; configuration never edits
them -- a role shapes behaviour through its instructions, which travel in the
preamble. Each template delimits what it carries in blocks -- ``<task>``,
``<diff>``, ``<review role="…" provider="…" model="…">``, ``<findings>`` -- whose
attributes go through :func:`headless_agents.context.xml_attribute`. A block's
content travels verbatim: a diff can try to steer a judge, which is why the
verdict is advisory and ``ha`` never merges nor pushes (§3.3).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from .context import xml_attribute

_NO_GIT: Final = (
    "Do not run git: the engine commits your changes when you finish. A commit, a checkout, "
    "a reset or any other move of HEAD or of a branch fails the run.\n"
)

IMPLEMENT_TEMPLATE: Final = (
    """\
Implement the task below in the repository of your workspace.

<task>
{task}
</task>

"""
    + _NO_GIT
)

#: §3.7: the review and judge templates end with it, verbatim.
OUTPUT_CONTRACT: Final = (
    "List each finding with its severity (critical, high, medium or low) and its `file:line`.\n"
    "End your answer with exactly one line: `VERDICT: APPROVE` if the change can be merged as\n"
    "it is, or `VERDICT: CHANGES` if anything must change first.\n"
)

REVIEW_TEMPLATE: Final = (
    """\
Review the change below. Your workspace holds the repository at the reviewed commit, \
read-only: read the changed files in full where the diff is not enough.

<task>
{task}
</task>

<diff>
{diff}</diff>

"""
    + OUTPUT_CONTRACT
)

JUDGE_TEMPLATE: Final = (
    """\
Judge the reviews below of the change below. Your workspace holds the repository at the \
reviewed commit, read-only. Check every finding against the code, merge the duplicates, \
discard the unfounded, and keep what is real.

<task>
{task}
</task>

<diff>
{diff}</diff>

{reviews}
"""
    + OUTPUT_CONTRACT
)

#: The task of a fix when ``--findings`` is given without one (§3.6: optional guidance).
FIX_DEFAULT_TASK: Final = "Address the findings below."

FIX_TEMPLATE: Final = (
    """\
Implement the task below in the repository of your workspace, addressing the findings \
of a review of this branch.

<task>
{task}
</task>

<findings>
{findings}
</findings>

Address each finding, or say why you do not.

"""
    + _NO_GIT
)


@dataclass(frozen=True)
class ReviewText:
    """One reviewer's answer, as the judge receives it (§3.5 step 4)."""

    role: str
    provider: str
    model: str
    text: str


def _with_newline(text: str) -> str:
    return text if text.endswith("\n") or not text else text + "\n"


def implement_prompt(task: str) -> str:
    """The implement template around ``task`` (§3.6 step 2: do not run git, the engine commits).

    The task travels verbatim: braces, markup and blank lines inside it are the
    operator's words, never template syntax.
    """
    return IMPLEMENT_TEMPLATE.format(task=task.strip())


def fix_prompt(task: str, findings: str) -> str:
    """The implement template plus the ``<findings>`` block of a review (§3.6, §3.7)."""
    return FIX_TEMPLATE.format(task=task.strip() or FIX_DEFAULT_TASK, findings=findings.strip())


def review_prompt(task: str, diff: str) -> str:
    """What each reviewer gets: the task, the diff and the output contract (§3.5 step 2)."""
    return REVIEW_TEMPLATE.format(task=task.strip(), diff=_with_newline(diff))


def judge_prompt(task: str, diff: str, reviews: Sequence[ReviewText]) -> str:
    """What the judge gets: the task, the diff, and every review labelled (§3.5 step 4)."""
    blocks = "\n".join(
        f'<review role="{xml_attribute(review.role)}" provider="{xml_attribute(review.provider)}"'
        f' model="{xml_attribute(review.model)}">\n{_with_newline(review.text.strip())}</review>\n'
        for review in reviews
    )
    return JUDGE_TEMPLATE.format(task=task.strip(), diff=_with_newline(diff), reviews=blocks)


Verdict = Literal["approve", "changes"]

_VERDICT: Final = re.compile(r"VERDICT:\s*(APPROVE|CHANGES)", re.IGNORECASE)


def read_verdict(text: str | None) -> Verdict | None:
    """The verdict of ``text``: its last non-empty line, and nowhere else (§3.5 step 5).

    The ``*``, ``_`` and backtick characters around the line are stripped; what
    remains must be ``VERDICT: APPROVE`` or ``VERDICT: CHANGES``, whatever the
    case. Anything else is unreadable -- ``None``, a failure, never an approval.
    """
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    found = _VERDICT.fullmatch(lines[-1].strip("*_`").strip())
    if found is None:
        return None
    return "approve" if found.group(1).upper() == "APPROVE" else "changes"


__all__ = [
    "FIX_DEFAULT_TASK",
    "FIX_TEMPLATE",
    "IMPLEMENT_TEMPLATE",
    "JUDGE_TEMPLATE",
    "OUTPUT_CONTRACT",
    "REVIEW_TEMPLATE",
    "ReviewText",
    "Verdict",
    "fix_prompt",
    "implement_prompt",
    "judge_prompt",
    "read_verdict",
    "review_prompt",
]
