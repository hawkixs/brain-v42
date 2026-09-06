"""Read one dream night's tool calls across both wire dialects.

The dream pool writes one ``<date>_<project>_<phase>.events.jsonl`` file per
(project, phase) run under ``logs/dream/``. A pre-pool shape,
``<date>_<phase>.events.jsonl`` (no project component), is still on disk for
165 of 1684 real files across 28 nights starting 2026-07-13; it is recognised
too, mapped to the real phase with an explicit :data:`PRE_POOL_PROJECT`
label rather than a fabricated "unknown" that would read like a genuine
measurement. A filename matching neither shape is never guessed at either —
it is surfaced by the census CLI as its own ``UNMEASURED naming`` bucket.
Two shapes of *file content* have also been observed on disk, and they map
onto the two rails documented by the existing metrics parsers
(``src/brain_v42/metrics/agy_dream_parser.py`` and
``codex_dream_parser.py``) rather than onto a naive guess:

- the **codex** rail (``codex exec --json``, the live default,
  ``BRAIN_DREAM_AGENT_PROVIDER=codex``) emits ``thread.started`` /
  ``item.started`` / ``item.completed`` / ``turn.completed`` records. A tool
  call is an ``item`` with ``type == "mcp_tool_call"``: ``arguments``,
  ``result`` (MCP content blocks) and ``error`` live directly on it.
- the **agy** rail (``agy --output-format stream-json``, the older/fallback
  path) emits ``step_update`` records under a top-level ``"event"`` key. A
  tool call is a ``step_update`` with ``step_type == "tool"``: the real tool
  name and arguments live under ``tool_info.parameters`` (``ToolName`` /
  ``Arguments``), because ``tool_name`` itself is always the constant
  ``"call_mcp_tool"``. Each call produces TWO records sharing one
  ``step_index`` — an ``ACTIVE`` start and a terminal ``DONE``/``ERROR`` — and
  only the terminal one carries ``tool_info.output``. That terminal record
  can still omit ``tool_info.output`` even on a successful, non-error call
  (measured: 105 of 694 agy ``brain_search`` calls); see
  :func:`emptiness_unknown`.

A census that reads only one of the two shapes silently drops every event
written by the other rail — this is exactly what happened to the CLEAN phase
and to REORG's call count in the 2026-09 W11 investigation. This module
normalises both shapes into one :class:`ToolCall` so a census (see
``main`` below) or any other consumer never has to special-case a rail. The
census also never lets a filtered-out phase or project vanish from its
report, and it never scores a call whose emptiness could not be measured as
"not empty" — both silently reproduce the same kind of blind spot the
dialect split created, at a finer grain.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Dialect tag for the codex rail (``item.started``/``item.completed`` shape).
CODEX = "codex"
#: Dialect tag for the agy rail (``step_update`` shape).
AGY = "agy"

_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<project>.+)_(?P<phase>[^_]+)\.events\.jsonl$"
)

#: The pre-pool naming shape, predating the dream pool: no project
#: component. Phase is restricted to the six known dream phases so an
#: unrelated one-segment name is not misread as a phase (it falls through
#: to the ``UNMEASURED naming`` bucket instead, see :func:`_parse_filename`).
_LEGACY_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<phase>clean|connect|promote|reorg|scan|synth)\.events\.jsonl$"
)

#: Project label used for the pre-pool filename shape (see
#: :data:`_LEGACY_FILENAME_RE`), which names no project. Rendered
#: explicitly so it is never mistaken for a real, unresolved project name.
PRE_POOL_PROJECT = "(pre-pool, no project)"

#: Matches the exact empty-result header written by
#: ``brain_v42.mcp.tools.formatters.format_search_results`` when a search
#: returns zero hits: ``## 0 results for "<query>" (across all types)``. A
#: degraded-mode banner line may precede it, hence MULTILINE + search rather
#: than a full-string match. The alternation also matches
#: ``format_knowledge_by_type``'s zero-items header (formatters.py:750),
#: reached when ``brain_search(group_by_type=True)`` returns nothing:
#: ``## Everything known about "<topic>" (0 items)``. Measured on the real
#: corpus: 5 calls used ``group_by_type=True``, 1 of which returned 0 items.
_EMPTY_SEARCH_RE = re.compile(
    r'^## 0 results for "|^## Everything known about ".*" \(0 items\)$', re.MULTILINE
)


class UnknownDialectError(ValueError):
    """Raised when an events.jsonl file matches neither known wire dialect.

    Covers both a genuinely empty file (zero records, so no dialect marker
    was ever seen) and a file whose records carry neither a codex-shaped
    ``"type"`` key nor an agy-shaped ``"event"`` key.
    """


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One normalised tool call, regardless of which dialect wrote it."""

    dialect: str
    phase: str
    project: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    output: str | None = None
    is_error: bool = False
    #: Always ``None`` today: neither dialect emits a per-event timestamp
    #: under ``logs/dream`` (measured 2026-09-06: 0/29312 normalised calls
    #: carried one). Both extraction sites look for a ``timestamp`` key that
    #: is simply never written; the field is kept for forward compatibility
    #: with a rail that starts emitting one, not because it is populated now.
    timestamp: str | None = None


def _parse_filename(path: Path) -> tuple[str, str] | None:
    """Return ``(project, phase)`` parsed from a dream events filename.

    Two shapes are recognised:

    - the pool-era ``<date>_<project>_<phase>.events.jsonl`` convention;
    - the pre-pool ``<date>_<phase>.events.jsonl`` convention (no project),
      returned as (:data:`PRE_POOL_PROJECT`, phase).

    Returns ``None`` for a name matching neither shape. This used to fall
    back to a fabricated ``("unknown", "unknown")`` pair that a census could
    not tell apart from a genuinely measured phase called "unknown" — the
    census CLI is the one caller that names filenames, and it now renders a
    ``None`` result as its own explicit ``UNMEASURED naming`` bucket instead
    of guessing.
    """
    match = _FILENAME_RE.match(path.name)
    if match:
        return (match.group("project"), match.group("phase"))
    legacy_match = _LEGACY_FILENAME_RE.match(path.name)
    if legacy_match:
        return (PRE_POOL_PROJECT, legacy_match.group("phase"))
    return None


def _load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}: invalid JSON on line {line_no}") from exc
            if isinstance(record, dict):
                records.append(record)
    return records


def _dialect_of(records: list[dict[str, Any]], path: Path) -> str:
    for record in records:
        if "event" in record:
            return AGY
        if "type" in record:
            return CODEX
    raise UnknownDialectError(
        f"{path}: no record carries a codex-shaped 'type' key or an "
        f"agy-shaped 'event' key ({len(records)} record(s) read)"
    )


def _load_and_detect(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Read ``path`` once and return ``(dialect, records)``.

    Both :func:`detect_dialect` and :func:`iter_tool_calls` need the parsed
    records to know the dialect, and the census loop needs them again to
    extract calls. Routing every caller through this one helper means a
    given file's JSON is parsed exactly once per call site instead of once
    per site that happens to want the dialect *and* the records.
    """
    records = _load_records(path)
    dialect = _dialect_of(records, path)
    return dialect, records


def detect_dialect(path: Path | str) -> str:
    """Return :data:`CODEX` or :data:`AGY` for the given events.jsonl file.

    Raises :class:`UnknownDialectError` for an empty file or one whose
    records match neither shape.
    """
    dialect, _records = _load_and_detect(Path(path))
    return dialect


def _error_message(error: Any) -> str | None:
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return None


def _iter_codex_calls(
    records: list[dict[str, Any]], project: str, phase: str
) -> Iterator[ToolCall]:
    for record in records:
        if record.get("type") != "item.completed":
            continue
        item = record.get("item")
        if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
            continue
        error = item.get("error")
        output = _codex_output_text(item.get("result"))
        if output is None:
            output = _error_message(error)
        yield ToolCall(
            dialect=CODEX,
            phase=phase,
            project=project,
            tool=item.get("tool") or "",
            arguments=item.get("arguments") or {},
            output=output,
            is_error=error is not None,
            timestamp=record.get("timestamp") or item.get("timestamp"),
        )


def _codex_output_text(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            return first["text"]
    structured = result.get("structured_content")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        return structured["result"]
    return None


def _iter_agy_calls(records: list[dict[str, Any]], project: str, phase: str) -> Iterator[ToolCall]:
    for record in records:
        if record.get("event") != "step_update":
            continue
        step = record.get("step_update")
        if not isinstance(step, dict):
            continue
        if step.get("step_type") != "tool" or step.get("state") not in ("DONE", "ERROR"):
            # Skip the ACTIVE start of a call (no output yet, matched by its
            # own terminal record below) and any non-tool step.
            continue
        tool_info = step.get("tool_info")
        tool_info = tool_info if isinstance(tool_info, dict) else {}
        parameters = tool_info.get("parameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        error = tool_info.get("error")
        is_error = step.get("state") == "ERROR" or error is not None
        output = tool_info.get("output")
        if not isinstance(output, str):
            output = _error_message(error)
        yield ToolCall(
            dialect=AGY,
            phase=phase,
            project=project,
            tool=parameters.get("ToolName") or "",
            arguments=parameters.get("Arguments") or {},
            output=output,
            is_error=is_error,
            timestamp=record.get("timestamp") or step.get("timestamp"),
        )


def _iter_calls(
    records: list[dict[str, Any]], dialect: str, project: str, phase: str
) -> Iterator[ToolCall]:
    if dialect == CODEX:
        yield from _iter_codex_calls(records, project, phase)
    else:
        yield from _iter_agy_calls(records, project, phase)


def iter_tool_calls(path: Path | str) -> Iterator[ToolCall]:
    """Return an iterator of :class:`ToolCall` for every completed call in ``path``.

    Normalises both the codex (``item.completed``) and agy (``step_update``)
    shapes. Only terminal records are yielded — one per logical call — so a
    call that starts but never completes (a truncated file) is silently
    absent rather than reported half-done.

    Raises :class:`UnknownDialectError` immediately, when this function is
    called — not only once the returned iterator is first advanced. This is
    a plain function that loads and classifies the file eagerly and returns
    the generator from a private helper, precisely so ``try: iter_tool_calls(p)
    / except UnknownDialectError`` behaves as a caller would expect.

    A filename matching neither known naming convention (see
    :func:`_parse_filename`) falls back to ``("unknown", "unknown")`` here:
    this function returns ``ToolCall`` objects, not a rendered report, so
    there is nothing for the fallback to masquerade as. The census CLI
    (``run_census``) does not use this fallback — it surfaces the same
    filenames explicitly instead.
    """
    path = Path(path)
    dialect, records = _load_and_detect(path)
    project, phase = _parse_filename(path) or ("unknown", "unknown")
    return _iter_calls(records, dialect, project, phase)


def is_empty_search(call: ToolCall) -> bool:
    """True when ``call`` is a ``brain_search`` call whose result was empty.

    Mirrors the exact contract of
    ``brain_v42.mcp.tools.formatters.format_search_results``: with zero
    results the header renders as ``## 0 results for "<query>" (across all
    types)``, optionally preceded by a degraded-mode banner line. An error
    result, a missing output, or any other tool is never an empty search.
    """
    if call.tool != "brain_search" or call.is_error or call.output is None:
        return False
    return bool(_EMPTY_SEARCH_RE.search(call.output))


def emptiness_unknown(call: ToolCall) -> bool:
    """True when whether ``call`` returned an empty search cannot be measured.

    A non-error ``brain_search`` call always carries its result text under
    ``tool_info.output`` on the codex rail, but the agy rail's terminal
    ``DONE`` record can omit ``tool_info.output`` entirely even though the
    call itself succeeded (measured on the real corpus: 105 of 694 agy
    ``brain_search`` calls, 15.1%). :func:`is_empty_search` folds that case
    into "not empty", which would silently understate an empty-result rate
    computed over the same denominator. Use this predicate to route such
    calls into their own ``unknown`` bucket instead.
    """
    return call.tool == "brain_search" and not call.is_error and call.output is None


# --- Census CLI -------------------------------------------------------------


@dataclass
class _Counts:
    calls: int = 0
    empties: int = 0
    #: Non-error ``brain_search`` calls whose emptiness could not be
    #: determined (see :func:`emptiness_unknown`) — never folded into
    #: ``empties`` or silently dropped.
    unknown: int = 0


@dataclass
class CensusReport:
    """Result of censusing one night's dream tool calls."""

    night: str
    tool_filter: str | None
    files_total: int
    files_by_dialect: dict[str, int]
    unclassified_files: dict[str, str]
    #: Files whose dialect was classified but whose name matched neither
    #: known naming convention (see :func:`_parse_filename`) — rendered as
    #: their own ``UNMEASURED naming`` bucket, never folded into a
    #: fabricated "unknown" phase or project.
    unnamed_files: dict[str, str]
    calls_by_phase: dict[str, _Counts]
    calls_by_project: dict[str, _Counts]
    total_calls: int
    total_empties: int
    total_unknown: int


def _default_logs_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "logs" / "dream"


def _night_files(logs_dir: Path, night: str) -> list[Path]:
    return sorted(logs_dir.glob(f"{night}_*.events.jsonl"))


def run_census(logs_dir: Path, night: str, tool_filter: str | None = None) -> CensusReport:
    """Census every tool call written for ``night`` under ``logs_dir``.

    Never returns a silent zero for a night with no files: callers must
    check ``files_total`` and report ``UNMEASURED`` rather than printing the
    zeroed totals as if they were a real, empty measurement. A file whose
    dialect could not be classified, or whose JSON is malformed, costs only
    that one file (filed under ``unclassified_files``) rather than aborting
    the whole night; a file whose name matches neither known naming
    convention costs only that one file too (filed under ``unnamed_files``),
    never a fabricated "unknown" phase or project.
    """
    files = _night_files(logs_dir, night)
    files_by_dialect: dict[str, int] = defaultdict(int)
    unclassified_files: dict[str, str] = {}
    unnamed_files: dict[str, str] = {}
    calls_by_phase: dict[str, _Counts] = defaultdict(_Counts)
    calls_by_project: dict[str, _Counts] = defaultdict(_Counts)
    total_calls = 0
    total_empties = 0
    total_unknown = 0

    for path in files:
        try:
            dialect, records = _load_and_detect(path)
        except ValueError as exc:
            # UnknownDialectError (an unrecognised or empty file) and a bare
            # ValueError from malformed JSON (see _load_records) are both
            # ValueError; either way, one bad file costs one file, not the
            # night's whole census.
            unclassified_files[str(path)] = str(exc)
            continue
        files_by_dialect[dialect] += 1

        parsed = _parse_filename(path)
        if parsed is None:
            unnamed_files[str(path)] = (
                "filename matches neither the pool-era "
                "(<date>_<project>_<phase>.events.jsonl) nor the pre-pool "
                "(<date>_<phase>.events.jsonl) naming convention"
            )
            continue
        project, phase = parsed

        # A successfully classified and named file names its own (project,
        # phase) even if every call inside it is filtered out below: seed
        # both buckets here so "0 calls" always renders, rather than the
        # phase or project silently disappearing from the report.
        calls_by_phase.setdefault(phase, _Counts())
        calls_by_project.setdefault(project, _Counts())

        for call in _iter_calls(records, dialect, project, phase):
            if tool_filter is not None and call.tool != tool_filter:
                continue
            total_calls += 1
            phase_counts = calls_by_phase[call.phase]
            project_counts = calls_by_project[call.project]
            phase_counts.calls += 1
            project_counts.calls += 1
            if is_empty_search(call):
                total_empties += 1
                phase_counts.empties += 1
                project_counts.empties += 1
            elif emptiness_unknown(call):
                total_unknown += 1
                phase_counts.unknown += 1
                project_counts.unknown += 1

    return CensusReport(
        night=night,
        tool_filter=tool_filter,
        files_total=len(files),
        files_by_dialect=dict(files_by_dialect),
        unclassified_files=unclassified_files,
        unnamed_files=unnamed_files,
        calls_by_phase=dict(calls_by_phase),
        calls_by_project=dict(calls_by_project),
        total_calls=total_calls,
        total_empties=total_empties,
        total_unknown=total_unknown,
    )


def format_census_report(report: CensusReport, logs_dir: Path) -> str:
    """Render a :class:`CensusReport` as deterministic, greppable text."""
    lines: list[str] = [f"night: {report.night}"]
    if report.tool_filter is not None:
        lines.append(f"tool filter: {report.tool_filter}")

    if report.files_total == 0:
        lines.append(f"UNMEASURED: no events.jsonl files found under {logs_dir} for this night")
        return "\n".join(lines)

    lines.append(f"files: {report.files_total} total")
    for dialect in sorted(report.files_by_dialect):
        lines.append(f"  dialect {dialect}: {report.files_by_dialect[dialect]} files")
    if report.unclassified_files:
        lines.append(f"  UNMEASURED dialect: {len(report.unclassified_files)} file(s)")
        for path in sorted(report.unclassified_files):
            lines.append(f"    {path}: {report.unclassified_files[path]}")
    if report.unnamed_files:
        lines.append(f"  UNMEASURED naming: {len(report.unnamed_files)} file(s)")
        for path in sorted(report.unnamed_files):
            lines.append(f"    {path}: {report.unnamed_files[path]}")

    lines.append("by phase:")
    for phase in sorted(report.calls_by_phase):
        counts = report.calls_by_phase[phase]
        lines.append(
            f"  {phase}: calls={counts.calls} empties={counts.empties} unknown={counts.unknown}"
        )

    lines.append("by project:")
    for project in sorted(report.calls_by_project):
        counts = report.calls_by_project[project]
        lines.append(
            f"  {project}: calls={counts.calls} empties={counts.empties} unknown={counts.unknown}"
        )

    lines.append(
        f"total: calls={report.total_calls} empties={report.total_empties} "
        f"unknown={report.total_unknown}"
    )
    if report.total_unknown:
        lines.append(
            f"UNMEASURED emptiness: {report.total_unknown} call(s) with no recorded output "
            "(never folded into empties)"
        )
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.dream.events_reader",
        description=(
            "Census dream tool calls across both wire dialects (codex and agy) for one night."
        ),
    )
    parser.add_argument("--night", required=True, help="Night to census, e.g. 2026-09-05")
    parser.add_argument(
        "--tool",
        default=None,
        help="Restrict the census to one tool name, e.g. brain_search",
    )
    parser.add_argument(
        "--logs-dir",
        default=None,
        type=Path,
        help="Override the logs/dream directory (defaults to the repo's logs/dream)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logs_dir: Path = args.logs_dir if args.logs_dir is not None else _default_logs_dir()
    report = run_census(logs_dir=logs_dir, night=args.night, tool_filter=args.tool)
    print(format_census_report(report, logs_dir=logs_dir))
    return 0 if report.files_total > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
