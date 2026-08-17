"""Normalize ``codex exec --json`` events and persist one Dream phase run."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Iterable
from pathlib import Path

from brain_v42.metrics.dream_parser import PhaseTelemetry, _insert_dream_run, _str_to_bool


def _events(content: str) -> Iterable[dict[str, object]]:
    """Yield JSON-object events, ignoring blank, diagnostic, and future lines."""
    for raw_line in content.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _usage_int(usage: object, key: str) -> int:
    if not isinstance(usage, dict):
        return 0
    value = usage.get(key, 0)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return max(0, int(value))
    return 0


def parse_codex_jsonl(content: str) -> PhaseTelemetry:
    """Map Codex JSONL usage and MCP calls to the historical Dream schema.

    Codex reports total input tokens with the cached portion included.  Dream's
    schema stores fresh input and cache reads separately, so cached input is
    subtracted before persistence.  ChatGPT-authenticated runs do not expose a
    trustworthy dollar cost, cache-creation count, or underlying API-call count;
    those fields remain ``NULL``.
    """
    telemetry = PhaseTelemetry(
        cache_creation_tokens=None,
        cost_usd=None,
        api_calls=None,
    )
    completed = False

    for event in _events(content):
        event_type = event.get("type")
        if event_type == "turn.completed":
            usage = event.get("usage")
            if not isinstance(usage, dict):
                raise ValueError("turn.completed event has no usage object")
            for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                value = usage.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(f"turn.completed usage.{key} is missing or invalid")
            total_input = _usage_int(usage, "input_tokens")
            cached_input = min(total_input, _usage_int(usage, "cached_input_tokens"))
            if total_input <= 0 or _usage_int(usage, "output_tokens") <= 0:
                raise ValueError("turn.completed token usage must be positive")
            if _usage_int(usage, "cached_input_tokens") > total_input:
                raise ValueError("cached input exceeds total input")
            telemetry.input_tokens += total_input - cached_input
            telemetry.cache_read_tokens += cached_input
            telemetry.output_tokens += _usage_int(usage, "output_tokens")
            completed = True
        elif event_type == "item.completed":
            item = event.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "mcp_tool_call"
                and item.get("server") == "brain-v42"
            ):
                telemetry.tool_calls += 1

    if not completed:
        raise ValueError("event stream has no valid turn.completed usage")
    return telemetry


def _terminal_error(content: str) -> str | None:
    messages: list[str] = []
    for event in _events(content):
        if event.get("type") not in {"turn.failed", "error"}:
            continue
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message")
        else:
            message = event.get("message") or error
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
    return messages[-1] if messages else None


def _error_tail(*contents: str, max_chars: int = 2000) -> str | None:
    text = "\n".join(content.strip() for content in contents if content.strip()).strip()
    if not text:
        return None
    return text[-max_chars:]


def _read(path: str | None) -> str:
    if not path or not os.path.isfile(path):
        return ""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persist Dream telemetry from Codex JSONL")
    parser.add_argument("events_file", help="Codex --json event stream")
    parser.add_argument("--phase", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument(
        "--project-key",
        required=True,
        help="Project the phase ran for — required, deliberately without a default",
    )
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--raw-log", default=None, help="Codex stderr/error snapshot")
    parser.add_argument("--report-log", default=None, help="Codex final report")
    parser.add_argument("--phase-dry-run", type=_str_to_bool, default=False)
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    events_content = _read(args.events_file)
    raw_content = _read(args.raw_log)
    report_content = _read(args.report_log)
    telemetry: PhaseTelemetry | None
    telemetry_error: str | None = None
    try:
        telemetry = parse_codex_jsonl(events_content)
    except ValueError as exc:
        telemetry = None
        telemetry_error = str(exc)

    status = args.status
    terminal_error = _terminal_error(events_content)
    if status == "done" and (terminal_error or telemetry_error):
        status = "fail"
        print(f"[codex_dream_parser] {args.phase}: terminal event detected — status done→fail")

    error_message = None
    if status != "done":
        error_message = _error_tail(
            raw_content,
            report_content,
            terminal_error or telemetry_error or "",
        )

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

    if telemetry is None:
        print(f"[codex_dream_parser] {args.phase}: telemetry unavailable, cost=n/a")
    else:
        total_tokens = telemetry.input_tokens + telemetry.output_tokens
        print(
            f"[codex_dream_parser] {args.phase}: {total_tokens} fresh tokens, "
            f"{telemetry.cache_read_tokens} cached, cost=n/a, "
            f"{telemetry.tool_calls} tool calls"
        )


if __name__ == "__main__":
    main()
