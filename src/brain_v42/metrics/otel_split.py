"""Split combined Claude Code stdout into (report, otel) streams.

When `claude -p` is launched with the OTEL console exporters enabled
(``OTEL_LOGS_EXPORTER=console`` / ``OTEL_METRICS_EXPORTER=console``), the
final assistant text, tool output and OTEL JS-object blobs are all written
to the *same* stdout stream. dream.sh used to concatenate everything into a
single ``{date}_{phase}.log`` file, which made those logs useless for human
diagnosis — on 2026-04-09 the 156 KB synth log contained zero lines of
actual report text because the synth phase timed out mid-run and only OTEL
events had been flushed.

This module extracts the two streams:

    raw stdout   →   report.log   (assistant + tool output only)
                     otel.log     (OTEL JS-object blobs only)

Detection heuristic (verified against real Claude Code OTEL output, the
``@opentelemetry/sdk-node`` ConsoleLogRecordExporter format):

    * a top-level OTEL blob is always opened by a line consisting of
      exactly ``{`` (no leading whitespace, no trailing content);
    * the blob is closed by a line consisting of exactly ``}``;
    * nested fields inside the blob are always indented (``"  resource:"``
      etc.), so an inner ``{`` never starts at column 0.

The first condition is cheap and robust enough: any line that starts with
``{`` followed by other content (e.g. a markdown code fence ``{ foo: 1 }``
on a single line) is treated as regular report text.

Truncated OTEL blobs (timeout before flush finished the closing brace)
are captured to the otel file through EOF, so the report file stays clean
even when the run is killed mid-event.
"""

from __future__ import annotations

import argparse
import os


def _looks_like_otel_first_line(line: str) -> bool:
    """Return True if `line` has the shape of an OTEL JS-object first key.

    OTEL console export uses JS-object syntax (unquoted keys):
        resource: {
        body: "claude_code.api_request",
        descriptor: {...}

    A PROMOTE REPORT JSON body uses quoted keys:
        "dry_run": true,
        "target_type": "runbook"

    Distinguish by checking whether the indented key is quoted. Empty
    lines are treated as OTEL-like (no evidence either way — keep
    scanning as OTEL since nesting would already have us in-block).
    """
    stripped = line.strip()
    if not stripped:
        return True
    # JSON: "<key>": ...
    if stripped.startswith('"'):
        return False
    # JS object: <key>: ..., or nested { ... }
    return True


def split_log(raw: str) -> tuple[str, str]:
    """Split combined claude stdout into ``(report_text, otel_text)``.

    The returned strings preserve original newlines; their concatenation
    (in order) matches the input modulo line ordering inside each stream.
    An unclosed OTEL block at EOF is treated as OTEL through EOF.

    A bare ``{`` on its own line only starts an OTEL block if the NEXT
    non-empty line has JS-object syntax (unquoted key). A quoted key
    (``"foo":``) signals a JSON report body — PROMOTE emits one inside
    its ``=== PROMOTE REPORT === ... === END ===`` markers. Before this
    check the splitter routed the entire JSON body into the OTEL stream,
    so the validator saw empty markers and marked dream_runs partial.
    """
    lines = raw.splitlines(keepends=True)
    report_parts: list[str] = []
    otel_parts: list[str] = []
    in_otel = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip("\r\n")

        if in_otel:
            otel_parts.append(line)
            if stripped == "}":
                in_otel = False
        else:
            if stripped == "{":
                # Peek the next line to distinguish OTEL from a JSON body.
                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                if _looks_like_otel_first_line(next_line):
                    in_otel = True
                    otel_parts.append(line)
                else:
                    report_parts.append(line)
            else:
                report_parts.append(line)
        i += 1

    return "".join(report_parts), "".join(otel_parts)


def split_log_cli(raw_path: str, report_path: str, otel_path: str) -> None:
    """Read ``raw_path``, split it, write the two outputs.

    A missing input file is treated as an empty stream — both output files
    are still created (empty) so downstream tools never hit ENOENT when a
    phase produced no stdout at all.
    """
    if os.path.isfile(raw_path):
        with open(raw_path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    else:
        raw = ""

    report, otel = split_log(raw)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    with open(otel_path, "w", encoding="utf-8") as f:
        f.write(otel)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split combined claude stdout into report and OTEL files",
    )
    parser.add_argument("raw", help="Path to combined stdout log")
    parser.add_argument("--report", required=True, help="Output path for report-only log")
    parser.add_argument("--otel", required=True, help="Output path for OTEL-only log")
    args = parser.parse_args()

    split_log_cli(args.raw, args.report, args.otel)


if __name__ == "__main__":
    main()
