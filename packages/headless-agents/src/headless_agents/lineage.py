"""Lineage state: a write's worktree, its members and the write in flight (spec 0.5.0 §3.8.1).

A lineage is owned by the run that started it (``owner`` = that run's id) and
lives in ``<state>/lineages/<owner>.json``: its worktree and branch, its base,
the status of every member -- a write run's status has no other authority --
the pending write, if any, and why it is compromised, if it is. It also
records the repository's resolved common git dir, the key every repository
check uses, so enumerating a repository's lineages runs no git and reads no
worktree.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .runs import RUN_ID_PATTERN
from .state import Unknown, create_once, publish, read


@dataclass(frozen=True)
class PendingWrite:
    run_id: str
    providers: tuple[str, ...]
    unconfined: bool
    start_tip: str | None
    start_reflog: int | None


@dataclass(frozen=True)
class LineageState:
    owner: str
    repository: Path
    common_dir: Path
    worktree: Path
    branch: str
    #: The resolved base commit; ``None`` until preparation resolves it (§3.8.3 step 3).
    base: str | None
    members: Mapping[str, str]
    pending: PendingWrite | None
    compromised: str | None


def lineage_path(state: Path, owner: str) -> Path:
    return state / "lineages" / f"{owner}.json"


def lineage_lock(state: Path, owner: str) -> Path:
    return state / "lineages" / f"{owner}.lock"


def registry_lock(state: Path) -> Path:
    return state / "lineages.lock"


def _document(lineage: LineageState) -> dict[str, object]:
    pending = lineage.pending
    return {
        "owner": lineage.owner,
        "repository": str(lineage.repository),
        "common_dir": str(lineage.common_dir),
        "worktree": str(lineage.worktree),
        "branch": lineage.branch,
        "base": lineage.base,
        "members": dict(lineage.members),
        "pending": None
        if pending is None
        else {
            "run_id": pending.run_id,
            "providers": list(pending.providers),
            "unconfined": pending.unconfined,
            "start_tip": pending.start_tip,
            "start_reflog": pending.start_reflog,
        },
        "compromised": lineage.compromised,
    }


def _str(document: Mapping[str, object], key: str, path: Path) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise Unknown(f"{path}: {key} is not a string")
    return value


def _optional_str(value: object, what: str, path: Path) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise Unknown(f"{path}: {what} is malformed")


def _optional_int(value: object, what: str, path: Path) -> int | None:
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise Unknown(f"{path}: {what} is malformed")


def _pending(value: object, path: Path) -> PendingWrite | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise Unknown(f"{path}: pending is malformed")
    providers = value.get("providers")
    if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
        raise Unknown(f"{path}: pending providers are malformed")
    unconfined = value.get("unconfined")
    if not isinstance(unconfined, bool):
        raise Unknown(f"{path}: pending unconfined is malformed")
    start_tip = _optional_str(value.get("start_tip"), "pending start_tip", path)
    start_reflog = _optional_int(value.get("start_reflog"), "pending start_reflog", path)
    return PendingWrite(
        run_id=_str(value, "run_id", path),
        providers=tuple(providers),
        unconfined=unconfined,
        start_tip=start_tip,
        start_reflog=start_reflog,
    )


def load(state: Path, owner: str) -> LineageState:
    """The lineage ``owner``; :class:`~headless_agents.state.Unknown` on any doubt."""
    path = lineage_path(state, owner)
    document = read(path, expect_id=("owner", owner))
    members = document.get("members")
    if not isinstance(members, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in members.items()
    ):
        raise Unknown(f"{path}: members are malformed")
    compromised = _optional_str(document.get("compromised"), "compromised", path)
    return LineageState(
        owner=owner,
        repository=Path(_str(document, "repository", path)),
        common_dir=Path(_str(document, "common_dir", path)),
        worktree=Path(_str(document, "worktree", path)),
        branch=_str(document, "branch", path),
        base=_optional_str(document.get("base"), "base", path),
        members=dict(members),
        pending=_pending(document.get("pending"), path),
        compromised=compromised,
    )


def create(state: Path, lineage: LineageState) -> None:
    """Create a new lineage's state once; :class:`FileExistsError` if it exists."""
    create_once(lineage_path(state, lineage.owner), _document(lineage))


def save(state: Path, lineage: LineageState) -> None:
    """Publish the lineage's state whole (spec §3.8.1)."""
    publish(lineage_path(state, lineage.owner), _document(lineage))


def owners(state: Path) -> list[str]:
    """Every lineage owner in the state, ascending."""
    directory = state / "lineages"
    if not directory.is_dir():
        return []
    return sorted(
        path.stem for path in directory.glob("*.json") if RUN_ID_PATTERN.fullmatch(path.stem)
    )


def of_repository(state: Path, common_dir: Path) -> list[str]:
    """Owners whose repository shares ``common_dir``, ascending.

    An unreadable lineage is included: it reads as ``Unknown`` when loaded and
    refuses, rather than silently leaving the set.
    """
    key = common_dir.resolve()
    found = []
    for owner in owners(state):
        try:
            document = read(lineage_path(state, owner), expect_id=("owner", owner))
        except Unknown:
            found.append(owner)
            continue
        recorded = document.get("common_dir")
        if not isinstance(recorded, str) or Path(recorded).resolve() == key:
            found.append(owner)
    return found


__all__ = [
    "LineageState",
    "PendingWrite",
    "create",
    "lineage_lock",
    "lineage_path",
    "load",
    "of_repository",
    "owners",
    "registry_lock",
    "save",
]
