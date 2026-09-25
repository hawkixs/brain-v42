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
from .state import Unknown, create_once, publish, read

RUN_ID_PATTERN: Final = re.compile(r"\d{8}T\d{6}-[0-9a-f]{8}")
FINAL_STATUSES: Final = frozenset(
    {"answered", "failed", "committed", "no_change", "approved", "changes"}
)
#: What ``ha`` stores as a run's status, in its entry or in its lineage: ``running``,
#: or a final status. ``incomplete`` is derived from the lifecycle lock, never stored.
STORED_STATUSES: Final = FINAL_STATUSES | {"running"}
MINT_ATTEMPTS: Final = 100


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


def _null_or_text(value: object) -> bool:
    return value is None or (isinstance(value, str) and bool(value))


def _is_target(target: Mapping[object, object]) -> bool:
    """A target states its kind and its name, in text -- as the engine writes it."""
    return all(isinstance(value, str) for value in target.values()) and all(
        target.get(key) for key in ("kind", "name")
    )


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

    def create(
        self,
        run_id: str,
        *,
        run_dir: Path | None,
        target: Mapping[str, str],
        repository: Path | None,
        lineage: str | None,
    ) -> Entry:
        """Create the entry of ``run_id`` once; ``FileExistsError`` when it is taken.

        The engine calls this while holding the id's lifecycle lock, so no
        registered run is ever seen with a free lock before it starts (§3.8.3).
        """
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
        create_once(self._path(run_id), document)
        return self._entry(document, self._path(run_id))

    def register(
        self,
        *,
        run_dir: Path | None,
        target: Mapping[str, str],
        repository: Path | None,
        lineage: str | None,
    ) -> Entry:
        """Mint an id and create its entry; the default run dir is built from that id."""
        for _ in range(MINT_ATTEMPTS):
            try:
                return self.create(
                    self.mint(),
                    run_dir=run_dir,
                    target=target,
                    repository=repository,
                    lineage=lineage,
                )
            except FileExistsError:
                continue
        raise RegistryError(f"could not mint a fresh run id in {MINT_ATTEMPTS} attempts")

    @staticmethod
    def _entry(document: Mapping[str, object], path: Path) -> Entry:
        """The entry ``document`` states; :class:`Unknown` when a key :meth:`create` writes
        is missing or holds what ``ha`` never writes there.

        ``read`` vouches for the JSON and the id only: a well-formed object without its
        ``run_dir`` escaped as ``KeyError`` and crashed ``ha runs`` and ``ha clean``
        (codex review of the lot 2 plan, round 3); a target turned into text showed a
        run of target ``None`` (codex review of lot 2 PR B, round 1); and a missing
        status let a run outside any lineage take ``running`` or ``incomplete`` from
        its lock (round 3) -- a silent authority is no status (plan P6). Unknown is
        never empty, nor invented (§3.8.1).
        """
        run_dir, target = document.get("run_dir"), document.get("target")
        if not isinstance(run_dir, str) or not run_dir:
            raise Unknown(f"{path}: run_dir is malformed")
        if not isinstance(target, dict) or not _is_target(target):
            raise Unknown(f"{path}: target is malformed")
        for key in ("repository", "lineage", "cleaned_at"):
            if key not in document or not _null_or_text(document[key]):
                raise Unknown(f"{path}: {key} is malformed")
        status = document.get("status")
        if not isinstance(status, str) or status not in STORED_STATUSES:
            raise Unknown(f"{path}: status is malformed")
        created_at = document.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise Unknown(f"{path}: created_at is malformed")
        return Entry(
            run_id=str(document["run_id"]),
            run_dir=Path(run_dir),
            repository=_optional_path(document.get("repository")),
            target=dict(target),
            lineage=_optional_str(document.get("lineage")),
            status=status,
            cleaned_at=_optional_str(document.get("cleaned_at")),
        )

    def run_ids(self) -> list[str]:
        """Every registered run id, from the entries' names (unordered)."""
        if not self._entries.is_dir():
            return []
        return [
            path.stem
            for path in self._entries.glob("*.json")
            if RUN_ID_PATTERN.fullmatch(path.stem)
        ]

    def resolve(self, run_id: str) -> Entry:
        """The entry of ``run_id``; :class:`~headless_agents.state.Unknown` when it is corrupt."""
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise RegistryError(f"not a run id: {run_id!r}")
        path = self._path(run_id)
        if not path.exists():
            raise RegistryError(f"no run {run_id} in {self._entries}")
        return self._entry(read(path, expect_id=("run_id", run_id)), path)

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

    def forget(self, run_id: str) -> None:
        """Remove the entry of a run that never started (§3.8.1): nothing to keep."""
        self._path(run_id).unlink(missing_ok=True)

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
    # Checked again on what was actually created: a path component swapped for a
    # symbolic link between the check and the creation (codex review of #207).
    created = target.resolve()
    for label, root in forbidden.items():
        if created.is_relative_to(root.resolve()):
            os.rmdir(created)
            raise RegistryError(f"--run-dir {run_dir} resolved inside {label} ({root})")


__all__ = [
    "FINAL_STATUSES",
    "MINT_ATTEMPTS",
    "RUN_ID_PATTERN",
    "STORED_STATUSES",
    "Entry",
    "Registry",
    "RegistryError",
    "make_run_dir",
]
