"""Every git command of ``ha``, hardened, with hooks disabled (spec 0.5.0 §3.8.3, §4).

On top of the 0.4.0 hardening (:func:`~headless_agents.git_tripwire.git_command`:
a pinned git dir, no fsmonitor, no inherited ``GIT_*``), every command runs with
``core.hooksPath`` pointed at an empty directory of the state: a hook planted in
the repository never runs under ``ha``. The one exception is the engine's own
commit (§3.8.3 step 8), which runs the repository's hooks on purpose and
attributes whatever they do.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Literal, overload

from .git_tripwire import GitTampered, git_command, git_environment

GIT_TIMEOUT_SECONDS: Final = 120


def empty_hooks_dir(state: Path) -> Path:
    """``<state>/empty-hooks``, mode ``0700``; it must stay empty."""
    path = state / "empty-hooks"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


@overload
def git(
    root: Path,
    args: Sequence[str],
    environ: Mapping[str, str],
    *,
    state: Path,
    hooks: bool = False,
    tampered: Sequence[str] = (),
    binary: Literal[False] = False,
) -> subprocess.CompletedProcess[str]: ...


@overload
def git(
    root: Path,
    args: Sequence[str],
    environ: Mapping[str, str],
    *,
    state: Path,
    hooks: bool = False,
    tampered: Sequence[str] = (),
    binary: Literal[True],
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]: ...


def git(
    root: Path,
    args: Sequence[str],
    environ: Mapping[str, str],
    *,
    state: Path,
    hooks: bool = False,
    tampered: Sequence[str] = (),
    binary: bool = False,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run ``git args`` in ``root``; ``hooks=True`` is for the engine commit ONLY.

    Raises :class:`GitTampered`, before anything runs, when the tripwire fired,
    ``root`` has no git dir to pin, or the empty hooks directory is not empty.

    Git prints what the repository holds -- the diff of a file that is not
    UTF-8, a hook's message, a commit subject -- so its output is decoded as
    UTF-8 with every undecodable byte replaced: none of it may crash ``ha``
    (ticket 0b3fcdbf). That text is for reading only: it also has ``\r\n`` and
    ``\r`` turned into ``\n``. ``binary=True`` returns the bytes untouched, for
    what is recorded, parsed on its own separators, or shown to a model as the
    repository holds it: a patch, a hook's output, a log (review of #236).
    ``stdin``, bytes too, is what a ``--pathspec-from-file=-`` reads.
    """
    if stdin is not None and not binary:
        raise ValueError("stdin is bytes: pass binary=True")
    command = git_command(root, tampered=tampered)
    if not hooks:
        empty = empty_hooks_dir(state)
        if any(empty.iterdir()):
            raise GitTampered(f"{empty}: the empty hooks directory is not empty")
        command += ["-c", f"core.hooksPath={empty}"]
    argv, env = [*command, *args], git_environment(environ, root)
    if binary:
        return subprocess.run(  # noqa: S603 - argv list, no shell
            argv,
            env=env,
            input=stdin,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    return subprocess.run(  # noqa: S603 - argv list, no shell
        argv,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )


__all__ = ["GIT_TIMEOUT_SECONDS", "empty_hooks_dir", "git"]
