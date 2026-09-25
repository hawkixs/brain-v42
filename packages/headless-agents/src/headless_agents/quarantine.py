"""Quarantines: a compromise can outgrow its lineage (spec 0.5.0 §3.8.5).

A fired tripwire names the paths that changed; the engine publishes the
quarantine of the WIDEST scope one of them belongs to:

- ``lineage`` -- the worktree's ``.git`` file or git dir, or a hooks path
  inside the worktree: the caller compromises the lineage, no file here;
- ``repository`` -- the repository's common dir: ``quarantine/repo-<id>.json``,
  keyed by the resolved common dir, refuses every git command of ``ha`` there;
- ``operator`` -- the operator's git configuration, or any path that resolves
  anywhere else: ``quarantine/operator.json`` refuses every git command of
  ``ha``, everywhere.

Paths are compared lexically (normalised, never resolved): the symbolic links
of a tampered tree are exactly what cannot be trusted. ``ha`` never lifts a
quarantine; the operator deletes its file.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Literal

from .state import Unknown, create_once, read

Scope = Literal["lineage", "repository", "operator"]

_RANK: Final[dict[str, int]] = {"lineage": 0, "repository": 1, "operator": 2}


def _normal(path: Path | str) -> str:
    return os.path.normpath(os.path.abspath(path))


def _inside(path: str, root: Path) -> bool:
    top = _normal(root)
    return path == top or path.startswith(top.rstrip(os.sep) + os.sep)


def _scope_of(
    path: str, *, worktree: Path, git_dir: Path, common_dir: Path, operator: Sequence[Path]
) -> Scope:
    normal = _normal(path)
    if any(_inside(normal, config) for config in operator):
        return "operator"
    if _normal(git_dir) != _normal(common_dir) and _inside(normal, git_dir):
        return "lineage"
    if _inside(normal, common_dir):
        return "repository"
    if _inside(normal, worktree):
        return "lineage"
    return "operator"


def widest_scope(
    paths: Sequence[str],
    *,
    worktree: Path,
    git_dir: Path,
    common_dir: Path,
    home: Path,
    environ: Mapping[str, str],
) -> Scope:
    """The widest scope any of ``paths`` belongs to; a path under no known root is ``operator``."""
    operator = [
        home / ".gitconfig",
        Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "git" / "config",
    ]
    widest: Scope = "lineage"
    for path in paths:
        scope = _scope_of(
            path, worktree=worktree, git_dir=git_dir, common_dir=common_dir, operator=operator
        )
        if _RANK[scope] > _RANK[widest]:
            widest = scope
    return widest


def repository_id(common_dir: Path) -> str:
    return hashlib.sha256(str(common_dir.resolve()).encode("utf-8")).hexdigest()[:16]


def quarantine_path(state: Path, scope: Scope, common_dir: Path | None) -> Path:
    if scope == "operator":
        return state / "quarantine" / "operator.json"
    if scope == "lineage":
        raise ValueError("a lineage scope compromises the lineage; it has no quarantine file")
    if common_dir is None:
        raise ValueError("a repository quarantine needs the repository's common dir")
    return state / "quarantine" / f"repo-{repository_id(common_dir)}.json"


def publish(
    state: Path,
    scope: Scope,
    *,
    reason: str,
    run_id: str,
    paths: Sequence[str],
    common_dir: Path | None,
) -> None:
    """Publish the quarantine of ``scope``; the first one stands until the operator lifts it."""
    path = quarantine_path(state, scope, common_dir)
    document: dict[str, object] = {
        "scope": scope,
        "reason": reason,
        "run_id": run_id,
        "paths": list(paths),
        "common_dir": str(common_dir.resolve()) if common_dir is not None else None,
    }
    try:
        create_once(path, document)
    except FileExistsError:
        pass


def _refusal(path: Path, label: str) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        document = read(path)
    except Unknown as exc:
        return f"{label} quarantine ({exc}: unreadable); lift it by hand after inspection"
    return (
        f"{label} quarantine: {document.get('reason')} in run {document.get('run_id')} "
        f"({path}); lift it by hand after inspection"
    )


def active(state: Path) -> list[dict[str, object]]:
    """Every quarantine in force, the operator's first (spec §3.8.5: at the top of ``ha runs``).

    A file present is a quarantine in force -- ``ha`` never lifts one, the
    operator deletes its file. An unreadable file is listed as such: :func:`check`
    still refuses on it.
    """
    directory = state / "quarantine"
    if not directory.is_dir():
        return []
    paths = sorted(
        directory.glob("*.json"), key=lambda path: (path.name != "operator.json", path.name)
    )
    found: list[dict[str, object]] = []
    for path in paths:
        try:
            document = read(path)
        except Unknown as exc:
            found.append(
                {
                    "file": str(path),
                    "readable": False,
                    "scope": None,
                    "reason": str(exc),
                    "run_id": None,
                }
            )
            continue
        found.append(
            {
                "file": str(path),
                "readable": True,
                "scope": document.get("scope"),
                "reason": document.get("reason"),
                "run_id": document.get("run_id"),
            }
        )
    return found


def check(state: Path, common_dir: Path | None) -> str | None:
    """The refusal message of the quarantine covering ``common_dir``; ``None`` when clear.

    The operator's first, then the repository's. An unreadable quarantine file
    counts as a quarantine.
    """
    refusal = _refusal(quarantine_path(state, "operator", None), "operator")
    if refusal is not None or common_dir is None:
        return refusal
    return _refusal(quarantine_path(state, "repository", common_dir), "repository")


__all__ = [
    "Scope",
    "active",
    "check",
    "publish",
    "quarantine_path",
    "repository_id",
    "widest_scope",
]
