"""Isolated ``codex exec`` adapter for one Dream phase -- CLI entry point.

Moved to :mod:`brain_v42.agents.providers.codex` (lot 1 of the agent runtime
extraction, Brain ticket c31bad72). This file keeps the ``argparse`` CLI and
``main()`` that ``dream.sh`` invokes, and re-exports every name
``tests/unit/test_dream_codex_runner.py`` and
``tests/unit/test_dream_provider_chain.py`` import from ``scripts.dream.codex_runner``
by name -- including ``subprocess`` itself, which those tests monkeypatch
(``runner.subprocess.Popen``); patching that shared module object also patches
the ``subprocess.Popen`` the package's ``run_codex`` calls.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # noqa: F401  (re-exported: tests monkeypatch runner.subprocess.Popen)
import sys
from collections.abc import Sequence
from pathlib import Path

from brain_v42.agents.capability import (
    CAPABILITY_CONFIGURATION_ERROR as _CAPABILITY_CONFIGURATION_ERROR,
)
from brain_v42.agents.capability import (
    preflight_capabilities as _preflight_capabilities,
)
from brain_v42.agents.providers.codex import (
    _DISABLED_FEATURES as _DISABLED_FEATURES,
)
from brain_v42.agents.providers.codex import (
    PHASE_TOOL_ALLOWLISTS as PHASE_TOOL_ALLOWLISTS,
)
from brain_v42.agents.providers.codex import (
    brain_tool_call_completed as brain_tool_call_completed,
)
from brain_v42.agents.providers.codex import (
    build_codex_command as build_codex_command,
)
from brain_v42.agents.providers.codex import (
    run_codex as run_codex,
)
from brain_v42.mcp.dream_capabilities import DreamCapabilityConfigurationError

_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)


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
            _preflight_capabilities(args.project_key, os.environ)
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
