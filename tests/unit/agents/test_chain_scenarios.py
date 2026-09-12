"""Scenario tests for the provider chain execution path (lot 2, Brain ticket
``afd56820``): :func:`brain_v42.agents.phase.run_phase`,
:func:`brain_v42.agents.chain.run_chain` wired together, and the
``python -m brain_v42.agents.run_phase_chain`` CLI end to end.

Every test monkeypatches :func:`brain_v42.agents.phase.spawn` -- the single
seam that launches a runner/parser/``otel_split`` subprocess -- with a fake
that classifies the call by which module is in ``argv`` and returns a
pre-chosen exit code. No real subprocess, no real agent CLI, is ever
launched.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from brain_v42.agents import chain, lines, phase
from brain_v42.agents import run_phase_chain as cli

REQUIRED_ENVIRON: dict[str, str] = {
    "BRAIN_DREAM_CODEX_FAST_MODEL": "gpt-5.6-terra",
    "BRAIN_DREAM_CODEX_DEEP_MODEL": "gpt-5.6-luna",
    "BRAIN_DREAM_CODEX_FAST_REASONING": "medium",
    "BRAIN_DREAM_CODEX_DEEP_REASONING": "high",
    "BRAIN_DREAM_AGY_FAST_MODEL": "gemini-3.1-pro",
    "BRAIN_DREAM_AGY_DEEP_MODEL": "gemini-3.1-pro-high",
    "BRAIN_DREAM_CODEX_BIN": "codex",
    "BRAIN_DREAM_AGY_BIN": "agy",
    "BRAIN_DREAM_CLAUDE_BIN": "claude",
}


def _fake_spawn(
    rc_by_provider: Mapping[str, int],
    *,
    otel_rc: int = 0,
    parser_rc: int = 0,
    calls: list[list[str]] | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def _spawn(
        argv: Sequence[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        if calls is not None:
            calls.append(argv)
        joined = " ".join(argv)
        if phase.OTEL_SPLIT_MODULE in joined:
            return subprocess.CompletedProcess(argv, otel_rc, stdout="")
        if "brain_v42.metrics." in joined:
            return subprocess.CompletedProcess(argv, parser_rc, stdout="")
        for provider, rc in rc_by_provider.items():
            if f"providers.{provider}" in joined:
                return subprocess.CompletedProcess(argv, rc, stdout="")
        raise AssertionError(f"unexpected argv passed to spawn: {argv}")

    return _spawn


def _write_prompt(dream_dir: Path, phase_name: str) -> None:
    (dream_dir / f"phase_{phase_name}.md").write_text("scan prompt for {{PROJECT_KEY}}\n")


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    log_dir = tmp_path / "logs"
    dream_dir = tmp_path / "dream"
    log_dir.mkdir()
    dream_dir.mkdir()
    return log_dir, dream_dir


def _run_one_phase(
    provider: str,
    log_dir: Path,
    dream_dir: Path,
    rc: int,
    *,
    logged: list[str],
) -> int:
    _write_prompt(dream_dir, "scan")
    return phase.run_phase(
        provider,
        phase="scan",
        model_tier="fast",
        timeout_minutes=5,
        max_turns=30,
        project_key="brain-v42",
        timestamp="2026-09-12",
        log_dir=log_dir,
        dream_dir=dream_dir,
        dry_run="false",
        reorg_dry_run="false",
        environ=REQUIRED_ENVIRON,
        log=logged.append,
    )


@pytest.mark.parametrize(
    "provider,code,expected_rc,expects",
    [
        ("codex", 0, 0, lines.done_line("scan")),
        ("codex", 1, 1, lines.fail_line("scan", 1, fallback=False)),
        ("codex", 3, 3, lines.fail_line("scan", 3, fallback=True)),
        ("codex", 124, 2, lines.timeout_line("scan", 5)),
        ("agy", 0, 0, lines.done_line("scan")),
        ("claude", 0, 0, lines.done_line("scan")),
    ],
)
def test_run_phase_maps_every_exit_code(
    dirs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    code: int,
    expected_rc: int,
    expects: str,
) -> None:
    log_dir, dream_dir = dirs
    monkeypatch.setattr(phase, "spawn", _fake_spawn({provider: code}))
    logged: list[str] = []

    rc = _run_one_phase(provider, log_dir, dream_dir, code, logged=logged)

    assert rc == expected_rc
    assert expects in logged
    assert logged[0].startswith("START scan")


def test_run_phase_skips_when_prompt_file_is_missing(
    dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir, dream_dir = dirs
    monkeypatch.setattr(phase, "spawn", _fake_spawn({"codex": 0}))
    logged: list[str] = []

    rc = phase.run_phase(
        "codex",
        phase="scan",
        model_tier="fast",
        timeout_minutes=5,
        max_turns=30,
        project_key="brain-v42",
        timestamp="2026-09-12",
        log_dir=log_dir,
        dream_dir=dream_dir,
        dry_run="false",
        reorg_dry_run="false",
        environ=REQUIRED_ENVIRON,
        log=logged.append,
    )

    assert rc == 0
    assert logged == [lines.skip_missing_prompt_line("scan", dream_dir / "phase_scan.md")]


def test_run_phase_reports_an_unsupported_tier_as_an_ordinary_failure(
    dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir, dream_dir = dirs
    _write_prompt(dream_dir, "scan")
    monkeypatch.setattr(phase, "spawn", _fake_spawn({"codex": 0}))
    logged: list[str] = []

    rc = phase.run_phase(
        "codex",
        phase="scan",
        model_tier="ultra-deep",
        timeout_minutes=5,
        max_turns=30,
        project_key="brain-v42",
        timestamp="2026-09-12",
        log_dir=log_dir,
        dream_dir=dream_dir,
        dry_run="false",
        reorg_dry_run="false",
        environ=REQUIRED_ENVIRON,
        log=logged.append,
    )

    assert rc == 1
    assert logged == [lines.unsupported_tier_line("scan", "codex", "ultra-deep")]


@pytest.mark.parametrize(
    "providers,codes,expected_provider,expected_rc,expected_fallbacks",
    [
        (["codex"], {"codex": 3}, "codex", 1, []),
        (["codex", "agy"], {"codex": 3, "agy": 0}, "agy", 0, ["codex"]),
        (
            ["codex", "agy", "claude"],
            {"codex": 3, "agy": 3, "claude": 3},
            "claude",
            1,
            ["codex", "agy"],
        ),
        (["codex", "agy"], {"codex": 1}, "codex", 1, []),
        (["codex", "agy"], {"codex": 124}, "codex", 2, []),
    ],
)
def test_chain_over_run_phase(
    dirs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    providers: list[str],
    codes: dict[str, int],
    expected_provider: str,
    expected_rc: int,
    expected_fallbacks: list[str],
) -> None:
    log_dir, dream_dir = dirs
    _write_prompt(dream_dir, "scan")
    monkeypatch.setattr(phase, "spawn", _fake_spawn(codes))
    logged: list[str] = []

    def run_one(provider: str) -> int:
        return phase.run_phase(
            provider,
            phase="scan",
            model_tier="fast",
            timeout_minutes=5,
            max_turns=30,
            project_key="brain-v42",
            timestamp="2026-09-12",
            log_dir=log_dir,
            dream_dir=dream_dir,
            dry_run="false",
            reorg_dry_run="false",
            environ=REQUIRED_ENVIRON,
            log=logged.append,
        )

    result = chain.run_chain(
        providers,
        run_one=run_one,
        log=logged.append,
        project_key="brain-v42",
        phase="scan",
    )

    assert result.provider == expected_provider
    assert result.rc == expected_rc
    assert list(result.fallbacks) == expected_fallbacks


def test_cli_runs_the_chain_and_writes_the_result_json(
    dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir, dream_dir = dirs
    _write_prompt(dream_dir, "scan")
    for key, value in REQUIRED_ENVIRON.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(phase, "spawn", _fake_spawn({"codex": 3, "agy": 0}))

    result_json = log_dir / "2026-09-12_brain-v42_scan.chain.json"
    rc = cli.main(
        [
            "--phase",
            "scan",
            "--tier",
            "fast",
            "--timeout-minutes",
            "5",
            "--max-turns",
            "30",
            "--project-key",
            "brain-v42",
            "--timestamp",
            "2026-09-12",
            "--log-dir",
            str(log_dir),
            "--dream-dir",
            str(dream_dir),
            "--providers",
            "codex,agy",
            "--dry-run",
            "false",
            "--reorg-dry-run",
            "false",
            "--result-json",
            str(result_json),
        ]
    )

    assert rc == 0
    payload = json.loads(result_json.read_text())
    assert payload == {
        "provider": "agy",
        "rc": 0,
        "status": "done",
        "fallbacks": ["codex"],
    }

    main_log = log_dir / "2026-09-12.log"
    log_text = main_log.read_text()
    assert lines.fallback_line("brain-v42", "scan", "codex", "agy") in log_text
    # Every line in the main log is prefixed with [HH:MM:SS].
    assert all(line.startswith("[") for line in log_text.splitlines() if line)


def test_cli_returns_the_chain_rc_on_a_failing_chain(
    dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir, dream_dir = dirs
    _write_prompt(dream_dir, "scan")
    for key, value in REQUIRED_ENVIRON.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(phase, "spawn", _fake_spawn({"codex": 1}))

    result_json = log_dir / "2026-09-12_brain-v42_scan.chain.json"
    rc = cli.main(
        [
            "--phase",
            "scan",
            "--tier",
            "fast",
            "--timeout-minutes",
            "5",
            "--max-turns",
            "30",
            "--project-key",
            "brain-v42",
            "--timestamp",
            "2026-09-12",
            "--log-dir",
            str(log_dir),
            "--dream-dir",
            str(dream_dir),
            "--providers",
            "codex",
            "--dry-run",
            "false",
            "--reorg-dry-run",
            "false",
            "--result-json",
            str(result_json),
        ]
    )

    assert rc == 1
    payload = json.loads(result_json.read_text())
    assert payload == {"provider": "codex", "rc": 1, "status": "fail", "fallbacks": []}
