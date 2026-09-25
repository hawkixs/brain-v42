"""A ``.git`` tripwire for writable workspaces, and a hardened git invocation.

Brain ticket 0b622f47, the entry condition of lot 4 (``ha run --write``).

THE HOLE. In a writable workspace claude, opencode and (unmeasured) codex let
an agent write under ``<ws>/.git``: a hook, ``core.fsmonitor``, a filter
driver, or -- in a linked worktree -- the ``.git`` FILE itself, pointed at a
directory the agent built. Whatever is planted runs the next time git runs in
that checkout: the operator's own ``git status``, or lot 4's carrier commit.
It runs outside every sandbox, and none of it appears in ``git status`` or
``git diff`` -- reading the diff does not reveal it. agy's guard denies such
writes since lot 2; the other rails cannot be relied on to.

THE TRIPWIRE. :meth:`Tripwire.arm` fingerprints, before the run, every place
from which a later git command would execute something the repository
controls; :meth:`Tripwire.tampered` compares after it. Watched:

- ``<ws>/.git`` itself (absent, a directory, or a linked worktree's file);
- in the git dir: ``config``, ``config.worktree``, ``commondir``, ``gitdir``,
  and everything under ``hooks/`` and ``info/``;
- in the common dir of a linked worktree: ``config``, ``hooks/``, ``info/``;
- every directory named by ``core.hooksPath`` (husky-style, often INSIDE the
  work tree, where an edit also runs at the next commit);
- the operator's own ``~/.gitconfig`` and ``$XDG_CONFIG_HOME/git/config``.

Not watched, on purpose: the index, objects and refs. An agent committing
with its own shell changes them, and nothing in them executes later.

:func:`settle` turns any change into a NON-replayable failure (``1``, never
the chain's ``3``/``4``) and names the paths on stderr; the rails put them in
``result.json`` under ``workspace.git_tampered``.

THE INVOCATION. A repository planted in any subdirectory escapes the watch
list: git discovers it implicitly when it runs from inside that directory.
Measured (git 2.34): a nested ``sub/.git`` with an index and a
``core.fsmonitor`` fires from ``sub`` or any directory below it; a planted
BARE one does not (git refuses to work from inside a git dir), which newer
gits may not guarantee. So git never runs from a subdirectory of an
agent-written tree: :func:`git_command` pins
``-C <root> --git-dir <resolved> --work-tree <root>``, disables the fsmonitor
and implicit bare repositories, and :func:`git_environment` bounds discovery
with ``GIT_CEILING_DIRECTORIES`` and drops every inherited ``GIT_*`` variable.
Both refuse a workspace that tripped.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .profile import Workspace

#: A hooks directory holding more entries than this is recorded as truncated
#: -- a change past the cap still shows as a change of the truncation marker's
#: neighbours, and a normal repository holds about a dozen hooks.
MAX_ENTRIES_PER_TREE: Final = 5000

_GIT_DIR_FILES: Final = ("config", "config.worktree", "commondir", "gitdir")
_GIT_DIR_TREES: Final = ("hooks", "info")
_COMMON_DIR_FILES: Final = ("config",)
_COMMON_DIR_TREES: Final = ("hooks", "info")


class GitTampered(RuntimeError):
    """This workspace's git state cannot be trusted: no git command may run in it."""


Descriptor = tuple[object, ...]


def _describe(path: Path) -> Descriptor:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ("absent",)
    except OSError as exc:
        return ("unreadable", type(exc).__name__)
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return ("symlink", os.readlink(path))
    if stat.S_ISDIR(info.st_mode):
        return ("dir", mode)
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1 << 16), b""):
                    digest.update(block)
        except OSError as exc:
            return ("unreadable", type(exc).__name__)
        return ("file", mode, digest.hexdigest())
    return ("other", stat.S_IFMT(info.st_mode), mode)


def _record_tree(root: Path, entries: dict[str, Descriptor]) -> None:
    """Record ``root`` and, when it is a real directory, everything under it."""
    entries[str(root)] = _describe(root)
    if entries[str(root)][0] != "dir":
        return
    pending = [root]
    count = 0
    while pending:
        directory = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            entries[str(directory)] = ("unreadable", type(exc).__name__)
            continue
        for child in children:
            count += 1
            if count > MAX_ENTRIES_PER_TREE:
                entries[f"{root}/…"] = ("truncated", MAX_ENTRIES_PER_TREE)
                return
            path = Path(child.path)
            entries[str(path)] = _describe(path)
            if entries[str(path)][0] == "dir":
                pending.append(path)


def _pointed_git_dir(dot_git_file: Path, base: Path) -> Path | None:
    """The directory a ``gitdir: <path>`` file names, or ``None``."""
    try:
        first = dot_git_file.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first.startswith("gitdir:"):
        return None
    target = Path(first.removeprefix("gitdir:").strip())
    return Path(os.path.normpath(target if target.is_absolute() else base / target))


def resolve_git_dir(root: Path) -> Path | None:
    """The git dir of the work tree at ``root``: ``.git`` itself, or what its file names."""
    dot_git = root / ".git"
    if dot_git.is_symlink():
        return None
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        return _pointed_git_dir(dot_git, root)
    return None


def _common_dir(git_dir: Path) -> Path | None:
    pointer = git_dir / "commondir"
    try:
        value = pointer.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not value:
        return None
    target = Path(value)
    return Path(os.path.normpath(target if target.is_absolute() else git_dir / target))


def common_dir(git_dir: Path) -> Path | None:
    """The repository's common dir a linked worktree's git dir names, or ``None``.

    ``None`` for a plain checkout's ``.git``, which is its own common dir. Read
    from the ``commondir`` file only: no git command runs.
    """
    return _common_dir(git_dir)


def _hooks_paths(config_files: Sequence[Path], root: Path) -> list[Path]:
    """Every ``core.hooksPath`` the config files set, resolved like git does.

    ``git config --file`` only READS the file: nothing in it runs.
    """
    found: list[Path] = []
    for config in config_files:
        if not config.is_file():
            continue
        try:
            completed = subprocess.run(
                ["git", "config", "--file", str(config), "--get-all", "core.hooksPath"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in completed.stdout.splitlines():
            value = line.strip()
            if not value:
                continue
            path = Path(os.path.expanduser(value))
            found.append(path if path.is_absolute() else root / path)
    return found


@dataclass
class _Plan:
    """Which files and trees to fingerprint, fixed at arming time."""

    files: list[Path] = field(default_factory=list)
    trees: list[Path] = field(default_factory=list)


def _plan(root: Path, home: Path, environ: Mapping[str, str]) -> _Plan:
    plan = _Plan(files=[root / ".git"])
    user_configs = [
        home / ".gitconfig",
        Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "git" / "config",
    ]
    plan.files.extend(user_configs)
    config_files = list(user_configs)
    git_dir = resolve_git_dir(root)
    if git_dir is not None:
        plan.files.extend(git_dir / name for name in _GIT_DIR_FILES)
        plan.trees.extend(git_dir / name for name in _GIT_DIR_TREES)
        config_files += [git_dir / "config", git_dir / "config.worktree"]
        common = _common_dir(git_dir)
        if common is not None and common != git_dir:
            plan.files.extend(common / name for name in _COMMON_DIR_FILES)
            plan.trees.extend(common / name for name in _COMMON_DIR_TREES)
            config_files.append(common / "config")
    plan.trees.extend(_hooks_paths(config_files, root))
    return plan


def _snapshot(plan: _Plan) -> dict[str, Descriptor]:
    entries: dict[str, Descriptor] = {}
    for path in plan.files:
        entries[str(path)] = _describe(path)
    for tree in plan.trees:
        _record_tree(tree, entries)
    return entries


class Tripwire:
    """The fingerprint of one writable workspace's executable git state."""

    def __init__(self, root: Path, *, home: Path, environ: Mapping[str, str]) -> None:
        self.root = root
        self._plan = _plan(root, home, environ)
        self._before = _snapshot(self._plan)

    @classmethod
    def arm(
        cls,
        workspace: Workspace | None,
        *,
        home: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Tripwire | None:
        """A tripwire for a WRITABLE workspace; ``None`` otherwise (nothing to watch)."""
        if workspace is None or not workspace.write:
            return None
        return cls(
            workspace.path,
            home=home if home is not None else Path.home(),
            environ=os.environ if environ is None else environ,
        )

    def tampered(self) -> tuple[str, ...]:
        """Every watched path whose state changed since arming, sorted."""
        after = _snapshot(self._plan)
        keys = self._before.keys() | after.keys()
        return tuple(sorted(key for key in keys if self._before.get(key) != after.get(key)))


def settle(
    tampered: tuple[str, ...] | None, exit_code: int, stderr_log: Path | None
) -> tuple[int, tuple[str, ...] | None]:
    """The run's code once the tripwire is read: ``1`` whenever anything changed.

    Never the chain's ``3`` or ``4``: a run that touched ``.git`` must not be
    replayed on another rail, and must not look like a success either.
    """
    if not tampered:
        return exit_code, tampered
    if stderr_log is not None:
        stderr_log.parent.mkdir(parents=True, exist_ok=True)
        with stderr_log.open("a", encoding="utf-8") as stream:
            for path in tampered:
                stream.write(
                    f"git tripwire: {path} changed during the run; "
                    "no git command may run in this workspace\n"
                )
    return 1, tampered


def git_command(root: Path, *, tampered: Sequence[str] = ()) -> list[str]:
    """The prefix of every git command a runtime may run in ``root``.

    Raises :class:`GitTampered` when the tripwire fired, or when ``root`` has
    no git dir it can pin (none, a symlinked ``.git``, a ``.git`` file naming
    a directory that does not exist).
    """
    if tampered:
        raise GitTampered(f"{root}: the .git tripwire fired; refusing to run git there")
    git_dir = resolve_git_dir(root)
    if git_dir is None or not git_dir.is_dir():
        raise GitTampered(f"{root}: no git dir to pin; refusing to run git there")
    return [
        "git",
        "-C",
        str(root),
        "--git-dir",
        str(git_dir),
        "--work-tree",
        str(root),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "safe.bareRepository=explicit",
    ]


def git_environment(environ: Mapping[str, str], root: Path) -> dict[str, str]:
    """``environ`` without any inherited ``GIT_*``, discovery bounded above ``root``."""
    child = {name: value for name, value in environ.items() if not name.startswith("GIT_")}
    child["GIT_CEILING_DIRECTORIES"] = str(root.parent)
    return child
