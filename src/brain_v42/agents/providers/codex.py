"""The Dream's ``codex exec`` rail: a phase-scoped profile over the runtime.

Since Brain ticket b2a2d1a5 the hardened command line, the stream validation
and the exit-code discipline live in :mod:`headless_agents.providers.codex`.
What stays here is what only the Dream knows: which Brain tools a phase may
call, which ``(project, phase)`` bearer scopes it, and the argv contract
``dream.sh`` invokes through ``python -m brain_v42.agents.providers.codex``.
Every public name, signature and exit code of this module is unchanged from
lot 1/2 (tickets c31bad72, afd56820): the golden fixtures under
``tests/fixtures/agents_golden`` pin the argv and child environment byte for
byte, and ``tests/unit/test_dream_codex_runner.py`` keeps passing with its
assertions untouched (one test patches ``TERMINATION_GRACE_SECONDS`` where
``terminate_process_group`` now lives, ``headless_agents.capability``).
"""

from __future__ import annotations

import argparse
import os
import subprocess  # noqa: F401  (re-exported: tests monkeypatch runner.subprocess.Popen)
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    DreamCapabilityConfigurationError,
)
from headless_agents.providers import codex as _runtime
from headless_agents.providers.codex import (
    _DISABLED_FEATURES as _DISABLED_FEATURES,
)
from headless_agents.providers.codex import (
    CHILD_ENV_PASSTHROUGH as _CODEX_CHILD_ENV_EXTRA,
)
from headless_agents.providers.codex import (
    REASONING_EFFORTS as _REASONING_EFFORTS,
)

from ..capability import (
    BRAIN_MCP_SERVER_NAME,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    brain_mcp_server,
    build_child_environment,
    preflight_capabilities,
    terminate_process_group,
)
from ..capability import (
    CAPABILITY_CONFIGURATION_ERROR as _CAPABILITY_CONFIGURATION_ERROR,
)
from ..capability import (
    DEFAULT_MCP_URL as _DEFAULT_MCP_URL,
)
from ..result import RunResult
from ..spec import RunSpec

PHASE_TOOL_ALLOWLISTS = DREAM_PHASE_TOOL_ALLOWLISTS

__all__ = [
    "PHASE_TOOL_ALLOWLISTS",
    "PROVIDER_FALLBACK_EXIT_CODE",
    "TIMEOUT_EXIT_CODE",
    "CodexProvider",
    "brain_tool_call_completed",
    "build_codex_command",
    "main",
    "run_codex",
    "terminate_process_group",
]


def _codex_child_environment(
    *,
    project_key: str | None,
    phase: str,
    environ: Mapping[str, str],
) -> dict[str, str] | None:
    return build_child_environment(
        project_key=project_key,
        phase=phase,
        environ=environ,
        extra_allowlist=_CODEX_CHILD_ENV_EXTRA,
    )


def _preflight_capabilities(project_key: str, environ: Mapping[str, str]) -> None:
    preflight_capabilities(project_key, environ)


def _write_capability_configuration_error(stderr_log: Path) -> None:
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.write_text(f"{_CAPABILITY_CONFIGURATION_ERROR}\n", encoding="utf-8")


def _toml(value: object) -> str:
    return _runtime._toml(value)


def build_codex_command(
    *,
    phase: str,
    model: str,
    reasoning_effort: str,
    report_log: Path,
    workspace: Path,
    codex_executable: str = "codex",
    mcp_url: str | None = None,
) -> list[str]:
    """Build the hardened non-interactive Codex command for one phase."""
    server_url = mcp_url or os.environ.get("BRAIN_DREAM_MCP_URL", _DEFAULT_MCP_URL)
    mcp = brain_mcp_server(agent=f"dream-codex-{phase}", phase=phase, url=server_url)
    return _runtime.build_codex_command(
        model=model,
        reasoning_effort=reasoning_effort,
        report_log=report_log,
        workspace=workspace,
        mcp=mcp,
        executable=codex_executable,
    )


# The fail-closed line the Dream's stderr logs and tests have always carried
# for a turn that completed without touching the Brain.
_MISSING_BRAIN_CALL_MESSAGE = "Codex completed with no completed Brain MCP tool call"


def brain_tool_call_completed(events_log: Path) -> bool:
    """Did a Brain tool call SUCCEED anywhere in this event stream?"""
    return _runtime.tool_call_completed(events_log, server=BRAIN_MCP_SERVER_NAME)


def _event_stream_error(events_log: Path) -> str | None:
    return _runtime.event_stream_error(
        events_log,
        server=BRAIN_MCP_SERVER_NAME,
        missing_call_message=_MISSING_BRAIN_CALL_MESSAGE,
    )


def run_codex(
    *,
    prompt: str,
    phase: str,
    project_key: str | None = None,
    model: str,
    reasoning_effort: str,
    timeout_seconds: float,
    report_log: Path,
    events_log: Path,
    stderr_log: Path,
    codex_executable: str = "codex",
    workspace: Path | None = None,
) -> int:
    """Run one Codex phase and return its exit code (``124`` on timeout)."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    try:
        child_environment = _codex_child_environment(
            project_key=project_key,
            phase=phase,
            environ=os.environ,
        )
    except DreamCapabilityConfigurationError:
        _write_capability_configuration_error(stderr_log)
        return 1
    server_url = os.environ.get("BRAIN_DREAM_MCP_URL", _DEFAULT_MCP_URL)
    mcp = brain_mcp_server(agent=f"dream-codex-{phase}", phase=phase, url=server_url)
    return _runtime.run_codex(
        prompt=prompt,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        report_log=report_log,
        events_log=events_log,
        stderr_log=stderr_log,
        mcp=mcp,
        environment=child_environment,
        executable=codex_executable,
        workspace=workspace,
        temp_prefix=f"brain-v42-dream-{phase}-",
        missing_call_message=_MISSING_BRAIN_CALL_MESSAGE,
    )


class CodexProvider:
    """:class:`~brain_v42.agents.protocol.AgentProvider` adapter over Codex."""

    name = "codex"

    def build_command(self, spec: RunSpec) -> list[str]:
        assert spec.report_log is not None, "RunSpec.report_log is required for Codex"
        return build_codex_command(
            phase=spec.phase,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            report_log=spec.report_log,
            workspace=spec.workspace or Path.cwd(),
            codex_executable=spec.executable or "codex",
            mcp_url=spec.mcp_url,
        )

    def child_environment(self, spec: RunSpec, environ: Mapping[str, str]) -> dict[str, str] | None:
        return _codex_child_environment(
            project_key=spec.project_key, phase=spec.phase, environ=environ
        )

    def prepare_home(self, spec: RunSpec) -> Path | None:
        return None

    def tool_call_completed(self, events_log: Path) -> bool:
        return brain_tool_call_completed(events_log)

    def run(self, spec: RunSpec) -> RunResult:
        assert spec.report_log is not None
        assert spec.events_log is not None
        assert spec.stderr_log is not None
        start = time.monotonic()
        exit_code = run_codex(
            prompt=spec.prompt,
            phase=spec.phase,
            project_key=spec.project_key,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            timeout_seconds=spec.timeout_seconds,
            report_log=spec.report_log,
            events_log=spec.events_log,
            stderr_log=spec.stderr_log,
            codex_executable=spec.executable or "codex",
            workspace=spec.workspace,
        )
        duration = time.monotonic() - start
        return RunResult(
            exit_code=exit_code,
            provider=self.name,
            model=spec.model,
            report_path=spec.report_log,
            events_log=spec.events_log,
            # Not measured here: brain_v42.metrics.codex_dream_parser.parse_codex_jsonl
            # already owns turn.completed token parsing for the historical
            # Dream telemetry pipeline. RunResult.tokens=None means "not
            # measured", never a fabricated zero.
            tokens=None,
            duration_seconds=duration,
            tool_call_completed=brain_tool_call_completed(spec.events_log),
        )


# --- CLI entry point --------------------------------------------------------
#
# Moved from scripts/dream/codex_runner.py (lot 2 of the agent runtime
# extraction, Brain ticket afd56820). The shim there now just calls main()
# below; its argparse contract and exit codes are unchanged.


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one isolated Dream phase with Codex")
    parser.add_argument("--preflight-capabilities", action="store_true")
    parser.add_argument("--project-key")
    parser.add_argument("--phase", choices=tuple(PHASE_TOOL_ALLOWLISTS))
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort", choices=tuple(_REASONING_EFFORTS))
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--report-log", type=Path)
    parser.add_argument("--events-log", type=Path)
    parser.add_argument("--stderr-log", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument(
        "--codex-executable", default=os.environ.get("BRAIN_DREAM_CODEX_BIN", "codex")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.preflight_capabilities:
        if args.project_key is None:
            parser.error("--project-key is required with --preflight-capabilities")
        try:
            preflight_capabilities(args.project_key, os.environ)
        except DreamCapabilityConfigurationError:
            print(_CAPABILITY_CONFIGURATION_ERROR, file=sys.stderr)
            return 1
        return 0

    required_arguments = {
        "--phase": args.phase,
        "--model": args.model,
        "--reasoning-effort": args.reasoning_effort,
        "--timeout-seconds": args.timeout_seconds,
        "--report-log": args.report_log,
        "--events-log": args.events_log,
        "--stderr-log": args.stderr_log,
    }
    missing = [name for name, value in required_arguments.items() if value is None]
    if missing:
        parser.error(f"the following arguments are required: {', '.join(missing)}")

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("Dream Codex prompt is empty", file=sys.stderr)
        return 1
    return run_codex(
        prompt=prompt,
        phase=args.phase,
        project_key=args.project_key,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout_seconds=args.timeout_seconds,
        report_log=args.report_log,
        events_log=args.events_log,
        stderr_log=args.stderr_log,
        codex_executable=args.codex_executable,
        workspace=args.workspace,
    )


if __name__ == "__main__":
    raise SystemExit(main())
