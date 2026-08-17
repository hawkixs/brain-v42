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
