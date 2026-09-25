"""The run registry: engine-minted ids, one entry per run, liveness from a lock (spec 0.5.0 §3.8.1)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from headless_agents.runs import (
    RUN_ID_PATTERN,
    Registry,
    RegistryError,
    make_run_dir,
)
from headless_agents.state import Unknown

_TARGET = {"kind": "role", "name": "codex"}


def _registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "state", runs_root=tmp_path / "cache" / "runs")


def test_a_minted_id_has_the_spec_shape(tmp_path: Path) -> None:
    assert RUN_ID_PATTERN.fullmatch(_registry(tmp_path).mint())


def test_register_writes_one_entry_and_defaults_the_run_dir_to_the_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    entry = registry.register(run_dir=None, target=_TARGET, repository=None, lineage=None)
    assert entry.run_dir == tmp_path / "cache" / "runs" / entry.run_id
    document = json.loads((tmp_path / "state" / "runs" / f"{entry.run_id}.json").read_text())
    assert document["run_id"] == entry.run_id
    assert document["run_dir"] == str(entry.run_dir)
    assert document["target"] == _TARGET
    assert document["status"] == "running"


def test_a_colliding_mint_is_retried_and_the_run_dir_follows_the_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Focus 4: two processes minting in the same second."""
    registry = _registry(tmp_path)
    ids = iter(["20260925T000000-aaaaaaaa", "20260925T000000-aaaaaaaa", "20260925T000000-bbbbbbbb"])
    monkeypatch.setattr(registry, "mint", lambda: next(ids))
    first = registry.register(run_dir=None, target=_TARGET, repository=None, lineage=None)
    second = registry.register(run_dir=None, target=_TARGET, repository=None, lineage=None)
    assert (first.run_id, second.run_id) == (
        "20260925T000000-aaaaaaaa",
        "20260925T000000-bbbbbbbb",
    )
    assert second.run_dir.name == "20260925T000000-bbbbbbbb"


def test_two_custom_run_dirs_of_the_same_name_are_two_runs(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    a = registry.register(
        run_dir=tmp_path / "a" / "x", target=_TARGET, repository=None, lineage=None
    )
    b = registry.register(
        run_dir=tmp_path / "b" / "x", target=_TARGET, repository=None, lineage=None
    )
    assert a.run_id != b.run_id
    assert registry.resolve(a.run_id).run_dir == tmp_path / "a" / "x"
    assert registry.resolve(b.run_id).run_dir == tmp_path / "b" / "x"


@pytest.mark.parametrize("bad", ["", ".", "..", "../x", "a/b", "20260925T000000-AAAAAAAA", "x"])
def test_resolve_refuses_what_is_not_a_run_id(tmp_path: Path, bad: str) -> None:
    with pytest.raises(RegistryError, match="not a run id"):
        _registry(tmp_path).resolve(bad)


def test_resolve_of_an_unregistered_id_is_a_registry_error(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="no run"):
        _registry(tmp_path).resolve("20260925T000000-aaaaaaaa")


def test_a_corrupt_entry_is_unknown(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    entry = registry.register(run_dir=None, target=_TARGET, repository=None, lineage=None)
    (tmp_path / "state" / "runs" / f"{entry.run_id}.json").write_text("{")
    with pytest.raises(Unknown):
        registry.resolve(entry.run_id)


def test_status_and_cleaned_at_are_recorded(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    entry = registry.register(run_dir=None, target=_TARGET, repository=Path("/r"), lineage=None)
    registry.set_status(entry.run_id, "answered")
    registry.set_cleaned(entry.run_id, "2026-09-25T00:00:00Z")
    again = registry.resolve(entry.run_id)
    assert (again.status, again.cleaned_at, again.repository) == (
        "answered",
        "2026-09-25T00:00:00Z",
        Path("/r"),
    )


_HOLD = """
import fcntl, os, pathlib, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
pathlib.Path(sys.argv[2]).write_text("ok")
time.sleep(60)
"""


def test_a_non_final_status_reads_running_while_its_lock_is_held_then_incomplete(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    entry = registry.register(run_dir=None, target=_TARGET, repository=None, lineage=None)
    lock = registry.lifecycle_lock(entry.run_id)
    ready = tmp_path / "ready"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(lock), str(ready)])
    try:
        while not ready.exists():
            time.sleep(0.02)
        assert registry.effective_status(entry, None) == "running"
    finally:
        holder.kill()
        holder.wait()
    assert registry.effective_status(entry, None) == "incomplete"


def test_a_final_status_is_itself_and_a_lineage_status_wins(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    write_run = registry.register(run_dir=None, target=_TARGET, repository=None, lineage="L")
    assert registry.effective_status(write_run, "committed") == "committed"
    # A lineage member's status has one authority, its lineage state: the
    # registry entry's own field is never read for it.
    registry.set_status(write_run.run_id, "answered")
    assert registry.effective_status(registry.resolve(write_run.run_id), None) == "incomplete"
    other = registry.register(run_dir=None, target=_TARGET, repository=None, lineage=None)
    registry.set_status(other.run_id, "answered")
    assert registry.effective_status(registry.resolve(other.run_id), None) == "answered"


def _roots(tmp_path: Path) -> dict[str, Path]:
    roots = {
        "the repository": tmp_path / "repo",
        "the state directory": tmp_path / "state",
        "another run": tmp_path / "runs" / "x",
    }
    for root in roots.values():
        root.mkdir(parents=True)
    return roots


@pytest.mark.parametrize("label", ["the repository", "the state directory", "another run"])
def test_a_run_dir_inside_a_forbidden_tree_is_refused(tmp_path: Path, label: str) -> None:
    """Review Focus 3."""
    roots = _roots(tmp_path)
    with pytest.raises(RegistryError, match=label):
        make_run_dir(roots[label] / "mine", forbidden=roots)


def test_an_existing_run_dir_is_refused(tmp_path: Path) -> None:
    (tmp_path / "exists").mkdir()
    with pytest.raises(RegistryError, match="already exists"):
        make_run_dir(tmp_path / "exists", forbidden={})


def test_a_new_run_dir_is_created_with_its_parents(tmp_path: Path) -> None:
    make_run_dir(tmp_path / "a" / "b" / "run", forbidden=_roots(tmp_path))
    assert (tmp_path / "a" / "b" / "run").is_dir()
