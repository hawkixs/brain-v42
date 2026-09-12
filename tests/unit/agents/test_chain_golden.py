"""Golden oracle for lot 2 (Brain ticket afd56820): the provider chain and the
single-phase runner leave ``scripts/dream.sh`` for ``brain_v42.agents``.

The fixtures under ``tests/fixtures/agents_chain_golden/`` were captured from
``scripts/dream.sh`` at ``c0e48966`` BEFORE ``run_phase`` and ``run_phase_chain``
were deleted, and from the two real night logs of 2026-09-12. Every template
is quoted verbatim from the shell; the Python side must reproduce it byte for
byte. Renaming a function below is allowed only together with this test;
changing a produced byte is not.

Expected API (the implementation may reorganise modules, the names are the
contract of this test):

- ``brain_v42.agents.lines``: ``start_line``, ``done_line``, ``timeout_line``,
  ``fail_line``, ``skip_missing_prompt_line``, ``unsupported_tier_line``,
  ``otel_warn_line``, ``parser_warn_line``, ``killswitch_line``,
  ``fallback_line``, ``fallback_end_line``, ``prefixed``.
- ``brain_v42.agents.phase``: ``PHASE_DEPS``, ``PhasePaths``, ``runner_argv``,
  ``runner_module``, ``parser_argv``, ``parser_module``, ``otel_split_argv``,
  ``effective_dry_run``, ``map_exit_code``.
- ``brain_v42.agents.chain``: ``run_chain`` returning an object with
  ``provider``, ``rc`` and ``fallbacks``.
"""

from __future__ import annotations

import json
from datetime import time
from pathlib import Path

import pytest

from brain_v42.agents import chain, lines, phase  # RED until lot 2 lands

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "agents_chain_golden"
LOG_LINES = json.loads((FIXTURES / "log_lines.json").read_text(encoding="utf-8"))
ARGV = json.loads((FIXTURES / "argv.json").read_text(encoding="utf-8"))
PHASES = json.loads((FIXTURES / "phases.json").read_text(encoding="utf-8"))

SAMPLE = {
    "$name": "scan",
    "$model": "gpt-5.6-luna",
    "${timeout}": "5",
    "$reasoning_effort": "high",
    "$max_turns": "30",
    "$prompt_file": "/golden/dream/phase_scan.md",
    "$BRAIN_DREAM_AGENT_PROVIDER/$model_tier": "codex/fast",
    "$PROJECT_KEY": "brain-v42",
    "$provider": "codex",
    "${PROVIDER_CHAIN[$index]}": "agy",
    "$var_name": "BRAIN_DREAM_REORG_DRY_RUN",
    "$value": "maybe",
}


def _fill(template: str, **overrides: str) -> str:
    values = {**SAMPLE, **overrides}
    for key in sorted(values, key=len, reverse=True):
        template = template.replace(key, values[key])
    assert "$" not in template, template
    return template


def _paths() -> phase.PhasePaths:
    return phase.PhasePaths.build(
        log_dir=Path("/golden/logs"),
        timestamp="2026-09-12",
        project_key="brain-v42",
        phase="scan",
        dream_dir=Path("/golden/dream"),
    )


class TestLogLinesAreByteIdentical:
    def test_start_lines(self) -> None:
        t = LOG_LINES["templates"]
        assert lines.start_line("agy", "scan", "gpt-5.6-luna", 5) == _fill(t["start_agy"])
        assert lines.start_line("codex", "scan", "gpt-5.6-luna", 5, reasoning="high") == _fill(
            t["start_codex"]
        )
        assert lines.start_line("claude", "scan", "gpt-5.6-luna", 5, max_turns=30) == _fill(
            t["start_claude"]
        )

    def test_outcome_lines(self) -> None:
        t = LOG_LINES["templates"]
        assert lines.done_line("scan") == _fill(t["done"])
        assert lines.timeout_line("scan", 5) == _fill(t["timeout"])
        assert lines.fail_line("scan", 3, fallback=True) == _fill(
            t["fail_fallback"], **{"$code": "3"}
        )
        assert lines.fail_line("scan", 7, fallback=False) == _fill(
            t["fail_other"], **{"$code": "7"}
        )
        assert lines.skip_missing_prompt_line("scan", Path("/golden/dream/phase_scan.md")) == _fill(
            t["skip_missing_prompt"]
        )
        assert lines.unsupported_tier_line("scan", "codex", "fast") == _fill(t["unsupported_tier"])

    def test_warning_lines(self) -> None:
        t = LOG_LINES["templates"]
        assert lines.otel_warn_line("scan") == _fill(t["warn_otel"])
        assert lines.parser_warn_line("agy", "scan") == _fill(t["warn_parser_agy"])
        assert lines.parser_warn_line("codex", "scan") == _fill(t["warn_parser_codex"])
        assert lines.parser_warn_line("claude", "scan") == _fill(t["warn_parser_claude"])
        assert lines.killswitch_line("BRAIN_DREAM_REORG_DRY_RUN", "maybe") == _fill(t["killswitch"])

    def test_chain_lines(self) -> None:
        t = LOG_LINES["templates"]
        assert lines.fallback_line("brain-v42", "scan", "codex", "agy") == _fill(t["fallback"])
        assert lines.fallback_end_line("brain-v42", "scan", "codex") == _fill(t["fallback_end"])

    def test_real_night_examples_are_reproducible(self) -> None:
        ex = LOG_LINES["examples"]
        assert lines.done_line("scan") == ex["done"].replace(ex["done"].split()[-1], "scan")
        # exact reproduction of two real lines from the 2026-09-12 night log
        assert (
            lines.fallback_line("brain-v42", "synth", "codex", "agy")
            == "FALLBACK brain-v42/synth — codex a échoué sans aucun appel d'outil Brain abouti, bascule vers agy"
        )
        assert (
            lines.fail_line("synth", 3, fallback=True)
            == "FAIL  synth (exit=3 — aucun appel d'outil Brain abouti)"
        )

    def test_prefix_format(self) -> None:
        assert lines.prefixed("DONE  scan", time(6, 7, 8)) == "[06:07:08] DONE  scan"


class TestArgvIsByteIdentical:
    def test_file_layout(self) -> None:
        p = _paths()
        f = ARGV["files"]
        expected = {
            k: v.replace("$LOG_DIR", "/golden/logs")
            .replace("${TIMESTAMP}", "2026-09-12")
            .replace("$TIMESTAMP", "2026-09-12")
            .replace("${PROJECT_KEY}", "brain-v42")
            .replace("${name}", "scan")
            .replace("$DREAM_DIR", "/golden/dream")
            for k, v in f.items()
        }
        assert str(p.raw_log) == expected["raw_log"]
        assert str(p.report_log) == expected["report_log"]
        assert str(p.otel_log) == expected["otel_log"]
        assert str(p.events_log) == expected["events_log"]
        assert str(p.stderr_log) == expected["stderr_log"]
        assert str(p.err_log) == expected["err_log_claude"]
        assert str(p.prompt_file) == expected["prompt_file"]
        assert str(p.main_log) == expected["main_log"]

    @pytest.mark.parametrize("provider", ["agy", "codex", "claude"])
    def test_runner_argv(self, provider: str) -> None:
        p = _paths()
        got = phase.runner_argv(
            provider,
            phase="scan",
            project_key="brain-v42",
            model="gpt-5.6-luna",
            reasoning="high",
            timeout_minutes=5,
            max_turns=30,
            paths=p,
            executable="/golden/bin/" + provider,
        )
        subst = {
            "$name": "scan",
            "$PROJECT_KEY": "brain-v42",
            "$model": "gpt-5.6-luna",
            "$reasoning_effort": "high",
            "$(( timeout * 60 ))": "300",
            "$max_turns": "30",
            "$events_log": str(p.events_log),
            "$report_log": str(p.report_log),
            "$stderr_log": str(p.stderr_log),
            "$raw_log": str(p.raw_log),
            "$BRAIN_DREAM_AGY_BIN": "/golden/bin/agy",
            "$BRAIN_DREAM_CODEX_BIN": "/golden/bin/codex",
            "$BRAIN_DREAM_CLAUDE_BIN": "/golden/bin/claude",
        }
        expected = [subst.get(tok, tok) for tok in ARGV["runner"][provider]]
        assert got == expected
        # the runner now lives in the package; bash used scripts.dream.<runner>, whose main() moves
        assert phase.runner_module(provider) == ARGV["runner_module"][provider].replace(
            "scripts.dream.", "brain_v42.agents.providers."
        ).replace("_runner", "")

    @pytest.mark.parametrize("provider", ["agy", "codex", "claude"])
    def test_parser_argv(self, provider: str) -> None:
        p = _paths()
        got = phase.parser_argv(
            provider,
            phase="scan",
            model="gpt-5.6-luna",
            timestamp="2026-09-12",
            status="done",
            duration=42,
            project_key="brain-v42",
            effective_dry_run="false",
            scan_log=p.stderr_log,
            paths=p,
        )
        subst = {
            "$name": "scan",
            "$model": "gpt-5.6-luna",
            "$TIMESTAMP": "2026-09-12",
            "$status": "done",
            "$duration": "42",
            "$PROJECT_KEY": "brain-v42",
            "$effective_dry_run": "false",
            "$scan_log": str(p.stderr_log),
            "$report_log": str(p.report_log),
            "$events_log": str(p.events_log),
            "$otel_log": str(p.otel_log),
        }
        expected = [
            subst.get(tok, tok)
            for tok in ARGV["parser_base"]
            + ARGV["parser_raw_log_option"]
            + ARGV["parser_tail"][provider]
        ]
        assert got == expected
        assert phase.parser_module(provider) == ARGV["parser_module"][provider]

    def test_parser_argv_without_scan_log(self) -> None:
        p = _paths()
        got = phase.parser_argv(
            "codex",
            phase="scan",
            model="m",
            timestamp="2026-09-12",
            status="fail",
            duration=1,
            project_key="brain-v42",
            effective_dry_run="true",
            scan_log=None,
            paths=p,
        )
        assert "--raw-log" not in got

    def test_otel_split_argv(self) -> None:
        p = _paths()
        assert phase.otel_split_argv(p) == [
            str(p.raw_log),
            "--report",
            str(p.report_log),
            "--otel",
            str(p.otel_log),
        ]
        assert phase.OTEL_SPLIT_MODULE == ARGV["otel_split"]["module"]


class TestPhaseDataAndRules:
    def test_phase_deps_equal_the_bash_map(self) -> None:
        assert {k: list(v) for k, v in phase.PHASE_DEPS.items()} == PHASES["phase_deps"]

    @pytest.mark.parametrize(
        "row",
        PHASES["dry_run_derivation"],
        ids=lambda r: f"{r['phase']}-{r['dry_run']}-{r['reorg_dry_run']}",
    )
    def test_dry_run_derivation(self, row: dict[str, str | None]) -> None:
        effective, killswitch = phase.effective_dry_run(
            row["phase"], row["dry_run"], row["reorg_dry_run"]
        )
        assert effective == row["effective"]
        assert killswitch == row["killswitch_line"]

    @pytest.mark.parametrize(
        "code,status,rc",
        [(0, "done", 0), (124, "timeout", 2), (3, "fail", 3), (1, "fail", 1), (7, "fail", 1)],
    )
    def test_exit_code_mapping(self, code: int, status: str, rc: int) -> None:
        assert phase.map_exit_code(code) == (status, rc)


class TestChainSemantics:
    def _run(self, providers: list[str], codes: dict[str, int]) -> tuple[object, list[str]]:
        logged: list[str] = []
        result = chain.run_chain(
            providers,
            run_one=lambda provider: codes[provider],
            log=logged.append,
            project_key="brain-v42",
            phase="synth",
        )
        return result, logged

    def test_a_provable_no_write_failure_falls_back_to_the_next_provider(self) -> None:
        result, logged = self._run(["codex", "agy", "claude"], {"codex": 3, "agy": 0})
        assert (result.provider, result.rc, list(result.fallbacks)) == ("agy", 0, ["codex"])
        assert logged == [lines.fallback_line("brain-v42", "synth", "codex", "agy")]

    def test_an_ordinary_failure_never_falls_back(self) -> None:
        result, logged = self._run(["codex", "agy"], {"codex": 1})
        assert (result.provider, result.rc, list(result.fallbacks), logged) == ("codex", 1, [], [])

    def test_a_timeout_never_falls_back(self) -> None:
        result, logged = self._run(["codex", "agy"], {"codex": 2})
        assert (result.provider, result.rc, list(result.fallbacks), logged) == ("codex", 2, [], [])

    def test_a_failing_chain_still_reaches_the_end_of_the_night(self) -> None:
        result, logged = self._run(["codex", "agy", "claude"], {"codex": 3, "agy": 3, "claude": 3})
        assert (result.provider, result.rc, list(result.fallbacks)) == (
            "claude",
            1,
            ["codex", "agy"],
        )
        assert logged[-1] == lines.fallback_end_line("brain-v42", "synth", "claude")

    def test_a_single_provider_chain_ending_on_fallback_code_returns_one(self) -> None:
        result, logged = self._run(["codex"], {"codex": 3})
        assert (result.rc, list(result.fallbacks)) == (1, [])
        assert logged == [lines.fallback_end_line("brain-v42", "synth", "codex")]
