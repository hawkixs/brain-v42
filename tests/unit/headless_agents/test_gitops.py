"""Hardened git with hooks disabled, except for the engine's commit (spec 0.5.0 §3.8.3, §4)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from headless_agents import gitops
from headless_agents.git_tripwire import GitTampered

_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"}
_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(root), "add", "f.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(root), *_IDENTITY, "commit", "-q", "-m", "init"],
        check=True,
        env={**_ENV, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return root


def _plant_hook(repo: Path, name: str, marker: Path) -> None:
    hook = repo / ".git" / "hooks" / name
    hook.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    hook.chmod(0o755)


def test_the_empty_hooks_dir_is_private_and_empty(tmp_path: Path) -> None:
    empty = gitops.empty_hooks_dir(tmp_path / "state")
    assert empty == tmp_path / "state" / "empty-hooks"
    assert stat.S_IMODE(empty.stat().st_mode) == 0o700
    assert not any(empty.iterdir())


def test_a_planted_hook_does_not_run(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    _plant_hook(repo, "post-checkout", marker)
    result = gitops.git(
        repo, ["worktree", "add", "-q", str(tmp_path / "wt"), "HEAD"], _ENV, state=tmp_path / "s"
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_the_engine_commit_runs_hooks(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    _plant_hook(repo, "post-checkout", marker)
    result = gitops.git(
        repo,
        ["worktree", "add", "-q", str(tmp_path / "wt"), "HEAD"],
        _ENV,
        state=tmp_path / "s",
        hooks=True,
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists()


def test_a_file_in_the_empty_hooks_dir_refuses_before_running(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "s"
    (gitops.empty_hooks_dir(state) / "post-checkout").write_text("#!/bin/sh\n")
    calls: list[object] = []
    monkeypatch.setattr(gitops.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(GitTampered, match="empty hooks directory is not empty"):
        gitops.git(repo, ["status"], _ENV, state=state)
    assert calls == []


def test_a_fired_tripwire_refuses_before_running(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(GitTampered):
        gitops.git(repo, ["status"], _ENV, state=tmp_path / "s", tampered=["x"])


def test_git_runs_bounded_and_without_inherited_git_variables(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs, command=command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(gitops.subprocess, "run", fake_run)
    gitops.git(repo, ["status"], {**_ENV, "GIT_DIR": "/elsewhere"}, state=tmp_path / "s")
    assert seen["timeout"] == gitops.GIT_TIMEOUT_SECONDS
    env = seen["env"]
    assert isinstance(env, dict) and "GIT_DIR" not in env
    command = seen["command"]
    assert isinstance(command, list)
    assert f"core.hooksPath={tmp_path / 's' / 'empty-hooks'}" in command
    assert command[-1] == "status"
