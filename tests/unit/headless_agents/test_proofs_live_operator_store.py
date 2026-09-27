"""``_codex_touched_the_operator_session_store`` (lot 1b, review round): the
live confinement probe must never mistake a write to the operator's real
``$CODEX_HOME`` for one to ``~/.codex`` when the two differ.

This is a plain unit test of a helper defined in a `tests/live/` module: the
helper itself spends no quota and touches nothing live, so it is tested here
rather than only ever exercised behind ``HA_LIVE=1``.
"""

from __future__ import annotations

import time
from pathlib import Path

from tests.live.headless_agents.test_proofs_live import (
    _codex_touched_the_operator_session_store,
)


def _plant_rollout(codex_home: Path) -> Path:
    sessions = codex_home / "sessions" / "2026" / "09" / "27"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-2026-09-27T04-18-26-uuid.jsonl"
    rollout.write_text('{"line":1}\n', encoding="utf-8")
    return rollout


#: A write always lands after a marker taken this far in the past: real
#: filesystem mtimes are not always monotonic with ``time.time()`` at
#: sub-millisecond resolution (measured on this machine), so a marker taken
#: right before the write can occasionally read as LATER than the mtime it
#: is meant to precede.
_MARKER_SAFETY_MARGIN_SECONDS = 2.0


def test_honours_codex_home_when_set(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home-never-checked"
    real_home.mkdir()
    custom_codex_home = tmp_path / "custom-codex-home"
    custom_codex_home.mkdir()
    marker = time.time() - _MARKER_SAFETY_MARGIN_SECONDS
    _plant_rollout(custom_codex_home)

    touched = _codex_touched_the_operator_session_store(
        marker, environ={"CODEX_HOME": str(custom_codex_home)}, real_home=real_home
    )

    assert touched is True


def test_a_write_under_the_real_home_is_invisible_when_codex_home_is_set(
    tmp_path: Path,
) -> None:
    """A write to ``~/.codex`` must not count when ``$CODEX_HOME`` points
    elsewhere: that is not the store codex was actually told to use."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    custom_codex_home = tmp_path / "custom-codex-home"
    custom_codex_home.mkdir()
    marker = time.time()
    _plant_rollout(real_home / ".codex")

    touched = _codex_touched_the_operator_session_store(
        marker, environ={"CODEX_HOME": str(custom_codex_home)}, real_home=real_home
    )

    assert touched is False


def test_falls_back_to_dot_codex_when_unset(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    marker = time.time() - _MARKER_SAFETY_MARGIN_SECONDS
    _plant_rollout(real_home / ".codex")

    touched = _codex_touched_the_operator_session_store(marker, environ={}, real_home=real_home)

    assert touched is True


def test_an_older_rollout_does_not_count(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    _plant_rollout(real_home / ".codex")
    marker = time.time() + 60.0

    touched = _codex_touched_the_operator_session_store(marker, environ={}, real_home=real_home)

    assert touched is False
