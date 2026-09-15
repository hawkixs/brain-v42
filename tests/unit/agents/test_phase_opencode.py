"""The fourth link in ``brain_v42.agents.phase``: opencode.

The golden fixtures under ``tests/fixtures/agents_chain_golden/`` were
captured from ``scripts/dream.sh`` before this rail existed and stay as
they are; this file pins the opencode branch of every dispatch table the
golden test covers for the three historical rails -- runner module, runner
argv, parser module and argv, model selection, START line -- and runs one
phase through the chain with the spawn seam stubbed.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from brain_v42.agents import chain, lines, phase

ENVIRON: dict[str, str] = {
    "BRAIN_DREAM_CODEX_FAST_MODEL": "gpt-5.6-luna",
    "BRAIN_DREAM_CODEX_DEEP_MODEL": "gpt-6-astra",
    "BRAIN_DREAM_CODEX_FAST_REASONING": "high",
    "BRAIN_DREAM_CODEX_DEEP_REASONING": "max",
    "BRAIN_DREAM_AGY_FAST_MODEL": "gemini-3.8-flash-high",
    "BRAIN_DREAM_AGY_DEEP_MODEL": "gemini-3.1-pro-high",
    "BRAIN_DREAM_OPENCODE_FAST_MODEL": "opencode-go/glm-5.3-flash",
    "BRAIN_DREAM_OPENCODE_DEEP_MODEL": "opencode-go/deepseek-v4.1-flash",
    "BRAIN_DREAM_OPENCODE_FAST_VARIANT": "",
    "BRAIN_DREAM_OPENCODE_DEEP_VARIANT": "high",
    "BRAIN_DREAM_CODEX_BIN": "codex",
    "BRAIN_DREAM_AGY_BIN": "agy",
    "BRAIN_DREAM_CLAUDE_BIN": "claude",
    "BRAIN_DREAM_OPENCODE_BIN": "/home/x/.opencode/bin/opencode",
}


def _paths(tmp_path: Path) -> phase.PhasePaths:
    return phase.PhasePaths.build(
        log_dir=tmp_path / "logs",
        timestamp="2026-09-15",
        project_key="brain-v42",
        phase="reorg",
        dream_dir=tmp_path / "dream",
    )


class TestDispatchTables:
    def test_runner_and_parser_modules(self) -> None:
        assert phase.runner_module("opencode") == "brain_v42.agents.providers.opencode"
        assert phase.parser_module("opencode") == "brain_v42.metrics.opencode_dream_parser"

    def test_runner_argv_carries_the_variant_and_the_executable(self, tmp_path: Path) -> None:
        p = _paths(tmp_path)
        got = phase.runner_argv(
            "opencode",
            phase="reorg",
            project_key="brain-v42",
            model="opencode-go/deepseek-v4.1-flash",
            reasoning="high",
            timeout_minutes=10,
            max_turns=50,
            paths=p,
            executable="/home/x/.opencode/bin/opencode",
        )
        assert got == [
            "--phase",
            "reorg",
            "--project-key",
            "brain-v42",
            "--model",
            "opencode-go/deepseek-v4.1-flash",
            "--variant",
            "high",
            "--timeout-seconds",
            "600",
            "--events-log",
            str(p.events_log),
            "--report-log",
            str(p.report_log),
            "--stderr-log",
            str(p.stderr_log),
            "--opencode-executable",
            "/home/x/.opencode/bin/opencode",
        ]

    def test_runner_argv_passes_an_empty_variant_as_empty(self, tmp_path: Path) -> None:
        got = phase.runner_argv(
            "opencode",
            phase="scan",
            project_key="brain-v42",
            model="m",
            reasoning=None,
            timeout_minutes=5,
            max_turns=30,
            paths=_paths(tmp_path),
            executable="opencode",
        )
        assert got[got.index("--variant") + 1] == ""

    def test_parser_argv_reads_the_report_and_the_event_stream(self, tmp_path: Path) -> None:
        p = _paths(tmp_path)
        got = phase.parser_argv(
            "opencode",
            phase="reorg",
            model="m",
            timestamp="2026-09-15",
            status="done",
            duration=42,
            project_key="brain-v42",
            effective_dry_run="false",
            scan_log=p.stderr_log,
            paths=p,
        )
        assert got[-3:] == ["--report-log", str(p.report_log), str(p.events_log)]
        assert got[:2] == ["--phase", "reorg"]

    def test_select_model_reads_the_opencode_variables_with_no_default(self) -> None:
        assert phase._select_model("opencode", "fast", ENVIRON) == (
            "opencode-go/glm-5.3-flash",
            "",
        )
        assert phase._select_model("opencode", "deep", ENVIRON) == (
            "opencode-go/deepseek-v4.1-flash",
            "high",
        )
        assert phase._select_model("opencode", "fast", {}) == ("", "")
        with pytest.raises(phase.UnsupportedTierError):
            phase._select_model("opencode", "ultra", ENVIRON)

    def test_start_line_names_the_variant_only_when_there_is_one(self) -> None:
        assert lines.start_line("opencode", "scan", "opencode-go/glm-5.3-flash", 5) == (
            "START scan (provider=opencode, model=opencode-go/glm-5.3-flash, timeout=5m)"
        )
        assert lines.start_line(
            "opencode", "reorg", "opencode-go/deepseek-v4.1-flash", 10, reasoning="high"
        ) == (
            "START reorg (provider=opencode, model=opencode-go/deepseek-v4.1-flash, "
            "variant=high, timeout=10m)"
        )
        assert lines.start_line("opencode", "scan", "m", 5, reasoning="") == (
            "START scan (provider=opencode, model=m, timeout=5m)"
        )

    def test_the_historical_rails_are_untouched(self) -> None:
        assert lines.start_line("agy", "scan", "g", 5) == (
            "START scan (provider=agy, model=g, timeout=5m)"
        )
        assert lines.start_line("codex", "scan", "c", 5, reasoning="high") == (
            "START scan (provider=codex, model=c, reasoning=high, timeout=5m)"
        )
        assert lines.parser_warn_line("opencode", "scan") == (
            "WARN  opencode_dream_parser failed for scan (non-fatal)"
        )


def _fake_spawn(
    rc_by_provider: Mapping[str, int], calls: list[list[str]]
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def _spawn(
        argv: Sequence[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        calls.append(argv)
        joined = " ".join(argv)
        if "brain_v42.metrics." in joined:
            return subprocess.CompletedProcess(argv, 0, stdout="")
        for provider, rc in rc_by_provider.items():
            if f"providers.{provider}" in joined:
                return subprocess.CompletedProcess(argv, rc, stdout="")
        raise AssertionError(f"unexpected argv passed to spawn: {argv}")

    return _spawn


def test_the_chain_falls_from_codex_to_opencode_and_stops_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_dir = tmp_path / "logs"
    dream_dir = tmp_path / "dream"
    log_dir.mkdir()
    dream_dir.mkdir()
    (dream_dir / "phase_reorg.md").write_text("reorg prompt for {{PROJECT_KEY}}\n")
    calls: list[list[str]] = []
    monkeypatch.setattr(phase, "spawn", _fake_spawn({"codex": 3, "opencode": 0}, calls))
    logged: list[str] = []

    def run_one(provider: str) -> int:
        return phase.run_phase(
            provider,
            phase="reorg",
            model_tier="deep",
            timeout_minutes=10,
            max_turns=50,
            project_key="brain-v42",
            timestamp="2026-09-15",
            log_dir=log_dir,
            dream_dir=dream_dir,
            dry_run="false",
            reorg_dry_run="false",
            environ=ENVIRON,
            log=logged.append,
        )

    result = chain.run_chain(
        ["codex", "opencode", "agy", "claude"],
        run_one=run_one,
        log=logged.append,
        project_key="brain-v42",
        phase="reorg",
    )

    assert (result.provider, result.rc, list(result.fallbacks)) == ("opencode", 0, ["codex"])
    assert (
        "START reorg (provider=opencode, model=opencode-go/deepseek-v4.1-flash, "
        "variant=high, timeout=10m)"
    ) in logged
    runner_calls = [argv for argv in calls if "brain_v42.agents.providers.opencode" in argv]
    assert len(runner_calls) == 1
    argv = runner_calls[0]
    assert argv[argv.index("--opencode-executable") + 1] == "/home/x/.opencode/bin/opencode"
    parser_calls = [argv for argv in calls if "brain_v42.metrics.opencode_dream_parser" in argv]
    assert len(parser_calls) == 1
    assert parser_calls[0][parser_calls[0].index("--status") + 1] == "done"
