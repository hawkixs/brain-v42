"""run.json, the run report (spec 0.5.0 §3.10): its key set is pinned here."""

from __future__ import annotations

import json
from pathlib import Path

from headless_agents.report import (
    RUN_JSON,
    RUN_KEYS,
    STEP_KEYS,
    new_report,
    step_dir_name,
    step_entry,
    with_step,
    write_report,
)
from headless_agents.result import RunResult, TokenUsage


def _result(**overrides: object) -> RunResult:
    fields: dict[str, object] = {
        "exit_code": 0,
        "provider": "codex",
        "model": "m",
        "report_path": None,
        "events_log": None,
        "tokens": TokenUsage(input=10, output=2),
        "duration_seconds": 1.5,
        "tool_call_completed": False,
        "cost_usd": 0.25,
    }
    fields.update(overrides)
    return RunResult(**fields)  # type: ignore[arg-type]


def _report() -> dict[str, object]:
    return new_report(
        run_id="20260925T000000-aaaaaaaa",
        target={"kind": "role", "name": "codex"},
        repository=Path("/repo"),
        pid=42,
        started_at="2026-09-25T00:00:00Z",
    )


def test_the_run_key_set_is_the_spec_one() -> None:
    assert tuple(_report()) == RUN_KEYS
    assert RUN_KEYS == (
        "schema", "run_id", "target", "status", "exit_code", "verdict", "text",
        "repository", "base", "head", "branch", "lineage", "continues", "findings_from",
        "implement_providers", "commits", "failure_reason", "vendor_check", "cleanup", "pid",
        "started_at", "duration_seconds", "cost_usd", "cost_complete", "steps",
    )  # fmt: skip


def test_a_new_report_is_running_with_every_unmeasured_field_null() -> None:
    report = _report()
    assert report["schema"] == 1
    assert report["status"] == "running"
    assert report["steps"] == []
    for key in ("exit_code", "verdict", "text", "base", "head", "branch", "lineage",
                "continues", "findings_from", "implement_providers", "commits",
                "failure_reason", "vendor_check", "cleanup", "duration_seconds", "cost_usd"):  # fmt: skip
        assert report[key] is None, key


def test_the_step_key_set_is_the_spec_one() -> None:
    step = step_entry(
        index=1, slot="run", role="codex", step_dir="steps/01-run-codex", result=_result()
    )
    assert tuple(step) == STEP_KEYS
    assert step["tokens"] == {
        "input": 10,
        "output": 2,
        "fresh": None,
        "cached": None,
        "thinking": None,
    }
    assert step["tools"] is None, "tool counters arrive in lot 2: not measured is null"


def test_an_unmeasured_token_count_is_null_not_zero() -> None:
    step = step_entry(index=1, slot="run", role="codex", step_dir="d", result=_result(tokens=None))
    assert step["tokens"] is None


def test_cost_is_the_sum_and_incomplete_when_a_step_is_unmeasured() -> None:
    report = with_step(
        _report(), step_entry(index=1, slot="run", role="a", step_dir="d1", result=_result())
    )
    assert (report["cost_usd"], report["cost_complete"]) == (0.25, True)
    report = with_step(
        report,
        step_entry(index=2, slot="run", role="b", step_dir="d2", result=_result(cost_usd=None)),
    )
    assert (report["cost_usd"], report["cost_complete"]) == (0.25, False)


def test_with_step_does_not_mutate_its_input() -> None:
    report = _report()
    with_step(report, step_entry(index=1, slot="run", role="a", step_dir="d", result=_result()))
    assert report["steps"] == []


def test_step_dir_name() -> None:
    assert step_dir_name(1, "run", "codex") == "01-run-codex"
    assert step_dir_name(12, "judge", "judge") == "12-judge-judge"


def test_write_report_publishes_run_json(tmp_path: Path) -> None:
    write_report(tmp_path, _report())
    assert json.loads((tmp_path / RUN_JSON).read_text())["run_id"] == "20260925T000000-aaaaaaaa"


def test_a_step_carries_its_tool_counts() -> None:
    entry = step_entry(
        index=1,
        slot="run",
        role="codex",
        step_dir="steps/01-run-codex",
        result=_result(),
        tools={"command_execution": 4},
    )
    assert entry["tools"] == {"command_execution": 4}


def test_a_step_without_counts_is_not_measured() -> None:
    entry = step_entry(
        index=1, slot="run", role="codex", step_dir="steps/01-run-codex", result=_result()
    )
    assert entry["tools"] is None
