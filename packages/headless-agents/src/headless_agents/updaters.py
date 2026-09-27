"""Update the provider CLIs, then re-prove what changed (spec 0.5.2 §3.4, ``ha providers --update``).

WHY A MODULE OF ITS OWN. Each CLI rail's vendor ships its own updater (measured
2026-09-27 from each ``--help``; no updater was run to measure them). The table could
have lived beside each rail in ``providers/*.py``, as the spec words it -- but those
files are the rails' fingerprinted isolation source (``proofs._ISOLATION_SOURCE_FILES``):
editing one would make every installed isolation proof of that rail stale. Here it
moves nothing.

TRUST CHAIN. Updating is not proving: this module may CALL
:func:`headless_agents.prove.prove`, which alone records a proof, and never records one
itself (pinned by a test).

ROLLBACK is reported, never performed (a spec non-goal), and a path is named only when
it exists at report time:

- claude keeps its previous versions (``~/.local/share/claude/versions/<v>``) and
  reinstalls one with ``claude install <v>``; Claude Code also updates itself, which may
  undo a rollback (spec Q2).
- codex keeps its previous releases (``~/.codex/packages/standalone/releases/<v>-*``);
  there is no command: ``~/.local/bin/codex`` is repointed at the kept release by hand.
- agy keeps nothing, so ``--update`` copies its binary aside first, to
  ``<state>/rollback/agy/<v>/agy`` (lot 4 plan, orchestrator default 3).
- opencode keeps nothing either, but reinstalls a version: ``opencode upgrade <v>``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True)
class Updater:
    rail: str
    #: Appended to the executable path the probe measured -- never to a bare name.
    args: tuple[str, ...]
    #: A vendor dry run; ``None`` for all four today (measured): ``--check`` then
    #: reports ``unknown``. A vendor that gains one is a one-line change here.
    check_args: tuple[str, ...] | None


UPDATERS: Final[Mapping[str, Updater]] = {
    "claude": Updater("claude", ("update",), None),
    "codex": Updater("codex", ("update",), None),
    "agy": Updater("agy", ("update",), None),
    "opencode": Updater("opencode", ("upgrade",), None),
}

#: An updater downloads a release: ten minutes, then its whole process group is killed.
UPDATE_TIMEOUT_SECONDS: Final = 600.0

_SEMVER: Final = re.compile(r"\d+\.\d+\.\d+")


def semver(version: str | None) -> str | None:
    """The first ``X.Y.Z`` of a rail's ``--version`` line, or ``None``.

    Only this ever reaches a path or a command: digits and dots, nothing a vendor's
    output could turn into another directory.
    """
    if not version:
        return None
    match = _SEMVER.search(version)
    return match.group(0) if match else None


def agy_copy(state: Path, version: str) -> Path:
    """Where ``--update`` keeps the agy binary of ``version`` (a semver) before updating."""
    return state / "rollback" / "agy" / version / "agy"


def rollback(
    rail: str, old_version: str | None, home: Path, state: Path
) -> tuple[Path | None, str | None]:
    """What would return ``rail`` to ``old_version``: a path that exists now, and the
    vendor's command when it has one. ``(None, None)`` for a version that does not parse.
    """
    version = semver(old_version)
    if version is None:
        return None, None
    if rail == "claude":
        kept = home / ".local" / "share" / "claude" / "versions" / version
        return (kept if kept.exists() else None), f"claude install {version}"
    if rail == "codex":
        releases = home / ".codex" / "packages" / "standalone" / "releases"
        found = sorted(releases.glob(f"{version}-*/bin/codex"))
        # Two builds of one version (two architectures): naming one would be a guess.
        return (found[0] if len(found) == 1 else None), None
    if rail == "agy":
        copy = agy_copy(state, version)
        return (copy if copy.is_file() else None), None
    if rail == "opencode":
        return None, f"opencode upgrade {version}"
    return None, None


__all__ = [
    "UPDATERS",
    "UPDATE_TIMEOUT_SECONDS",
    "Updater",
    "agy_copy",
    "rollback",
    "semver",
]
