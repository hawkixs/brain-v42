"""What every rail does once its process has exited: read the answer, record the run.

Shared by the four CLI rails so that ``RunResult.text`` means the same thing
on each of them, and so that ``result.json`` is written in one place.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from .result import RunResult
from .spec import RunSpec, run_dir_name

RESULT_FILE_NAME = "result.json"


def _failure_note(error: OSError) -> str:
    """One line naming ``error``: exception class + errno name, never its message.

    The errno name (``ENOTDIR``, ``EACCES``, ...) is enough to diagnose a
    write failure from the log; the OS-supplied message can echo path
    fragments or, on some platforms, more than that -- never worth the risk
    for a bookkeeping note.
    """
    code = errno.errorcode.get(error.errno, "UNKNOWN") if error.errno is not None else "UNKNOWN"
    return f"record: could not write {RESULT_FILE_NAME}: {type(error).__name__} ({code})\n"


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
    """A run is named by its directory (:func:`~headless_agents.spec.run_dir_name`),
    or ``None`` without one.

    Not the raw ``.name``: ``run_dir=Path('.')`` has an empty one, yet names
    the caller's current directory. ``RunSpec.__post_init__`` already refused
    a ``run_dir`` with no name, so this never returns ``""`` for a constructed
    spec.
    """
    return run_dir_name(spec.run_dir) if spec.run_dir is not None else None


def record(spec: RunSpec, result: RunResult) -> RunResult:
    """Write ``result.json`` into ``spec.run_dir`` when there is one; return ``result``.

    Written under a temporary name, then renamed: a reader listing run
    directories never parses half a file.

    Never raises ``OSError``. By the time this runs the agent already ran --
    quota spent, maybe files written -- so a write failure here (read-only
    ``run_dir``, a full disk, ``run_dir`` being a plain file) must not cost
    the caller the ``RunResult`` of a run that already happened. On failure
    ``result`` is returned unchanged; one line naming the failure is appended
    to ``spec.stderr_log`` when that path is set and writable, and stays
    silent otherwise.

    The temporary file is created EXCLUSIVELY under a unique name
    (``tempfile.mkstemp``, so ``0600``), and only that file is ever cleaned
    up: a fixed partial name meant a failed write could delete a partial this
    invocation never created -- a read-only leftover, or a concurrent
    writer's (independent review of PR #197, finding 2).
    """
    if spec.run_dir is None:
        return result
    target = spec.run_dir / RESULT_FILE_NAME
    owned: Path | None = None
    try:
        spec.run_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=spec.run_dir, prefix=f".{RESULT_FILE_NAME}.", suffix=".partial"
        )
        owned = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n")
        os.replace(owned, target)
        owned = None
    except OSError as error:
        if spec.stderr_log is not None:
            with suppress(OSError):
                with spec.stderr_log.open("a", encoding="utf-8") as stream:
                    stream.write(_failure_note(error))
        if owned is not None:
            with suppress(OSError):
                owned.unlink()
    return result
