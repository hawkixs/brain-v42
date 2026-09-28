"""``_codex_touched_the_operator_session_store`` (lot 1b, review round): the
live confinement probe must never mistake a write to the operator's real
session store for one to a store it never actually resolved to.

Review round 2 (codex major): the probe's own RunSpec carries an
``environment`` WITHOUT ``CODEX_HOME`` in it -- ``run_codex`` then resolves
its real home from THAT environment (falling back to ``Path.home()/.codex``,
which reads the CURRENT PROCESS's own ``$HOME``, never ``environment["HOME"]``).
A guard that instead read ``$CODEX_HOME`` from the PARENT (this pytest)
process's own environment could watch a DIFFERENT store than the one
``run_codex`` could actually have written to, whenever the parent process's
own ``$CODEX_HOME`` is set: it would then miss a real write there, or watch a
value ``run_codex`` never even reads.
:func:`headless_agents.providers.codex.resolve_real_codex_home` is
``run_codex``'s own resolution, extracted so both sides use EXACTLY the same
logic; the fix below checks BOTH the store that resolution would give for
the probe's own spec environment, and the one it gives for the parent
process's environment (``None``) -- a write to EITHER counts.

The helper moved from the live test module into :mod:`headless_agents.prove` (0.5.2
lot 4a); it spends no quota and touches nothing live, so it is tested here rather than
only ever exercised behind ``HA_LIVE=1``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from headless_agents.prove import _codex_touched_the_operator_session_store, _probe_thread_ids


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


def _marker() -> float:
    return time.time() - _MARKER_SAFETY_MARGIN_SECONDS


def _clear_parent_codex_home(monkeypatch: pytest.MonkeyPatch, *, home: Path) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))


def _set_parent_codex_home(
    monkeypatch: pytest.MonkeyPatch, *, home: Path, codex_home: Path
) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


class TestParentCodexHomeUnsetSpecWithoutCodexHome:
    """Both resolutions fall back to Path.home()/.codex: one store."""

    def test_a_write_there_is_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _clear_parent_codex_home(monkeypatch, home=home)
        _plant_rollout(home / ".codex")

        touched = _codex_touched_the_operator_session_store(
            _marker(), spec_environment={"PATH": "/usr/bin", "HOME": str(home)}
        )

        assert touched is True

    def test_no_write_is_not_touched(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _clear_parent_codex_home(monkeypatch, home=home)

        touched = _codex_touched_the_operator_session_store(
            _marker(), spec_environment={"PATH": "/usr/bin", "HOME": str(home)}
        )

        assert touched is False


class TestParentCodexHomeUnsetSpecWithCodexHome:
    """The spec's own CODEX_HOME is one store; Path.home()/.codex (the
    parent process's fallback) is a DIFFERENT one. Both are watched."""

    def test_a_write_to_the_specs_own_store_is_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _clear_parent_codex_home(monkeypatch, home=home)
        spec_codex_home = tmp_path / "spec-codex-home"
        spec_codex_home.mkdir()
        _plant_rollout(spec_codex_home)

        touched = _codex_touched_the_operator_session_store(
            _marker(),
            spec_environment={
                "PATH": "/usr/bin",
                "HOME": str(home),
                "CODEX_HOME": str(spec_codex_home),
            },
        )

        assert touched is True

    def test_a_write_to_the_parent_fallback_store_is_also_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _clear_parent_codex_home(monkeypatch, home=home)
        spec_codex_home = tmp_path / "spec-codex-home"
        spec_codex_home.mkdir()
        _plant_rollout(home / ".codex")

        touched = _codex_touched_the_operator_session_store(
            _marker(),
            spec_environment={
                "PATH": "/usr/bin",
                "HOME": str(home),
                "CODEX_HOME": str(spec_codex_home),
            },
        )

        assert touched is True


class TestParentCodexHomeSetSpecWithoutCodexHome:
    """The review-round regression: run_codex, given this exact spec
    environment, resolves Path.home()/.codex (its own fallback -- the spec
    carries no CODEX_HOME at all). A guard that instead read the PARENT
    process's own $CODEX_HOME alone would watch a THIRD, unrelated store and
    miss a write to the one run_codex could actually have made."""

    def test_a_write_to_the_parent_process_own_codex_home_is_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        parent_codex_home = tmp_path / "parent-codex-home"
        parent_codex_home.mkdir()
        _set_parent_codex_home(monkeypatch, home=home, codex_home=parent_codex_home)
        _plant_rollout(parent_codex_home)

        touched = _codex_touched_the_operator_session_store(
            _marker(), spec_environment={"PATH": "/usr/bin", "HOME": str(home)}
        )

        assert touched is True

    def test_a_write_to_the_specs_own_fallback_store_is_also_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        parent_codex_home = tmp_path / "parent-codex-home"
        parent_codex_home.mkdir()
        _set_parent_codex_home(monkeypatch, home=home, codex_home=parent_codex_home)
        _plant_rollout(home / ".codex")

        touched = _codex_touched_the_operator_session_store(
            _marker(), spec_environment={"PATH": "/usr/bin", "HOME": str(home)}
        )

        assert touched is True

    def test_no_write_at_all_is_not_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        parent_codex_home = tmp_path / "parent-codex-home"
        parent_codex_home.mkdir()
        _set_parent_codex_home(monkeypatch, home=home, codex_home=parent_codex_home)

        touched = _codex_touched_the_operator_session_store(
            _marker(), spec_environment={"PATH": "/usr/bin", "HOME": str(home)}
        )

        assert touched is False


class TestParentCodexHomeSetSpecWithCodexHome:
    """Both a spec-own CODEX_HOME and the parent's differ; both are stores
    the probe (or an unrelated concurrent ha process) could have written to,
    and both are watched."""

    def test_a_write_to_the_specs_own_store_is_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        parent_codex_home = tmp_path / "parent-codex-home"
        parent_codex_home.mkdir()
        _set_parent_codex_home(monkeypatch, home=home, codex_home=parent_codex_home)
        spec_codex_home = tmp_path / "spec-codex-home"
        spec_codex_home.mkdir()
        _plant_rollout(spec_codex_home)

        touched = _codex_touched_the_operator_session_store(
            _marker(),
            spec_environment={
                "PATH": "/usr/bin",
                "HOME": str(home),
                "CODEX_HOME": str(spec_codex_home),
            },
        )

        assert touched is True

    def test_a_write_to_the_parent_process_own_store_is_also_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        parent_codex_home = tmp_path / "parent-codex-home"
        parent_codex_home.mkdir()
        _set_parent_codex_home(monkeypatch, home=home, codex_home=parent_codex_home)
        spec_codex_home = tmp_path / "spec-codex-home"
        spec_codex_home.mkdir()
        _plant_rollout(parent_codex_home)

        touched = _codex_touched_the_operator_session_store(
            _marker(),
            spec_environment={
                "PATH": "/usr/bin",
                "HOME": str(home),
                "CODEX_HOME": str(spec_codex_home),
            },
        )

        assert touched is True

    def test_no_write_at_all_is_not_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        parent_codex_home = tmp_path / "parent-codex-home"
        parent_codex_home.mkdir()
        _set_parent_codex_home(monkeypatch, home=home, codex_home=parent_codex_home)
        spec_codex_home = tmp_path / "spec-codex-home"
        spec_codex_home.mkdir()

        touched = _codex_touched_the_operator_session_store(
            _marker(),
            spec_environment={
                "PATH": "/usr/bin",
                "HOME": str(home),
                "CODEX_HOME": str(spec_codex_home),
            },
        )

        assert touched is False


def test_an_older_rollout_does_not_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clear_parent_codex_home(monkeypatch, home=home)
    _plant_rollout(home / ".codex")
    marker = time.time() + 60.0

    touched = _codex_touched_the_operator_session_store(
        marker, spec_environment={"PATH": "/usr/bin", "HOME": str(home)}
    )

    assert touched is False


def test_another_clients_recent_session_does_not_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clear_parent_codex_home(monkeypatch, home=home)
    rollout = _plant_rollout(home / ".codex")
    rollout = rollout.rename(rollout.with_name("rollout-2026-09-27T04-18-26-desktop-thread.jsonl"))
    rollout.write_text('{"type":"session_meta","payload":{"id":"desktop-thread"}}\n')

    assert not _codex_touched_the_operator_session_store(
        _marker(),
        spec_environment={"HOME": str(home)},
        probe_thread_ids={"probe-thread"},
    )


@pytest.mark.parametrize(
    ("thread_id", "expected"), [("desktop-thread", False), ("probe-thread", True)]
)
def test_long_session_metadata_is_attributed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, thread_id: str, expected: bool
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clear_parent_codex_home(monkeypatch, home=home)
    rollout = _plant_rollout(home / ".codex")
    rollout = rollout.rename(rollout.with_name(f"rollout-2026-09-27T04-18-26-{thread_id}.jsonl"))
    payload = json.dumps(
        {"type": "session_meta", "payload": {"id": thread_id, "instructions": "x" * 20_000}}
    )
    rollout.write_text(payload + "\n")

    assert (
        _codex_touched_the_operator_session_store(
            _marker(), spec_environment={"HOME": str(home)}, probe_thread_ids={"probe-thread"}
        )
        is expected
    )


def test_session_metadata_without_newline_within_bound_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clear_parent_codex_home(monkeypatch, home=home)
    rollout = _plant_rollout(home / ".codex")
    rollout.write_bytes(
        b'{"type":"session_meta","payload":{"id":"desktop-thread"},"pad":"' + b"x" * 1_048_600
    )

    assert _codex_touched_the_operator_session_store(
        _marker(), spec_environment={"HOME": str(home)}, probe_thread_ids={"probe-thread"}
    )


def test_the_probes_recent_session_counts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clear_parent_codex_home(monkeypatch, home=home)
    rollout = _plant_rollout(home / ".codex")
    rollout = rollout.rename(rollout.with_name("rollout-2026-09-27T04-18-26-probe-thread.jsonl"))
    rollout.write_text('{"type":"session_meta","payload":{"id":"probe-thread"}}\n')

    assert _codex_touched_the_operator_session_store(
        _marker(),
        spec_environment={"HOME": str(home)},
        probe_thread_ids={"probe-thread"},
    )


def test_unidentified_recent_session_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clear_parent_codex_home(monkeypatch, home=home)
    _plant_rollout(home / ".codex")

    assert _codex_touched_the_operator_session_store(
        _marker(), spec_environment={"HOME": str(home)}, probe_thread_ids={"probe-thread"}
    )


def test_session_filename_disagrees_with_metadata_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _clear_parent_codex_home(monkeypatch, home=home)
    rollout = _plant_rollout(home / ".codex")
    rollout.write_text('{"type":"session_meta","payload":{"id":"desktop-thread"}}\n')

    assert _codex_touched_the_operator_session_store(
        _marker(), spec_environment={"HOME": str(home)}, probe_thread_ids={"probe-thread"}
    )


def test_malformed_probe_events_cannot_bind_a_thread(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text('null\n{"type":"thread.started","thread_id":"t"}\n')
    assert _probe_thread_ids([run_dir]) is None


def test_two_probe_runs_sharing_a_thread_cannot_be_attributed(tmp_path: Path) -> None:
    run_dirs = [tmp_path / name for name in ("first", "second")]
    for run_dir in run_dirs:
        run_dir.mkdir()
        (run_dir / "events.jsonl").write_text('{"type":"thread.started","thread_id":"same"}\n')
    assert _probe_thread_ids(run_dirs) is None
