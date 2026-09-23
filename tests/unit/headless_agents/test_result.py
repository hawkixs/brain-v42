"""``RunResult.to_dict``: the schema-1 contract of ``result.json`` and ``ha run --json``."""

from __future__ import annotations

import json
from pathlib import Path

from headless_agents.result import RESULT_SCHEMA_VERSION, RunResult, TokenUsage

SCHEMA_1_KEYS = [
    "schema",
    "run_id",
    "provider",
    "model",
    "model_reported",
    "exit_code",
    "text",
    "tokens",
    "cost_usd",
    "duration_seconds",
    "tool_call_completed",
    "context",
    "workspace",
    "branch",
    "logs",
]


def _result(**overrides: object) -> RunResult:
    fields: dict[str, object] = {
        "exit_code": 0,
        "provider": "codex",
        "model": "m",
        "report_path": Path("/runs/r1/report.log"),
        "events_log": Path("/runs/r1/events.jsonl"),
        "tokens": None,
        "duration_seconds": 1.5,
        "tool_call_completed": False,
    }
    fields.update(overrides)
    return RunResult(**fields)  # type: ignore[arg-type]


class TestNewFields:
    def test_default_to_none_so_existing_callers_are_untouched(self) -> None:
        result = _result()
        assert result.text is None
        assert result.run_id is None
        assert result.stderr_log is None
        assert result.raw_log is None


class TestToDict:
    def test_schema_1_carries_exactly_the_documented_keys_in_order(self) -> None:
        assert RESULT_SCHEMA_VERSION == 1
        data = _result().to_dict()
        assert list(data) == SCHEMA_1_KEYS
        assert data["schema"] == 1

    def test_is_json_safe_and_round_trips_with_non_ascii_text(self) -> None:
        result = _result(
            text="déjà vu ✓",
            run_id="r1",
            stderr_log=Path("/runs/r1/stderr.log"),
            tokens=TokenUsage(input=10, output=2, cached=4),
            cost_usd=0.01,
            model_reported="m-2026",
        )
        data = result.to_dict()
        assert json.loads(json.dumps(data, ensure_ascii=False)) == data
        assert data["text"] == "déjà vu ✓"

    def test_logs_are_strings_and_absent_logs_are_null(self) -> None:
        data = _result(stderr_log=Path("/runs/r1/stderr.log")).to_dict()
        assert data["logs"] == {
            "report": "/runs/r1/report.log",
            "events": "/runs/r1/events.jsonl",
            "stderr": "/runs/r1/stderr.log",
            "raw": None,
        }

    def test_null_keeps_its_meaning_not_measured_never_zero(self) -> None:
        measured = _result(tokens=TokenUsage(input=10)).to_dict()
        assert measured["tokens"] == {
            "input": 10,
            "output": None,
            "fresh": None,
            "cached": None,
            "thinking": None,
        }
        assert measured["cost_usd"] is None
        assert _result().to_dict()["tokens"] is None

    def test_fields_the_runtime_cannot_fill_yet_are_present_and_null(self) -> None:
        data = _result().to_dict()
        assert data["context"] is None
        assert data["workspace"] is None
        assert data["branch"] is None


def test_to_dict_carries_context_and_workspace() -> None:
    result = RunResult(
        exit_code=0,
        provider="codex",
        model="m",
        report_path=None,
        events_log=None,
        tokens=None,
        duration_seconds=1.0,
        tool_call_completed=False,
        context=(
            {
                "path": "/u.md",
                "scope": "user",
                "size_bytes": 1,
                "sha256": "a" * 64,
            },
        ),
        workspace={"path": "/ws", "write": False, "shell": False},
    )
    data = result.to_dict()
    assert data["context"] == [
        {
            "path": "/u.md",
            "scope": "user",
            "size_bytes": 1,
            "sha256": "a" * 64,
        }
    ]
    assert data["workspace"] == {"path": "/ws", "write": False, "shell": False}


def test_to_dict_keeps_null_without_context() -> None:
    result = RunResult(
        exit_code=0,
        provider="codex",
        model="m",
        report_path=None,
        events_log=None,
        tokens=None,
        duration_seconds=1.0,
        tool_call_completed=False,
    )
    assert (result.to_dict()["context"], result.to_dict()["workspace"]) == (None, None)
