"""Security contract for the root-owned Shrik environment publisher."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.unit._fixture_modes import make_directory

ROOT = Path(__file__).resolve().parents[3]
CONTROL = ROOT / "deploy" / "brain-shrik-env-control"
SUDOERS = ROOT / "deploy" / "sudoers" / "brain-shrik-env-control"


def _load_control() -> ModuleType:
    assert CONTROL.is_file(), "root-owned Shrik control helper is missing"
    loader = importlib.machinery.SourceFileLoader("brain_shrik_env_control", str(CONTROL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _write(path: Path, payload: bytes, mode: int) -> Path:
    path.write_bytes(payload)
    path.chmod(mode)
    return path


def test_publish_is_durable_and_reuses_the_same_path_for_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _load_control()
    private = tmp_path / "private"
    target_dir = tmp_path / "etc" / "shrik"
    make_directory(private, mode=0o700)
    make_directory(target_dir, parents=True)
    stage = private / ".shrik-env.install"
    target = _write(target_dir / "env", b"old-generation\n", 0o640)
    fsync_calls: list[int] = []
    real_fsync = control.os.fsync

    def tracked_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(control.os, "fsync", tracked_fsync)
    _write(stage, b"new-generation\n", 0o600)

    control._publish(
        stage,
        target,
        operator_uid=os.getuid(),
        target_uid=os.getuid(),
        target_gid=os.getgid(),
        max_bytes=1024,
    )

    assert target.read_bytes() == b"new-generation\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert (target.stat().st_uid, target.stat().st_gid) == (os.getuid(), os.getgid())
    assert not stage.exists()
    assert len(fsync_calls) >= 3

    _write(stage, b"old-generation\n", 0o600)
    control._publish(
        stage,
        target,
        operator_uid=os.getuid(),
        target_uid=os.getuid(),
        target_gid=os.getgid(),
        max_bytes=1024,
    )

    assert target.read_bytes() == b"old-generation\n"
    assert not stage.exists()


def test_publish_failure_before_replace_preserves_the_old_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _load_control()
    private = tmp_path / "private"
    target_dir = tmp_path / "etc" / "shrik"
    make_directory(private, mode=0o700)
    make_directory(target_dir, parents=True)
    stage = _write(private / ".shrik-env.install", b"new-generation\n", 0o600)
    target = _write(target_dir / "env", b"old-generation\n", 0o640)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace-canary")

    monkeypatch.setattr(control.os, "replace", fail_replace)

    with pytest.raises(control.ControlError, match="publication failed"):
        control._publish(
            stage,
            target,
            operator_uid=os.getuid(),
            target_uid=os.getuid(),
            target_gid=os.getgid(),
            max_bytes=1024,
        )

    assert target.read_bytes() == b"old-generation\n"
    assert stage.read_bytes() == b"new-generation\n"


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "mode", "oversize"])
def test_publish_rejects_unsafe_staging(
    tmp_path: Path,
    unsafe: str,
) -> None:
    control = _load_control()
    private = tmp_path / "private"
    target_dir = tmp_path / "etc" / "shrik"
    make_directory(private, mode=0o700)
    make_directory(target_dir, parents=True)
    target = _write(target_dir / "env", b"old-generation\n", 0o640)
    stage = private / ".shrik-env.install"
    source = _write(private / "source", b"new-generation\n", 0o600)
    if unsafe == "symlink":
        stage.symlink_to(source)
    elif unsafe == "hardlink":
        os.link(source, stage)
    elif unsafe == "mode":
        _write(stage, b"new-generation\n", 0o640)
    else:
        _write(stage, b"x" * 1025, 0o600)

    with pytest.raises(control.ControlError, match="unsafe staging file"):
        control._publish(
            stage,
            target,
            operator_uid=os.getuid(),
            target_uid=os.getuid(),
            target_gid=os.getgid(),
            max_bytes=1024,
        )

    assert target.read_bytes() == b"old-generation\n"


def test_check_is_read_only_and_service_actions_have_fixed_argv(tmp_path: Path) -> None:
    control = _load_control()
    helper = _write(tmp_path / "helper", b"#!/usr/bin/python3\n", 0o755)
    private_parent = tmp_path / "private-parent"
    make_directory(private_parent, mode=0o700)
    stage = private_parent / "rotation" / ".shrik-env.install"
    target_dir = tmp_path / "etc" / "shrik"
    make_directory(target_dir, parents=True)
    target = _write(target_dir / "env", b"current-generation\n", 0o640)
    before = (target.read_bytes(), target.stat().st_mode, tuple(target_dir.iterdir()))

    control._check_contract(
        helper,
        stage,
        target,
        operator_uid=os.getuid(),
        target_uid=os.getuid(),
        target_gid=os.getgid(),
    )

    assert (target.read_bytes(), target.stat().st_mode, tuple(target_dir.iterdir())) == before
    commands: list[list[str]] = []

    def run_command(args: list[str], **_kwargs: Any) -> Any:
        commands.append(args)
        return control.subprocess.CompletedProcess(args, 0)

    for action in ("stop", "start", "is-active"):
        control._service_action(action, runner=run_command)

    assert commands == [
        ["/usr/bin/systemctl", "stop", "red-shrik.service"],
        ["/usr/bin/systemctl", "start", "red-shrik.service"],
        ["/usr/bin/systemctl", "is-active", "red-shrik.service"],
    ]

    def report_inactive(args: list[str], **_kwargs: Any) -> Any:
        return control.subprocess.CompletedProcess(args, 3)

    assert control._service_action("is-active", runner=report_inactive) is False
    with pytest.raises(control.ControlError, match="unsupported service action"):
        control._service_action("restart", runner=run_command)

    helper.chmod(0o775)
    with pytest.raises(control.ControlError, match="unsafe privileged helper"):
        control._check_contract(
            helper,
            stage,
            target,
            operator_uid=os.getuid(),
            target_uid=os.getuid(),
            target_gid=os.getgid(),
        )


def test_is_active_reports_inactive_without_masking_its_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control = _load_control()
    monkeypatch.setattr(control, "_run", lambda action: action != "--is-active")

    assert control.main(["--is-active"]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "action": "is-active",
        "active": False,
        "status": "ok",
    }


def test_sudoers_grants_only_the_exact_root_helper_actions() -> None:
    grants = [
        line.strip()
        for line in SUDOERS.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    helper = "/usr/local/sbin/brain-shrik-env-control"
    assert grants == [
        f"hawixs ALL=(root) NOPASSWD: {helper} --check",
        f"hawixs ALL=(root) NOPASSWD: {helper} --publish",
        f"hawixs ALL=(root) NOPASSWD: {helper} --stop",
        f"hawixs ALL=(root) NOPASSWD: {helper} --start",
        f"hawixs ALL=(root) NOPASSWD: {helper} --is-active",
    ]
