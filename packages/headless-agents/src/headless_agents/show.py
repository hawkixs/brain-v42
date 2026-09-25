"""``ha show``: one run, rebuilt from the state directory, then rendered (spec 0.5.0 §3.9, §3.10).

``run.json`` is a report, never an authority (§3.8.1): a crash after a write's
publication leaves it stale (§3.8.3 step 9), and ``ha clean`` removes it with
the run directory. So every fact with an authority in the state comes from the
state -- identity from the registry entry, the status from the entry or the
lineage state, a write's lineage, branch, base and commits from the lineage
state and the provenance records -- and the rest is display data, read from a
report only when it names this run (plan P6). Nothing here runs git (plan P5);
the only lock touched is the non-blocking liveness probe of the lifecycle lock.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import lineage as lineages
from . import provenance
from .report import PROMPT_FILE, RUN_JSON, RUN_KEYS, SCHEMA
from .run_record import RESULT_FILE_NAME
from .runs import RUN_ID_PATTERN, Entry, Registry, RegistryError
from .state import Unknown
from .write_flow import PATCH_FILE

#: Fields whose one authority is a state record a later lot introduces (plan P6): a
#: review's result (spec §3.8.1, §3.8.6) and the continuation records (§3.10).
#: ``ha show`` never takes them from ``run.json``; lots 3 and 4 fill them from the state.
LATER_AUTHORITIES: Final = (
    "verdict",
    "vendor_check",
    "cleanup",
    "continues",
    "findings_from",
    "implement_providers",
)


class NotShown(ValueError):
    """What ``ha show`` cannot name: not a run id, no registered run, or a 0.4.0 run."""


@dataclass(frozen=True)
class Diffstat:
    insertions: int
    deletions: int
    files: int


@dataclass(frozen=True)
class Shown:
    report: dict[str, object]
    task: str | None
    diffstat: Diffstat | None
    notes: tuple[str, ...]
    warnings: tuple[str, ...]
    unknown: bool


def read_task(run_dir: Path) -> str | None:
    """The first non-blank line of the run's ``prompt.md``; ``None`` when there is none."""
    try:
        text = (run_dir / PROMPT_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return next((line.strip() for line in text.splitlines() if line.strip()), None)


def read_diffstat(run_dir: Path) -> Diffstat | None:
    """Count ``change.patch`` (``git diff --binary base HEAD``): files, and lines inside hunks.

    Only a line after a hunk header counts, so a file header's ``+++``/``---``
    does not, and an added line whose own text starts with ``++`` does.
    """
    try:
        text = (run_dir / PATCH_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    insertions = deletions = files = 0
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            files += 1
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            insertions += 1
        elif in_hunk and line.startswith("-"):
            deletions += 1
    return Diffstat(insertions=insertions, deletions=deletions, files=files)


def _bare(run_id: str) -> dict[str, object]:
    """What the state alone says of a run: its steps, text and measures are unknown."""
    document: dict[str, object] = dict.fromkeys(RUN_KEYS)
    document.update(schema=SCHEMA, run_id=run_id, steps=[], cost_complete=False)
    return document


def _usable_report(entry: Entry, notes: list[str]) -> dict[str, object] | None:
    """The run's ``run.json`` cut to the pinned key set, when it names this run."""
    path = entry.run_dir / RUN_JSON
    if not path.exists():
        if entry.cleaned_at is not None:
            notes.append(f"removed by ha clean at {entry.cleaned_at}: shown from the state")
        else:
            notes.append(f"{path} is missing: shown from the state")
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        notes.append(f"{path} cannot be read: shown from the state")
        return None
    if not isinstance(document, dict) or document.get("run_id") != entry.run_id:
        notes.append(f"{path} names another run: ignored, shown from the state")
        return None
    return {key: document.get(key) for key in RUN_KEYS}


def _commits(reported: object, recorded: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """The recorded commits: in the report's order where it has one, the rest by sha."""
    by_sha = {
        str(r.get("sha")): {"sha": r.get("sha"), "made_by": r.get("made_by")} for r in recorded
    }
    order = (
        [c.get("sha") for c in reported if isinstance(c, dict)]
        if isinstance(reported, list)
        else []
    )
    known = [by_sha.pop(sha) for sha in order if isinstance(sha, str) and sha in by_sha]
    return known + [by_sha[sha] for sha in sorted(by_sha)]


def rebuild(run_id: str, *, state: Path, runs_root: Path) -> Shown:
    """``run_id``'s report, rebuilt from the state (plan P6).

    :class:`NotShown` when ``run_id`` names no registered run -- a 0.4.0 run
    named as such (§3.8.1). ``unknown`` is set when the registry entry, the
    lineage state or a provenance record cannot be read.
    """
    registry = Registry(state, runs_root=runs_root)
    try:
        entry = registry.resolve(run_id)
    except RegistryError as exc:
        legacy = runs_root / run_id
        if (
            RUN_ID_PATTERN.fullmatch(run_id)
            and (legacy / RESULT_FILE_NAME).is_file()
            and not (legacy / RUN_JSON).exists()
        ):
            raise NotShown(
                f"{run_id} is a 0.4.0 run: ha runs lists it, no command accepts it"
            ) from None
        raise NotShown(str(exc)) from None
    except Unknown as exc:
        document = _bare(run_id)
        document["status"] = "unknown"
        return Shown(
            report=document,
            task=None,
            diffstat=None,
            notes=(f"{exc}: recover it by hand",),
            warnings=(),
            unknown=True,
        )
    notes: list[str] = []
    warnings: list[str] = []
    unknown = False
    report = _usable_report(entry, notes)
    document = dict(report) if report is not None else _bare(run_id)
    document.update(
        run_id=run_id,
        target=dict(entry.target),
        repository=str(entry.repository) if entry.repository is not None else None,
    )
    # Plan P6: their authority is a state record a later lot adds; run.json never supplies them.
    document.update(dict.fromkeys(LATER_AUTHORITIES))
    status: str | None = None
    lineage_status: str | None = None
    if entry.lineage is not None:
        try:
            lineage = lineages.load(state, entry.lineage)
        except Unknown as exc:
            notes.append(f"{exc}: the lineage cannot be read")
            status, unknown = "unknown", True
        else:
            document.update(lineage=lineage.owner, branch=lineage.branch, base=lineage.base)
            if run_id in lineage.members:
                lineage_status = lineage.members[run_id]
            else:
                # The lineage is created with its first member listed (write_flow._intent):
                # silence about this run is no status, never "running" (plan P6).
                notes.append(f"the lineage {lineage.owner} does not list this run")
                status, unknown = "unknown", True
            if lineage.compromised is not None:
                warnings.append(f"lineage {lineage.owner} is compromised: {lineage.compromised}")
        recorded, unreadable = provenance.of_run(state, run_id)
        if unreadable:
            notes.append(
                f"{len(unreadable)} provenance record(s) cannot be read: "
                "the commits may be incomplete"
            )
            unknown = True
        document["commits"] = _commits(document.get("commits"), recorded)
    if status is None:
        status = registry.effective_status(entry, lineage_status)
    if report is not None and report.get("status") != status:
        notes.append(
            f"run.json says {report.get('status')}; the state says {status}: shown from the state"
        )
    document["status"] = status
    return Shown(
        report=document,
        task=read_task(entry.run_dir),
        diffstat=read_diffstat(entry.run_dir) if entry.lineage is not None else None,
        notes=tuple(notes),
        warnings=tuple(warnings),
        unknown=unknown,
    )


__all__ = [
    "LATER_AUTHORITIES",
    "Diffstat",
    "NotShown",
    "Shown",
    "read_diffstat",
    "read_task",
    "rebuild",
]
