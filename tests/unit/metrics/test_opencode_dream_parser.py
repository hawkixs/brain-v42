"""Unit contract for ``opencode run --format json`` Dream telemetry normalization.

Every event shape here was measured on opencode 1.18.30 (2026-09-15): the
``step_finish`` part with ``tokens.{input,output,reasoning,cache.{read,write}}``
and ``cost``, the ``tool_use`` part with ``tool`` and ``state.status``, and
the terminal ``error`` event with ``error.{name,data.message}``.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

from brain_v42.metrics.dream_parser import PhaseTelemetry

REPO_ROOT = Path(__file__).resolve().parents[3]
PARSER_PATH = REPO_ROOT / "src" / "brain_v42" / "metrics" / "opencode_dream_parser.py"


def _parser() -> ModuleType:
    assert PARSER_PATH.is_file(), (
        "opencode Dream telemetry parser is missing: expected "
        "src/brain_v42/metrics/opencode_dream_parser.py"
    )
    return importlib.import_module("brain_v42.metrics.opencode_dream_parser")


def _jsonl(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _step_finish(
    *,
    input: int,
    output: int,
    cache_read: int,
    cache_write: int = 0,
    reasoning: int | None = 0,
    cost: float = 0.0,
) -> dict[str, object]:
    tokens: dict[str, object] = {
        "total": input + output + cache_read + (reasoning or 0),
        "input": input,
        "output": output,
        "cache": {"read": cache_read, "write": cache_write},
    }
    if reasoning is not None:
        tokens["reasoning"] = reasoning
    return {
        "type": "step_finish",
        "sessionID": "ses_1",
        "part": {"type": "step-finish", "reason": "stop", "tokens": tokens, "cost": cost},
    }


def _tool_use(tool: str, status: str = "completed") -> dict[str, object]:
    return {
        "type": "tool_use",
        "sessionID": "ses_1",
        "part": {"type": "tool", "tool": tool, "state": {"status": status, "input": {}}},
    }


def test_parse_opencode_jsonl_normalizes_step_usage_to_phase_telemetry() -> None:
    parser = _parser()
    content = _jsonl(
        {"type": "step_start", "part": {"type": "step-start"}},
        _step_finish(input=2002, output=143, cache_read=1728, reasoning=0, cost=0.0003251584),
        _step_finish(input=263, output=57, cache_read=3712, reasoning=0, cost=6.31736e-05),
    )

    telemetry = parser.parse_opencode_jsonl(content)

    assert telemetry == PhaseTelemetry(
        input_tokens=2265,
        output_tokens=200,
        cache_read_tokens=5440,
        cache_creation_tokens=0,
        cost_usd=pytest.approx(0.000388332),  # type: ignore[arg-type]
        api_calls=2,
        tool_calls=0,
        thinking_tokens=0,
    )


def test_parse_opencode_jsonl_counts_completed_brain_calls_only() -> None:
    parser = _parser()
    content = _jsonl(
        _tool_use("brain-v42_brain_list"),
        _tool_use("brain-v42_brain_get", status="error"),
        _tool_use("other_search"),
        _tool_use("brain-v42_brain_search"),
        _step_finish(input=10, output=2, cache_read=0),
    )
    assert parser.parse_opencode_jsonl(content).tool_calls == 2


def test_reasoning_is_measured_only_when_the_stream_reports_it() -> None:
    parser = _parser()
    with_reasoning = _jsonl(
        _step_finish(input=10, output=8, cache_read=0, reasoning=5),
        _step_finish(input=10, output=8, cache_read=0, reasoning=3),
    )
    assert parser.parse_opencode_jsonl(with_reasoning).thinking_tokens == 8
    without = _jsonl(_step_finish(input=10, output=8, cache_read=0, reasoning=None))
    assert parser.parse_opencode_jsonl(without).thinking_tokens is None


def test_parse_opencode_jsonl_ignores_blank_malformed_and_unknown_lines() -> None:
    parser = _parser()
    content = "\n".join(
        [
            "",
            "not json",
            json.dumps({"type": "text", "part": {"text": "hello"}}),
            json.dumps(["a", "list"]),
            json.dumps(_step_finish(input=5, output=1, cache_read=4, cost=0.1)),
        ]
    )
    telemetry = parser.parse_opencode_jsonl(content)
    assert (telemetry.input_tokens, telemetry.cache_read_tokens) == (5, 4)
    assert telemetry.api_calls == 1


def test_parse_opencode_jsonl_rejects_a_stream_without_usage() -> None:
    parser = _parser()
    with pytest.raises(ValueError, match="step_finish"):
        parser.parse_opencode_jsonl(_jsonl({"type": "text", "part": {"text": "x"}}))
    with pytest.raises(ValueError, match="step_finish"):
        parser.parse_opencode_jsonl(
            _jsonl({"type": "step_finish", "part": {"reason": "stop", "tokens": "?"}})
        )


def test_the_measured_error_event_is_the_terminal_error() -> None:
    parser = _parser()
    content = _jsonl(
        {
            "type": "error",
            "sessionID": "ses_1",
            "error": {
                "name": "UnknownError",
                "data": {"message": "Unexpected server error. Check server logs.", "ref": "e"},
            },
        }
    )
    error = parser._terminal_error(content)
    assert error is not None
    assert "UnknownError" in error and "Unexpected server error" in error
    assert parser._terminal_error(_jsonl(_step_finish(input=1, output=1, cache_read=0))) is None


def test_a_failed_tool_call_alone_is_not_terminal() -> None:
    """The model may retry a failed call; only opencode's own error is terminal."""
    parser = _parser()
    content = _jsonl(
        _tool_use("brain-v42_brain_list", status="error"),
        _step_finish(input=1, output=1, cache_read=0),
    )
    assert parser._terminal_error(content) is None


def test_error_tail_keeps_the_terminal_cause_when_the_report_is_long() -> None:
    parser = _parser()
    tail = parser._error_tail("x" * 5000, "cause", max_chars=200)
    assert tail is not None and tail.endswith("cause") and len(tail) == 200
    assert parser._error_tail("", "  ") is None


def test_the_cli_reports_the_measured_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = _parser()
    events = tmp_path / "events.jsonl"
    events.write_text(
        _jsonl(
            _tool_use("brain-v42_brain_list"),
            _step_finish(input=100, output=20, cache_read=400, reasoning=7, cost=0.0123),
        ),
        encoding="utf-8",
    )
    inserted: list[dict[str, object]] = []

    async def fake_insert(**kwargs: object) -> None:
        inserted.append(kwargs)

    monkeypatch.setattr(parser, "_insert_dream_run", fake_insert)
    monkeypatch.setattr(
        "sys.argv",
        [
            "opencode_dream_parser",
            str(events),
            "--phase",
            "scan",
            "--model",
            "opencode-go/glm-5.3-flash",
            "--date",
            "2026-09-15",
            "--status",
            "done",
            "--duration",
            "12",
            "--project-key",
            "brain-v42",
            "--phase-dry-run",
            "false",
        ],
    )
    parser.main()
    out = capsys.readouterr().out
    assert (
        "[opencode_dream_parser] scan: 120 fresh tokens, 400 cached, cost=$0.0123, 1 tool calls"
        in out
    )
    row = inserted[0]
    assert row["status"] == "done" and row["project_key"] == "brain-v42"
    telemetry = row["telemetry"]
    assert isinstance(telemetry, PhaseTelemetry)
    assert telemetry.thinking_tokens == 7 and telemetry.cost_usd == pytest.approx(0.0123)


def test_the_cli_turns_a_done_status_into_fail_on_a_terminal_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = _parser()
    events = tmp_path / "events.jsonl"
    events.write_text(
        _jsonl({"type": "error", "error": {"name": "UnknownError", "data": {"message": "boom"}}}),
        encoding="utf-8",
    )
    inserted: list[dict[str, object]] = []

    async def fake_insert(**kwargs: object) -> None:
        inserted.append(kwargs)

    monkeypatch.setattr(parser, "_insert_dream_run", fake_insert)
    monkeypatch.setattr(
        "sys.argv",
        [
            "opencode_dream_parser",
            str(events),
            "--phase",
            "reorg",
            "--model",
            "m",
            "--date",
            "2026-09-15",
            "--status",
            "done",
            "--project-key",
            "red",
        ],
    )
    parser.main()
    out = capsys.readouterr().out
    assert "status done→fail" in out and "telemetry unavailable" in out
    assert inserted[0]["status"] == "fail"
    assert inserted[0]["telemetry"] is None
    assert "boom" in str(inserted[0]["error_message"])
