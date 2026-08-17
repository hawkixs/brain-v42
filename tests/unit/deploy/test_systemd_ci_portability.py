"""Hermetic CI-portability contracts for the Dream systemd smoke."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = ROOT / "deploy" / "systemd"
INSTALL = SYSTEMD_DIR / "install.sh"
INTEGRATION_SCRIPT = ROOT / "tests" / "integration" / "test_dream_systemd_install.sh"
PYTHON_RESOLVER = ROOT / "tests" / "integration" / "resolve_test_python.sh"


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip())
    path.chmod(0o755)


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(SYSTEMD_DIR, repo / "deploy" / "systemd")
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "dream.sh", repo / "scripts" / "dream.sh")
    shutil.copy2(
        ROOT / "scripts" / "check_mcp_http_port.py",
        repo / "scripts" / "check_mcp_http_port.py",
    )
    shutil.copy2(
        ROOT / "scripts" / "check_graph_projector_env.py",
        repo / "scripts" / "check_graph_projector_env.py",
    )
    _write_executable(
        repo / ".venv" / "bin" / "python",
        """
        #!/bin/sh
        exit 0
    """,
    )
    return repo


def _installer_environment(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    fake_bin = tmp_path / "install-bin"
    logs = {
        "id": tmp_path / "id.log",
        "loginctl": tmp_path / "loginctl.log",
        "systemctl": tmp_path / "systemctl.log",
        "systemd_analyze": tmp_path / "systemd-analyze.log",
    }
    _write_executable(
        fake_bin / "systemctl",
        """
        #!/bin/sh
        printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
        case "$*" in
          "--user is-active brain-mcp-http-watchdog.timer"|\
          "--user is-active brain-mcp-http-watchdog.service")
            printf 'inactive\n'
            exit 3
            ;;
          "--user is-enabled brain-mcp-http-watchdog.timer")
            printf 'disabled\n'
            exit 1
            ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "systemd-analyze",
        """
        #!/bin/sh
        printf '%s\n' "$*" >> "$SYSTEMD_ANALYZE_LOG"
        """,
    )
    _write_executable(
        fake_bin / "id",
        """
        #!/bin/sh
        printf '%s\n' "$*" >> "$ID_LOG"
        [ "$#" -eq 1 ] && [ "$1" = "-u" ] || exit 64
        printf '4242\n'
        """,
    )
    _write_executable(
        fake_bin / "loginctl",
        """
        #!/bin/sh
        printf '%s\n' "$*" >> "$LOGINCTL_LOG"
        printf 'Linger=yes\n'
        """,
    )

    env = os.environ.copy()
    env.pop("USER", None)
    for key in tuple(env):
        if key.casefold() in {
            "brain_dream_capability_enforcement",
            "graph_ledger_write_enabled",
            "graph_projector_enabled",
            "graph_projector_neo4j_password",
            "graph_projector_neo4j_url",
            "graph_projector_neo4j_user",
            "mcp_http_dream_tokens",
            "mcp_http_host",
            "mcp_http_port",
            "mcp_http_token",
            "neo4j_password",
            "neo4j_url",
            "neo4j_user",
        }:
            env.pop(key)
    env.update(
        {
            "MCP_HTTP_HOST": "127.0.0.1",
            "MCP_HTTP_PORT": "8765",
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "ID_LOG": str(logs["id"]),
            "LOGINCTL_LOG": str(logs["loginctl"]),
            "SYSTEMCTL_LOG": str(logs["systemctl"]),
            "SYSTEMD_ANALYZE_LOG": str(logs["systemd_analyze"]),
        }
    )
    return env, logs


def _run_installer(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "deploy" / "systemd" / "install.sh")],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_resolver(
    source_root: Path,
    path: Path,
    *,
    cwd: Path | None = None,
    override: Path | None = None,
    prove_no_git_or_venv: bool = False,
) -> subprocess.CompletedProcess[str]:
    preflight = ""
    if prove_no_git_or_venv:
        preflight = """
        if [[ -n "$(type -P git)" ]]; then
          echo "unexpected git executable" >&2
          exit 90
        fi
        if [[ -x "$SOURCE_ROOT/.venv/bin/python" ]]; then
          echo "unexpected worktree venv" >&2
          exit 91
        fi
        """
    script = f"""
        set -euo pipefail
        SOURCE_ROOT="$1"
        source "$2"
        {preflight}
        resolve_test_python "$SOURCE_ROOT"
    """
    env = {"PATH": str(path)}
    if override is not None:
        env["BRAIN_TEST_PYTHON"] = str(override)
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            textwrap.dedent(script),
            "resolver-test",
            str(source_root),
            str(PYTHON_RESOLVER),
        ],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_normal_install_uses_effective_uid_when_user_is_unset(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    env, logs = _installer_environment(tmp_path)

    result = _run_installer(repo, env)

    assert result.returncode == 0, result.stderr
    assert logs["id"].read_text().splitlines() == ["-u"]
    assert logs["loginctl"].read_text().splitlines() == ["show-user -- 4242"]
    assert "brain-v42-automation.service remains dormant" in result.stdout
    assert "brain-v42-automation" not in logs["systemctl"].read_text()


def test_failed_linger_probe_warns_without_failing_install(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    env, logs = _installer_environment(tmp_path)
    fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
    _write_executable(
        fake_bin / "loginctl",
        """
        #!/bin/sh
        printf '%s\n' "$*" >> "$LOGINCTL_LOG"
        exit 1
        """,
    )

    result = _run_installer(repo, env)

    assert result.returncode == 0, result.stderr
    assert logs["loginctl"].read_text().splitlines() == ["show-user -- 4242"]
    assert "WARN: unable to determine linger status for UID 4242." in result.stderr
    assert "brain-v42-automation.service remains dormant" in result.stdout
    assert "brain-v42-automation" not in logs["systemctl"].read_text()


def test_integration_smoke_uses_the_portable_python_resolver() -> None:
    script = INTEGRATION_SCRIPT.read_text()

    assert 'source "$SOURCE_ROOT/tests/integration/resolve_test_python.sh"' in script
    assert 'PROJECT_PYTHON="$(resolve_test_python "$SOURCE_ROOT")"' in script


def test_integration_smoke_copies_all_preflights_and_pins_mcp_binding() -> None:
    script = INTEGRATION_SCRIPT.read_text()

    assert "check_mcp_http_port.py" in script
    assert "check_graph_projector_env.py" in script
    install_invocation = next(
        line for line in script.splitlines() if '"$INSTALL_SCRIPT" --dry-run' in line
    )
    assert 'MCP_HTTP_HOST="127.0.0.1"' in install_invocation
    assert 'MCP_HTTP_PORT="8765"' in install_invocation


def test_python_resolver_accepts_executable_explicit_override(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    path = tmp_path / "empty-bin"
    override = tmp_path / "chosen-python"
    source_root.mkdir()
    path.mkdir()
    _write_executable(override, "#!/bin/sh\nexit 0\n")

    result = _run_resolver(source_root, path, override=override)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(override)


def test_python_resolver_rejects_invalid_explicit_override_without_fallback(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    path = tmp_path / "bin"
    invalid_override = tmp_path / "not-executable-python"
    source_root.mkdir()
    path.mkdir()
    invalid_override.write_text("not executable\n")
    _write_executable(path / "python3", "#!/bin/sh\nexit 0\n")

    result = _run_resolver(source_root, path, override=invalid_override)

    assert result.returncode != 0
    assert f"ERROR: BRAIN_TEST_PYTHON is not executable: {invalid_override}" in result.stderr
    assert result.stdout == ""


def test_python_resolver_rejects_executable_directory_override_without_fallback(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    path = tmp_path / "bin"
    directory_override = tmp_path / "executable-directory"
    source_root.mkdir()
    path.mkdir()
    directory_override.mkdir()
    directory_override.chmod(0o755)
    _write_executable(path / "python3", "#!/bin/sh\nexit 0\n")

    result = _run_resolver(source_root, path, override=directory_override)

    assert result.returncode != 0
    assert f"ERROR: BRAIN_TEST_PYTHON is not executable: {directory_override}" in result.stderr
    assert result.stdout == ""


def test_python_resolver_normalizes_relative_explicit_override(tmp_path: Path) -> None:
    cwd = tmp_path / "resolver working directory"
    source_root = tmp_path / "source"
    path = tmp_path / "empty-bin"
    relative_override = Path("relative bin") / "python"
    absolute_override = cwd / relative_override
    cwd.mkdir()
    source_root.mkdir()
    path.mkdir()
    _write_executable(absolute_override, "#!/bin/sh\nexit 0\n")

    result = _run_resolver(
        source_root,
        path,
        cwd=cwd,
        override=relative_override,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(absolute_override)


def test_python_resolver_uses_common_worktree_venv_when_git_resolves_it(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "linked-worktree"
    common_root = tmp_path / "common-repo"
    path = tmp_path / "bin"
    git_log = tmp_path / "git.log"
    source_root.mkdir()
    path.mkdir()
    common_python = common_root / ".venv" / "bin" / "python"
    _write_executable(common_python, "#!/bin/sh\nexit 0\n")
    _write_executable(
        path / "git",
        f"""
        #!/bin/sh
        printf '%s\n' "$*" >> {shlex.quote(str(git_log))}
        printf '%s\n' {shlex.quote(str(common_root / ".git"))}
        """,
    )
    _write_executable(path / "python3", "#!/bin/sh\nexit 0\n")

    result = _run_resolver(source_root, path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(common_python)
    assert git_log.read_text().splitlines() == [
        f"-C {source_root} rev-parse --path-format=absolute --git-common-dir"
    ]


def test_python_resolver_ignores_executable_directory_in_common_worktree_venv(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "linked worktree"
    common_root = tmp_path / "common repo"
    path = tmp_path / "bin"
    source_root.mkdir()
    path.mkdir()
    common_candidate = common_root / ".venv" / "bin" / "python"
    common_candidate.mkdir(parents=True)
    common_candidate.chmod(0o755)
    _write_executable(
        path / "git",
        f"""
        #!/bin/sh
        printf '%s\n' {shlex.quote(str(common_root / ".git"))}
        """,
    )
    fallback = path / "python3"
    _write_executable(fallback, "#!/bin/sh\nexit 0\n")

    result = _run_resolver(source_root, path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(fallback)


def test_python_resolver_falls_back_to_python3_without_git_or_worktree_venv(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source-without-venv"
    path = tmp_path / "bin-without-git"
    source_root.mkdir()
    path.mkdir()
    python3 = path / "python3"
    _write_executable(python3, "#!/bin/sh\nexit 0\n")

    result = _run_resolver(source_root, path, prove_no_git_or_venv=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(python3)


def test_python_resolver_falls_back_to_python_when_python3_is_absent(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    path = tmp_path / "bin"
    source_root.mkdir()
    path.mkdir()
    python = path / "python"
    _write_executable(python, "#!/bin/sh\nexit 0\n")

    result = _run_resolver(source_root, path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(python)


@pytest.mark.parametrize("command_name", ["python3", "python"])
def test_python_resolver_normalizes_relative_path_candidate(
    tmp_path: Path,
    command_name: str,
) -> None:
    cwd = tmp_path / "resolver working directory"
    source_root = tmp_path / "source"
    relative_bin = Path("relative bin")
    executable = cwd / relative_bin / command_name
    cwd.mkdir()
    source_root.mkdir()
    _write_executable(executable, "#!/bin/sh\nexit 0\n")

    result = _run_resolver(source_root, relative_bin, cwd=cwd)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(executable)


def test_python_resolver_falls_back_when_git_rev_parse_fails(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    path = tmp_path / "bin"
    git_log = tmp_path / "git.log"
    source_root.mkdir()
    path.mkdir()
    _write_executable(
        path / "git",
        f"""
        #!/bin/sh
        printf '%s\n' "$*" >> {shlex.quote(str(git_log))}
        exit 7
        """,
    )
    python3 = path / "python3"
    _write_executable(python3, "#!/bin/sh\nexit 0\n")

    result = _run_resolver(source_root, path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(python3)
    assert git_log.read_text().splitlines() == [
        f"-C {source_root} rev-parse --path-format=absolute --git-common-dir"
    ]


def test_python_resolver_fails_clearly_when_no_interpreter_exists(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    path = tmp_path / "empty-bin"
    source_root.mkdir()
    path.mkdir()

    result = _run_resolver(source_root, path)

    assert result.returncode != 0
    assert "ERROR: no executable Python interpreter found" in result.stderr
    assert result.stdout == ""
