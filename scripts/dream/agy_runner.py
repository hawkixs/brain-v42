"""An isolated ``agy`` adapter for one Dream phase -- CLI entry point.

Moved to :mod:`brain_v42.agents.providers.agy` (lot 1 of the agent runtime
extraction, Brain ticket c31bad72). This file keeps the ``argparse`` CLI and
``main()`` that ``dream.sh`` invokes, plus ``GUARD_PATH`` -- the absolute path
to the versioned tool-use guard (``scripts/dream/agy_tool_guard.sh``), which
the package does not hardcode: ``build_ephemeral_home`` there takes
``guard_path`` as an explicit parameter instead (see
``brain_v42.agents.sandbox``'s module docstring). ``build_ephemeral_home`` and
``guard_denies_machine_tools`` below are thin wrappers that supply this
module's ``GUARD_PATH``, keeping the OLD (no-``guard_path``-argument)
signature ``tests/unit/test_dream_agy_runner.py`` calls.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from brain_v42.agents.capability import (
    CAPABILITY_CONFIGURATION_ERROR,
    preflight_capabilities,
)
from brain_v42.agents.providers.agy import (
    PHASE_TOOL_ALLOWLISTS as PHASE_TOOL_ALLOWLISTS,
)
from brain_v42.agents.providers.agy import (
    brain_tool_call_completed as brain_tool_call_completed,
)
from brain_v42.agents.providers.agy import (
    build_agy_command as build_agy_command,
)
from brain_v42.agents.providers.agy import (
    extract_report as extract_report,
)
from brain_v42.agents.providers.agy import (
    guard_denies_machine_tools as _package_guard_denies_machine_tools,
)
from brain_v42.agents.providers.agy import (
    run_agy as _package_run_agy,
)
from brain_v42.agents.sandbox import (
    build_ephemeral_home as _package_build_ephemeral_home,
)
from brain_v42.agents.sandbox import (
    ephemeral_root as ephemeral_root,
)
from brain_v42.mcp.dream_capabilities import DreamCapabilityConfigurationError

GUARD_PATH = Path(__file__).resolve().parent / "agy_tool_guard.sh"


def build_ephemeral_home(
    *,
    root: Path,
    phase: str,
    project_key: str,
    environ: Mapping[str, str],
    real_home: Path,
    mcp_url: str | None = None,
) -> Path:
    """Compose a phase HOME, wiring the versioned guard at ``GUARD_PATH``."""
    return _package_build_ephemeral_home(
        root=root,
        phase=phase,
        project_key=project_key,
        environ=environ,
        real_home=real_home,
        guard_path=GUARD_PATH,
        mcp_url=mcp_url,
    )


def guard_denies_machine_tools(guard: Path | None = None) -> bool:
    return _package_guard_denies_machine_tools(guard or GUARD_PATH)


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
    agy_executable: str = "agy",
) -> int:
    """Run a phase, wiring the versioned guard at ``GUARD_PATH``."""
    return _package_run_agy(
        prompt=prompt,
        phase=phase,
        project_key=project_key,
        model=model,
        timeout_seconds=timeout_seconds,
        events_log=events_log,
        report_log=report_log,
        stderr_log=stderr_log,
        guard_path=GUARD_PATH,
        agy_executable=agy_executable,
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

    if args.preflight_capabilities:
        if args.project_key is None:
            parser.error("--project-key est requis avec --preflight-capabilities")
        if not guard_denies_machine_tools():
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
        agy_executable=args.agy_executable,
    )


if __name__ == "__main__":
    raise SystemExit(main())
