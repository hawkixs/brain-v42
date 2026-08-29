"""Parse OTEL console output from dream phase runs and insert into dream_runs table.

Usage (THIS COMMAND WRITES A ROW TO THE CONFIGURED DATABASE — see below):
    python -m brain_v42.metrics.dream_parser \
        --phase scan --model sonnet --date 2000-01-01 \
        --status done --duration 42 --project-key example-project output.log

The date and project key above are DELIBERATELY IMPOSSIBLE. Running this
example verbatim inserts a real `dream_runs` row against whatever
`POSTGRES_URL` points at, which in this repository is production — it
happened on 2026-08-09, from a read-only review agent that ran the
documented line to see what it did. A plausible date (`2026-04-05`) made
that row indistinguishable from real telemetry, and it skewed
`dream_preflight`'s `max(created_at)` enough to nearly make the next
night skip its expensive phases. An impossible date makes the same
mistake obvious at a glance: `SELECT * FROM dream_runs WHERE run_date
< '2026-01-01'` finds it.

`--project-key` is required and has no default: this is the command an
operator copies to replay a lost night by hand, and a guessed key would
mislabel that night with no way to tell afterwards.

The OTEL console format is JavaScript object notation (NOT strict JSON).
Event type is in `body: "claude_code.api_request"`.
Token/cost values are in `attributes` as quoted strings: `input_tokens: "3"`.
Events and report text are mixed on stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass

from brain_v42.db.dsn import resolve_postgres_dsn

try:
    import asyncpg
except ImportError:  # pragma: no cover — asyncpg absent only in minimal envs
    asyncpg = None


@dataclass
class PhaseTelemetry:
    """Aggregated telemetry for one dream phase."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int | None = 0
    cost_usd: float | None = 0.0
    api_calls: int | None = 0
    tool_calls: int = 0
    #: 049 — NULL = « ce rail ne distingue pas le thinking » (claude/codex
    #: aujourd'hui). Seul agy le mesure : 962 thinking pour 1554 output sur le
    #: run du 2026-08-11, ~38 % de tokens comptés nulle part (ticket 76e11c9f).
    thinking_tokens: int | None = None


# Event type is in the body field
_EVENT_TYPE = re.compile(r'body:\s*"claude_code\.(\w+)"')

# Token/cost fields — values can be quoted strings or bare numbers
# Matches: input_tokens: "3"  or  input_tokens: 3  or  cost_usd: "0.086"
_FIELD_VALUE = re.compile(r'(\w+):\s*"?([\d.]+)"?')

# OTEL JS-object blobs start at column 0 with `{` and end at column 0 with `}`.
_OTEL_BLOB = re.compile(r"^\{.*?^\}\s*$", re.DOTALL | re.MULTILINE)


def extract_error_tail(content: str, max_chars: int = 2000) -> str | None:
    """Return the last non-OTEL textual lines from a failed phase log.

    Strips OTEL JS-object blobs (noise), keeps human-readable error lines.
    Returns None if no meaningful content remains.
    """
    if not content:
        return None
    stripped = _OTEL_BLOB.sub("", content)
    lines = [line.rstrip() for line in stripped.splitlines() if line.strip()]
    if not lines:
        return None
    tail = "\n".join(lines)
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


# Inline-code spans (single-line backtick-wrapped text) are how a SYNTH/report
# phase QUOTES a signature string when it DOCUMENTS this very detector (e.g. the
# 2026-06-25 synth report). Claude Code never backtick-wraps its own terminal
# emission, so stripping inline code before matching removes the self-referential
# false positive without risking a false negative. Bounded to a single line so a
# stray lone backtick can't swallow a real signature on a later line.
_INLINE_CODE = re.compile(r"`[^`\n]*`")

# Signatures meaning a phase exited 0 (clean shell exit) but did NOT complete its
# work — a soft/terminal failure the shell's exit code cannot detect. Keep this
# CONSERVATIVE: only the DEFINITIVE post-retry marker. The bare "tool call could
# not be parsed" is the FIRST-attempt message — Claude Code retries and often
# succeeds — so it is NOT terminal and also matches benign report prose; only the
# "(retry also failed)" form is definitive. Never key on heuristics like
# "0 tool_calls" — many nights legitimately no-op.
_TERMINAL_FAILURE_SIGNATURES = ("could not be parsed (retry also failed)",)


def detect_terminal_failure(content: str) -> str | None:
    """Return the matched terminal-failure signature, or None.

    A dream phase can exit 0 yet have failed semantically — most notably when
    the model's tool call could not be parsed AND the automatic retry also
    failed. The shell only sees the exit code, so such a night was recorded as
    'done' and masked a real failure (2026-06-22 audit). Detect the definitive
    post-retry signature so the caller can flip the recorded status to 'fail'.

    Quoted (inline-code) mentions are stripped first: on a 'done' phase the
    scanned blob is the model's own clean report, which legitimately discusses
    this detector and would otherwise self-trip (2026-06-25 synth false
    positive).
    """
    if not content:
        return None
    lowered = _INLINE_CODE.sub("", content).lower()
    for signature in _TERMINAL_FAILURE_SIGNATURES:
        if signature in lowered:
            return signature
    return None


def parse_otel_log(content: str) -> PhaseTelemetry:
    """Parse OTEL console output and aggregate metrics.

    Handles the real Claude Code OTEL console format where:
    - Event type is in `body: "claude_code.api_request"`
    - Values are quoted strings in attributes: `input_tokens: "3"`
    - OTEL events are mixed with report text on stdout
    """
    telemetry = PhaseTelemetry()

    # Split into blocks by top-level `{` at start of line.
    blocks = re.split(r"\n(?=\{)", content)

    for block in blocks:
        type_match = _EVENT_TYPE.search(block)
        if not type_match:
            continue
        event_type = type_match.group(1)

        if event_type == "api_request":
            telemetry.api_calls = (telemetry.api_calls or 0) + 1
            for match in _FIELD_VALUE.finditer(block):
                key, value = match.group(1), match.group(2)
                if key == "input_tokens":
                    telemetry.input_tokens += int(value)
                elif key == "output_tokens":
                    telemetry.output_tokens += int(value)
                elif key == "cache_read_tokens":
                    telemetry.cache_read_tokens += int(value)
                elif key == "cache_creation_tokens":
                    telemetry.cache_creation_tokens = (telemetry.cache_creation_tokens or 0) + int(
                        value
                    )
                elif key == "cost_usd":
                    telemetry.cost_usd = (telemetry.cost_usd or 0.0) + float(value)

        elif event_type == "tool_result":
            telemetry.tool_calls += 1

    return telemetry


async def _insert_dream_run(
    *,
    run_date: str,
    phase: str,
    model: str,
    status: str,
    duration_s: float,
    telemetry: PhaseTelemetry | None,
    project_key: str,
    error_message: str | None = None,
    phase_dry_run: bool = False,
) -> None:
    """Insert a dream_runs row via asyncpg.

    `project_key` is required and has no default. Two CLI entry points share
    this function — `dream_parser` (the explicit Claude rollback) and
    `codex_dream_parser` (the default, live rail) — and a default here would
    silently label another project's night `brain-v42` if either forgot to
    forward its flag.
    """
    from datetime import date as date_type

    # asyncpg needs datetime.date, not str
    run_date_obj = date_type.fromisoformat(run_date)

    # Pas de DSN par défaut ici : voir brain_v42.db.dsn. Le littéral qui vivait
    # à cette ligne était le DSN de production par coïncidence, et sa rotation a
    # vidé dream_runs pendant deux nuits sans qu'aucune phase ne se dise en échec.
    dsn = resolve_postgres_dsn()

    # When telemetry parsing yields nothing, store NULL — not zero. Zero
    # would silently land in cost rollups as a real "0 tokens, 0 cost" row.
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            INSERT INTO dream_runs
                (run_date, phase, model, status, duration_s,
                 input_tokens, output_tokens, cache_read_tokens,
                 cache_creation_tokens, cost_usd, api_calls, tool_calls,
                 error_message, project_key, thinking_tokens, phase_dry_run)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    $14, $15, $16)
            """,
            run_date_obj,
            phase,
            model,
            status,
            duration_s,
            telemetry.input_tokens if telemetry else None,
            telemetry.output_tokens if telemetry else None,
            telemetry.cache_read_tokens if telemetry else None,
            telemetry.cache_creation_tokens if telemetry else None,
            telemetry.cost_usd if telemetry else None,
            telemetry.api_calls if telemetry else None,
            telemetry.tool_calls if telemetry else None,
            error_message,
            project_key,
            # 049 — AVANT phase_dry_run : le drapeau de répétition à blanc
            # reste le DERNIER lié, pin de test_dream_parser_phase_dry_run.
            telemetry.thinking_tokens if telemetry else None,
            phase_dry_run,
        )
    finally:
        await conn.close()


def _str_to_bool(v: str) -> bool:
    """Convert a CLI string value to bool."""
    return v.lower() in ("true", "1", "yes")


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser for dream_parser."""
    parser = argparse.ArgumentParser(description="Parse OTEL log and insert dream_runs row")
    parser.add_argument("otel_file", help="Path to combined stdout+OTEL log file")
    parser.add_argument(
        "--phase",
        required=True,
        help="Phase name (scan/clean/connect/synth/reorg)",
    )
    parser.add_argument("--model", required=True, help="Model used (sonnet/opus)")
    parser.add_argument("--date", required=True, help="Run date (YYYY-MM-DD)")
    parser.add_argument("--status", required=True, help="Phase status (done/timeout/fail)")
    parser.add_argument(
        "--project-key",
        required=True,
        help="Project the phase ran for — required, deliberately without a default",
    )
    parser.add_argument("--duration", type=float, default=0, help="Phase duration in seconds")
    parser.add_argument(
        "--raw-log",
        default=None,
        help="Path to raw combined stdout log — used to extract error tail on fail/timeout",
    )
    parser.add_argument(
        "--phase-dry-run",
        type=_str_to_bool,
        default=False,
        help="Whether this phase ran in dry-run mode (true/false)",
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    # Parse OTEL log
    content = ""
    if os.path.isfile(args.otel_file):
        with open(args.otel_file) as f:
            content = f.read()

    telemetry = parse_otel_log(content)

    # Read the human-readable scan log once (the clean report on a 'done' phase,
    # the full raw snapshot on fail/timeout) — used for both masked-failure
    # detection and error-tail extraction.
    scan_content = ""
    if args.raw_log and os.path.isfile(args.raw_log):
        with open(args.raw_log) as f:
            scan_content = f.read()

    # A phase can exit 0 yet fail semantically (e.g. the model's tool call could
    # not be parsed). Flip 'done' -> 'fail' on a definitive terminal-failure
    # signature so the night is not masked as successful (2026-06-22 audit).
    status = args.status
    if status == "done" and detect_terminal_failure(scan_content or content):
        status = "fail"
        print(f"[dream_parser] {args.phase}: terminal-failure detected — status done→fail")

    # Extract an error tail for any non-success status (including a flipped one).
    error_message: str | None = None
    if status != "done":
        error_message = extract_error_tail(scan_content or content)

    # Insert into DB
    asyncio.run(
        _insert_dream_run(
            run_date=args.date,
            phase=args.phase,
            model=args.model,
            status=status,
            duration_s=args.duration,
            telemetry=telemetry,
            project_key=args.project_key,
            error_message=error_message,
            phase_dry_run=args.phase_dry_run,
        )
    )

    total_tokens = telemetry.input_tokens + telemetry.output_tokens
    print(
        f"[dream_parser] {args.phase}: "
        f"{total_tokens} tokens, ${telemetry.cost_usd:.4f}, "
        f"{telemetry.api_calls} api_calls, {telemetry.tool_calls} tool_calls"
    )


_PROMOTE_REPORT = re.compile(
    r"===\s*PROMOTE\s+REPORT\s*===\s*(\{.*?\})\s*===\s*END\s*===",
    re.DOTALL,
)


def extract_promote_report(content: str) -> dict | None:
    """Parse the `=== PROMOTE REPORT ===` JSON block emitted by PROMOTE.

    Returns None on missing markers or malformed JSON — the caller
    (dream.sh) decides whether the absence is an error (e.g. the phase
    was supposed to run and produce a report) or expected (killswitch).
    """
    m = _PROMOTE_REPORT.search(content)
    if m is None:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


if __name__ == "__main__":
    main()
