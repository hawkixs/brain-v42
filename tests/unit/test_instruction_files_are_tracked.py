"""CLAUDE.md and AGENTS.md are tracked, so every checkout carries the rules.

Decided on 2026-09-22 (ticket 5081f3ff), superseding the CLAUDE.md / AGENTS.md
part of decision 14856555. While both files were gitignored, a worktree had
neither: an agent working there read no project rule at all, and every assertion
of `test_documentation_contract.py` that reads CLAUDE.md skipped -- in every
worktree, and in CI, where "no CI will ever tell you" was literally true of a
stale migration head.

Both files are English and hold no host address, user name, key path or token:
machine access lives in brain (learning 2a23883d). Tracking makes them public,
which is the point: what they say can now be reviewed in a pull request.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")


@pytest.mark.parametrize("name", INSTRUCTION_FILES)
def test_the_instruction_file_is_tracked(name: str) -> None:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"{name} is not tracked: {result.stderr.strip()}"


@pytest.mark.parametrize("name", INSTRUCTION_FILES)
def test_the_instruction_file_is_not_ignored(name: str) -> None:
    """An ignore rule would let the file vanish from the next commit unnoticed."""
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", name],
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 1, f"{name} matches an ignore rule"
