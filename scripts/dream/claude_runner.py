"""Isolated ``claude -p`` adapter for one Dream phase -- CLI entry point.

Moved to :mod:`brain_v42.agents.providers.claude` (lot 1 of the agent runtime
extraction, Brain ticket c31bad72). This file keeps the ``argparse`` CLI and
``main()`` that ``dream.sh`` invokes, and re-exports every name
``tests/unit/test_dream_claude_runner.py`` and
``tests/unit/test_dream_provider_chain.py`` import from
``scripts.dream.claude_runner`` by name.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from brain_v42.agents.capability import (
    CAPABILITY_CONFIGURATION_ERROR as CAPABILITY_CONFIGURATION_ERROR,
)
from brain_v42.agents.capability import (
    preflight_capabilities as preflight_capabilities,
)
from brain_v42.agents.providers.claude import (
    PHASE_TOOL_ALLOWLISTS as PHASE_TOOL_ALLOWLISTS,
)
from brain_v42.agents.providers.claude import (
    brain_tool_call_completed as brain_tool_call_completed,
)
from brain_v42.agents.providers.claude import (
    build_claude_command as build_claude_command,
)
from brain_v42.agents.providers.claude import (
    build_claude_mcp_config as build_claude_mcp_config,
)
from brain_v42.agents.providers.claude import (
    claude_child_environment as claude_child_environment,
)
from brain_v42.agents.providers.claude import (
    run_claude as run_claude,
)
from brain_v42.mcp.dream_capabilities import DreamCapabilityConfigurationError


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
