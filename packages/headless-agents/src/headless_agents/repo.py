"""The repository a run works in, identified without git (plan decision P2).

Quarantines and lineages are keyed by the repository and checked before the
first git command (spec 0.5.0 §3.8.3, §3.8.5), so the identity they need
cannot come from ``git rev-parse``. It is read from the filesystem instead:
the first ``.git`` above the start directory, the git dir it is or names, and
that git dir's ``commondir`` -- the same files git itself reads, parsed by
:mod:`headless_agents.git_tripwire`. A ``.git`` that cannot be pinned -- a
symbolic link, or a file naming no directory -- is refused, never followed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .git_tripwire import common_dir, resolve_git_dir


class RepoError(ValueError):
    """A ``.git`` that cannot be pinned."""


@dataclass(frozen=True)
class RepoIdentity:
    work_tree: Path
    git_dir: Path
    #: The repository's own git dir, shared by all its worktrees: the key of
    #: its quarantine and of its lineages.
    common_dir: Path


def discover(start: Path, *, ceiling: Path | None = None) -> RepoIdentity | None:
    """The repository holding ``start``, or ``None`` outside any; never runs git.

    ``ceiling``, like ``GIT_CEILING_DIRECTORIES``, is the last directory looked
    at: nothing above it is searched.
    """
    here = start.resolve()
    top = ceiling.resolve() if ceiling is not None else None
    for directory in (here, *here.parents):
        if top is not None and directory != top and not directory.is_relative_to(top):
            break
        dot_git = directory / ".git"
        if not dot_git.exists() and not dot_git.is_symlink():
            continue
        git_dir = resolve_git_dir(directory)
        if git_dir is None or not git_dir.is_dir():
            raise RepoError(
                f"{dot_git} cannot be pinned: a symbolic link, or a .git file naming no "
                "directory; refusing to guess the repository"
            )
        common = common_dir(git_dir) or git_dir
        return RepoIdentity(
            work_tree=directory, git_dir=git_dir.resolve(), common_dir=common.resolve()
        )
    return None


__all__ = ["RepoError", "RepoIdentity", "discover"]
