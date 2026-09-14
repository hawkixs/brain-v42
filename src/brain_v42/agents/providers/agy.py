"""The Dream's ``agy`` rail: a phase-scoped profile over the runtime.

Since Brain ticket b2a2d1a5 the command line, the ephemeral HOME, the guard
probe and the exit-code discipline live in
:mod:`headless_agents.providers.agy` and :mod:`headless_agents.sandbox`. What
stays here is what only the Dream knows: the ``(project, phase)`` bearer
written into the HOME, the phase's Brain tool allowlist, the versioned guard
under ``scripts/dream/agy_tool_guard.sh`` (named by
``BRAIN_DREAM_AGY_GUARD_PATH``), the ``BRAIN_DREAM_CAPABILITY_ENFORCEMENT``
requirement, and the argv contract ``dream.sh`` invokes through
``python -m brain_v42.agents.providers.agy``. Every public name, signature
and exit code is unchanged from lot 1/2; the golden fixtures pin the argv
and the HOME's files byte for byte.

TWO PROTECTIONS, TWO PERIMETERS, never to be confused:
- ``agy_tool_guard.sh``, wired as a ``PreToolUse`` hook, protects the MACHINE;
- the ``(project, phase)`` bearer protects the CORPUS, and the server is what
  enforces it.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # noqa: F401  (re-exported: tests monkeypatch runner.subprocess.Popen)
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    DreamCapabilityConfigurationError,
)
from headless_agents.providers import agy as _runtime
from headless_agents.providers.agy import (
    MAX_PROMPT_BYTES as _MAX_PROMPT_BYTES,
)
from headless_agents.providers.agy import (
    extract_report as extract_report,
)
from headless_agents.providers.agy import (
    guard_denies_machine_tools as guard_denies_machine_tools,
)
from headless_agents.providers.agy import (
    tool_call_completed as brain_tool_call_completed,
)

from ..capability import (
    CAPABILITY_CONFIGURATION_ERROR,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    capability_enforcement_enabled,
    preflight_capabilities,
    terminate_process_group,
)
from ..result import RunResult
from ..sandbox import build_ephemeral_home, dream_agy_home_name, dream_agy_profile, ephemeral_root
from ..spec import RunSpec

PHASE_TOOL_ALLOWLISTS = DREAM_PHASE_TOOL_ALLOWLISTS

__all__ = [
    "PHASE_TOOL_ALLOWLISTS",
    "PROVIDER_FALLBACK_EXIT_CODE",
    "TIMEOUT_EXIT_CODE",
    "AgyProvider",
    "brain_tool_call_completed",
    "build_agy_command",
    "extract_report",
    "guard_denies_machine_tools",
    "main",
    "run_agy",
    "terminate_process_group",
]


def build_agy_command(
    *,
    model: str,
    prompt: str,
    agy_executable: str = "agy",
    timeout_seconds: float = 300.0,
) -> list[str]:
    """The headless command line of one phase. THE PROMPT GOES IN ARGV: agy
    ignores stdin (measured 2026-08-11) -- see the runtime's docstring."""
    try:
        return _runtime.build_agy_command(
            model=model, prompt=prompt, executable=agy_executable, timeout_seconds=timeout_seconds
        )
    except ValueError:
        prompt_bytes = len(prompt.encode("utf-8"))
        raise ValueError(
            f"prompt trop long pour argv : {prompt_bytes} octets > {_MAX_PROMPT_BYTES}"
        ) from None


def run_agy(
    *,
    prompt: str,
    phase: str,
    project_key: str,
    model: str,
    timeout_seconds: float,
    events_log: Path,
    report_log: Path,
    stderr_log: Path,
    guard_path: Path,
    agy_executable: str = "agy",
) -> int:
    """Run a phase and return its code (``124`` on deadline, ``3`` if replayable)."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    for path in (events_log, report_log, stderr_log):
        path.parent.mkdir(parents=True, exist_ok=True)
    report_log.write_text("", encoding="utf-8")

    # Fail-closed BEFORE launching anything: without a proven guard, an agy
    # phase would have a free shell. Refusing to start is the only safe
    # choice, and it is logged.
    if not guard_denies_machine_tools(guard_path):
        stderr_log.write_text(
            "garde d'outils agy absente ou permissive — phase refusée\n", encoding="utf-8"
        )
        return 1

    if not capability_enforcement_enabled(os.environ):
        stderr_log.write_text(
            "le rail agy exige BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true\n", encoding="utf-8"
        )
        return 1

    try:
        profile = dream_agy_profile(
            phase=phase, project_key=project_key, environ=os.environ, guard_path=guard_path
        )
    except DreamCapabilityConfigurationError:
        stderr_log.write_text(f"{CAPABILITY_CONFIGURATION_ERROR}\n", encoding="utf-8")
        return 1

    try:
        build_agy_command(
            model=model,
            prompt=prompt,
            agy_executable=agy_executable,
            timeout_seconds=timeout_seconds,
        )
    except ValueError as exc:
        stderr_log.write_text(f"{exc}\n", encoding="utf-8")
        return PROVIDER_FALLBACK_EXIT_CODE

    real_home = Path(os.environ.get("HOME", str(Path.home())))
    return _runtime.run_agy(
        prompt=prompt,
        name=dream_agy_home_name(project_key, phase),
        model=model,
        timeout_seconds=timeout_seconds,
        events_log=events_log,
        report_log=report_log,
        stderr_log=stderr_log,
        profile=profile,
        real_home=real_home,
        environment=os.environ,
        executable=agy_executable,
        ephemeral_root=ephemeral_root(os.environ),
    )


class AgyProvider:
    """:class:`~brain_v42.agents.protocol.AgentProvider` adapter over agy.

    ``guard_path`` is required: this package ships no guard of its own.
    Callers that need the nightly Dream guard pass
    ``scripts/dream/agy_tool_guard.sh``.
    """

    name = "agy"

    def __init__(self, *, guard_path: Path) -> None:
        self._guard_path = guard_path

    def build_command(self, spec: RunSpec) -> list[str]:
        return build_agy_command(
            model=spec.model,
            prompt=spec.prompt,
            agy_executable=spec.executable or "agy",
            timeout_seconds=spec.timeout_seconds,
        )

    def child_environment(self, spec: RunSpec, environ: Mapping[str, str]) -> dict[str, str] | None:
        # agy's own child environment is not the capability-scoped allowlist:
        # the bearer travels through the ephemeral HOME's mcp_config.json
        # instead.
        return None

    def prepare_home(self, spec: RunSpec) -> Path | None:
        assert spec.project_key is not None
        real_home = Path(os.environ.get("HOME", str(Path.home())))
        root = ephemeral_root(os.environ) or Path(tempfile.gettempdir())
        return build_ephemeral_home(
            root=root,
            phase=spec.phase,
            project_key=spec.project_key,
            environ=os.environ,
            real_home=real_home,
            guard_path=self._guard_path,
            mcp_url=spec.mcp_url,
        )

    def tool_call_completed(self, events_log: Path) -> bool:
        return brain_tool_call_completed(events_log)

    def run(self, spec: RunSpec) -> RunResult:
        assert spec.project_key is not None
        assert spec.events_log is not None
        assert spec.report_log is not None
        assert spec.stderr_log is not None
        start = time.monotonic()
        exit_code = run_agy(
            prompt=spec.prompt,
            phase=spec.phase,
            project_key=spec.project_key,
            model=spec.model,
            timeout_seconds=spec.timeout_seconds,
            events_log=spec.events_log,
            report_log=spec.report_log,
            stderr_log=spec.stderr_log,
            guard_path=self._guard_path,
            agy_executable=spec.executable or "agy",
        )
        duration = time.monotonic() - start
        return RunResult(
            exit_code=exit_code,
            provider=self.name,
            model=spec.model,
            report_path=spec.report_log,
            events_log=spec.events_log,
            tokens=None,
            duration_seconds=duration,
            tool_call_completed=brain_tool_call_completed(spec.events_log),
        )


# --- CLI entry point --------------------------------------------------------
#
# Moved from scripts/dream/agy_runner.py (lot 2 of the agent runtime
# extraction, Brain ticket afd56820). The shim there keeps its own GUARD_PATH
# and wrapper functions for its existing tests, which call them in-process.
# This CLI is what `brain_v42.agents.phase.run_phase` invokes as a subprocess
# (`python -m brain_v42.agents.providers.agy`), so it must resolve the guard
# on its own -- WITHOUT importing `scripts.dream`.


def _default_guard_path() -> Path:
    """The tool guard the CLI wires into the ephemeral HOME.

    Only ``BRAIN_DREAM_AGY_GUARD_PATH`` names it. The working directory is
    NEVER consulted: the dream unit runs with the mutable repository as cwd
    while the code lives in an immutable release tree, so a cwd-relative guess
    would enforce whatever branch the checkout happens to be on.
    """
    override = os.environ.get("BRAIN_DREAM_AGY_GUARD_PATH")
    if override:
        return Path(override)
    raise SystemExit(
        "BRAIN_DREAM_AGY_GUARD_PATH is not set: the agy entry point does not "
        "guess its tool guard from the working directory"
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jouer une phase de Dream avec agy")
    parser.add_argument("--preflight-capabilities", action="store_true")
    parser.add_argument("--project-key")
    parser.add_argument("--phase", choices=tuple(PHASE_TOOL_ALLOWLISTS))
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--events-log", type=Path)
    parser.add_argument("--report-log", type=Path)
    parser.add_argument("--stderr-log", type=Path)
    parser.add_argument("--agy-executable", default=os.environ.get("BRAIN_DREAM_AGY_BIN", "agy"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    guard_path = _default_guard_path()

    if args.preflight_capabilities:
        if args.project_key is None:
            parser.error("--project-key est requis avec --preflight-capabilities")
        if not guard_denies_machine_tools(guard_path):
            print("garde d'outils agy absente ou permissive", file=sys.stderr)
            return 1
        try:
            preflight_capabilities(args.project_key, os.environ)
        except DreamCapabilityConfigurationError:
            print(CAPABILITY_CONFIGURATION_ERROR, file=sys.stderr)
            return 1
        return 0

    required = {
        "--phase": args.phase,
        "--project-key": args.project_key,
        "--timeout-seconds": args.timeout_seconds,
        "--events-log": args.events_log,
        "--report-log": args.report_log,
        "--stderr-log": args.stderr_log,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        parser.error(f"arguments requis manquants : {', '.join(missing)}")

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("prompt de phase agy vide", file=sys.stderr)
        return 1
    return run_agy(
        prompt=prompt,
        phase=args.phase,
        project_key=args.project_key,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        events_log=args.events_log,
        report_log=args.report_log,
        stderr_log=args.stderr_log,
        guard_path=guard_path,
        agy_executable=args.agy_executable,
    )


if __name__ == "__main__":
    raise SystemExit(main())
