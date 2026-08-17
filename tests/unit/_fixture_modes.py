"""Umask-independent filesystem helpers for hardening fixtures.

Several hardening guards reject a path that group or others may write, i.e.
`mode & 0o022` (`scripts/rotate_neo4j_credential.py::_trusted_repository_command_assets`,
`deploy/systemd/install.sh::path_chain_blocks_other_users`).  Fixtures that build a
fake repository or unit tree with a bare `Path.mkdir()` inherit the caller's umask,
so the very same tree is `0755` under `umask 022` (CI) and `0775` under `umask 002`
(the PC Serveur default) — and the guards correctly reject the latter.  That made
~40 unit tests fail on a developer machine while CI stayed green (ticket 5619c851).

`Path.mkdir(mode=...)` does not fix this: its mode argument is masked by the umask,
and intermediate `parents=True` levels are created with the default mode regardless.
`chmod` is not masked, so these helpers create each level and then pin it explicitly.

Fixtures must therefore state the mode they need instead of inheriting it.  Keep
using a bare `chmod` where a test deliberately builds a hostile mode (e.g. `0o777`
to prove a guard rejects it) — the point here is determinism, not permissiveness.
"""

from __future__ import annotations

from pathlib import Path

DIRECTORY_MODE = 0o755
FILE_MODE = 0o644


def make_directory(
    path: Path,
    *,
    mode: int = DIRECTORY_MODE,
    parents: bool = False,
    exist_ok: bool = False,
) -> Path:
    """Create `path` with an explicit `mode`, pinning every level this call creates.

    Mirrors `Path.mkdir(parents=..., exist_ok=...)`, except the resulting modes do
    not depend on the ambient umask.
    """
    if parents:
        missing: list[Path] = []
        candidate = path
        while candidate != candidate.parent and not candidate.exists():
            missing.append(candidate)
            candidate = candidate.parent
        missing.reverse()
    else:
        missing = [path]

    for level in missing:
        # Only the leaf may pre-exist; the ancestors were just proven missing.
        level.mkdir(exist_ok=exist_ok and level == path)
        level.chmod(mode)
    return path


def write_file(
    path: Path,
    content: str,
    *,
    mode: int = FILE_MODE,
    encoding: str = "utf-8",
) -> Path:
    """Write `content` to `path` and pin an explicit `mode`, ignoring the umask."""
    path.write_text(content, encoding=encoding)
    path.chmod(mode)
    return path
