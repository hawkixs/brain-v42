"""Hermetic contracts for the standalone delivery-observer systemd installer."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = ROOT / "deploy" / "systemd"
TEMPLATE = SYSTEMD_DIR / "brain-v42-delivery-observer.service.tmpl"
INSTALLER = SYSTEMD_DIR / "install-delivery-observer.sh"
UNIT = "brain-v42-delivery-observer.service"
SECRET = "delivery-observer-private-token-never-output"


def _write(path: Path, content: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(mode)


def _fixture_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    systemd = repo / "deploy" / "systemd"
    systemd.mkdir(parents=True)
    shutil.copy2(TEMPLATE, systemd / TEMPLATE.name)
    shutil.copy2(INSTALLER, systemd / INSTALLER.name)
    (systemd / INSTALLER.name).chmod(0o755)
    python = repo / ".venv" / "bin" / "python"
    _write(python, "#!/bin/sh\nexit 0\n", mode=0o755)
    private = tmp_path / "private" / "delivery-observer.env"
    _write(
        private,
        f"BRAIN_DELIVERY_GITHUB_TOKEN={SECRET}\nBRAIN_DELIVERY_POSTGRES_URL=postgresql://unused\n",
        mode=0o600,
    )
    for directory in (repo, repo / ".venv", repo / ".venv" / "bin", private.parent):
        directory.chmod(0o755)
    return repo, python, private


def _environment(tmp_path: Path, python: Path, private: Path) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "fake-bin"
    analyzer_log = tmp_path / "systemd-analyze.log"
    _write(
        fake_bin / "systemd-analyze",
        """
        #!/bin/sh
        printf '%s\\n' "$*" >> "$SYSTEMD_ANALYZE_LOG"
        test "$1" = "--user"
        test "$2" = "verify"
        test -f "$3"
        grep -q '^ExecStart=' "$3"
        if [ -n "${ANALYZER_CREATE_TARGET:-}" ]; then
          mkdir "$ANALYZER_CREATE_TARGET"
          printf 'foreign target\n' > "$ANALYZER_CREATE_TARGET/marker"
        fi
        if [ -n "${ANALYZER_CHMOD_PARENT:-}" ]; then
          chmod 755 "$ANALYZER_CHMOD_PARENT"
        fi
        if [ -n "${ANALYZER_CHMOD_PATH:-}" ]; then
          chmod 777 "$ANALYZER_CHMOD_PATH"
        fi
        if [ -n "${ANALYZER_REPLACE_PATH:-}" ]; then
          mv "$ANALYZER_REPLACE_PATH" "$ANALYZER_REPLACE_PATH.replaced"
          printf 'BRAIN_DELIVERY_GITHUB_TOKEN=replaced-by-verifier\\n' > "$ANALYZER_REPLACE_PATH"
          chmod 600 "$ANALYZER_REPLACE_PATH"
        fi
        if [ "${ANALYZER_FAIL:-}" = "1" ]; then
          exit 41
        fi
        if [ "${ANALYZER_BLOCK:-}" = "1" ]; then
          : > "$ANALYZER_READY"
          while :; do sleep 1; done
        fi
        """,
        mode=0o755,
    )
    _write(
        fake_bin / "systemctl",
        """
        #!/bin/sh
        printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
        exit 97
        """,
        mode=0o755,
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "SYSTEMD_ANALYZE_LOG": str(analyzer_log),
            "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
            "BRAIN_DELIVERY_OBSERVER_PYTHON": str(python),
            "BRAIN_DELIVERY_OBSERVER_ENV_FILE": str(private),
        }
    )
    return env, analyzer_log


def _run(repo: Path, env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "deploy" / "systemd" / "install-delivery-observer.sh"), *arguments],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_template_declares_observer_only_hardened_service() -> None:
    assert TEMPLATE.is_file(), "delivery observer template is required"
    content = TEMPLATE.read_text(encoding="utf-8")
    required = {
        "EnvironmentFile=__ENV_FILE__",
        "ExecStart=__PYTHON__ -m brain_v42.delivery_observer --env-file __ENV_FILE__",
        "Restart=on-failure",
        "TimeoutStopSec=30",
        "UMask=0077",
        "NoNewPrivileges=true",
        "PrivateUsers=true",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    }
    assert required <= set(content.splitlines())
    assert "ListenStream=" not in content and "Socket=" not in content


def test_check_only_verifies_in_isolated_stage_without_persistent_output(tmp_path: Path) -> None:
    assert INSTALLER.is_file(), "delivery observer installer is required"
    repo, python, private = _fixture_repo(tmp_path)
    env, analyzer_log = _environment(tmp_path, python, private)

    result = _run(repo, env, "--check-only")

    assert result.returncode == 0, result.stderr
    assert "check-only" in result.stdout
    assert analyzer_log.read_text().startswith("--user verify ")
    assert not (repo / UNIT).exists()
    assert not (tmp_path / "systemctl.log").exists()
    assert SECRET not in result.stdout + result.stderr


def test_render_dir_publishes_only_verified_unit_without_systemctl(tmp_path: Path) -> None:
    repo, python, private = _fixture_repo(tmp_path)
    env, analyzer_log = _environment(tmp_path, python, private)
    parent = tmp_path / "render-root"
    parent.mkdir(mode=0o700)
    target = parent / "observer-units"

    result = _run(repo, env, "--render-dir", str(target))

    assert result.returncode == 0, result.stderr
    assert {path.name for path in target.iterdir()} == {UNIT}
    rendered = (target / UNIT).read_text(encoding="utf-8")
    assert f"EnvironmentFile={private}" in rendered
    assert f"ExecStart={python} -m brain_v42.delivery_observer --env-file {private}" in rendered
    assert stat.S_IMODE((target / UNIT).stat().st_mode) == 0o644
    assert analyzer_log.read_text().startswith("--user verify ")
    assert not (tmp_path / "systemctl.log").exists()
    assert not list(parent.glob(".brain-v42-delivery-observer.*"))
    assert SECRET not in result.stdout + result.stderr + rendered


def test_verifier_failure_leaves_no_render_target_or_staging(tmp_path: Path) -> None:
    repo, python, private = _fixture_repo(tmp_path)
    env, _ = _environment(tmp_path, python, private)
    parent = tmp_path / "render-root"
    parent.mkdir(mode=0o700)
    target = parent / "observer-units"
    env["ANALYZER_FAIL"] = "1"

    result = _run(repo, env, "--render-dir", str(target))

    assert result.returncode != 0
    assert not target.exists()
    assert not list(parent.glob(".brain-v42-delivery-observer.*"))
    assert SECRET not in result.stdout + result.stderr


def test_verifier_created_target_is_never_overwritten(tmp_path: Path) -> None:
    repo, python, private = _fixture_repo(tmp_path)
    env, _ = _environment(tmp_path, python, private)
    parent = tmp_path / "render-root"
    parent.mkdir(mode=0o700)
    target = parent / "observer-units"
    env["ANALYZER_CREATE_TARGET"] = str(target)

    result = _run(repo, env, "--render-dir", str(target))

    assert result.returncode != 0
    assert (target / "marker").read_text(encoding="utf-8") == "foreign target\n"
    assert not (target / UNIT).exists()
    assert not list(parent.glob(".brain-v42-delivery-observer.*"))


def test_parent_permission_change_during_verify_refuses_publication(tmp_path: Path) -> None:
    repo, python, private = _fixture_repo(tmp_path)
    env, _ = _environment(tmp_path, python, private)
    parent = tmp_path / "render-root"
    parent.mkdir(mode=0o700)
    target = parent / "observer-units"
    env["ANALYZER_CHMOD_PARENT"] = str(parent)

    result = _run(repo, env, "--render-dir", str(target))

    assert result.returncode != 0
    assert not target.exists()
    assert not list(parent.glob(".brain-v42-delivery-observer.*"))


def test_interruption_during_verify_cleans_isolated_staging(tmp_path: Path) -> None:
    repo, python, private = _fixture_repo(tmp_path)
    env, _ = _environment(tmp_path, python, private)
    parent = tmp_path / "render-root"
    parent.mkdir(mode=0o700)
    target = parent / "observer-units"
    ready = tmp_path / "analyzer-ready"
    env.update(ANALYZER_BLOCK="1", ANALYZER_READY=str(ready))
    process = subprocess.Popen(
        [
            str(repo / "deploy" / "systemd" / "install-delivery-observer.sh"),
            "--render-dir",
            str(target),
        ],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "controlled verifier did not begin"
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode != 0
    assert not target.exists()
    assert not list(parent.glob(".brain-v42-delivery-observer.*"))
    assert SECRET not in stdout + stderr


@pytest.mark.parametrize("mode", ["relative", "existing", "symlink", "parent_symlink"])
def test_render_dir_refuses_unsafe_or_non_new_targets(tmp_path: Path, mode: str) -> None:
    repo, python, private = _fixture_repo(tmp_path)
    env, _ = _environment(tmp_path, python, private)
    parent = tmp_path / "render-root"
    parent.mkdir(mode=0o700)
    target: Path | str = parent / "observer-units"
    if mode == "relative":
        target = "relative-output"
    elif mode == "existing":
        assert isinstance(target, Path)
        target.mkdir()
    elif mode == "symlink":
        assert isinstance(target, Path)
        target.symlink_to(tmp_path / "elsewhere")
    else:
        link = tmp_path / "linked-root"
        link.symlink_to(parent, target_is_directory=True)
        target = link / "observer-units"

    result = _run(repo, env, "--render-dir", str(target))

    assert result.returncode != 0
    assert "unsafe" in result.stderr or "absolute" in result.stderr or "new" in result.stderr
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "mode",
    [
        "public_private",
        "symlink_private",
        "relative_python",
        "symlink_python",
        "specifier_private",
        "dotdot_private",
    ],
)
def test_installer_refuses_unsafe_credential_or_interpreter_paths(
    tmp_path: Path, mode: str
) -> None:
    repo, python, private = _fixture_repo(tmp_path)
    env, _ = _environment(tmp_path, python, private)
    if mode == "public_private":
        private.chmod(0o644)
    elif mode == "symlink_private":
        link = private.with_name("linked.env")
        link.symlink_to(private)
        env["BRAIN_DELIVERY_OBSERVER_ENV_FILE"] = str(link)
    elif mode == "relative_python":
        env["BRAIN_DELIVERY_OBSERVER_PYTHON"] = ".venv/bin/python"
    elif mode == "symlink_python":
        link = python.with_name("python-link")
        link.symlink_to(python)
        env["BRAIN_DELIVERY_OBSERVER_PYTHON"] = str(link)
    elif mode == "specifier_private":
        special = private.with_name("observer%h.env")
        shutil.copy2(private, special)
        special.chmod(0o600)
        env["BRAIN_DELIVERY_OBSERVER_ENV_FILE"] = str(special)
    else:
        env["BRAIN_DELIVERY_OBSERVER_ENV_FILE"] = str(
            private.parent / ".." / "private" / private.name
        )

    result = _run(repo, env, "--check-only")

    assert result.returncode != 0
    assert SECRET not in result.stdout + result.stderr
    assert not (tmp_path / "systemctl.log").exists()


@pytest.mark.parametrize("configured_path", ["python", "private"])
def test_installer_refuses_configured_path_below_nonsticky_writable_ancestor(
    tmp_path: Path, configured_path: str
) -> None:
    repo, python, private = _fixture_repo(tmp_path)
    env, _ = _environment(tmp_path, python, private)
    shared = tmp_path / f"shared-{configured_path}"
    shared.mkdir()
    shared.chmod(0o777)

    if configured_path == "python":
        unsafe_python = shared / "python"
        shutil.copy2(python, unsafe_python)
        unsafe_python.chmod(0o755)
        env["BRAIN_DELIVERY_OBSERVER_PYTHON"] = str(unsafe_python)
    else:
        unsafe_private = shared / "delivery-observer.env"
        shutil.copy2(private, unsafe_private)
        unsafe_private.chmod(0o600)
        env["BRAIN_DELIVERY_OBSERVER_ENV_FILE"] = str(unsafe_private)

    result = _run(repo, env, "--check-only")

    assert result.returncode != 0
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("mutation", ["chmod_ancestor", "replace_private"])
def test_verifier_mutation_of_configured_paths_refuses_check_only_success(
    tmp_path: Path, mutation: str
) -> None:
    repo, python, private = _fixture_repo(tmp_path)
    private.parent.chmod(0o700)
    env, _ = _environment(tmp_path, python, private)
    if mutation == "chmod_ancestor":
        env["ANALYZER_CHMOD_PATH"] = str(private.parent)
    else:
        env["ANALYZER_REPLACE_PATH"] = str(private)

    result = _run(repo, env, "--check-only")

    assert result.returncode != 0
    assert "check-only: verified" not in result.stdout
    assert SECRET not in result.stdout + result.stderr
