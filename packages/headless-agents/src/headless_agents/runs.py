"""The run registry (spec 0.5.0 §3.8.1).

The engine always mints a run's id -- ``<UTC timestamp>-<8 hex>`` -- whatever
``--run-dir`` says, and registers it with ``O_EXCL``: an id is never
registered twice, and a collision (two ``ha`` processes in the same second)
mints again. Ids are resolved through the registry, never by joining them to
a cache path. A run's liveness is its lifecycle lock: a status that is not
final while that lock is free reads ``incomplete`` -- no pid is consulted.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .locks import is_free
from .state import create_once, publish, read

RUN_ID_PATTERN: Final = re.compile(r"\d{8}T\d{6}-[0-9a-f]{8}")
FINAL_STATUSES: Final = frozenset(
    {"answered", "failed", "committed", "no_change", "approved", "changes"}
)
_MINT_ATTEMPTS: Final = 100


class RegistryError(ValueError):
    """A run id that is not one, or names no registered run; a run dir that may not be used."""


@dataclass(frozen=True)
class Entry:
    run_id: str
    run_dir: Path
    repository: Path | None
    target: Mapping[str, str]
    lineage: str | None
    status: str | None
    cleaned_at: str | None


def _optional_path(value: object) -> Path | None:
    return Path(value) if isinstance(value, str) and value else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


class Registry:
    def __init__(self, state: Path, *, runs_root: Path) -> None:
        self.state = state
        self.runs_root = runs_root
        self._entries = state / "runs"

    def mint(self) -> str:
        return f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{secrets.token_hex(4)}"

    def _path(self, run_id: str) -> Path:
        return self._entries / f"{run_id}.json"

    def lifecycle_lock(self, run_id: str) -> Path:
        return self._entries / f"{run_id}.lock"

    def register(
        self,
        *,
        run_dir: Path | None,
        target: Mapping[str, str],
        repository: Path | None,
        lineage: str | None,
    ) -> Entry:
        """Mint an id and create its entry; the default run dir is built from that id."""
        for _ in range(_MINT_ATTEMPTS):
            run_id = self.mint()
            path = run_dir if run_dir is not None else self.runs_root / run_id
            document: dict[str, object] = {
                "run_id": run_id,
                "run_dir": str(path),
                "repository": str(repository) if repository is not None else None,
                "target": dict(target),
                "lineage": lineage,
                "status": "running",
                "cleaned_at": None,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            try:
                create_once(self._path(run_id), document)
            except FileExistsError:
                continue
            return self._entry(document)
        raise RegistryError(f"could not mint a fresh run id in {_MINT_ATTEMPTS} attempts")

    @staticmethod
    def _entry(document: Mapping[str, object]) -> Entry:
        target = document.get("target")
        return Entry(
            run_id=str(document["run_id"]),
            run_dir=Path(str(document["run_dir"])),
            repository=_optional_path(document.get("repository")),
            target={str(k): str(v) for k, v in target.items()} if isinstance(target, dict) else {},
            lineage=_optional_str(document.get("lineage")),
            status=_optional_str(document.get("status")),
            cleaned_at=_optional_str(document.get("cleaned_at")),
        )

    def resolve(self, run_id: str) -> Entry:
        """The entry of ``run_id``; :class:`~headless_agents.state.Unknown` when it is corrupt."""
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise RegistryError(f"not a run id: {run_id!r}")
        path = self._path(run_id)
        if not path.exists():
            raise RegistryError(f"no run {run_id} in {self._entries}")
        return self._entry(read(path, expect_id=("run_id", run_id)))

    def _update(self, run_id: str, **fields: object) -> None:
        path = self._path(run_id)
        document = read(path, expect_id=("run_id", run_id))
        document.update(fields)
        publish(path, document)

    def set_status(self, run_id: str, status: str) -> None:
        """For a run outside any lineage: its status' one authority (§3.8.1)."""
        self._update(run_id, status=status)

    def set_cleaned(self, run_id: str, when: str) -> None:
        self._update(run_id, cleaned_at=when)

    def effective_status(self, entry: Entry, lineage_status: str | None) -> str:
        """The status to show: final as recorded, else ``running`` or ``incomplete``.

        A write run's status lives in its lineage state (``lineage_status``),
        any other run's in its entry.
        """
        status = lineage_status if entry.lineage is not None else entry.status
        if status in FINAL_STATUSES:
            return str(status)
        return "incomplete" if is_free(self.lifecycle_lock(entry.run_id)) else "running"


def make_run_dir(run_dir: Path, *, forbidden: Mapping[str, Path]) -> None:
    """Create ``run_dir`` exclusively, refusing one inside a forbidden tree.

    ``forbidden`` maps a label ("the repository", "the state directory",
    "another run") to a root: a worktree nested in its own repository, or a
    report written over state, is refused before anything is created.
    """
    target = Path(os.path.abspath(run_dir))
    resolved_parent = target.parent.resolve()
    candidate = resolved_parent / target.name
    for label, root in forbidden.items():
        if candidate.is_relative_to(root.resolve()):
            raise RegistryError(f"--run-dir {run_dir} is inside {label} ({root})")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(target, 0o700)
    except FileExistsError:
        raise RegistryError(f"--run-dir {run_dir} already exists; a run needs a new one") from None


__all__ = ["FINAL_STATUSES", "RUN_ID_PATTERN", "Entry", "Registry", "RegistryError", "make_run_dir"]
