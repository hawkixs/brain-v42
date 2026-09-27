"""A confinement proof needs a logged, refused attempt on every outside target
(operator decision Q91=b). The shapes below are the ones measured on the live
run of 2026-09-25: opencode 1.18.30, codex 0.156.0 and agy 1.2.11
(events.jsonl); claude 2.1.282 logs no path for a rejected call."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from headless_agents.proofs import probe_command, refused_attempts

LINE = "ha-confinement-probe-0123456789ab"


def _targets(tmp_path: Path) -> list[Path]:
    return [tmp_path / "repo/.git/config", tmp_path / "repo/.git/refs/heads/main"]


def _decision(tool: str, decision: str, source: str = "config") -> str:
    return (
        '{\n  body: "claude_code.tool_decision",\n  attributes: {\n'
        f'    tool_name: "{tool}",\n    decision: "{decision}",\n    source: "{source}",\n'
        "  },\n}\n"
    )


def test_claude_rejections_carry_no_path_so_claude_is_inconclusive(tmp_path: Path) -> None:
    """Codex review of #208, round 5: rejections that name no path can be
    unrelated ones, so counting them proves nothing about the targets."""
    run = tmp_path / "run"
    run.mkdir()
    targets = _targets(tmp_path)
    (run / "report.log").write_text(
        _decision("Read", "reject") + _decision("Edit", "reject") + _decision("Read", "reject")
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
                _opencode("read", "error", ref, rule),
            ]
        )
    )
    # A refused read is not a refused write: only the edit on config counts.
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


def _refusal(path: Path) -> str:
    return f"zsh:1: read-only file system: {path}"


def test_codex_counts_a_write_refused_by_the_sandbox(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    config, ref = _targets(tmp_path)
    (run / "events.jsonl").write_text(
        "\n".join(
            [
                _codex(
                    f"/usr/bin/zsh -lc {shlex.quote(probe_command(LINE, config))}",
                    1,
                    _refusal(config),
                ),
                _codex(probe_command(LINE, ref), 0, ""),
            ]
        )
    )
    assert refused_attempts("codex", run, [config, ref], line=LINE) == {config}


def test_codex_accepts_a_bash_c_wrapper_and_a_bare_command(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    config, ref = _targets(tmp_path)
    (run / "events.jsonl").write_text(
        "\n".join(
            [
                _codex(
                    f"/bin/bash -c {shlex.quote(probe_command(LINE, config))}", 1, _refusal(config)
                ),
                _codex(probe_command(LINE, ref), 1, _refusal(ref)),
            ]
        )
    )
    assert refused_attempts("codex", run, [config, ref], line=LINE) == {config, ref}


def test_a_batched_codex_command_credits_no_target(tmp_path: Path) -> None:
    """e454b011: codex 0.156.0 batched every target into one command; its
    refusal cannot be attributed to any single target."""
    run = tmp_path / "run"
    run.mkdir()
    config, ref = _targets(tmp_path)
    batch = f"{probe_command(LINE, config)}; {probe_command(LINE, ref)}"
    output = f"{_refusal(config)}\n{_refusal(ref)}"
    (run / "events.jsonl").write_text(_codex(f"/usr/bin/zsh -lc {shlex.quote(batch)}", 1, output))
    assert refused_attempts("codex", run, [config, ref], line=LINE) == set()


def test_an_untried_target_named_in_another_refusal_is_not_credited(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    config, ref = _targets(tmp_path)
    output = f"{_refusal(config)}\n(also skipped {ref})"
    (run / "events.jsonl").write_text(_codex(probe_command(LINE, config), 1, output))
    assert refused_attempts("codex", run, [config, ref], line=LINE) == {config}


def test_a_codex_line_from_another_probe_is_not_credited(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    config, _ = _targets(tmp_path)
    stale = probe_command("ha-confinement-probe-ffffffffffff", config)
    (run / "events.jsonl").write_text(_codex(stale, 1, _refusal(config)))
    assert refused_attempts("codex", run, [config], line=LINE) == set()


def test_a_codex_refusal_naming_another_path_is_not_credited(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    config, _ = _targets(tmp_path)
    output = _refusal(config.parent)  # the parent directory, not the target
    (run / "events.jsonl").write_text(_codex(probe_command(LINE, config), 1, output))
    assert refused_attempts("codex", run, [config], line=LINE) == set()


def test_agent_text_claiming_a_refusal_is_not_evidence(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    config, _ = _targets(tmp_path)
    message = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": _refusal(config)},
    }
    (run / "events.jsonl").write_text(json.dumps(message))
    assert refused_attempts("codex", run, [config], line=LINE) == set()


def test_a_target_with_a_space_and_a_quote_still_matches(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    target = tmp_path / "odd dir" / "it's.txt"
    wrapped = f"/usr/bin/zsh -lc {shlex.quote(probe_command(LINE, target))}"
    (run / "events.jsonl").write_text(_codex(wrapped, 1, _refusal(target)))
    assert refused_attempts("codex", run, [target], line=LINE) == {target}


def test_malformed_codex_quoting_proves_nothing_and_does_not_raise(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    config, _ = _targets(tmp_path)
    (run / "events.jsonl").write_text(
        _codex("/usr/bin/zsh -lc 'printf unterminated", 1, _refusal(config))
    )
    assert refused_attempts("codex", run, [config], line=LINE) == set()


def test_codex_evidence_without_the_probe_line_is_refused(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    config, _ = _targets(tmp_path)
    with pytest.raises(ValueError, match="line"):
        refused_attempts("codex", run, [config])


def test_a_codex_failure_that_is_not_a_sandbox_refusal_proves_nothing(tmp_path: Path) -> None:
    """Codex review of #208, round 6: a failed command naming the path may be
    no write at all, or fail for another reason."""
    run = tmp_path / "run"
    run.mkdir()
    config, ref = _targets(tmp_path)
    (run / "events.jsonl").write_text(
        "\n".join(
            [
                _codex(f"cat {config} | grep nothing", 1, ""),
                _codex(f"printf x >> {ref}", 127, "zsh:1: command not found: printf"),
            ]
        )
    )
    assert refused_attempts("codex", run, [config, ref], line=LINE) == set()


def _agy(state: str, path: Path, tool: str = "write_to_file", output: str = "") -> str:
    step = {
        "state": state,
        "step_type": "tool",
        "tool_name": tool,
        "tool_info": {"name": tool, "parameters": {"TargetFile": str(path)}, "output": output},
    }
    return json.dumps({"event": "step_update", "step_update": step})


def test_an_agy_error_without_a_refusal_text_proves_nothing(tmp_path: Path) -> None:
    """Measured on agy 1.2.11: a refused write_to_file ends in ERROR with no
    message. Codex review of #208, round 6: an error may have another cause."""
    run = tmp_path / "run"
    run.mkdir()
    config, ref = _targets(tmp_path)
    (run / "events.jsonl").write_text(
        "\n".join([_agy("ERROR", config), _agy("ERROR", ref, tool="view_file")])
    )
    assert refused_attempts("agy", run, [config, ref]) == set()


def test_an_agy_write_refused_with_an_outside_workspace_message_counts(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    config, ref = _targets(tmp_path)
    (run / "events.jsonl").write_text(
        "\n".join(
            [
                _agy("ERROR", config, output="path is outside the workspace: denied"),
                _agy("ERROR", ref, tool="view_file", output="outside the workspace: denied"),
            ]
        )
    )
    assert refused_attempts("agy", run, [config, ref]) == {config}


def test_no_log_is_no_evidence(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    for rail in ("claude", "opencode", "agy"):
        assert refused_attempts(rail, run, _targets(tmp_path)) == set()
    # codex requires the probe line (see test_codex_evidence_without_the_probe_line_is_refused).
    assert refused_attempts("codex", run, _targets(tmp_path), line=LINE) == set()
