"""Normalize the ``agy --output-format stream-json`` stream and persist a phase.

Third parser of the dream, after ``dream_parser`` (claude, OTEL) and
``codex_dream_parser`` (codex, JSONL). Without it, a phase played by agy was
played but NOT MEASURED — and that hole dictated, for a few hours, an absurd
chain order, claude placed before agy to preserve the ``dream_runs`` rows, thus
putting the subscription we wanted to spare on the front line.

THE CACHE CONVENTION IS THE OPPOSITE OF CODEX'S. Measured on a real run:

    input_tokens      = 20137
    cache_read_tokens = 56950      <-- LARGER than input_tokens
    total_tokens      = 21691      = input + output, the cache is NOT in it

With codex, ``cached_input_tokens`` is a SUBSET of ``input_tokens``, and its
parser computes ``fresh = input - cached``. With agy the two counters are
INDEPENDENT: ``input_tokens`` is already the fresh one. Copying codex's formula
would produce a NEGATIVE number here, and a token column is not watched closely
enough for anyone to notice for weeks.

THE ``result`` EVENT IS THE AUTHORITATIVE AGGREGATE. Also measured: the sum of
the ``step_update`` usages equals exactly that of the ``result``. Adding both
would double every counter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Iterable
from pathlib import Path

from brain_v42.metrics.dream_parser import PhaseTelemetry, _insert_dream_run, _str_to_bool


def _events(content: str) -> Iterable[dict[str, object]]:
    """Yield the JSON events, ignoring blank lines, noise and unknown shapes."""
    for raw_line in content.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _counter(usage: dict[str, object], key: str) -> int:
    """Read a counter, refusing anything that cannot be a count."""
    value = usage.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"usage.{key} n'est pas un nombre")
    if value < 0:
        raise ValueError(f"usage.{key} est négatif")
    return int(value)


def parse_agy_stream(content: str) -> PhaseTelemetry:
    """Map agy's stream-json output onto the dream's historical schema."""
    telemetry = PhaseTelemetry(
        # Backed by a subscription: neither cost, nor API call count, nor
        # cache-creation tokens are exposed. Writing 0 would assert "free" and
        # "no call" — two lies. NULL says "not measured".
        cache_creation_tokens=None,
        cost_usd=None,
        api_calls=None,
    )
    result_usage: dict[str, object] | None = None

    for event in _events(content):
        kind = event.get("event")
        if kind == "result":
            result = event.get("result")
            if not isinstance(result, dict):
                raise ValueError("événement result sans objet result")
            usage = result.get("usage")
            if not isinstance(usage, dict):
                raise ValueError("événement result sans objet usage")
            result_usage = usage
        elif kind == "step_update":
            step = event.get("step_update")
            if not isinstance(step, dict):
                continue
            # Only COMPLETED MCP calls count. A run_command refused by the
            # guard does produce a tool step: counting it would suggest a phase
            # touched the brain when it was prevented from doing so.
            if (
                step.get("step_type") == "tool"
                and step.get("state") == "DONE"
                and step.get("tool_name") == "call_mcp_tool"
            ):
                telemetry.tool_calls += 1

    if result_usage is None:
        raise ValueError("le flux ne porte aucun événement result exploitable")

    # Pas de soustraction, contrairement à codex — voir l'en-tête du module.
    telemetry.input_tokens = _counter(result_usage, "input_tokens")
    telemetry.output_tokens = _counter(result_usage, "output_tokens")
    telemetry.cache_read_tokens = _counter(result_usage, "cache_read_tokens")
    # 049: measured SEPARATELY, never added to output_tokens — the rails that
    # do not distinguish thinking would leave an incomparable sum. Absent from
    # the stream = NULL ("not measured"), not 0 ("measured as nothing").
    if "thinking_tokens" in result_usage:
        telemetry.thinking_tokens = _counter(result_usage, "thinking_tokens")
    return telemetry


#: Exact prefix agy writes when it is a TOOL CALL that failed, and not agy
#: itself. It is the only family of failure a retry can recover from.
_TOOL_ERROR_PREFIX = "Error in MCP tool execution"


def _failure_messages(content: str) -> list[str]:
    """Every failure message carried by the stream, in stream order."""
    messages: list[str] = []
    for event in _events(content):
        result = event.get("result")
        if isinstance(result, dict) and result.get("status") not in (None, "SUCCESS"):
            message = result.get("error") or result.get("status")
            if isinstance(message, str) and message.strip():
                messages.append(message.strip())
        if event.get("event") == "error":
            message = event.get("message")
            if isinstance(message, str) and message.strip():
                messages.append(message.strip())
    return messages


def _terminal_error(content: str) -> str | None:
    """The last failure message carried by the stream, if there is one."""
    messages = _failure_messages(content)
    return messages[-1] if messages else None


def _last_failed_call_was_retried(content: str) -> bool:
    """Was the last failed MCP call REPLAYED successfully?

    The tool name lives in ``tool_info.parameters.ToolName``: ``tool_name`` is
    always ``call_mcp_tool`` and distinguishes nothing. Requiring the SAME tool
    is not zeal — measured on the night of 2026-08-12, settling for "some call
    succeeded afterwards" also cleared refondrre/connect, whose lost
    ``brain_assign_domain`` was never retried.

    Without a step index, "afterwards" has no meaning: we answer no,
    fail-closed.
    """
    last_failed_index = -1
    last_failed_tool: str | None = None
    successes: list[tuple[int, str | None]] = []

    for event in _events(content):
        if event.get("event") != "step_update":
            continue
        step = event.get("step_update")
        if not isinstance(step, dict):
            continue
        if step.get("step_type") != "tool" or step.get("tool_name") != "call_mcp_tool":
            continue
        index = step.get("step_index")
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        info = step.get("tool_info")
        parameters = info.get("parameters") if isinstance(info, dict) else None
        tool = parameters.get("ToolName") if isinstance(parameters, dict) else None
        tool_name = tool if isinstance(tool, str) else None

        if step.get("state") == "ERROR" and index > last_failed_index:
            last_failed_index, last_failed_tool = index, tool_name
        elif step.get("state") == "DONE":
            successes.append((index, tool_name))

    if last_failed_index < 0 or last_failed_tool is None:
        return False
    return any(index > last_failed_index and tool == last_failed_tool for index, tool in successes)


def _unrecovered_error(content: str) -> str | None:
    """The failure message that actually carried off the phase, if there is one.

    agy LATCHES ``result.status=ERROR`` as soon as one tool call has failed,
    even when the agent retried and succeeded afterwards. Measured on the night
    of 2026-08-12: 19 phases out of 21 were counted ``fail`` while producing 27
    durable artifacts, all verified in the database. A red that stands for
    nothing costs as much as a green that stands for nothing — it sends people
    looking for a failure that is not there.

    Only a TOOL failure is recoverable. A failure of agy itself stays terminal,
    and that is why we fall back on the next message rather than returning
    ``None``: removing the tool failure must never mask what stood behind it.
    """
    messages = _failure_messages(content)
    if not messages:
        return None
    if _last_failed_call_was_retried(content):
        messages = [message for message in messages if not message.startswith(_TOOL_ERROR_PREFIX)]
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
    parser = argparse.ArgumentParser(description="Persister la télémétrie d'une phase agy")
    parser.add_argument("events_file", help="flux stream-json d'agy")
    parser.add_argument("--phase", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--raw-log", default=None, help="stderr de la phase")
    parser.add_argument("--report-log", default=None, help="rapport final de la phase")
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
        telemetry = parse_agy_stream(events_content)
    except ValueError as exc:
        telemetry = None
        telemetry_error = str(exc)

    status = args.status
    terminal_error = _unrecovered_error(events_content)
    if terminal_error is None and _terminal_error(events_content) is not None:
        # Never silently: the phase did fail somewhere, and the operator must
        # be able to count these recoveries without re-reading the streams by
        # hand.
        print(f"[agy_dream_parser] {args.phase}: échec d'outil rejoué avec succès — statut tenu")
    if status == "done" and (terminal_error or telemetry_error):
        status = "fail"
        print(f"[agy_dream_parser] {args.phase}: événement terminal détecté — status done→fail")

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
        print(f"[agy_dream_parser] {args.phase}: télémétrie indisponible, cost=n/a")
    else:
        total_tokens = telemetry.input_tokens + telemetry.output_tokens
        print(
            f"[agy_dream_parser] {args.phase}: {total_tokens} fresh tokens, "
            f"{telemetry.cache_read_tokens} cached, cost=n/a, "
            f"{telemetry.tool_calls} tool calls"
        )


if __name__ == "__main__":
    main()
