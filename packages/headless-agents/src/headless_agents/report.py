"""``run.json``: the report of one run (spec 0.5.0 §3.10).

A report, never an authority: it is rebuilt from the state directory and
never read back to decide anything (§3.8.1). Written at the start
(``running``), replaced atomically after every step. ``null`` means "not
measured" or "not applicable to this target", never zero. Its key set is
pinned by a test.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Final

from .result import RunResult
from .state import publish

RUN_JSON: Final = "run.json"
#: The task, as given (spec §3.10): written once at the start of a run.
PROMPT_FILE: Final = "prompt.md"
SCHEMA: Final = 1
RUN_KEYS: Final = (
    "schema", "run_id", "target", "status", "exit_code", "verdict", "text",
    "repository", "base", "head", "branch", "lineage", "continues", "findings_from",
    "implement_providers", "commits", "failure_reason", "vendor_check", "cleanup", "pid",
    "started_at", "duration_seconds", "cost_usd", "cost_complete", "steps",
)  # fmt: skip
STEP_KEYS: Final = (
    "index", "slot", "role", "dir", "provider", "model", "model_reported",
    "exit_code", "duration_seconds", "tokens", "cost_usd", "tools", "verdict",
)  # fmt: skip


def new_report(
    *,
    run_id: str,
    target: Mapping[str, str],
    repository: Path | None,
    pid: int,
    started_at: str,
) -> dict[str, object]:
    report: dict[str, object] = dict.fromkeys(RUN_KEYS)
    report.update(
        schema=SCHEMA,
        run_id=run_id,
        target=dict(target),
        status="running",
        repository=str(repository) if repository is not None else None,
        pid=pid,
        started_at=started_at,
        cost_complete=True,
        steps=[],
    )
    return report


def step_dir_name(index: int, slot: str, role: str) -> str:
    """``<nn>-<slot>-<role>``: the launch order on two digits (§3.10)."""
    return f"{index:02d}-{slot}-{role}"


def step_entry(
    *,
    index: int,
    slot: str,
    role: str,
    step_dir: str,
    result: RunResult,
    tools: Mapping[str, int] | None = None,
) -> dict[str, object]:
    return {
        "index": index,
        "slot": slot,
        "role": role,
        "dir": step_dir,
        "provider": result.provider,
        "model": result.model,
        "model_reported": result.model_reported,
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "tokens": asdict(result.tokens) if result.tokens is not None else None,
        "cost_usd": result.cost_usd,
        # Spec §3.11: the rail's own names; None when the rail could not measure.
        "tools": dict(tools) if tools is not None else None,
        "verdict": None,
    }


def with_step(report: Mapping[str, object], step: Mapping[str, object]) -> dict[str, object]:
    """A copy of ``report`` with ``step`` appended and the cost recomputed."""
    previous = report.get("steps")
    steps = [*(previous if isinstance(previous, list) else []), dict(step)]
    costs = [s.get("cost_usd") for s in steps]
    measured = [c for c in costs if isinstance(c, int | float)]
    updated = dict(report)
    updated["steps"] = steps
    updated["cost_usd"] = sum(measured) if measured else None
    updated["cost_complete"] = len(measured) == len(costs)
    return updated


def write_report(run_dir: Path, report: Mapping[str, object]) -> None:
    publish(run_dir / RUN_JSON, report)


__all__ = [
    "PROMPT_FILE",
    "RUN_JSON",
    "RUN_KEYS",
    "SCHEMA",
    "STEP_KEYS",
    "new_report",
    "step_dir_name",
    "step_entry",
    "with_step",
    "write_report",
]
