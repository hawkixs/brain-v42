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
