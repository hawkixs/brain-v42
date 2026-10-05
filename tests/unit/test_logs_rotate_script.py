"""Behavioral tests for the log retention script."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts/logs-rotate.sh"


def test_dry_run_lists_old_files_without_deleting(tmp_path: Path) -> None:
    target = tmp_path / "logs"
    target.mkdir()
    old = target / "old.log"
    old.write_text("old")
    os.utime(old, (1, 1))

    result = subprocess.run([str(SCRIPT), "--dry-run", str(target)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert str(old) in result.stdout
    assert old.exists()
    assert not Path("/tmp/brain-v42-logs-rotate.log").exists()


def test_refuses_symlink_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    result = subprocess.run([str(SCRIPT), str(link)], capture_output=True, text=True)

    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()


def test_refused_target_does_not_prevent_later_target_rotation(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    later = tmp_path / "later"
    later.mkdir()
    old = later / "old.log"
    old.write_text("old")
    os.utime(old, (1, 1))

    result = subprocess.run([str(SCRIPT), str(link), str(later)], capture_output=True, text=True)

    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert "target: " + str(later) in result.stdout
    assert not old.exists()


def test_default_targets_include_repository_and_kept_release_logs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    release_logs = home / ".local/share/brain-v42/releases/abc/brain-v42/logs"
    release_logs.mkdir(parents=True)
    old_release_log = release_logs / "night.log"
    old_release_log.write_text("night")
    old_timestamp = 1_700_000_000
    os.utime(old_release_log, (old_timestamp, old_timestamp))
    repository_logs = tmp_path / "logs"
    repository_logs.mkdir()

    result = subprocess.run(
        [str(SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "BRAIN_LOGS_REPO_ROOT": str(tmp_path)},
    )

    assert result.returncode == 0, result.stderr
    assert f"target: {release_logs} (mtime > 90 days)" in result.stdout
    assert f"would delete: {old_release_log}" in result.stdout
    assert "files before: 1" in result.stdout
    assert "files eligible: 1" in result.stdout
