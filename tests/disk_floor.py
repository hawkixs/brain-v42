"""Refuse to start a test session on a nearly full filesystem.

Incident 2026-10-05 (ticket 998634d7): one lot's full-suite --basetemp directories
(~140 GB, never removed) filled the workstation's root filesystem, which also held
the production PostgreSQL bind mount, and PostgreSQL crashed. The session now
measures the filesystem its temporary files will land on before collecting
anything. The floor scales with the filesystem (5 %, at least 5 GiB) so it also
fits a small CI runner; BRAIN_V42_TEST_MIN_FREE_GB sets it explicitly, 0 disables.
"""

from __future__ import annotations

import math
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

GiB = 1024**3
_ENV = "BRAIN_V42_TEST_MIN_FREE_GB"
_MIN_FLOOR = 5 * GiB
_SHARE = 0.05


class DiskFloorError(Exception):
    """The filesystem holding the session's temporary files is under its floor."""


def floor_bytes(total: int, *, override: str | None) -> float:
    """The free space a session needs before it starts, in bytes."""
    if override is None:
        return max(_MIN_FLOOR, _SHARE * total)
    try:
        value = float(override)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise DiskFloorError(f"{_ENV} must be a non-negative number of GiB, got {override!r}")
    return value * GiB


def measured_path(basetemp: str | None, *, environ: Mapping[str, str]) -> Path:
    """Where the session writes, as pytest resolves it: --basetemp when given, else
    PYTEST_DEBUG_TEMPROOT when set, else the system temp dir."""
    if basetemp:
        return Path(basetemp)
    if environ.get("PYTEST_DEBUG_TEMPROOT"):
        return Path(environ["PYTEST_DEBUG_TEMPROOT"])
    try:
        return Path(tempfile.gettempdir())
    except OSError as exc:
        # gettempdir() probes candidates by writing to them: a full temp
        # filesystem makes it raise, which is this guard's refusal too.
        raise DiskFloorError(
            f"no usable temporary directory ({type(exc).__name__}): a full temp "
            "filesystem (ticket 998634d7)"
        ) from None


def check(
    path: Path,
    *,
    environ: Mapping[str, str],
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> None:
    """Raise DiskFloorError when `path`'s filesystem has less free space than its floor."""
    override = environ.get(_ENV)
    if override is not None and floor_bytes(0, override=override) == 0:
        return
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        usage = disk_usage(existing)
    except OSError as exc:
        raise DiskFloorError(
            f"cannot measure free space on {existing} ({type(exc).__name__}); set {_ENV}=0 "
            "to skip the check"
        ) from None
    floor = floor_bytes(usage.total, override=override)
    if usage.free < floor:
        raise DiskFloorError(
            f"{usage.free / GiB:.1f} GiB free on the filesystem of {existing}, under the "
            f"{floor / GiB:.1f} GiB floor (ticket 998634d7): remove old --basetemp "
            f"directories, or set {_ENV} (0 disables the check)"
        )
