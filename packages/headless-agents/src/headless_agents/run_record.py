"""What every rail does once its process has exited: read the answer, record the run.

Shared by the four CLI rails so that ``RunResult.text`` means the same thing
on each of them, and so that ``result.json`` is written in one place.
"""

from __future__ import annotations

import json
from pathlib import Path

from .result import RunResult
from .spec import RunSpec

RESULT_FILE_NAME = "result.json"


def answer_text(path: Path | None, *, exit_code: int, offset: int = 0) -> str | None:
    """The run's final answer, VERBATIM, or ``None`` when it produced none.

    ``None`` on a failed run even when the file holds something: a partial or
    stale report is not an answer. ``offset`` skips what the file held before
    this run started -- the claude rail APPENDS to its log, so a reused path
    would otherwise hand back an earlier run's answer. Undecodable bytes are
    replaced, never raised: reading the answer must not fail the run.
    """
    if exit_code != 0 or path is None or not path.is_file():
        return None
    with path.open("rb") as stream:
        stream.seek(offset)
        content = stream.read().decode("utf-8", errors="replace")
    return content if content.strip() else None


def run_id_of(spec: RunSpec) -> str | None:
    """A run is named by its directory: the RESOLVED ``run_dir``'s name, or ``None`` without one.

    Resolved, not raw: ``run_dir=Path('.')`` has an empty raw ``.name``, but it
    names one real directory -- the caller's current one -- whose name is what
    a reader of run directories actually sees. ``RunSpec.__post_init__``
    already refused a ``run_dir`` whose resolved name is empty, so this never
    returns ``""`` for a constructed spec.
    """
    return spec.run_dir.resolve().name if spec.run_dir is not None else None


def record(spec: RunSpec, result: RunResult) -> RunResult:
    """Write ``result.json`` into ``spec.run_dir`` when there is one; return ``result``.

    Written under a temporary name, then renamed: a reader listing run
    directories never parses half a file.
    """
    if spec.run_dir is None:
        return result
    spec.run_dir.mkdir(parents=True, exist_ok=True)
    target = spec.run_dir / RESULT_FILE_NAME
    partial = spec.run_dir / f".{RESULT_FILE_NAME}.partial"
    partial.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    partial.replace(target)
    return result
