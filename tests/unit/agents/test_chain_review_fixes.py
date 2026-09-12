"""Findings of the independent review of lot 2 (Brain ticket afd56820), pinned.

Each test names the bash behaviour it protects; the review found the Python
transposition diverged on it. See the review record in the pull request.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from brain_v42.agents import phase
from brain_v42.agents.providers import agy as agy_provider

ENVIRON: dict[str, str] = {
    "BRAIN_DREAM_CODEX_FAST_MODEL": "gpt-5.6-luna",
    "BRAIN_DREAM_CODEX_DEEP_MODEL": "gpt-6-astra",
    "BRAIN_DREAM_CODEX_FAST_REASONING": "high",
    "BRAIN_DREAM_CODEX_DEEP_REASONING": "max",
    "BRAIN_DREAM_AGY_FAST_MODEL": "gemini-3.8-flash-high",
    "BRAIN_DREAM_AGY_DEEP_MODEL": "gemini-3.1-pro-high",
    "BRAIN_DREAM_CODEX_BIN": "/golden/bin/codex",
    "BRAIN_DREAM_AGY_BIN": "/golden/bin/agy",
    "BRAIN_DREAM_CLAUDE_BIN": "/golden/bin/claude",
}


class _Spy:
    """A spawn double that records argv, stdin and the environment it was given."""

    def __init__(self, runner_rc: int = 0) -> None:
        self.calls: list[dict[str, object]] = []
        self.runner_rc = runner_rc

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append({"argv": argv, "input": input, "env": dict(env) if env else None})
        joined = " ".join(argv)
        if "providers." in joined:
            return subprocess.CompletedProcess(argv, self.runner_rc, stdout="")
        return subprocess.CompletedProcess(argv, 0, stdout="")

    def runner(self) -> dict[str, object]:
        return next(c for c in self.calls if "providers." in " ".join(c["argv"]))  # type: ignore[arg-type]


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    provider: str = "agy",
    phase_name: str = "scan",
    prompt_text: str = "scan prompt for {{PROJECT_KEY}}\n",
    environ: Mapping[str, str] | None = None,
    runner_rc: int = 0,
) -> tuple[_Spy, list[str], Path, Path]:
    log_dir = tmp_path / "logs"
    dream_dir = tmp_path / "dream"
    log_dir.mkdir(exist_ok=True)
    dream_dir.mkdir(exist_ok=True)
    (dream_dir / f"phase_{phase_name}.md").write_text(prompt_text, encoding="utf-8")
    spy = _Spy(runner_rc)
    monkeypatch.setattr(phase, "spawn", spy)
    logged: list[str] = []
    phase.run_phase(
        provider,
        phase=phase_name,
        model_tier="fast",
        timeout_minutes=5,
        max_turns=30,
        project_key="brain-v42",
        timestamp="2026-09-12",
        log_dir=log_dir,
        dream_dir=dream_dir,
        dry_run="false",
        reorg_dry_run="false",
        environ=dict(ENVIRON if environ is None else environ),
        log=logged.append,
    )
    return spy, logged, log_dir, dream_dir


def test_agy_runner_subprocess_receives_the_release_tree_guard_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The old shim anchored the guard to its own file (the release tree via
    PYTHONPATH). The package entry point must get that path explicitly: the
    dream unit's working directory is the mutable repository, never the release."""
    spy, _, _, dream_dir = _run(monkeypatch, tmp_path, provider="agy")
    env = spy.runner()["env"]
    assert env is not None
    assert env["BRAIN_DREAM_AGY_GUARD_PATH"] == str(dream_dir / "agy_tool_guard.sh")  # type: ignore[index]


def test_prompt_has_no_trailing_newline_like_bash_command_substitution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bash: prompt=$(python3 -m scripts.dream._render_prompt ...) strips every
    trailing newline; templates end with a newline, so every prompt bash ever
    sent ended without one."""
    spy, _, _, _ = _run(monkeypatch, tmp_path, prompt_text="scan prompt for {{PROJECT_KEY}}\n\n\n")
    stdin = spy.runner()["input"]
    assert isinstance(stdin, str)
    assert stdin == "scan prompt for brain-v42"


def test_dependency_report_injection_matches_the_bash_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bash: dep_section+="\\n### ${dep^^} phase output\\n$(cat "$dep_log")\\n" per dependency,
    then prompt="<header>\\n<sentence>\\n$dep_section\\n\\n---\\n\\n$prompt"; $(cat) strips
    the report's trailing newlines and the final prompt carries none."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "2026-09-12_brain-v42_scan.log").write_text(
        "SCAN REPORT\nline two\n", encoding="utf-8"
    )
    spy, _, _, _ = _run(monkeypatch, tmp_path, phase_name="clean", prompt_text="clean prompt\n")
    stdin = spy.runner()["input"]
    assert stdin == (
        "## Previous Phase Reports (reference context — do not mimic style)\n"
        "The orchestrator has injected the output from dependency phases below.\n"
        "\n### SCAN phase output\nSCAN REPORT\nline two\n"
        "\n\n---\n\n"
        "clean prompt"
    )


def test_a_missing_model_variable_yields_an_empty_model_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bash expanded an unset variable to the empty string; the runner then
    refused the empty model and the phase reddened with a FAIL line and a
    dream_runs row. A KeyError would kill the CLI with no line at all."""
    environ = {k: v for k, v in ENVIRON.items() if k != "BRAIN_DREAM_AGY_FAST_MODEL"}
    spy, logged, _, _ = _run(monkeypatch, tmp_path, provider="agy", environ=environ, runner_rc=1)
    argv = spy.runner()["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--model") + 1] == ""
    assert logged[0] == "START scan (provider=agy, model=, timeout=5m)"
    assert "FAIL  scan (exit=1)" in logged


def test_an_empty_promote_pool_variable_renders_as_an_empty_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bash: ${PROMOTE_CANDIDATE_POOL_JSON:-[]} replaces an EMPTY value too;
    an empty candidates file must not render as nothing."""
    environ = {**ENVIRON, "PROMOTE_CANDIDATE_POOL_JSON": "", "PROMOTE_RECENT_PROMOTIONS_JSON": ""}
    spy, _, _, _ = _run(
        monkeypatch,
        tmp_path,
        phase_name="promote",
        prompt_text="pool={{CANDIDATE_POOL_JSON}} recent={{RECENT_PROMOTIONS_JSON}}\n",
        environ=environ,
    )
    stdin = spy.runner()["input"]
    assert isinstance(stdin, str)
    assert "pool=[] recent=[]" in stdin


def test_the_package_agy_entry_point_never_guesses_the_guard_from_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRAIN_DREAM_AGY_GUARD_PATH", raising=False)
    with pytest.raises(SystemExit):
        agy_provider._default_guard_path()


def test_the_shim_agy_entry_point_defaults_the_guard_to_its_own_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.dream import agy_runner as shim

    monkeypatch.delenv("BRAIN_DREAM_AGY_GUARD_PATH", raising=False)
    seen: dict[str, str] = {}

    def _fake_package_main(argv: list[str] | None = None) -> int:
        seen["guard"] = os.environ.get("BRAIN_DREAM_AGY_GUARD_PATH", "")
        return 0

    monkeypatch.setattr(shim, "_package_main", _fake_package_main)
    assert shim.main([]) == 0
    assert seen["guard"] == str(shim.GUARD_PATH)
