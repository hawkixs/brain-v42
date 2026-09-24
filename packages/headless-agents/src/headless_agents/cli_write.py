"""``ha run --write``: a worktree, a writable run, a carrier commit (spec 3.4)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli import Io, RunPlan


def run_write(plan: RunPlan, args: argparse.Namespace, io: Io) -> int:
    from .cli import UsageError  # noqa: PLC0415

    raise UsageError("--write is not implemented yet")


def remove_worktree(run_dir: Path, io: Io) -> str | None:
    """Remove the run's worktree, if it has one; a message when it cannot."""
    return None
