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


def _install_failing_listing_find(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_find = fake_bin / "find"
    fake_find.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "$arg" == "$FAIL_LISTING_TARGET" ]]; then\n'
        "    target_match=1\n"
        "  fi\n"
        '  if [[ "$arg" == -print0 ]]; then\n'
        "    print0=1\n"
        "  fi\n"
        "done\n"
        'if [[ "${target_match:-}" == 1 && "${print0:-}" == 1 ]]; then\n'
        '    printf "simulated listing failure\\n" >&2\n'
        "    exit 1\n"
        "fi\n"
        'exec /usr/bin/find "$@"\n'
    )
    fake_find.chmod(0o755)
    return fake_bin


def test_listing_failure_is_reported_and_later_target_is_rotated(tmp_path: Path) -> None:
    first = tmp_path / "first"
    first.mkdir()
    (first / "not-listed.log").write_text("old")
    later = tmp_path / "later"
    later.mkdir()
    old_later = later / "old.log"
    old_later.write_text("old")
    os.utime(old_later, (1, 1))

    result = subprocess.run(
        [str(SCRIPT), str(first), str(later)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{_install_failing_listing_find(tmp_path)}:{os.environ['PATH']}",
            "FAIL_LISTING_TARGET": str(first),
        },
    )

    assert result.returncode != 0
    assert "listing failed" in result.stderr.lower()
    assert "target: " + str(later) in result.stdout
    assert not old_later.exists()


def test_dry_run_listing_failure_returns_nonzero(tmp_path: Path) -> None:
    target = tmp_path / "logs"
    target.mkdir()
    old = target / "old.log"
    old.write_text("old")
    os.utime(old, (1, 1))

    result = subprocess.run(
        [str(SCRIPT), "--dry-run", str(target)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{_install_failing_listing_find(tmp_path)}:{os.environ['PATH']}",
            "FAIL_LISTING_TARGET": str(target),
        },
    )

    assert result.returncode != 0
    assert "listing failed" in result.stderr.lower()


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
