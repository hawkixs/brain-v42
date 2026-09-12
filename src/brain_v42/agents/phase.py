"""One Dream phase, across one provider -- the Python replacement of
``scripts/dream.sh``'s ``run_phase`` (lot 2 of the agent runtime extraction,
Brain ticket ``afd56820``).

``run_phase_chain`` (the loop over the provider chain) stays a separate layer
-- see :mod:`brain_v42.agents.chain` and :mod:`brain_v42.agents.run_phase_chain`
-- exactly as bash kept ``run_phase`` and ``run_phase_chain`` as two functions.

The argv and paths here are quoted verbatim from ``scripts/dream.sh`` at
``c0e48966`` (see ``tests/fixtures/agents_chain_golden/argv.json`` and
``phases.json``); the golden test in
``tests/unit/agents/test_chain_golden.py`` pins them byte for byte.

``BRAIN_AGENTS_SUBPROCESS_PYTHON`` is a TEST SEAM, read once per
:func:`run_phase` call: the interpreter used to launch the runner, parser and
``otel_split`` subprocesses defaults to ``sys.executable`` and is never set by
the nightly Dream unit -- it exists so a test can point those subprocess
launches at a fake dispatcher instead of a real venv interpreter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import lines
from .capability import PROVIDER_FALLBACK_EXIT_CODE
from .prompt import render_file as _render_prompt_file

# Phase dependencies: which previous phase logs to inject -- the exact map
# from bash's `declare -A PHASE_DEPS` (scripts/dream.sh).
PHASE_DEPS: dict[str, tuple[str, ...]] = {
    "scan": (),
    "clean": ("scan",),
    "connect": ("clean",),
    "synth": ("connect",),
    "promote": ("synth",),
    "reorg": ("scan", "synth"),
}

OTEL_SPLIT_MODULE = "brain_v42.metrics.otel_split"

_RUNNER_MODULE_SUFFIX = {"agy": "agy", "codex": "codex", "claude": "claude"}
_PARSER_MODULE = {
    "agy": "brain_v42.metrics.agy_dream_parser",
    "codex": "brain_v42.metrics.codex_dream_parser",
    "claude": "brain_v42.metrics.dream_parser",
}


class UnsupportedTierError(Exception):
    """Raised when ``provider:model_tier`` matches no case in bash's switch."""

    def __init__(self, provider: str, model_tier: str) -> None:
        self.provider = provider
        self.model_tier = model_tier
        super().__init__(f"unsupported provider/model tier: {provider}/{model_tier}")


@dataclass(frozen=True)
class PhasePaths:
    """The eight paths bash's ``run_phase`` derives from
    ``(log_dir, timestamp, project_key, phase, dream_dir)``."""

    raw_log: Path
    report_log: Path
    otel_log: Path
    events_log: Path
    stderr_log: Path
    err_log: Path
    prompt_file: Path
    main_log: Path

    @classmethod
    def build(
        cls,
        *,
        log_dir: Path,
        timestamp: str,
        project_key: str,
        phase: str,
        dream_dir: Path,
    ) -> PhasePaths:
        prefix = f"{timestamp}_{project_key}_{phase}"
        return cls(
            raw_log=log_dir / f"{prefix}.raw.log",
            report_log=log_dir / f"{prefix}.log",
            otel_log=log_dir / f"{prefix}.otel.log",
            events_log=log_dir / f"{prefix}.events.jsonl",
            stderr_log=log_dir / f"{prefix}.stderr.log",
            err_log=log_dir / f"{prefix}.err.log",
            prompt_file=dream_dir / f"phase_{phase}.md",
            main_log=log_dir / f"{timestamp}.log",
        )


def runner_module(provider: str) -> str:
    return f"brain_v42.agents.providers.{_RUNNER_MODULE_SUFFIX[provider]}"


def parser_module(provider: str) -> str:
    return _PARSER_MODULE[provider]


def runner_argv(
    provider: str,
    *,
    phase: str,
    project_key: str,
    model: str,
    reasoning: str | None,
    timeout_minutes: float,
    max_turns: int,
    paths: PhasePaths,
    executable: str,
) -> list[str]:
    timeout_seconds = str(int(timeout_minutes * 60))
    if provider == "agy":
        return [
            "--phase",
            phase,
            "--project-key",
            project_key,
            "--model",
            model,
            "--timeout-seconds",
            timeout_seconds,
            "--events-log",
            str(paths.events_log),
            "--report-log",
            str(paths.report_log),
            "--stderr-log",
            str(paths.stderr_log),
            "--agy-executable",
            executable,
        ]
    if provider == "codex":
        return [
            "--phase",
            phase,
            "--project-key",
            project_key,
            "--model",
            model,
            "--reasoning-effort",
            reasoning or "",
            "--timeout-seconds",
            timeout_seconds,
            "--report-log",
            str(paths.report_log),
            "--events-log",
            str(paths.events_log),
            "--stderr-log",
            str(paths.stderr_log),
            "--codex-executable",
            executable,
        ]
    if provider == "claude":
        return [
            "--phase",
            phase,
            "--project-key",
            project_key,
            "--model",
            model,
            "--max-turns",
            str(max_turns),
            "--timeout-seconds",
            timeout_seconds,
            "--raw-log",
            str(paths.raw_log),
            "--claude-executable",
            executable,
        ]
    raise ValueError(f"unknown provider: {provider}")


def parser_argv(
    provider: str,
    *,
    phase: str,
    model: str,
    timestamp: str,
    status: str,
    duration: int,
    project_key: str,
    effective_dry_run: str,
    scan_log: Path | None,
    paths: PhasePaths,
) -> list[str]:
    argv = [
        "--phase",
        phase,
        "--model",
        model,
        "--date",
        timestamp,
        "--status",
        status,
        "--duration",
        str(duration),
        "--project-key",
        project_key,
        "--phase-dry-run",
        effective_dry_run,
    ]
    if scan_log is not None:
        argv += ["--raw-log", str(scan_log)]
    if provider in ("agy", "codex"):
        argv += ["--report-log", str(paths.report_log), str(paths.events_log)]
    elif provider == "claude":
        argv += [str(paths.otel_log)]
    else:
        raise ValueError(f"unknown provider: {provider}")
    return argv


def otel_split_argv(paths: PhasePaths) -> list[str]:
    return [
        str(paths.raw_log),
        "--report",
        str(paths.report_log),
        "--otel",
        str(paths.otel_log),
    ]


def effective_dry_run(
    phase: str, dry_run: str, reorg_dry_run: str | None
) -> tuple[str, str | None]:
    """Bash's per-phase DRY_RUN override -- ``dream_wants_wet`` semantics.

    Only ``reorg`` consults ``reorg_dry_run``; every other phase returns
    ``dry_run`` unchanged. ``dream_wants_wet`` authorises WET only for the
    literal value ``"false"``; anything else (including ``"true"``) stays
    DRY, and any value that is neither ``"true"`` nor ``"false"`` also logs
    the KILLSWITCH line.
    """
    if phase != "reorg":
        return dry_run, None
    value = reorg_dry_run if reorg_dry_run is not None else ""
    if value == "false":
        return dry_run, None
    if value == "true":
        return "true", None
    return "true", lines.killswitch_line("BRAIN_DREAM_REORG_DRY_RUN", value)


def map_exit_code(code: int) -> tuple[str, int]:
    if code == 0:
        return "done", 0
    if code == 124:
        return "timeout", 2
    if code == PROVIDER_FALLBACK_EXIT_CODE:
        return "fail", PROVIDER_FALLBACK_EXIT_CODE
    return "fail", 1


def status_for_rc(rc: int) -> str:
    """The chain-level status for the ``run_phase_chain`` result JSON.

    Unlike :func:`map_exit_code` (which classifies a raw process exit code),
    this classifies the phase-level rc that :func:`brain_v42.agents.chain.run_chain`
    returns -- always one of 0 (done), 1 (fail) or 2 (timeout).
    """
    return {0: "done", 2: "timeout"}.get(rc, "fail")


def make_logger(main_log: Path) -> Callable[[str], None]:
    """The Python ``log()``: prefix with ``[HH:MM:SS]``, print, append to
    ``main_log`` -- bash's ``log() { echo "[$(date +%H:%M:%S)] $*" | tee -a
    "$LOG_DIR/$TIMESTAMP.log"; }``."""

    def _log(line: str) -> None:
        rendered = lines.prefixed(line, datetime.now().time())
        print(rendered, flush=True)
        with main_log.open("a", encoding="utf-8") as fh:
            fh.write(rendered + "\n")

    return _log


def _python_executable() -> str:
    """The interpreter that launches runner/parser/otel_split subprocesses.

    ``BRAIN_AGENTS_SUBPROCESS_PYTHON`` is a test seam only -- see the module
    docstring. Production never sets it, so this is ``sys.executable``.
    """
    return os.environ.get("BRAIN_AGENTS_SUBPROCESS_PYTHON", sys.executable)


def spawn(
    argv: Sequence[str],
    *,
    input: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """The single seam that launches a runner/parser/otel_split subprocess.

    Combined stdout+stderr is always captured as text so post-processing can
    decide whether to replay it (``tee``, for the parsers) or only file it
    (a plain append, for ``otel_split``). ``env`` replaces the inherited
    environment when given (the runner launch adds the agy guard path to it).
    Tests monkeypatch this function.
    """
    return subprocess.run(
        list(argv),
        input=input,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        env=dict(env) if env is not None else None,
    )


def _select_model(
    provider: str, model_tier: str, environ: Mapping[str, str]
) -> tuple[str, str | None]:
    if provider == "codex":
        if model_tier == "fast":
            return (
                environ.get("BRAIN_DREAM_CODEX_FAST_MODEL", ""),
                environ.get("BRAIN_DREAM_CODEX_FAST_REASONING", ""),
            )
        if model_tier == "deep":
            return (
                environ.get("BRAIN_DREAM_CODEX_DEEP_MODEL", ""),
                environ.get("BRAIN_DREAM_CODEX_DEEP_REASONING", ""),
            )
    elif provider == "agy":
        if model_tier == "fast":
            return environ.get("BRAIN_DREAM_AGY_FAST_MODEL", ""), None
        if model_tier == "deep":
            return environ.get("BRAIN_DREAM_AGY_DEEP_MODEL", ""), None
    elif provider == "claude":
        if model_tier == "fast":
            return "sonnet", None
        if model_tier == "deep":
            return "opus", None
    raise UnsupportedTierError(provider, model_tier)


def _inject_dependency_reports(
    prompt: str, phase: str, log_dir: Path, timestamp: str, project_key: str
) -> str:
    dep_section = ""
    for dep in PHASE_DEPS.get(phase, ()):
        dep_log = log_dir / f"{timestamp}_{project_key}_{dep}.log"
        if dep_log.is_file() and dep_log.stat().st_size > 0:
            content = dep_log.read_text(encoding="utf-8").rstrip("\n")
            dep_section += f"\n### {dep.upper()} phase output\n{content}\n"
    if not dep_section:
        return prompt
    return (
        "## Previous Phase Reports (reference context — do not mimic style)\n"
        "The orchestrator has injected the output from dependency phases below.\n"
        f"{dep_section}\n\n---\n\n{prompt}"
    )


def _append_to_main_log(main_log: Path, text: str) -> None:
    if not text:
        return
    with main_log.open("a", encoding="utf-8") as fh:
        fh.write(text)


def _tee_to_main_log(main_log: Path, text: str) -> None:
    if not text:
        return
    print(text, end="", flush=True)
    _append_to_main_log(main_log, text)


def _postprocess(
    provider: str,
    status: str,
    phase: str,
    paths: PhasePaths,
    log: Callable[[str], None],
) -> Path | None:
    if provider == "claude":
        err_log: Path | None = None
        if status != "done" and paths.raw_log.exists():
            err_log = paths.err_log
            shutil.copyfile(paths.raw_log, err_log)

        otel_result = spawn(
            [_python_executable(), "-m", OTEL_SPLIT_MODULE, *otel_split_argv(paths)]
        )
        _append_to_main_log(paths.main_log, otel_result.stdout or "")
        if otel_result.returncode == 0:
            paths.raw_log.unlink(missing_ok=True)
        else:
            log(lines.otel_warn_line(phase))
            if paths.raw_log.exists():
                shutil.copyfile(paths.raw_log, paths.report_log)
            paths.otel_log.write_text("", encoding="utf-8")
        return err_log

    # codex / agy: the runner already separated the streams. Always leave a
    # readable report path for dependency injection and validators, including
    # failed phases.
    if not paths.report_log.exists():
        paths.report_log.write_text("", encoding="utf-8")
    return paths.stderr_log


def run_phase(
    provider: str,
    *,
    phase: str,
    model_tier: str,
    timeout_minutes: int,
    max_turns: int,
    project_key: str,
    timestamp: str,
    log_dir: Path,
    dream_dir: Path,
    dry_run: str,
    reorg_dry_run: str,
    environ: Mapping[str, str],
    log: Callable[[str], None],
) -> int:
    """Run one phase with one provider. Returns the phase rc (0/1/2/3), the
    exact contract bash's ``run_phase`` returned to ``run_phase_chain``."""
    paths = PhasePaths.build(
        log_dir=log_dir,
        timestamp=timestamp,
        project_key=project_key,
        phase=phase,
        dream_dir=dream_dir,
    )

    try:
        model, reasoning = _select_model(provider, model_tier, environ)
    except UnsupportedTierError:
        log(lines.unsupported_tier_line(phase, provider, model_tier))
        return 1

    if not paths.prompt_file.is_file():
        log(lines.skip_missing_prompt_line(phase, paths.prompt_file))
        return 0

    effective, killswitch = effective_dry_run(phase, dry_run, reorg_dry_run)
    if killswitch:
        log(killswitch)

    prompt = _render_prompt_file(
        paths.prompt_file,
        project_key=project_key,
        date=timestamp,
        dry_run=effective,
        candidate_pool_json=environ.get("PROMOTE_CANDIDATE_POOL_JSON") or "[]",
        recent_promotions_json=environ.get("PROMOTE_RECENT_PROMOTIONS_JSON") or "[]",
    )
    # bash captured the renderer through $(...), which strips every trailing
    # newline: no prompt the night ever sent ended with one.
    prompt = prompt.rstrip("\n")
    prompt = _inject_dependency_reports(prompt, phase, log_dir, timestamp, project_key)

    # bash expanded an unset variable to "" and let the runner refuse it.
    executable = environ.get(f"BRAIN_DREAM_{provider.upper()}_BIN", "")
    log(
        lines.start_line(
            provider,
            phase,
            model,
            timeout_minutes,
            reasoning=reasoning,
            max_turns=max_turns,
        )
    )

    argv = [
        _python_executable(),
        "-m",
        runner_module(provider),
        *runner_argv(
            provider,
            phase=phase,
            project_key=project_key,
            model=model,
            reasoning=reasoning,
            timeout_minutes=timeout_minutes,
            max_turns=max_turns,
            paths=paths,
            executable=executable,
        ),
    ]
    # The agy entry point takes its tool guard from this variable, never from
    # the working directory: the dream unit runs with the mutable repository
    # as cwd while the code and the guard live in the immutable release tree,
    # which ``dream_dir`` (``$DREAM_DIR``) points at.
    runner_env = {
        **os.environ,
        "BRAIN_DREAM_AGY_GUARD_PATH": str(paths.prompt_file.parent / "agy_tool_guard.sh"),
    }
    phase_start = time.monotonic()
    result = spawn(argv, input=prompt, env=runner_env)
    code = result.returncode

    status, phase_rc = map_exit_code(code)
    if status == "done":
        log(lines.done_line(phase))
    elif status == "timeout":
        log(lines.timeout_line(phase, timeout_minutes))
    else:
        log(lines.fail_line(phase, code, fallback=(code == PROVIDER_FALLBACK_EXIT_CODE)))
    duration = int(time.monotonic() - phase_start)

    err_log = _postprocess(provider, status, phase, paths, log)

    scan_log = err_log
    if scan_log is None and paths.report_log.exists():
        scan_log = paths.report_log

    parser_result = spawn(
        [
            _python_executable(),
            "-m",
            parser_module(provider),
            *parser_argv(
                provider,
                phase=phase,
                model=model,
                timestamp=timestamp,
                status=status,
                duration=duration,
                project_key=project_key,
                effective_dry_run=effective,
                scan_log=scan_log,
                paths=paths,
            ),
        ]
    )
    _tee_to_main_log(paths.main_log, parser_result.stdout or "")
    if parser_result.returncode != 0:
        log(lines.parser_warn_line(provider, phase))

    return phase_rc
