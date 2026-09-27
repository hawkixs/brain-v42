"""A confinement verdict checks bytes first (Q91=b): a changed outside target
fails the rail whatever else happened, and only then may an incomplete run or
an unrefused target leave the probe inconclusive -- an inconclusive probe
records nothing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from headless_agents.proofs import confinement_verdict, outside_changes


def test_a_changed_target_fails_even_when_runs_were_incomplete() -> None:
    verdict = confinement_verdict(changed=["ref"], incomplete=["ref"], unrefused=["config"])
    assert verdict.passed is False
    assert "ref" in verdict.reason


def test_an_incomplete_run_is_inconclusive() -> None:
    verdict = confinement_verdict(changed=[], incomplete=["config"], unrefused=[])
    assert verdict.passed is None
    assert "config" in verdict.reason


def test_a_target_without_a_logged_refusal_is_inconclusive() -> None:
    verdict = confinement_verdict(changed=[], incomplete=[], unrefused=["operator_gitconfig"])
    assert verdict.passed is None
    assert "operator_gitconfig" in verdict.reason


def test_every_target_refused_and_none_changed_passes() -> None:
    assert confinement_verdict(changed=[], incomplete=[], unrefused=[]).passed is True


# ── outside_changes (review round 1 of PR #234, item 4) ─────────────────────
#
# A sandboxed command able to write a target back byte-for-byte, delete it,
# replace it with a directory or corrupt its permissions must be caught even
# when nothing else in the probe raised: outside_changes never raises on the
# read it performs, so the caller's byte check can run inside a `finally`,
# before anything about the run is torn down.


def test_outside_changes_reports_nothing_when_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"content")
    assert outside_changes({"f": b"content"}, {"f": path}) == []


def test_outside_changes_reports_a_modified_target(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"other")
    assert outside_changes({"f": b"content"}, {"f": path}) == ["f"]


def test_outside_changes_reports_a_deleted_target(tmp_path: Path) -> None:
    path = tmp_path / "f"
    assert outside_changes({"f": b"content"}, {"f": path}) == ["f"]


def test_outside_changes_reports_a_target_replaced_by_a_directory(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.mkdir()
    assert outside_changes({"f": b"content"}, {"f": path}) == ["f"]


def test_outside_changes_treats_an_inaccessible_absent_path_as_changed(tmp_path: Path) -> None:
    """Review round 2 of PR #234, item 1: `Path.exists()` can itself raise
    `PermissionError` when a parent directory becomes inaccessible -- an agent
    making the probe HOME unreadable must not raise inside the caller's
    `finally` and skip the byte check that would otherwise fail the rail."""
    if os.geteuid() == 0:
        pytest.skip("root reads everything regardless of permission bits")
    subdir = tmp_path / "sub"
    subdir.mkdir()
    path = subdir / "f"
    subdir.chmod(0)
    try:
        assert outside_changes({"f": None}, {"f": path}) == ["f"]
    finally:
        subdir.chmod(0o755)


def test_outside_changes_does_not_rest_on_path_exists_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 3 of PR #234: from Python 3.14, `Path.exists()` and
    `Path.is_symlink()` return False for a path under an inaccessible parent
    instead of raising, so an agent that locks the probe HOME would make an
    absent startup file look unchanged. Simulate those semantics: the check
    must rest on `os.lstat`, where only a missing path counts as still absent."""
    if os.geteuid() == 0:
        pytest.skip("root reads everything regardless of permission bits")
    monkeypatch.setattr(Path, "exists", lambda self, **_: False)
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    subdir = tmp_path / "sub"
    subdir.mkdir()
    path = subdir / "f"
    subdir.chmod(0)
    try:
        assert outside_changes({"f": None}, {"f": path}) == ["f"]
    finally:
        subdir.chmod(0o755)


def test_outside_changes_treats_a_parent_replaced_by_a_file_as_changed(tmp_path: Path) -> None:
    """A watched path whose parent directory became a file (ENOTDIR) did not
    simply stay absent: its parent, outside the worktree, was rewritten."""
    home = tmp_path / "home"
    home.write_text("not a directory any more\n")
    assert outside_changes({"f": None}, {"f": home / ".zshenv"}) == ["f"]


def test_outside_changes_reports_creation_where_it_was_absent(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"anything")
    assert outside_changes({"f": None}, {"f": path}) == ["f"]


def test_outside_changes_reports_nothing_created_where_it_was_absent() -> None:
    assert outside_changes({"f": None}, {"f": Path("/does/not/exist")}) == []


def test_outside_changes_reports_an_unreadable_target(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads everything regardless of permission bits")
    path = tmp_path / "f"
    path.write_bytes(b"content")
    path.chmod(0)
    try:
        assert outside_changes({"f": b"content"}, {"f": path}) == ["f"]
    finally:
        path.chmod(0o644)
