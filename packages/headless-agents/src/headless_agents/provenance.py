"""Per-commit provenance: who made each commit a write recorded (spec 0.5.0 §3.8.1, §3.8.3).

One file per recorded commit, ``<state>/provenance/<sha>.json``, written once:
the run and lineage it belongs to, and ``made_by`` -- the ``engine``'s own
commit, one an ``agent`` made by itself, one a ``hook`` made during the
engine's commit, or ``unknown``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal

from .state import create_once, read_optional

MadeBy = Literal["engine", "agent", "hook", "unknown"]

_SHA: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _path(state: Path, sha: str) -> Path:
    if not _SHA.fullmatch(sha):
        raise ValueError(f"not a commit sha: {sha!r}")
    return state / "provenance" / f"{sha}.json"


def record(
    state: Path,
    sha: str,
    *,
    run_id: str,
    lineage: str,
    made_by: MadeBy,
    providers: Sequence[str],
) -> None:
    """Record ``sha`` once; :class:`FileExistsError` if it is already recorded."""
    create_once(
        _path(state, sha),
        {
            "sha": sha,
            "run_id": run_id,
            "lineage": lineage,
            "made_by": made_by,
            "providers": list(providers),
        },
    )


def lookup(state: Path, sha: str) -> dict[str, object] | None:
    """The record of ``sha``; ``None`` when none was written; ``Unknown`` when unreadable."""
    return read_optional(_path(state, sha), expect_id=("sha", sha))


__all__ = ["MadeBy", "lookup", "record"]
