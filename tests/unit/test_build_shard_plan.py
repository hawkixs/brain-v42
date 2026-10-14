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


def test_hosted_log_lines_with_job_step_and_timestamp_prefixes_are_parsed(tmp_path) -> None:
    """`gh run view --log` prefixes each line with the job, the step and a timestamp."""
    log = tmp_path / "hosted.log"
    log.write_text(
        "unit-shard (1)\tRun unit tests\t2026-10-14T12:00:01.1234567Z 12.34s call     "
        "tests/unit/test_a.py::test_one[param]\n"
        "unit-shard (2)\tRun unit tests\t2026-10-14T12:00:02.7654321Z 0.66s setup    "
        "tests/unit/test_a.py::test_two\n"
        "unit-shard (2)\tRun unit tests\t2026-10-14T12:00:03.0000000Z tests/unit/test_a.py::"
        "test_x PASSED [ 10%]\n"
    )
    assert parse_duration_logs([log]) == {"tests/unit/test_a.py": 13.0}


def test_only_the_files_of_the_requested_suite_are_counted(tmp_path) -> None:
    log = tmp_path / "both.log"
    log.write_text(
        "2.00s call     tests/unit/test_a.py::test_one\n"
        "5.00s call     tests/integration/db/test_b.py::test_two\n"
    )
    assert parse_duration_logs([log], prefix="tests/integration/") == {
        "tests/integration/db/test_b.py": 5.0
    }
