"""``ha show``: one run, rebuilt from the state directory, then rendered (spec 0.5.0 §3.9, §3.10).

``run.json`` is a report, never an authority (§3.8.1): a crash after a write's
publication leaves it stale (§3.8.3 step 9), and ``ha clean`` removes it with
the run directory. So every fact with an authority in the state comes from the
state -- identity from the registry entry, the status from the entry or the
lineage state, a write's lineage, branch, base and commits from the lineage
state and the provenance records -- and the rest is display data, read from a
report only when it names this run (plan P6), and from the directory's prompt
and patch only while that directory is still the run's. Nothing here runs git
(plan P5); the only lock touched is the non-blocking liveness probe of the
lifecycle lock.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import lineage as lineages
from . import provenance, reviews
from .report import KIND, PROMPT_FILE, RUN_JSON, RUN_KEYS, SCHEMA
from .run_record import RESULT_FILE_NAME
from .runs import RUN_ID_PATTERN, Entry, Registry, RegistryError
from .state import Unknown
from .write_flow import PATCH_FILE

#: Fields a report never supplies for a run they do not belong to (lot 2 plan P6): a
#: review's verdict and check come from its records in the state (§3.8.1, §3.8.6), its
#: ``cleanup`` from its own report, and ``findings_from`` from a fix's registry entry.
LATER_AUTHORITIES: Final = ("verdict", "vendor_check", "cleanup", "findings_from")
#: What the engine writes for a write run only; a run outside any lineage has none of them.
_WRITE_FIELDS: Final = ("lineage", "branch", "base", "head", "commits")


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


def _finite(text: str) -> float | None:
    number = float(text)
    return number if math.isfinite(number) else None


def _not_finite(_: str) -> None:
    return None


def load_json(text: str) -> object:
    """``json.loads``, but a number that is not finite reads as ``null``: not measured.

    ``json.loads`` accepts ``NaN`` and ``Infinity`` and reads ``1e999`` as infinity,
    and ``json.dumps`` writes them back out as JSON no strict parser reads: one such
    number in a report broke every listing (final review of lot 2 PR B). A document
    nested too deep raises :class:`ValueError` here, as any other that does not parse,
    not the ``RecursionError`` of ``json.loads`` (codex review of PR B, round 2).
    """
    try:
        return json.loads(text, parse_constant=_not_finite, parse_float=_finite)
    except RecursionError:
        raise ValueError("nested too deep to parse") from None


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
    document.update(schema=SCHEMA, kind=KIND, run_id=run_id, steps=[], cost_complete=False)
    return document


def read_run_dir(entry: Entry) -> tuple[dict[str, object] | None, bool, str | None]:
    """What the run's directory says of it: its ``run.json`` cut to the pinned key set when
    it names this run; whether the directory's other files are this run's; and the note
    saying why the report is not used.

    ``prompt.md`` and ``change.patch`` name no run. ``ha clean`` sets ``cleaned_at`` once
    the directory is gone, so what stands there now -- a later run given the same
    ``--run-dir`` -- is not this run's; a report naming another run disowns its
    directory the same way (final review of lot 2 PR B).

    Ticket 1a76fe55: a report with no ``"kind"`` predates the discriminator and is
    tolerated as a run; a ``"kind"`` other than ``"run"`` is not a run.json at all,
    and disowns the directory the same as a report naming another run.
    """
    path = entry.run_dir / RUN_JSON
    if entry.cleaned_at is not None:
        return None, False, f"removed by ha clean at {entry.cleaned_at}: shown from the state"
    if not path.exists():
        return None, True, f"{path} is missing: shown from the state"
    try:
        document = load_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None, True, f"{path} cannot be read: shown from the state"
    if (
        not isinstance(document, dict)
        or document.get("run_id") != entry.run_id
        or document.get("kind") not in (None, KIND)
    ):
        return None, False, f"{path} names another run: its directory is ignored"
    report = {key: document.get(key) for key in RUN_KEYS}
    report["kind"] = KIND
    return report, True, None


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
    report, own, note = read_run_dir(entry)
    notes: list[str] = [] if note is None else [note]
    warnings: list[str] = []
    unknown = False
    document = dict(report) if report is not None else _bare(run_id)
    document.update(
        run_id=run_id,
        target=dict(entry.target),
        repository=str(entry.repository) if entry.repository is not None else None,
    )
    # Plan P6: their authority is a state record a later lot adds; run.json never supplies them.
    document.update(dict.fromkeys(LATER_AUTHORITIES))
    # §3.10: a write run's continuation records, copied from its registry entry.
    document.update(
        continues=entry.continues,
        findings_from=entry.findings_from,
        implement_providers=list(entry.providers) if entry.lineage is not None else None,
    )
    status: str | None = None
    lineage_status: str | None = None
    if entry.lineage is not None:
        try:
            lineage = lineages.load(state, entry.lineage)
        except Unknown as exc:
            notes.append(f"{exc}: the lineage cannot be read")
            status, unknown = "unknown", True
            # Their one authority cannot be read: the report does not stand in for it.
            document.update(lineage=entry.lineage, branch=None, base=None)
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
    else:
        document.update(dict.fromkeys(_WRITE_FIELDS))
    if is_review(entry):
        unknown = _review_records(document, report, state, entry, notes) or unknown
    if status is None:
        status = registry.effective_status(entry, lineage_status)
    if report is not None and report.get("status") != status:
        notes.append(
            f"run.json says {report.get('status')}; the state says {status}: shown from the state"
        )
    document["status"] = status
    patched = entry.lineage is not None or is_review(entry)
    return Shown(
        report=document,
        task=read_task(entry.run_dir) if own else None,
        diffstat=read_diffstat(entry.run_dir) if own and patched else None,
        notes=tuple(notes),
        warnings=tuple(warnings),
        unknown=unknown,
    )


def is_review(entry: Entry) -> bool:
    return entry.target.get("kind") == "workflow" and entry.target.get("shape") == "review"


def _review_records(
    document: dict[str, object],
    report: Mapping[str, object] | None,
    state: Path,
    entry: Entry,
    notes: list[str],
) -> bool:
    """A review's head, verdict, text and check from its records (§3.8.6); ``True`` when one
    cannot be read. Its ``cleanup`` has no record in the state: the report's, for display."""
    run_id = entry.run_id
    unknown = False
    result = None
    # The report's copies are never shown as the review's: only its result names them.
    document.update(verdict=None, head=None, text=None)
    if reviews.result_path(state, run_id).exists():
        try:
            result = reviews.load_result(state, run_id)
        except Unknown as exc:
            notes.append(f"{exc}: the review result cannot be read")
            unknown = True
    elif entry.status in ("approved", "changes"):
        # The registry recorded a verdict, so its result was written before: it is lost.
        notes.append("the review result is missing although a verdict was recorded")
        unknown = True
    check = result.check if result is not None else None
    if result is None:
        try:
            check = reviews.load_check(state, run_id)
        except Unknown as exc:
            notes.append(f"{exc}: the vendor check cannot be read")
            unknown = True
    document.update(
        vendor_check=check.to_document() if check is not None else None,
        cleanup=report.get("cleanup") if report is not None else None,
        base=report.get("base") if report is not None else None,
    )
    if result is not None:
        document.update(verdict=result.verdict, head=result.head, text=result.text)
    return unknown


def from_dir(run_dir: Path) -> Shown:
    """``ha show --dir PATH``: a run directory's ``run.json``, for display only (§3.8.6).

    A review kept with ``--run-dir`` stays readable after the state directory is
    lost; nothing reads a report found by path to decide anything.
    """
    path = run_dir / RUN_JSON
    try:
        document = load_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise NotShown(f"{path} cannot be read: not a run directory of ha") from None
    run_id = document.get("run_id") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or not (isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id))
        or document.get("kind") not in (None, KIND)
    ):
        raise NotShown(f"{path} is not a run.json of ha")
    report = {key: document.get(key) for key in RUN_KEYS}
    report["kind"] = KIND
    return Shown(
        report=report,
        task=read_task(run_dir),
        diffstat=read_diffstat(run_dir),
        notes=(f"display only: {path}, not the state directory",),
        warnings=(),
        unknown=False,
    )


_STATUS_WORDS: Final[Mapping[str, str]] = {
    "no_change": "no change",
    "changes": "changes requested",
}


def _measure(value: object) -> float | None:
    """``value`` as a finite float; ``None`` when it is not one -- NaN, an infinity, or an
    int too large for a float, which ``round`` and ``:.2f`` raise on."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def format_diffstat(stat: Diffstat) -> str:
    """``+120 -14  5 files``: what ``ha show`` and a write's header print (§3.9, §3.10)."""
    return (
        f"+{stat.insertions} -{stat.deletions}  {stat.files} file{'' if stat.files == 1 else 's'}"
    )


def format_duration(seconds: object) -> str:
    """``45s``, ``2m40s``, ``1h02m``; ``-`` when not measured."""
    measured = _measure(seconds)
    if measured is None:
        return "-"
    whole = round(measured)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


def format_cost(cost: object) -> str:
    """``$0.12``; ``-`` when not measured."""
    measured = _measure(cost)
    return "-" if measured is None else f"${measured:.2f}"


def _thousands(count: object) -> str:
    if not isinstance(count, int) or _measure(count) is None:
        return "-"
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count // 1000}k"
    return f"{count / 1_000_000:.1f}M"


def format_tokens(tokens: object) -> str:
    """``in 150k out 3k``; ``-`` when neither side was measured."""
    if not isinstance(tokens, dict):
        return "-"
    given, produced = tokens.get("input"), tokens.get("output")
    if not isinstance(given, int) and not isinstance(produced, int):
        return "-"
    return f"in {_thousands(given)} out {_thousands(produced)}"


def format_tools(tools: object) -> str:
    """``read 25, edit 11``, most used first; ``none`` for ``{}``; ``-`` when not measured."""
    if not isinstance(tools, dict):
        return "-"
    counts = {str(name): count for name, count in tools.items() if isinstance(count, int)}
    if not counts:
        return "none"
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{name} {count}" for name, count in ranked)


def _dash(value: object) -> str:
    return "-" if value is None else str(value)


def _step_cells(step: Mapping[str, object]) -> list[str]:
    model, verdict = step.get("model"), step.get("verdict")
    return [
        _dash(step.get("slot")),
        _dash(step.get("role")),
        _dash(step.get("provider")),
        model if isinstance(model, str) and model.strip() else "(auto)",
        _dash(step.get("exit_code")),
        format_duration(step.get("duration_seconds")),
        format_tokens(step.get("tokens")),
        format_cost(step.get("cost_usd")),
        format_tools(step.get("tools")),
        verdict.upper() if isinstance(verdict, str) else "",
    ]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def vendors_line(check: Mapping[str, object]) -> str:
    """``vendors authors codex, opencode (2 recorded commits) — reviewers agy, claude:
    independent``: the check a review passed before its reviewers started (§3.8.6)."""
    raw_commits, raw_authors = check.get("commits"), check.get("authors")
    raw_reviewers = check.get("reviewers")
    commits = (
        [c for c in raw_commits if isinstance(c, dict)] if isinstance(raw_commits, list) else []
    )
    authors = [str(a) for a in raw_authors] if isinstance(raw_authors, list) else []
    reviewers = sorted(
        {
            str(provider)
            for providers in (raw_reviewers.values() if isinstance(raw_reviewers, dict) else [])
            if isinstance(providers, list)
            for provider in providers
        }
    )
    recorded = sum(1 for c in commits if c.get("run_id") is not None)
    hand = sum(1 for c in commits if c.get("made_by") == "hand")
    attributed = sum(1 for c in commits if c.get("made_by") == "unknown")
    parts = [
        text
        for count, text in (
            (recorded, _plural(recorded, "recorded commit")),
            (hand, f"{hand} hand-written"),
            (attributed, f"{attributed} attributed to unconfined writers"),
        )
        if count
    ]
    detail = f" ({', '.join(parts)})" if parts else ""
    return (
        f"vendors authors {', '.join(authors) or 'none'}{detail} — reviewers "
        f"{', '.join(reviewers) or 'none'}: independent"
    )


def _table(rows: Sequence[Sequence[str]]) -> list[str]:
    """Each column as wide as its widest cell, cells two spaces apart (plan P8)."""
    if not rows:
        return []
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return [
        (
            "  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        ).rstrip()
        for row in rows
    ]


def render(shown: Shown) -> str:
    """The text of ``ha show`` (spec §3.10): header, task, head, warnings, steps, final text."""
    report = shown.report
    target = report.get("target")
    name = target.get("name") if isinstance(target, dict) else None
    status = str(report.get("status"))
    header = (
        f"{report.get('run_id')}  {name or '-'}  exit {_dash(report.get('exit_code'))}  "
        f"{_STATUS_WORDS.get(status, status)}"
    )
    reason = report.get("failure_reason")
    if isinstance(reason, str):
        header += f" ({reason})"
    cleanup = report.get("cleanup")
    if isinstance(cleanup, dict) and cleanup.get("status") == "failed":
        # §3.10: a review whose worktree could not be removed says so on its header.
        header += f"  cleanup failed: {cleanup.get('reason')}"
    lines = [header, f"task    {shown.task or '-'}"]
    branch, head, check = report.get("branch"), report.get("head"), report.get("vendor_check")
    line = None
    if isinstance(branch, str):
        commits = report.get("commits")
        count = len(commits) if isinstance(commits, list) else 0
        line = (
            f"head    {branch} @ {head[:7] if isinstance(head, str) else '-'}  "
            f"{_plural(count, 'commit')}"
        )
    elif isinstance(head, str) and isinstance(check, dict):
        commits = check.get("commits")
        count = len(commits) if isinstance(commits, list) else 0
        line = f"head    {head[:7]}  {_plural(count, 'commit')}"
    if line is not None:
        if shown.diffstat is not None:
            line += f"  {format_diffstat(shown.diffstat)}"
        lines.append(line)
    if isinstance(check, dict):
        lines.append(vendors_line(check))
    lines.extend(f"warning {warning}" for warning in shown.warnings)
    steps = report.get("steps")
    rows = (
        [_step_cells(step) for step in steps if isinstance(step, dict)]
        if isinstance(steps, list)
        else []
    )
    lines.extend(_table(rows))
    text = report.get("text")
    if isinstance(text, str) and text.strip():
        lines.append(f"--- {rows[-1][0] if rows else 'run'} ---")
        lines.append(text.rstrip("\n"))
    return "\n".join(lines) + "\n"


__all__ = [
    "LATER_AUTHORITIES",
    "Diffstat",
    "NotShown",
    "Shown",
    "format_cost",
    "format_diffstat",
    "format_duration",
    "format_tokens",
    "format_tools",
    "from_dir",
    "is_review",
    "load_json",
    "read_diffstat",
    "read_run_dir",
    "read_task",
    "rebuild",
    "render",
    "vendors_line",
]
