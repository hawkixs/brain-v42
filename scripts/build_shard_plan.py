#!/usr/bin/env python3
"""Build deterministic CI shard weights from pytest duration reports."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from tests.ci_shard import partition

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s+(?:call|setup|teardown)\s+(.+?\.py)::")


def parse_duration_logs(logs: list[Path]) -> dict[str, float]:
    """Sum reported call, setup and teardown time by test file."""
    totals: defaultdict[str, float] = defaultdict(float)
    for log in logs:
        for line in log.read_text().splitlines():
            match = _DURATION.match(line)
            if match:
                totals[match.group(2)] += float(match.group(1))
    return dict(sorted(totals.items()))


def write_plan(suite: str, weights: dict[str, float], output: Path) -> None:
    """Write stable, rounded JSON for the pytest plugin."""
    content = {
        "suite": suite,
        "weights": {key: round(value, 2) for key, value in sorted(weights.items())},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    weights = parse_duration_logs(args.logs)
    write_plan(args.suite, weights, args.out)
    files = list(weights)
    for count in range(2, 9):
        assigned = partition(files, weights, count)
        loads = [
            sum(weights[path] for path in files if assigned[path] == shard)
            for shard in range(1, count + 1)
        ]
        print(f"N={count}: {max(loads, default=0):.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
