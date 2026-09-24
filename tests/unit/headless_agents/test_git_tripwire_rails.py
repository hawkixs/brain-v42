"""Every CLI rail arms the ``.git`` tripwire on a writable workspace (ticket 0b622f47).

The rail's process is replaced by a fake that behaves like an agent: it
answers, and -- in the tampering case -- plants a hook under ``.git`` on the
way. The run must come back as a non-replayable failure, name the path on
stderr and in ``result.json``, and give no answer; a clean run is unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from headless_agents.profile import CapabilityProfile, Workspace
from headless_agents.providers import agy, claude, codex, opencode
from headless_agents.spec import RunSpec

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

RAILS: dict[str, tuple[object, str, Callable[[], object]]] = {
    "claude": (claude, "run_claude", claude.ClaudeProvider),
    "codex": (codex, "run_codex", codex.CodexProvider),
    "opencode": (opencode, "run_opencode", opencode.OpenCodeProvider),
    "agy": (agy, "run_agy", agy.AgyProvider),
}


@pytest.fixture
def workspace_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    root = tmp_path / "ws"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _fake_agent(*, plant: Path | None, rail: str) -> Callable[..., int]:
    def fake(**kwargs: object) -> int:
        answer = kwargs.get("answer_log") if rail == "claude" else kwargs.get("report_log")
        target = answer if isinstance(answer, Path) else kwargs.get("raw_log")
        assert isinstance(target, Path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("done\n", encoding="utf-8")
        if plant is not None:
            plant.write_text("#!/bin/sh\ntouch /tmp/pwned\n", encoding="utf-8")
            plant.chmod(0o755)
        return 0

    return fake


def _run(
    rail: str,
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plant: bool,
    write: bool = True,
):
    module, function, provider = RAILS[rail]
    hook = root / ".git" / "hooks" / "pre-commit"
    monkeypatch.setattr(module, function, _fake_agent(plant=hook if plant else None, rail=rail))
    run_dir = tmp_path / "runs" / rail
    spec = RunSpec(
        prompt="edit the code",
        model="m",
        run_dir=run_dir,
        profile=CapabilityProfile(workspace=Workspace(path=root, write=write)),
    )
    return provider().run(spec), run_dir, hook


@pytest.mark.parametrize("rail", sorted(RAILS))
def test_a_planted_hook_fails_the_run_and_is_named(
    rail: str, workspace_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, run_dir, hook = _run(rail, workspace_repo, tmp_path, monkeypatch, plant=True)
    assert result.exit_code == 1
    assert result.text is None
    assert result.workspace is not None
    assert result.workspace["git_tampered"] == [str(hook)]
    recorded = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert recorded["workspace"]["git_tampered"] == [str(hook)]
    stderr = result.stderr_log if result.stderr_log is not None else result.raw_log
    assert stderr is not None and "git tripwire" in stderr.read_text(encoding="utf-8")


@pytest.mark.parametrize("rail", sorted(RAILS))
def test_a_clean_writable_run_is_unchanged_and_says_so(
    rail: str, workspace_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run(rail, workspace_repo, tmp_path, monkeypatch, plant=False)
    assert result.exit_code == 0
    assert result.text == "done\n"
    assert result.workspace is not None
    assert result.workspace["git_tampered"] == []


@pytest.mark.parametrize("rail", sorted(RAILS))
def test_a_read_only_run_is_not_armed(
    rail: str, workspace_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run(rail, workspace_repo, tmp_path, monkeypatch, plant=False, write=False)
    assert result.workspace is not None
    assert "git_tampered" not in result.workspace


def test_the_home_the_tripwire_reads_is_the_real_one(workspace_repo: Path) -> None:
    """Sanity: the fixture pointed HOME at the throwaway directory."""
    assert Path(os.environ["HOME"]).name == "home"
