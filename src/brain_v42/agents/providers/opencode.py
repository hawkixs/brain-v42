"""The Dream's ``opencode`` rail: a phase-scoped profile over the runtime.

The command line, the inline config, the ephemeral HOME, the borrowed
runtime cache and the exit-code discipline live in
:mod:`headless_agents.providers.opencode`. What stays here is what only the
Dream knows: the ``(project, phase)`` bearer scoped into the child
environment under ``MCP_HTTP_TOKEN``, the phase's Brain tool allowlist, the
``dream-opencode-<phase>`` actor header, and the argv contract
``brain_v42.agents.phase`` invokes through
``python -m brain_v42.agents.providers.opencode``.

Unlike agy, this rail does NOT require ``BRAIN_DREAM_CAPABILITY_ENFORCEMENT``:
its tool wall is rendered into the inline config on every run, enforcement or
not, so a phase never sets off with a shell. Without enforcement it inherits
the ambient bearer, like codex -- the documented rollback path.

The runtime cache check (``~/.config/opencode/node_modules`` in the real
HOME) is part of ``--preflight-capabilities`` so a host that would send every
phase to npm drops the link before the night starts, not at the first phase.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # noqa: F401  (re-exported: tests monkeypatch rail.subprocess.Popen)
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    DreamCapabilityConfigurationError,
)
from headless_agents.providers import opencode as _runtime
from headless_agents.providers.opencode import (
    RUNTIME_CACHE_PATHS as RUNTIME_CACHE_PATHS,
)
from headless_agents.providers.opencode import (
    extract_report as extract_report,
)
from headless_agents.providers.opencode import (
    runtime_cache_present as runtime_cache_present,
)

from ..capability import (
    BRAIN_MCP_SERVER_NAME,
    CAPABILITY_CONFIGURATION_ERROR,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    build_child_environment,
    preflight_capabilities,
    terminate_process_group,
)
from ..result import RunResult
from ..sandbox import dream_opencode_home_name, dream_opencode_profile, ephemeral_root
from ..spec import RunSpec

PHASE_TOOL_ALLOWLISTS = DREAM_PHASE_TOOL_ALLOWLISTS

# The fail-closed line for a run that completed without touching the Brain,
# in the words the codex rail's stderr has always used.
_MISSING_BRAIN_CALL_MESSAGE = "opencode completed with no completed Brain MCP tool call"

__all__ = [
    "PHASE_TOOL_ALLOWLISTS",
    "PROVIDER_FALLBACK_EXIT_CODE",
    "RUNTIME_CACHE_PATHS",
    "TIMEOUT_EXIT_CODE",
    "OpenCodeProvider",
    "brain_tool_call_completed",
    "extract_report",
    "main",
    "run_opencode",
    "runtime_cache_present",
    "terminate_process_group",
]


def brain_tool_call_completed(events_log: Path) -> bool:
    """Did a Brain tool call SUCCEED anywhere in this event stream?"""
    return _runtime.tool_call_completed(events_log, server=BRAIN_MCP_SERVER_NAME)


def _real_home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def _runtime_cache_error() -> str:
    return (
        "opencode runtime cache absent from the real HOME "
        f"({', '.join(RUNTIME_CACHE_PATHS)}): the rail refuses to send a phase to npm; "
        "run opencode once by hand to seed it"
    )


def run_opencode(
    *,
    prompt: str,
    phase: str,
    project_key: str,
    model: str,
    variant: str | None,
    timeout_seconds: float,
    events_log: Path,
    report_log: Path,
    stderr_log: Path,
    opencode_executable: str = "opencode",
) -> int:
    """Run a phase and return its code (``124`` on deadline, ``3`` if replayable)."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    for path in (events_log, report_log, stderr_log):
        path.parent.mkdir(parents=True, exist_ok=True)
    report_log.write_text("", encoding="utf-8")

    real_home = _real_home()
    # Fail-closed BEFORE launching anything, with the Dream's own line: the
    # runtime would refuse too, but this is the wording the night's logs read.
    if not runtime_cache_present(real_home):
        stderr_log.write_text(f"{_runtime_cache_error()}\n", encoding="utf-8")
        return 1

    try:
        child_environment = build_child_environment(
            project_key=project_key, phase=phase, environ=os.environ
        )
        profile = dream_opencode_profile(phase=phase, project_key=project_key, environ=os.environ)
    except DreamCapabilityConfigurationError:
        stderr_log.write_text(f"{CAPABILITY_CONFIGURATION_ERROR}\n", encoding="utf-8")
        return 1

    name = dream_opencode_home_name(project_key, phase)
    return _runtime.run_opencode(
        prompt=prompt,
        name=name,
        model=model,
        timeout_seconds=timeout_seconds,
        events_log=events_log,
        report_log=report_log,
        stderr_log=stderr_log,
        profile=profile,
        real_home=real_home,
        # ``None`` inherits the ambient environment: the rollback path without
        # enforcement, where the ambient bearer is the one the phase uses.
        environment=child_environment if child_environment is not None else os.environ,
        executable=opencode_executable,
        variant=variant,
        title=name,
        ephemeral_root=ephemeral_root(os.environ),
        temp_prefix="brain-v42-dream-",
        missing_call_message=_MISSING_BRAIN_CALL_MESSAGE,
    )


class OpenCodeProvider:
    """:class:`~brain_v42.agents.protocol.AgentProvider` adapter over opencode.

    ``spec.reasoning_effort`` is the opencode ``--variant``.
    """

    name = "opencode"

    def _home(self, spec: RunSpec) -> Path:
        assert spec.project_key is not None
        root = ephemeral_root(os.environ) or Path(tempfile.gettempdir())
        return root / dream_opencode_home_name(spec.project_key, spec.phase)

    def build_command(self, spec: RunSpec) -> list[str]:
        return _runtime.build_opencode_command(
            model=spec.model,
            prompt=spec.prompt,
            home=self._home(spec),
            executable=spec.executable or "opencode",
            variant=spec.reasoning_effort,
            title=self._home(spec).name,
        )

    def child_environment(self, spec: RunSpec, environ: Mapping[str, str]) -> dict[str, str] | None:
        return build_child_environment(
            project_key=spec.project_key, phase=spec.phase, environ=environ
        )

    def prepare_home(self, spec: RunSpec) -> Path | None:
        assert spec.project_key is not None
        profile = dream_opencode_profile(
            phase=spec.phase,
            project_key=spec.project_key,
            environ=os.environ,
            mcp_url=spec.mcp_url,
        )
        return _runtime.build_opencode_home(
            root=self._home(spec).parent,
            name=self._home(spec).name,
            profile=profile,
            real_home=_real_home(),
        )

    def tool_call_completed(self, events_log: Path) -> bool:
        return brain_tool_call_completed(events_log)

    def run(self, spec: RunSpec) -> RunResult:
        assert spec.project_key is not None
        assert spec.events_log is not None
        assert spec.report_log is not None
        assert spec.stderr_log is not None
        start = time.monotonic()
        exit_code = run_opencode(
            prompt=spec.prompt,
            phase=spec.phase,
            project_key=spec.project_key,
            model=spec.model,
            variant=spec.reasoning_effort,
            timeout_seconds=spec.timeout_seconds,
            events_log=spec.events_log,
            report_log=spec.report_log,
            stderr_log=spec.stderr_log,
            opencode_executable=spec.executable or "opencode",
        )
        duration = time.monotonic() - start
        tokens, cost = _runtime.telemetry(spec.events_log)
        return RunResult(
            exit_code=exit_code,
            provider=self.name,
            model=spec.model,
            report_path=spec.report_log,
            events_log=spec.events_log,
            tokens=tokens,
            duration_seconds=duration,
            tool_call_completed=brain_tool_call_completed(spec.events_log),
            cost_usd=cost,
        )


# --- CLI entry point --------------------------------------------------------
#
# What `brain_v42.agents.phase.run_phase` invokes as a subprocess
# (`python -m brain_v42.agents.providers.opencode`), and what `dream.sh`'s
# preflight runs with `--preflight-capabilities`.


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one isolated Dream phase with opencode")
    parser.add_argument("--preflight-capabilities", action="store_true")
    parser.add_argument("--project-key")
    parser.add_argument("--phase", choices=tuple(PHASE_TOOL_ALLOWLISTS))
    parser.add_argument("--model", default="")
    parser.add_argument("--variant", default="")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--events-log", type=Path)
    parser.add_argument("--report-log", type=Path)
    parser.add_argument("--stderr-log", type=Path)
    parser.add_argument(
        "--opencode-executable", default=os.environ.get("BRAIN_DREAM_OPENCODE_BIN", "opencode")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.preflight_capabilities:
        if args.project_key is None:
            parser.error("--project-key is required with --preflight-capabilities")
        if not runtime_cache_present(_real_home()):
            print(_runtime_cache_error(), file=sys.stderr)
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
        parser.error(f"the following arguments are required: {', '.join(missing)}")

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("Dream opencode prompt is empty", file=sys.stderr)
        return 1
    return run_opencode(
        prompt=prompt,
        phase=args.phase,
        project_key=args.project_key,
        model=args.model,
        variant=args.variant or None,
        timeout_seconds=args.timeout_seconds,
        events_log=args.events_log,
        report_log=args.report_log,
        stderr_log=args.stderr_log,
        opencode_executable=args.opencode_executable,
    )


if __name__ == "__main__":
    raise SystemExit(main())
