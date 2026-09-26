"""A review's records in the state directory (spec 0.5.0 §3.8.1, §3.8.4 step 6, §3.8.6).

``<state>/reviews/<run_id>.check.json`` -- the vendor check, written once
before the reviewers start; ``<state>/reviews/<run_id>.json`` -- the result,
written once when the verdict is read: the pinned head, the verdict, the
deciding text and a copy of the check. They are the one authority for a
review's head, verdict and text: ``run.json`` only copies them, and
``--findings`` reads the result only, so it still works after ``ha clean``.
A record that is missing when it should exist, does not parse, or holds what
``ha`` never writes is :class:`~headless_agents.state.Unknown`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .runs import RUN_ID_PATTERN
from .state import Unknown, create_once, read, read_optional
from .templates import Verdict
from .vendor import VendorCheck

REVIEWS_DIR: Final = "reviews"
VERDICTS: Final = ("approve", "changes")
_SHA: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class ReviewResult:
    run_id: str
    #: The exact commit the review read (§3.5).
    head: str
    verdict: Verdict
    #: The deciding text: the judge's, or the only reviewer's.
    text: str
    check: VendorCheck


def _checked(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"not a run id: {run_id!r}")
    return run_id


def check_path(state: Path, run_id: str) -> Path:
    return state / REVIEWS_DIR / f"{_checked(run_id)}.check.json"


def result_path(state: Path, run_id: str) -> Path:
    return state / REVIEWS_DIR / f"{_checked(run_id)}.json"


def write_check(state: Path, run_id: str, check: VendorCheck) -> None:
    """Write the check once; :class:`FileExistsError` if it exists."""
    create_once(check_path(state, run_id), {"run_id": run_id, "vendor_check": check.to_document()})


def load_check(state: Path, run_id: str) -> VendorCheck | None:
    """The check; ``None`` when none was written (a review refused before it)."""
    path = check_path(state, run_id)
    document = read_optional(path, expect_id=("run_id", run_id))
    if document is None:
        return None
    raw = document.get("vendor_check")
    if not isinstance(raw, dict):
        raise Unknown(f"{path}: vendor_check is malformed")
    return VendorCheck.from_document(raw, where=str(path))


def write_result(
    state: Path, run_id: str, *, head: str, verdict: Verdict, text: str, check: VendorCheck
) -> None:
    """Write the result once, before the cleanup (§3.8.4 step 6); :class:`FileExistsError`."""
    create_once(
        result_path(state, run_id),
        {
            "run_id": run_id,
            "head": head,
            "verdict": verdict,
            "text": text,
            "vendor_check": check.to_document(),
        },
    )


def load_result(state: Path, run_id: str) -> ReviewResult:
    """The result; :class:`Unknown` when missing or not one ``ha`` writes."""
    path = result_path(state, run_id)
    document = read(path, expect_id=("run_id", run_id))
    head, verdict, text, raw = (
        document.get("head"),
        document.get("verdict"),
        document.get("text"),
        document.get("vendor_check"),
    )
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        raise Unknown(f"{path}: head is malformed")
    if verdict not in VERDICTS:
        raise Unknown(f"{path}: verdict is malformed")
    if not isinstance(text, str):
        raise Unknown(f"{path}: text is malformed")
    if not isinstance(raw, dict):
        raise Unknown(f"{path}: vendor_check is malformed")
    return ReviewResult(
        run_id=run_id,
        head=head,
        verdict="approve" if verdict == "approve" else "changes",
        text=text,
        check=VendorCheck.from_document(raw, where=str(path)),
    )


__all__ = [
    "REVIEWS_DIR",
    "ReviewResult",
    "VERDICTS",
    "check_path",
    "load_check",
    "load_result",
    "result_path",
    "write_check",
    "write_result",
]
