"""Contracts for building shard plans from pytest duration output."""

from __future__ import annotations

import json

from scripts.build_shard_plan import parse_duration_logs, write_plan


def test_duration_lines_sum_by_test_file(tmp_path) -> None:
    log = tmp_path / "durations.txt"
    log.write_text(
        "12.34s call     tests/unit/test_a.py::test_one[param]\n"
        "1.20s setup    tests/unit/test_a.py::test_two\n"
        "0.46s teardown tests/unit/test_b.py::test_three\n"
    )
    assert parse_duration_logs([log]) == {
        "tests/unit/test_a.py": 13.54,
        "tests/unit/test_b.py": 0.46,
    }


def test_plan_json_has_sorted_weights_and_trailing_newline(tmp_path) -> None:
    output = tmp_path / "plan.json"
    write_plan("unit", {"tests/unit/z.py": 1.236, "tests/unit/a.py": 2.0}, output)
    raw = output.read_text()
    assert raw.endswith("\n")
    assert json.loads(raw) == {
        "suite": "unit",
        "weights": {"tests/unit/a.py": 2.0, "tests/unit/z.py": 1.24},
    }
