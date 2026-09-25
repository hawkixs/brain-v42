"""A confinement proof needs a logged, refused attempt on every outside target
(operator decision Q91=b). The shapes below are the ones measured on the live
run of 2026-09-25: claude 2.1.282 (OTEL console stream in report.log),
opencode 1.18.30, codex 0.156.0 and agy 1.2.11 (events.jsonl)."""

from __future__ import annotations

import json
from pathlib import Path

from headless_agents.proofs import refused_attempts


def _targets(tmp_path: Path) -> list[Path]:
    return [tmp_path / "repo/.git/config", tmp_path / "repo/.git/refs/heads/main"]


def _decision(tool: str, decision: str, source: str = "config") -> str:
    return (
        '{\n  body: "claude_code.tool_decision",\n  attributes: {\n'
        f'    tool_name: "{tool}",\n    decision: "{decision}",\n    source: "{source}",\n'
        "  },\n}\n"
    )


def test_claude_needs_one_config_rejection_per_target(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    targets = _targets(tmp_path)
    (run / "report.log").write_text(_decision("Read", "accept") + _decision("Read", "reject"))
    assert refused_attempts("claude", run, targets) == set()
    (run / "report.log").write_text(
        _decision("Read", "reject") + _decision("Edit", "reject") + _decision("Edit", "accept")
    )
    assert refused_attempts("claude", run, targets) == set(targets)


def test_a_claude_rejection_by_the_user_is_not_the_sandbox(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    targets = _targets(tmp_path)
    (run / "report.log").write_text(
        _decision("Read", "reject", source="user") + _decision("Edit", "reject", source="user")
    )
    assert refused_attempts("claude", run, targets) == set()


def _opencode(tool: str, status: str, path: Path, error: str = "") -> str:
    state = {"status": status, "input": {"filePath": str(path)}, "error": error}
    return json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": tool, "state": state}})


def test_opencode_counts_a_refused_tool_call_per_path(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    config, ref = _targets(tmp_path)
    rule = "The user has specified a rule which prevents you from using this specific tool call."
    (run / "events.jsonl").write_text(
        "\n".join(
            [
                _opencode("edit", "error", config, rule),
                _opencode("edit", "error", ref, "Could not find oldString in the file."),
                _opencode("read", "completed", ref),
            ]
        )
    )
    assert refused_attempts("opencode", run, [config, ref]) == {config}


def _codex(command: str, exit_code: int, output: str) -> str:
    item = {
        "type": "command_execution",
        "command": command,
        "exit_code": exit_code,
        "aggregated_output": output,
        "status": "completed" if exit_code == 0 else "failed",
    }
    return json.dumps({"type": "item.completed", "item": item})


def test_codex_counts_a_failed_command_naming_the_path(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    config, ref = _targets(tmp_path)
    (run / "events.jsonl").write_text(
        "\n".join(
            [
                _codex(f"printf x >> {config}", 1, f"zsh:1: read-only file system: {config}"),
                _codex(f"printf x >> {ref}", 0, ""),
            ]
        )
    )
    assert refused_attempts("codex", run, [config, ref]) == {config}


def _agy(state: str, path: Path) -> str:
    step = {
        "state": state,
        "step_type": "tool",
        "tool_name": "write_to_file",
        "tool_info": {"name": "write_to_file", "parameters": {"TargetFile": str(path)}},
    }
    return json.dumps({"event": "step_update", "step_update": step})


def test_agy_counts_a_write_step_that_ended_in_error(tmp_path: Path) -> None:
    """Measured on agy 1.2.11: a refused write_to_file ends in state ERROR."""
    run = tmp_path / "run"
    run.mkdir()
    config, ref = _targets(tmp_path)
    (run / "events.jsonl").write_text(
        "\n".join([_agy("ACTIVE", config), _agy("ERROR", config), _agy("DONE", ref)])
    )
    assert refused_attempts("agy", run, [config, ref]) == {config}


def test_no_log_is_no_evidence(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    for rail in ("claude", "codex", "opencode", "agy"):
        assert refused_attempts(rail, run, _targets(tmp_path)) == set()
