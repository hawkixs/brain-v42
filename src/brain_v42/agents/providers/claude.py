"""The Dream's ``claude -p`` rail: a phase-scoped profile over the runtime.

Since Brain ticket b2a2d1a5 the per-run MCP client configuration, the
hardened command line and the exit-code discipline live in
:mod:`headless_agents.providers.claude`. What stays here is what only the
Dream knows: which Brain tools a phase may call, which ``(project, phase)``
bearer scopes it, the five ``dream.sh`` exports that belong to this rail,
and the argv contract ``dream.sh`` invokes through
``python -m brain_v42.agents.providers.claude``. Every public name,
signature and exit code is unchanged from lot 1/2; the golden fixtures pin
the argv, the child environment and the MCP config byte for byte.
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
from headless_agents.providers import claude as _runtime
from headless_agents.providers.claude import (
    CHILD_ENV_PASSTHROUGH as _CLAUDE_CHILD_ENV_EXTRA,
)

from ..capability import (
    CAPABILITY_CONFIGURATION_ERROR,
    DEFAULT_MCP_URL,
    MCP_URL_ENV,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    brain_mcp_server,
    build_child_environment,
    preflight_capabilities,
    terminate_process_group,
)
from ..result import RunResult
from ..spec import RunSpec

PHASE_TOOL_ALLOWLISTS = DREAM_PHASE_TOOL_ALLOWLISTS

__all__ = [
    "PHASE_TOOL_ALLOWLISTS",
    "PROVIDER_FALLBACK_EXIT_CODE",
    "TIMEOUT_EXIT_CODE",
    "ClaudeProvider",
    "brain_tool_call_completed",
    "build_claude_command",
    "build_claude_mcp_config",
    "claude_child_environment",
    "main",
    "run_claude",
    "terminate_process_group",
]


def claude_child_environment(
    *,
    project_key: str | None,
    phase: str,
    environ: Mapping[str, str],
) -> dict[str, str] | None:
    """Return the phase-scoped child environment, or ``None`` if unenforced."""
    return build_child_environment(
        project_key=project_key,
        phase=phase,
        environ=environ,
        extra_allowlist=_CLAUDE_CHILD_ENV_EXTRA,
    )


def build_claude_mcp_config(*, phase: str, mcp_url: str) -> dict[str, object]:
    """Render the per-phase MCP client configuration for ``claude -p``.

    An unknown phase raises, which keeps it from silently producing a config
    with no restriction.
    """
    return _runtime.build_claude_mcp_config(
        brain_mcp_server(agent=f"dream-claude-{phase}", phase=phase, url=mcp_url)
    )


def build_claude_command(
    *,
    phase: str,
    model: str,
    max_turns: int,
    mcp_config_path: Path,
    claude_executable: str = "claude",
) -> list[str]:
    """Build the hardened non-interactive Claude command for one phase."""
    # The URL plays no part in the argv; the phase's tool allowlist does.
    mcp = brain_mcp_server(agent=f"dream-claude-{phase}", phase=phase, url=DEFAULT_MCP_URL)
    return _runtime.build_claude_command(
        model=model,
        max_turns=max_turns,
        mcp_config_path=mcp_config_path,
        mcp=mcp,
        executable=claude_executable,
    )


def brain_tool_call_completed(raw_log: Path) -> bool:
    """Did a Brain tool call SUCCEED in this OTEL telemetry?"""
    return _runtime.tool_call_completed(raw_log)


def run_claude(
    *,
    prompt: str,
    phase: str,
    project_key: str | None = None,
    model: str,
    max_turns: int,
    timeout_seconds: float,
    raw_log: Path,
    claude_executable: str = "claude",
) -> int:
    """Run one Claude phase and return its exit code (``124`` on timeout)."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    try:
        child_environment = claude_child_environment(
            project_key=project_key,
            phase=phase,
            environ=os.environ,
        )
    except DreamCapabilityConfigurationError:
        raw_log.parent.mkdir(parents=True, exist_ok=True)
        with raw_log.open("a", encoding="utf-8") as stream:
            stream.write(f"{CAPABILITY_CONFIGURATION_ERROR}\n")
        return 1
    mcp_url = os.environ.get(MCP_URL_ENV, DEFAULT_MCP_URL)
    mcp = brain_mcp_server(agent=f"dream-claude-{phase}", phase=phase, url=mcp_url)
    return _runtime.run_claude(
        prompt=prompt,
        model=model,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        raw_log=raw_log,
        mcp=mcp,
        environment=child_environment,
        executable=claude_executable,
        temp_prefix=f"brain-v42-dream-claude-{phase}-",
    )


class ClaudeProvider:
    """:class:`~brain_v42.agents.protocol.AgentProvider` adapter over Claude."""

    name = "claude"

    def build_command(self, spec: RunSpec) -> list[str]:
        assert spec.mcp_config_path is not None, "RunSpec.mcp_config_path is required for Claude"
        return build_claude_command(
            phase=spec.phase,
            model=spec.model,
            max_turns=spec.max_turns,
            mcp_config_path=spec.mcp_config_path,
            claude_executable=spec.executable or "claude",
        )

    def child_environment(self, spec: RunSpec, environ: Mapping[str, str]) -> dict[str, str] | None:
        return claude_child_environment(
            project_key=spec.project_key, phase=spec.phase, environ=environ
        )

    def prepare_home(self, spec: RunSpec) -> Path | None:
        return None

    def tool_call_completed(self, events_log: Path) -> bool:
        return brain_tool_call_completed(events_log)

    def run(self, spec: RunSpec) -> RunResult:
        # Mirrors exactly what the --raw-log CLI argument passes to
        # run_claude: one field, no fallback between events_log/report_log
        # (see RunSpec.raw_log's docstring).
        assert spec.raw_log is not None, "RunSpec.raw_log is required for Claude"
        raw_log = spec.raw_log
        start = time.monotonic()
        exit_code = run_claude(
            prompt=spec.prompt,
            phase=spec.phase,
            project_key=spec.project_key,
            model=spec.model,
            max_turns=spec.max_turns,
            timeout_seconds=spec.timeout_seconds,
            raw_log=raw_log,
            claude_executable=spec.executable or "claude",
        )
        duration = time.monotonic() - start
        return RunResult(
            exit_code=exit_code,
            provider=self.name,
            model=spec.model,
            report_path=raw_log,
            events_log=raw_log,
            tokens=None,
            duration_seconds=duration,
            tool_call_completed=brain_tool_call_completed(raw_log),
        )


# --- CLI entry point --------------------------------------------------------
#
# Moved from scripts/dream/claude_runner.py (lot 2 of the agent runtime
# extraction, Brain ticket afd56820). The shim there now just calls main()
# below; its argparse contract and exit codes are unchanged.


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one isolated Dream phase with Claude")
    parser.add_argument("--preflight-capabilities", action="store_true")
    parser.add_argument("--project-key")
    parser.add_argument("--phase", choices=tuple(PHASE_TOOL_ALLOWLISTS))
    parser.add_argument("--model")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--raw-log", type=Path)
    parser.add_argument(
        "--claude-executable", default=os.environ.get("BRAIN_DREAM_CLAUDE_BIN", "claude")
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
            print(CAPABILITY_CONFIGURATION_ERROR, file=sys.stderr)
            return 1
        return 0

    required_arguments = {
        "--phase": args.phase,
        "--model": args.model,
        "--max-turns": args.max_turns,
        "--timeout-seconds": args.timeout_seconds,
        "--raw-log": args.raw_log,
    }
    missing = [name for name, value in required_arguments.items() if value is None]
    if missing:
        parser.error(f"the following arguments are required: {', '.join(missing)}")

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("Dream Claude prompt is empty", file=sys.stderr)
        return 1
    return run_claude(
        prompt=prompt,
        phase=args.phase,
        project_key=args.project_key,
        model=args.model,
        max_turns=args.max_turns,
        timeout_seconds=args.timeout_seconds,
        raw_log=args.raw_log,
        claude_executable=args.claude_executable,
    )


if __name__ == "__main__":
    raise SystemExit(main())
