"""The workstation headersHelper attributes Claude sessions to projects (ticket 8955769a).

The helper walks /proc from its parent to the Claude Code process and reads that
process's working directory. These tests run the real script under a copy of
/bin/sh named `claude`, started in the directory under test, so the walk itself is
exercised rather than bypassed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "deploy" / "clients" / "claude-brain-headers.sh"
TOKEN = "synthetic-test-token"

pytestmark = pytest.mark.skipif(
    not Path("/proc/self/exe").exists(), reason="the helper reads /proc (Linux only)"
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "BRAIN_CLAUDE_TOKEN_FILE": str(token),
        "BRAIN_AGENT_MAP_FILE": str(tmp_path / "agent-map"),
    }


@pytest.fixture
def claude(tmp_path: Path) -> Path:
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    shutil.copy2("/bin/sh", fake)
    return fake


def run(claude: Path, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # `; exit $?` keeps the shell alive as the helper's parent instead of exec'ing it.
    return subprocess.run(
        [str(claude), "-c", 'sh "$0"; exit $?', str(HELPER)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def headers(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
    assert result.returncode == 0, result.stderr
    [line] = result.stdout.splitlines()
    return json.loads(line)


def write_map(env: dict[str, str], *lines: str, mode: int = 0o600) -> None:
    path = Path(env["BRAIN_AGENT_MAP_FILE"])
    path.write_text("\n".join(lines) + "\n")
    path.chmod(mode)


def test_helper_is_versioned_and_executable() -> None:
    assert HELPER.is_file()
    assert os.access(HELPER, os.X_OK)


def test_unmapped_directory_keeps_the_path_as_agent(tmp_path, claude, env) -> None:
    work = tmp_path / "git" / "brain_v42"
    work.mkdir(parents=True)
    assert headers(run(claude, work, env)) == {
        "Authorization": f"Bearer {TOKEN}",
        "X-Brain-Agent": str(work.resolve()),
    }


def test_longest_mapped_prefix_names_the_project(tmp_path, claude, env) -> None:
    red = tmp_path / "git" / "ReD_v1"
    skills = red / "skills"
    (skills / "nested").mkdir(parents=True)
    write_map(
        env,
        "# operator map",
        f"{red.resolve()} red",
        f"{skills.resolve()}/ red-skills",
    )
    assert headers(run(claude, red, env))["X-Brain-Agent"] == "red"
    assert headers(run(claude, skills, env))["X-Brain-Agent"] == "red-skills"
    assert headers(run(claude, skills / "nested", env))["X-Brain-Agent"] == "red-skills"


def test_map_prefix_matches_whole_path_components_only(tmp_path, claude, env) -> None:
    red = tmp_path / "ReD_v1"
    sibling = tmp_path / "ReD_v1-old"
    red.mkdir()
    sibling.mkdir()
    write_map(env, f"{red.resolve()} red")
    assert headers(run(claude, sibling, env))["X-Brain-Agent"] == str(sibling.resolve())


def test_map_lines_with_an_invalid_key_are_ignored(tmp_path, claude, env) -> None:
    red = tmp_path / "ReD_v1"
    red.mkdir()
    write_map(env, f"{red.resolve()} Not_A_Key", "relative/path red", f"{red.resolve()}")
    assert headers(run(claude, red, env))["X-Brain-Agent"] == str(red.resolve())


def test_a_map_writable_by_others_is_ignored(tmp_path, claude, env) -> None:
    red = tmp_path / "ReD_v1"
    red.mkdir()
    write_map(env, f"{red.resolve()} red", mode=0o622)
    assert headers(run(claude, red, env))["X-Brain-Agent"] == str(red.resolve())


def test_home_directory_claims_no_project(claude, env) -> None:
    assert headers(run(claude, Path(env["HOME"]), env)) == {"Authorization": f"Bearer {TOKEN}"}


def test_home_directory_is_matched_before_the_map(claude, env) -> None:
    write_map(env, f"{env['HOME']} red")
    assert "X-Brain-Agent" not in headers(run(claude, Path(env["HOME"]), env))


def test_daemon_spare_claims_no_project(tmp_path, claude, env) -> None:
    root = Path("/tmp") / f"cc-daemon-{os.getuid()}-test-{tmp_path.name}"
    spare = root / "0123abcd" / "spare"
    spare.mkdir(parents=True)
    try:
        assert headers(run(claude, spare, env)) == {"Authorization": f"Bearer {TOKEN}"}
    finally:
        shutil.rmtree(root)


def test_a_path_unsafe_for_json_claims_no_project(tmp_path, claude, env) -> None:
    odd = tmp_path / 'quote"dir'
    odd.mkdir()
    assert "X-Brain-Agent" not in headers(run(claude, odd, env))


def test_missing_or_empty_token_fails_closed(tmp_path, claude, env) -> None:
    Path(env["BRAIN_CLAUDE_TOKEN_FILE"]).write_text("  \n")
    result = run(claude, tmp_path, env)
    assert result.returncode != 0 and result.stdout == ""
    env["BRAIN_CLAUDE_TOKEN_FILE"] = str(tmp_path / "absent")
    result = run(claude, tmp_path, env)
    assert result.returncode != 0 and result.stdout == ""
