"""The test session refuses to start on a nearly full filesystem.

Incident 2026-10-05 (ticket 998634d7): the full-suite --basetemp directories of one
lot (~140 GB, never removed) filled the workstation's root filesystem, which also held
the production PostgreSQL bind mount; PostgreSQL crashed for five minutes. The
workstation stays the development machine where full suites and ha workers run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from tests import disk_floor

GiB = 1024**3
Usage = namedtuple("Usage", "total used free")


@pytest.mark.parametrize(
    ("total_gib", "expected_gib"),
    [(31, 5), (72, 5), (100, 5), (926, 46.3)],
)
def test_the_floor_is_five_percent_of_the_filesystem_or_five_gib(total_gib, expected_gib):
    floor = disk_floor.floor_bytes(total_gib * GiB, override=None)
    assert floor == pytest.approx(expected_gib * GiB)


@pytest.mark.parametrize(("override", "expected"), [("80", 80 * GiB), ("0", 0), ("2.5", 2.5 * GiB)])
def test_an_explicit_floor_overrides_the_default(override, expected):
    assert disk_floor.floor_bytes(926 * GiB, override=override) == expected


@pytest.mark.parametrize("override", ["", "-1", "lots", "nan", "inf"])
def test_a_malformed_override_is_refused(override):
    with pytest.raises(disk_floor.DiskFloorError, match="BRAIN_V42_TEST_MIN_FREE_GB"):
        disk_floor.floor_bytes(926 * GiB, override=override)


def test_a_filesystem_under_its_floor_is_refused_with_both_numbers(tmp_path):
    usage = Usage(total=926 * GiB, used=900 * GiB, free=26 * GiB)
    with pytest.raises(disk_floor.DiskFloorError) as error:
        disk_floor.check(tmp_path, environ={}, disk_usage=lambda _: usage)
    message = str(error.value)
    assert "26.0 GiB free" in message
    assert "46.3 GiB" in message
    assert str(tmp_path) in message
    assert "BRAIN_V42_TEST_MIN_FREE_GB" in message


def test_a_filesystem_over_its_floor_passes(tmp_path):
    usage = Usage(total=926 * GiB, used=500 * GiB, free=426 * GiB)
    disk_floor.check(tmp_path, environ={}, disk_usage=lambda _: usage)


def test_a_zero_floor_disables_the_check(tmp_path):
    usage = Usage(total=926 * GiB, used=926 * GiB, free=0)
    disk_floor.check(
        tmp_path, environ={"BRAIN_V42_TEST_MIN_FREE_GB": "0"}, disk_usage=lambda _: usage
    )


def test_a_basetemp_that_does_not_exist_yet_is_measured_on_its_nearest_parent(tmp_path):
    seen: list[Path] = []

    def usage_of(path):
        seen.append(Path(path))
        return Usage(total=926 * GiB, used=0, free=926 * GiB)

    disk_floor.check(tmp_path / "not" / "yet", environ={}, disk_usage=usage_of)
    assert seen == [tmp_path]


def test_the_session_measures_the_basetemp_or_the_temp_dir(tmp_path):
    assert disk_floor.measured_path(str(tmp_path / "bt"), environ={}) == tmp_path / "bt"
    assert disk_floor.measured_path(None, environ={}) == Path(disk_floor.tempfile.gettempdir())


def test_the_session_measures_pytests_own_temp_root_when_it_is_set(tmp_path):
    """Codex review: pytest roots its temporary directories at PYTEST_DEBUG_TEMPROOT
    when set, before the system temp dir; the guard must measure the same place."""
    environ = {"PYTEST_DEBUG_TEMPROOT": str(tmp_path / "root")}
    assert disk_floor.measured_path(None, environ=environ) == tmp_path / "root"
    assert disk_floor.measured_path(str(tmp_path / "bt"), environ=environ) == tmp_path / "bt"


def test_no_usable_temp_dir_is_a_refusal_not_a_crash(monkeypatch):
    """Codex review: on a full temp filesystem tempfile.gettempdir() raises
    FileNotFoundError itself; that is the floor's refusal, not a traceback."""

    def full() -> str:
        raise FileNotFoundError("No usable temporary directory found")

    monkeypatch.setattr(disk_floor.tempfile, "gettempdir", full)
    with pytest.raises(disk_floor.DiskFloorError, match="no usable temporary directory"):
        disk_floor.measured_path(None, environ={})


def test_a_zero_floor_skips_even_a_failing_measurement(tmp_path):
    def broken(_):
        raise OSError("statvfs failed")

    disk_floor.check(tmp_path, environ={"BRAIN_V42_TEST_MIN_FREE_GB": "0"}, disk_usage=broken)


def test_a_zero_floor_disables_the_guard_before_any_path_is_resolved(monkeypatch):
    """Codex round 2: with the guard disabled and no usable temp dir, the path
    resolution itself refused the session; disabling must win first."""

    def full() -> str:
        raise FileNotFoundError("No usable temporary directory found")

    monkeypatch.setattr(disk_floor.tempfile, "gettempdir", full)
    disk_floor.guard(None, environ={"BRAIN_V42_TEST_MIN_FREE_GB": "0"})
    with pytest.raises(disk_floor.DiskFloorError, match="no usable temporary directory"):
        disk_floor.guard(None, environ={})


def test_the_guard_measures_the_resolved_path(tmp_path, monkeypatch):
    seen: list[Path] = []

    def check(path, *, environ, disk_usage=None):
        seen.append(path)

    monkeypatch.setattr(disk_floor, "check", check)
    disk_floor.guard(str(tmp_path / "bt"), environ={})
    assert seen == [tmp_path / "bt"]


def test_a_failing_measurement_is_a_refusal_not_a_crash(tmp_path):
    def broken(_):
        raise OSError("statvfs failed")

    with pytest.raises(disk_floor.DiskFloorError, match="cannot measure"):
        disk_floor.check(tmp_path, environ={}, disk_usage=broken)


def test_a_real_pytest_session_refuses_to_start_under_the_floor(tmp_path):
    """End to end through the repository's conftest: an absurd floor stops the
    session before collection, with a usage error and pytest's exit code 4."""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "bt"),
            "--collect-only",
            "-q",
            "tests/unit/test_disk_floor.py",
        ],
        cwd=root,
        env={
            **os.environ,
            "BRAIN_V42_TEST_MIN_FREE_GB": "1000000000",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "BRAIN_V42_TEST_MIN_FREE_GB" in result.stdout + result.stderr
