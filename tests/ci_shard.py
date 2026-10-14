"""Pytest support for deterministic file level CI sharding."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

import pytest


def partition(files: list[str], weights: dict[str, float], shards: int) -> dict[str, int]:
    """Assign files with greedy longest processing time, using 1-based shard IDs."""
    if shards < 1:
        raise ValueError("shards must be at least 1")
    known = [float(value) for value in weights.values()]
    fallback = statistics.median(known) if known else 1.0
    loads = [0.0] * shards
    result: dict[str, int] = {}
    for path in sorted(set(files), key=lambda item: (-float(weights.get(item, fallback)), item)):
        shard = min(range(shards), key=lambda index: (loads[index], index))
        result[path] = shard + 1
        loads[shard] += float(weights.get(path, fallback))
    return result


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("sharding")
    group.addoption("--shard", metavar="I/N", help="run shard I of N (1-based)")
    group.addoption("--shard-plan", metavar="PATH", help="JSON file timing test files")


def _load_weights(config: pytest.Config) -> dict[str, float]:
    plan_path = config.getoption("--shard-plan")
    if not plan_path:
        return {}
    try:
        data: Any = json.loads(Path(plan_path).read_text())
        weights = data["weights"]
        if not isinstance(data.get("suite"), str) or not isinstance(weights, dict):
            raise ValueError("expected suite string and weights object")
        if any(
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            for key, value in weights.items()
        ):
            raise ValueError("weights must map file paths to numbers")
        return {key: float(value) for key, value in weights.items()}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise pytest.UsageError(f"invalid shard plan {plan_path}: {exc}") from None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect whole files so module and class fixtures remain shard-local."""
    shard_spec = config.getoption("--shard")
    if not shard_spec:
        return
    try:
        current_text, total_text = shard_spec.split("/", 1)
        current, total = int(current_text), int(total_text)
        if total < 1 or current < 1 or current > total:
            raise ValueError
    except (ValueError, AttributeError):
        raise pytest.UsageError("--shard must be I/N with 1 <= I <= N") from None
    root = Path(config.rootpath)
    paths = {item: Path(item.path).relative_to(root).as_posix() for item in items}
    assignments = partition(list(paths.values()), _load_weights(config), total)
    kept = [item for item in items if assignments[paths[item]] == current]
    deselected = [item for item in items if assignments[paths[item]] != current]
    items[:] = kept
    config.hook.pytest_deselected(items=deselected)
