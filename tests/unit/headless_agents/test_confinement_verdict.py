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
