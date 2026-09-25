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

from .state import Unknown, create_once, read, read_optional

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


def of_run(state: Path, run_id: str) -> tuple[list[dict[str, object]], list[Path]]:
    """Every provenance record naming ``run_id``, and the record files that cannot be read.

    Provenance is keyed by commit, so this scans ``<state>/provenance/``. A
    record that cannot be read is returned apart, never skipped: it may be one
    of this run's commits, and unknown is never empty (spec §3.8.1).
    """
    directory = state / "provenance"
    if not directory.is_dir():
        return [], []
    found: list[dict[str, object]] = []
    unreadable: list[Path] = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = read(path, expect_id=("sha", path.stem))
        except Unknown:
            unreadable.append(path)
            continue
        if document.get("run_id") == run_id:
            found.append(document)
    return found, unreadable


__all__ = ["MadeBy", "lookup", "of_run", "record"]
