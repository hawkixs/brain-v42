"""PATH shims that hold a real subprocess step at a barrier (0.5.2 lot 5, G1).

WHY A SHIM AND NOT A PRODUCTION HOOK. ``ha`` resolves codex and git on the
``PATH`` it hands its own children (measured: ``executable_for`` returns
``None`` for codex, so it runs whatever ``codex`` resolves to; ``gitops.git``
runs a bare ``git``). Prepending a directory of shims to that ``PATH`` is
therefore the whole hook a concurrency test needs -- no engine code,
isolation fingerprint or CHANGELOG contract surface changes to make G1
provable. A shim ``execv``s or wraps the real binary unchanged, so what it
proves is ``ha``'s own locking, never a fake stand-in for a provider.

A matching call is held until :meth:`Barrier.release` (or :func:`abort`, or
its own ``hold_seconds`` deadline); everything else passes straight through.
The barrier sits before the provider's first token or before git touches the
repository, so a failure at it costs nothing.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

#: A matching call that is aborted or whose hold expires exits with this code;
#: the real binary never runs.
ABORT_EXIT_CODE: Final = 97

Match = Literal["codex-exec", "git-worktree-add"]
LockState = Literal["absent", "free", "shared", "exclusive"]

# A plain (non-f, non-format) string: every brace below is literal Python
# source for the GENERATED shim, never touched by string interpolation here.
# The header written ahead of it in install_shim() defines REAL, ROOT,
# DIRECTORY, MATCH, HOLD and ABORT_EXIT_CODE as plain literals.
_SHIM_BODY = """
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(ROOT)
DIRECTORY = Path(DIRECTORY)


def matches(argv):
    if MATCH == "codex-exec":
        return argv[1:2] == ["exec"]
    return "worktree" in argv and argv[argv.index("worktree") + 1 :][:1] == ["add"]


def record(kind, payload):
    target = DIRECTORY / f"{kind}-{uuid.uuid4().hex}.json"
    staging = target.with_suffix(".tmp")
    staging.write_text(json.dumps(payload))
    os.replace(staging, target)


if not matches(sys.argv):
    os.execv(REAL, [REAL, *sys.argv[1:]])

record("arrived", {"argv": sys.argv, "cwd": os.getcwd(), "pid": os.getpid(), "at": time.monotonic_ns()})

deadline = time.monotonic() + HOLD
while not (DIRECTORY / "release").exists():
    aborted = (ROOT / "abort").exists()
    if aborted or time.monotonic() > deadline:
        record("failed", {"pid": os.getpid(), "reason": "aborted" if aborted else "hold expired"})
        sys.exit(ABORT_EXIT_CODE)
    time.sleep(0.01)

if MATCH == "codex-exec":
    os.execv(REAL, [REAL, *sys.argv[1:]])

started = time.monotonic_ns()
code = subprocess.run([REAL, *sys.argv[1:]], check=False).returncode
record("done", {"pid": os.getpid(), "started": started, "ended": time.monotonic_ns(), "returncode": code})
sys.exit(code)
"""


@dataclass(frozen=True)
class Barrier:
    """One barrier a shim waits at.

    ``root`` is shared by every barrier of one test (it holds the ``abort``
    file read by every shim); ``name`` (``"codex"`` or ``"git"``) is this
    barrier's own subdirectory, where its shim records arrivals, releases and
    failures.
    """

    root: Path
    name: str

    @property
    def directory(self) -> Path:
        return self.root / self.name

    def release(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "release").touch()

    def _records(self, prefix: str) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        records = []
        for path in sorted(self.directory.glob(f"{prefix}-*.json")):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return records

    def arrivals(self) -> list[dict[str, Any]]:
        return self._records("arrived")

    def finished(self) -> list[dict[str, Any]]:
        """Git-barrier only: ``(started, ended, returncode)`` of a released call."""
        return self._records("done")

    def failures(self) -> list[dict[str, Any]]:
        return self._records("failed")


def abort(root: Path) -> None:
    """Make every shim sharing ``root`` exit at once, without running the real binary."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "abort").touch()


def install_shim(
    shims: Path,
    barrier: Barrier,
    *,
    command: str,
    real: Path,
    match: Match,
    hold_seconds: float,
    python: str = sys.executable,
) -> Path:
    """Write ``<shims>/<command>``, mode ``0755``, holding a ``match`` call at ``barrier``.

    Every value is baked into the script with ``repr``, so the shim reads no
    environment variable and cannot be steered by one. Raises :class:`ValueError`
    when ``real`` resolves to the shim itself, which would recurse forever.
    """
    shims.mkdir(parents=True, exist_ok=True)
    target = shims / command
    resolved_real = real.resolve()
    if resolved_real == target.resolve():
        raise ValueError(f"{real}: resolves to the shim itself ({target}); refusing to wrap it")
    barrier.directory.mkdir(parents=True, exist_ok=True)
    header = (
        f"#!{python} -IB\n"
        f"REAL = {str(resolved_real)!r}\n"
        f"ROOT = {str(barrier.root)!r}\n"
        f"DIRECTORY = {str(barrier.directory)!r}\n"
        f"MATCH = {match!r}\n"
        f"HOLD = {hold_seconds!r}\n"
        f"ABORT_EXIT_CODE = {ABORT_EXIT_CODE!r}\n"
    )
    target.write_text(header + _SHIM_BODY, encoding="utf-8")
    target.chmod(0o755)
    return target


def lock_state(path: Path) -> LockState:
    """Whether ``path`` is held, without creating it or leaving a lock behind.

    ``absent`` for a missing path (nothing is created). Otherwise: an
    exclusive, non-blocking probe that succeeds means ``free``; failing that,
    a shared probe that succeeds means ``shared`` (no exclusive holder);
    otherwise ``exclusive``. Closing the descriptor releases whatever the
    probe itself took.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return "absent"
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return "free"
        except BlockingIOError:
            pass
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return "shared"
        except BlockingIOError:
            return "exclusive"
    finally:
        os.close(descriptor)


__all__ = ["ABORT_EXIT_CODE", "Barrier", "Match", "abort", "install_shim", "lock_state"]
