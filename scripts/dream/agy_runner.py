"""An isolated ``agy`` adapter for one Dream phase -- CLI entry point.

Moved to :mod:`brain_v42.agents.providers.agy` (lot 1 of the agent runtime
extraction, Brain ticket c31bad72). The ``argparse`` CLI and ``main()`` moved
there too in lot 2 (Brain ticket afd56820): ``run_phase`` no longer shells
out to this module by name, it invokes ``brain_v42.agents.providers.agy``
directly (which resolves its own guard path -- see that module's CLI
section). This file keeps ``GUARD_PATH`` -- the absolute path to the
versioned tool-use guard (``scripts/dream/agy_tool_guard.sh``), which the
package does not hardcode: ``build_ephemeral_home`` there takes
``guard_path`` as an explicit parameter instead (see
``brain_v42.agents.sandbox``'s module docstring). ``build_ephemeral_home`` and
``guard_denies_machine_tools`` below are thin wrappers that supply this
module's ``GUARD_PATH``, keeping the OLD (no-``guard_path``-argument)
signature ``tests/unit/test_dream_agy_runner.py`` calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

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
    main as main,
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


if __name__ == "__main__":
    raise SystemExit(main())
