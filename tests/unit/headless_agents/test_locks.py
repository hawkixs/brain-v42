"""Locks: fixed order, bounded waits, gone with their process (spec 0.5.0 §3.8.2)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from headless_agents import locks
from headless_agents.locks import LockTimeout, Rank, held, is_free

_HOLDER = """
import fcntl, os, pathlib, sys, time
path, mode, ready = sys.argv[1], sys.argv[2], sys.argv[3]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX if mode == "ex" else fcntl.LOCK_SH)
pathlib.Path(ready).write_text("ok")
time.sleep(60)
"""


@contextmanager
def _held_elsewhere(path: Path, mode: str, tmp_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    ready = tmp_path / f"ready-{mode}-{time.monotonic_ns()}"
    process = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path), mode, str(ready)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline, "the holder never took the lock"
            time.sleep(0.02)
        yield process
    finally:
        process.kill()
        process.wait()


def test_an_exclusive_holder_excludes_a_shared_taker(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    with _held_elsewhere(path, "ex", tmp_path):
        with pytest.raises(LockTimeout, match="the test lock"):
            with held(path, rank=Rank.LIFECYCLE, exclusive=False, wait=0.3, what="the test lock"):
                pass


def test_shared_holders_coexist(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    with _held_elsewhere(path, "sh", tmp_path):
        with held(path, rank=Rank.LIFECYCLE, exclusive=False, wait=0.3, what="x"):
            pass


def test_a_lock_dies_with_its_process(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    with _held_elsewhere(path, "ex", tmp_path) as holder:
        holder.kill()
        holder.wait()
        start = time.monotonic()
        with held(path, rank=Rank.LIFECYCLE, exclusive=True, wait=5, what="x"):
            pass
        assert time.monotonic() - start < 1


def test_no_wait_means_one_try(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    with _held_elsewhere(path, "ex", tmp_path):
        start = time.monotonic()
        with pytest.raises(LockTimeout):
            with held(path, rank=Rank.LIFECYCLE, exclusive=True, wait=None, what="x"):
                pass
        assert time.monotonic() - start < 0.5


def test_a_subprocess_does_not_inherit_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    with held(path, rank=Rank.LIFECYCLE, exclusive=True, what="x"):
        child = subprocess.Popen(["sleep", "5"])
        try:
            targets = [
                os.readlink(f"/proc/{child.pid}/fd/{fd}")
                for fd in os.listdir(f"/proc/{child.pid}/fd")
            ]
        finally:
            child.kill()
            child.wait()
    assert str(path) not in targets


def test_a_lower_rank_after_a_higher_one_is_a_programming_error(tmp_path: Path) -> None:
    with held(tmp_path / "u.lock", rank=Rank.UNCONFINED, exclusive=False, what="u"):
        with pytest.raises(RuntimeError, match="lock order violated"):
            with held(tmp_path / "r.lock", rank=Rank.LIFECYCLE, exclusive=True, what="r"):
                pass


def test_lineage_locks_go_in_ascending_owner_order(tmp_path: Path) -> None:
    with held(tmp_path / "a.lock", rank=Rank.LINEAGE, exclusive=False, what="a", key="a"):
        with held(tmp_path / "b.lock", rank=Rank.LINEAGE, exclusive=False, what="b", key="b"):
            pass
    with held(tmp_path / "b.lock", rank=Rank.LINEAGE, exclusive=False, what="b", key="b"):
        with pytest.raises(RuntimeError, match="lock order violated"):
            with held(tmp_path / "a.lock", rank=Rank.LINEAGE, exclusive=False, what="a", key="a"):
                pass


def test_the_order_is_free_again_after_release(tmp_path: Path) -> None:
    with held(tmp_path / "u.lock", rank=Rank.UNCONFINED, exclusive=False, what="u"):
        pass
    with held(tmp_path / "r.lock", rank=Rank.LIFECYCLE, exclusive=True, what="r"):
        pass


def test_is_free_tells_a_live_writer_from_readers(tmp_path: Path) -> None:
    path = tmp_path / "lineage.lock"
    assert is_free(path)
    assert not path.exists(), "probing creates nothing"
    with _held_elsewhere(path, "sh", tmp_path):
        assert is_free(path), "shared holders coexist: only a writer makes the probe fail"
    with _held_elsewhere(path, "ex", tmp_path) as writer:
        assert not is_free(path)
        writer.kill()
        writer.wait()
        assert is_free(path)


def test_the_bound_is_the_spec_value() -> None:
    assert locks.LOCK_WAIT_SECONDS == 10.0


def test_releasing_the_registry_lock_before_a_lineage_lock_keeps_the_order_true(
    tmp_path: Path,
) -> None:
    """Spec §3.8.3 releases the lineage registry lock after the intent, while the
    lineage lock stays held: the order check must then see the lineage lock, not
    the registry lock, as the last one held."""
    registry = held(tmp_path / "r", rank=Rank.LINEAGE_REGISTRY, exclusive=True, what="r")
    registry.__enter__()
    with held(tmp_path / "b", rank=Rank.LINEAGE, exclusive=True, what="b", key="b"):
        registry.__exit__(None, None, None)
        with pytest.raises(RuntimeError, match="lock order violated"):
            with held(tmp_path / "a", rank=Rank.LINEAGE, exclusive=False, what="a", key="a"):
                pass
    with held(tmp_path / "u", rank=Rank.UNCONFINED, exclusive=False, what="u"):
        pass
