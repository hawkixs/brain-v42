"""Unit contract for Codex ``exec --json`` Dream telemetry normalization."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

from brain_v42.metrics.dream_parser import PhaseTelemetry

REPO_ROOT = Path(__file__).resolve().parents[3]
PARSER_PATH = REPO_ROOT / "src" / "brain_v42" / "metrics" / "codex_dream_parser.py"


def _parser() -> ModuleType:
    assert PARSER_PATH.is_file(), (
        "Codex Dream telemetry parser is missing: expected "
        "src/brain_v42/metrics/codex_dream_parser.py"
    )
    return importlib.import_module("brain_v42.metrics.codex_dream_parser")


def _jsonl(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def test_parse_codex_jsonl_normalizes_turn_usage_to_phase_telemetry() -> None:
    parser = _parser()
    content = _jsonl(
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1_200,
                "cached_input_tokens": 900,
                "output_tokens": 350,
            },
        },
    )

    telemetry = parser.parse_codex_jsonl(content)

    assert telemetry == PhaseTelemetry(
        input_tokens=300,
        output_tokens=350,
        cache_read_tokens=900,
        cache_creation_tokens=None,
        cost_usd=None,
        api_calls=None,
        tool_calls=0,
    )


def test_parse_codex_jsonl_counts_completed_mcp_calls_once() -> None:
    parser = _parser()
    tool_item = {
        "id": "item-1",
        "type": "mcp_tool_call",
        "server": "brain-v42",
        "tool": "brain_decay_status",
        "arguments": {},
    }
    content = _jsonl(
        {"type": "item.started", "item": tool_item},
        {"type": "item.completed", "item": {**tool_item, "result": {"content": []}}},
        {
            "type": "item.completed",
            "item": {"id": "item-2", "type": "agent_message", "text": "Scan complete"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2},
        },
    )

    assert parser.parse_codex_jsonl(content).tool_calls == 1


def test_parse_codex_jsonl_ignores_mcp_calls_from_other_servers() -> None:
    parser = _parser()
    content = _jsonl(
        {
            "type": "item.completed",
            "item": {
                "id": "item-1",
                "type": "mcp_tool_call",
                "server": "codex",
                "tool": "list_mcp_resources",
            },
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2},
        },
    )

    assert parser.parse_codex_jsonl(content).tool_calls == 0


def test_parse_codex_jsonl_ignores_blank_malformed_and_unknown_lines() -> None:
    parser = _parser()
    content = "\n".join(
        (
            "",
            "warning: non-JSON stderr must not crash telemetry persistence",
            json.dumps({"type": "future.event", "payload": {"value": 1}}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 0,
                        "output_tokens": 2,
                    },
                }
            ),
        )
    )

    assert parser.parse_codex_jsonl(content) == PhaseTelemetry(
        input_tokens=10,
        output_tokens=2,
        cache_creation_tokens=None,
        cost_usd=None,
        api_calls=None,
    )


def test_parse_codex_jsonl_never_reports_api_dollar_cost_for_chatgpt_auth() -> None:
    parser = _parser()
    content = _jsonl(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 2,
                "cost_usd": 99.0,
            },
        }
    )

    assert parser.parse_codex_jsonl(content).cost_usd is None


@pytest.mark.parametrize(
    "event",
    [
        {"type": "turn.completed"},
        {"type": "turn.completed", "usage": {}},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 11, "output_tokens": 2},
        },
    ],
)
def test_parse_codex_jsonl_rejects_missing_or_invalid_usage(event: dict[str, object]) -> None:
    parser = _parser()

    with pytest.raises(ValueError):
        parser.parse_codex_jsonl(_jsonl(event))


def test_error_tail_keeps_the_terminal_cause_when_the_report_is_long() -> None:
    parser = _parser()

    error = parser._error_tail("stderr", "x" * 4_000, "terminal cause")

    assert error is not None
    assert error.endswith("terminal cause")


#: The usage object of a REAL codex turn, copied from
#: `logs/dream/2026-09-02_red-shrik_reorg.events.jsonl` — the live rail, the night
#: of 2026-09-02. Not a fixture invented to match the parser: the ticket said the
#: field was "NOT verified", and this is the verification.
_REAL_TURN_USAGE: dict[str, object] = {
    "input_tokens": 235_532,
    "cached_input_tokens": 199_936,
    "cache_write_input_tokens": 0,
    "output_tokens": 3_938,
    "reasoning_output_tokens": 1_929,
}


def test_the_live_codex_rail_does_carry_reasoning_tokens() -> None:
    """Ticket 42b05302, settled by measurement rather than from memory.

    `PhaseTelemetry.thinking_tokens` carried a comment saying the codex OTEL format
    never distinguishes reasoning tokens from output tokens. Measured on a real
    stream, that is false: `turn.completed.usage` carries
    `reasoning_output_tokens`, 1929 of 3938 output tokens — 49 % counted nowhere,
    on the very rail whose order against agy was settled on a compared cost.
    """
    parser = _parser()

    telemetry = parser.parse_codex_jsonl(
        _jsonl({"type": "turn.completed", "usage": _REAL_TURN_USAGE})
    )

    assert telemetry.thinking_tokens == 1_929


def test_codex_thinking_tokens_are_never_summed_into_output() -> None:
    """The same rule as the agy rail: measured SEPARATELY, never added.

    `reasoning_output_tokens` is a SUBSET of `output_tokens` on this rail — 1929
    of 3938 on the measured turn. Adding it would double-count, and would make the
    two rails incomparable in the direction the 049 column exists to fix.
    """
    parser = _parser()

    telemetry = parser.parse_codex_jsonl(
        _jsonl({"type": "turn.completed", "usage": _REAL_TURN_USAGE})
    )

    assert telemetry.output_tokens == 3_938, "never inflated by the reasoning tokens"


def test_a_codex_turn_without_reasoning_stays_null_not_zero() -> None:
    """Absent from the stream = "not measured" (NULL), never "measured as nothing".

    A ChatGPT-authenticated run, or an older codex, may not report the field. Zero
    would claim a turn did no reasoning; NULL says nobody counted.
    """
    parser = _parser()
    usage = {
        key: value for key, value in _REAL_TURN_USAGE.items() if key != "reasoning_output_tokens"
    }

    telemetry = parser.parse_codex_jsonl(_jsonl({"type": "turn.completed", "usage": usage}))

    assert telemetry.thinking_tokens is None


def test_reasoning_tokens_accumulate_across_turns() -> None:
    """A phase is several turns; the column is the phase's total, like the others.

    Without this, a multi-turn phase would report only its last turn and read as a
    cheap one — the same under-count the ticket measured, one layer along.
    """
    parser = _parser()
    turn = {"type": "turn.completed", "usage": _REAL_TURN_USAGE}

    telemetry = parser.parse_codex_jsonl(_jsonl(turn, turn))

    assert telemetry.thinking_tokens == 2 * 1_929
    assert telemetry.output_tokens == 2 * 3_938
