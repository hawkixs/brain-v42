"""Normalize ``opencode run --format json`` events and persist one Dream phase run.

Measured on opencode 1.18.30 (2026-09-15). One ``step_finish`` per model
call carries ``part.tokens.{input,output,reasoning,cache.{read,write}}`` and
``part.cost``; ``tokens.input`` EXCLUDES the cache reads, which is already
the split Dream's schema stores. ``cost`` is opencode's own estimate from
models.dev prices -- the console bills DeepSeek at about twice it (learning
f60b35cd) -- kept as measured, never corrected here. A ``tool_use`` part
names an MCP tool ``<server>_<tool>`` and reaches ``state.status``
``completed`` or ``error``. The terminal event is ``{"type": "error",
"error": {"name": ..., "data": {"message": ...}}}``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Iterable
from pathlib import Path

from brain_v42.metrics.dream_parser import PhaseTelemetry, _insert_dream_run, _str_to_bool

BRAIN_TOOL_PREFIX = "brain-v42_"


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


def _part(event: dict[str, object]) -> dict[str, object]:
    part = event.get("part")
    return part if isinstance(part, dict) else {}


def _usage_int(usage: object, key: str) -> int:
    if not isinstance(usage, dict):
        return 0
    value = usage.get(key, 0)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return max(0, int(value))
    return 0


def _step_finish_parts(content: str) -> list[dict[str, object]]:
    """One ``step_finish`` part per id, last version wins: opencode re-emits a
    part on every update, and counting each emission would double every figure."""
    parts: dict[str, dict[str, object]] = {}
    for index, event in enumerate(_events(content)):
        if event.get("type") != "step_finish":
            continue
        part = _part(event)
        part_id = part.get("id")
        parts[part_id if isinstance(part_id, str) else f"#{index}"] = part
    return list(parts.values())


def parse_opencode_jsonl(content: str) -> PhaseTelemetry:
    """Map opencode step usage and Brain tool calls to the historical Dream schema.

    ``api_calls`` counts the ``step_finish`` parts: one per model call.
    ``thinking_tokens``, ``cache_creation_tokens`` and ``cost_usd`` are
    measured only when a step reports ``reasoning``, ``cache.write`` and
    ``cost`` respectively; a stream that never does stays ``NULL`` ("not
    measured"), never 0 -- "measured as free" is the figure this column must
    never carry. ``tool_calls`` counts COMPLETED Brain calls, the agy
    convention (codex counts every attempted one): a failed call did not
    touch the corpus.
    """
    telemetry = PhaseTelemetry(cache_creation_tokens=None, cost_usd=None, api_calls=0)
    measured = False

    for part in _step_finish_parts(content):
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            raise ValueError("step_finish event has no tokens object")
        cache = tokens.get("cache")
        telemetry.input_tokens += _usage_int(tokens, "input")
        telemetry.output_tokens += _usage_int(tokens, "output")
        telemetry.cache_read_tokens += _usage_int(cache, "read")
        if isinstance(cache, dict) and "write" in cache:
            telemetry.cache_creation_tokens = (telemetry.cache_creation_tokens or 0) + _usage_int(
                cache, "write"
            )
        if "reasoning" in tokens:
            telemetry.thinking_tokens = (telemetry.thinking_tokens or 0) + _usage_int(
                tokens, "reasoning"
            )
        cost = part.get("cost")
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            telemetry.cost_usd = (telemetry.cost_usd or 0.0) + float(cost)
        telemetry.api_calls = (telemetry.api_calls or 0) + 1
        measured = True

    for event in _events(content):
        event_type = event.get("type")
        part = _part(event)
        if event_type == "tool_use":
            tool = part.get("tool")
            state = part.get("state")
            if (
                isinstance(tool, str)
                and tool.startswith(BRAIN_TOOL_PREFIX)
                and isinstance(state, dict)
                and state.get("status") == "completed"
            ):
                telemetry.tool_calls += 1

    if not measured:
        raise ValueError("event stream has no step_finish usage")
    return telemetry


def _terminal_error(content: str) -> str | None:
    """opencode's own terminal ``error`` event, if there is one. A failed tool
    call is not terminal: the model may retry it."""
    messages: list[str] = []
    for event in _events(content):
        if event.get("type") != "error":
            continue
        error = event.get("error")
        name = error.get("name") if isinstance(error, dict) else None
        data = error.get("data") if isinstance(error, dict) else None
        message = data.get("message") if isinstance(data, dict) else None
        label = name if isinstance(name, str) and name.strip() else "error"
        text = message.strip() if isinstance(message, str) and message.strip() else ""
        messages.append(f"{label}: {text}" if text else label)
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
    parser = argparse.ArgumentParser(description="Persist Dream telemetry from opencode JSONL")
    parser.add_argument("events_file", help="opencode --format json event stream")
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
    parser.add_argument("--raw-log", default=None, help="opencode stderr/error snapshot")
    parser.add_argument("--report-log", default=None, help="opencode final report")
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
        telemetry = parse_opencode_jsonl(events_content)
    except ValueError as exc:
        telemetry = None
        telemetry_error = str(exc)

    status = args.status
    terminal_error = _terminal_error(events_content)
    if status == "done" and (terminal_error or telemetry_error):
        status = "fail"
        print(f"[opencode_dream_parser] {args.phase}: terminal event detected — status done→fail")

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
        print(f"[opencode_dream_parser] {args.phase}: telemetry unavailable, cost=n/a")
    else:
        total_tokens = telemetry.input_tokens + telemetry.output_tokens
        cost = f"${telemetry.cost_usd:.4f}" if telemetry.cost_usd is not None else "n/a"
        print(
            f"[opencode_dream_parser] {args.phase}: {total_tokens} fresh tokens, "
            f"{telemetry.cache_read_tokens} cached, cost={cost}, "
            f"{telemetry.tool_calls} tool calls"
        )


if __name__ == "__main__":
    main()
