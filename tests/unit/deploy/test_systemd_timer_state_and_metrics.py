"""Installer contracts for operator timer choices and the metrics unit."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit._fixture_modes import make_directory
from tests.unit.deploy.test_mcp_http_unit import _fake_systemd_environment, _run_installer
from tests.unit.deploy.test_systemd_sandbox_profiles import _section_directives

TIMERS = ("brain-v42-dream.timer", "brain-v42-graph-recon.timer")


@pytest.mark.parametrize("timer", TIMERS)
def test_reinstall_preserves_disabled_inactive_timer(tmp_path: Path, timer: str) -> None:
    environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
    make_directory(unit_dir, parents=True)
    (unit_dir / timer).write_text("existing timer\n")

    result = _run_installer(environment)

    assert result.returncode == 0, result.stderr
    calls = systemctl_log.read_text().splitlines()
    assert not any(
        call.split()[1] in {"enable", "disable", "start", "stop", "restart"}
        for call in calls
        if timer in call
    )
    assert f"{timer}: preserved enabled=disabled, active=inactive" in result.stdout
    other = next(name for name in TIMERS if name != timer)
    assert f"--user enable --now {other}" in calls
    assert f"timers enabled and started: {other}" in result.stdout
    assert f"timers left as found: {timer}" in result.stdout


def test_first_install_enables_and_starts_both_timers(tmp_path: Path) -> None:
    environment, systemctl_log, _ = _fake_systemd_environment(tmp_path)

    result = _run_installer(environment)

    assert result.returncode == 0, result.stderr
    calls = systemctl_log.read_text().splitlines()
    assert all(f"--user enable --now {timer}" in calls for timer in TIMERS)
    assert f"timers enabled and started: {' '.join(TIMERS)}" in result.stdout
    assert "timers left as found: none" in result.stdout


def test_explicit_flag_rearms_both_existing_timers(tmp_path: Path) -> None:
    environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
    make_directory(unit_dir, parents=True)
    for timer in TIMERS:
        (unit_dir / timer).write_text("existing timer\n")

    result = _run_installer(environment, "--enable-timers")

    assert result.returncode == 0, result.stderr
    calls = systemctl_log.read_text().splitlines()
    assert all(f"--user enable --now {timer}" in calls for timer in TIMERS)
    assert f"timers enabled and started: {' '.join(TIMERS)}" in result.stdout
    assert "timers left as found: none" in result.stdout


@pytest.mark.parametrize("mode", ["--dry-run", "--check-only", "--render-dir", "--uninstall"])
@pytest.mark.parametrize("flag_first", [True, False])
def test_enable_timers_rejects_other_modes_without_side_effects(
    tmp_path: Path, mode: str, flag_first: bool
) -> None:
    environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
    arguments = [mode]
    if mode == "--render-dir":
        arguments.append(str(tmp_path / "rendered"))
    arguments = ["--enable-timers", *arguments] if flag_first else [*arguments, "--enable-timers"]

    result = _run_installer(environment, *arguments)

    assert result.returncode == 2
    assert "cannot be combined" in result.stderr
    assert not systemctl_log.exists()
    assert not unit_dir.exists()
    assert not (tmp_path / "rendered").exists()


def test_help_documents_explicit_timer_activation(tmp_path: Path) -> None:
    environment, systemctl_log, _ = _fake_systemd_environment(tmp_path)

    result = _run_installer(environment, "--help")

    assert result.returncode == 0, result.stderr
    assert "--enable-timers" in result.stdout
    assert not systemctl_log.exists()


def test_metrics_install_renders_without_mutating_lifecycle(tmp_path: Path) -> None:
    environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)

    result = _run_installer(environment)

    assert result.returncode == 0, result.stderr
    repo = Path(environment["BRAIN_TEST_INSTALL_SCRIPT"]).parents[2]
    service = (unit_dir / "brain-metrics.service").read_text()
    assert f"WorkingDirectory={repo}" in service
    assert f"ExecStart={repo}/.venv/bin/python -m brain_v42.metrics" in service
    assert "__REPO_ROOT__" not in service
    assert not any("brain-metrics" in call for call in systemctl_log.read_text().splitlines())
    assert (
        "brain-metrics.service generated and validated; lifecycle remains operator-managed"
        in result.stdout
    )


@pytest.mark.parametrize("mode", [(), ("--dry-run",), ("--uninstall",)])
def test_metrics_host_dropins_survive(tmp_path: Path, mode: tuple[str, ...]) -> None:
    environment, _, unit_dir = _fake_systemd_environment(tmp_path)
    dropin_dir = make_directory(unit_dir / "brain-metrics.service.d", parents=True)
    dropin = dropin_dir / "90-immutable-release.conf"
    dropin.write_text(
        "[Service]\nExecStart=\nExecStart=/example/release/bin/python -m brain_v42.metrics\n"
    )
    (unit_dir / "brain-metrics.service").write_text("existing metrics\n")
    previous = dropin.read_bytes()

    result = _run_installer(environment, *mode)

    assert result.returncode == 0, result.stderr
    assert dropin.read_bytes() == previous


def test_metrics_verification_failure_preserves_live_unit(tmp_path: Path) -> None:
    environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
    make_directory(unit_dir, parents=True)
    service = unit_dir / "brain-metrics.service"
    service.write_text("preserve metrics\n")
    environment["SYSTEMD_ANALYZE_FAIL_MATCH"] = service.name

    result = _run_installer(environment, "--dry-run")

    assert result.returncode != 0
    assert service.read_text() == "preserve metrics\n"
    assert not systemctl_log.exists()


def test_uninstall_disables_metrics_before_removing_unit(tmp_path: Path) -> None:
    environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
    make_directory(unit_dir, parents=True)
    service = unit_dir / "brain-metrics.service"
    service.write_text("existing metrics\n")

    result = _run_installer(environment, "--uninstall")

    assert result.returncode == 0, result.stderr
    assert "--user disable --now brain-metrics.service" in systemctl_log.read_text().splitlines()
    assert not service.exists()


def test_metrics_template_declares_standalone_restart_policy() -> None:
    unit = "brain-metrics.service.tmpl"
    service = dict(_section_directives(unit, "Service"))

    assert service["Restart"] == "always"
    assert service["RestartSec"] == "2"
    assert service["RestartSteps"] == "6"
    assert service["RestartMaxDelaySec"] == "60"
    assert service["EnvironmentFile"] == "__REPO_ROOT__/.env"
    assert dict(_section_directives(unit, "Install"))["WantedBy"] == "default.target"
    assert dict(_section_directives(unit, "Unit"))["Description"] == (
        "brain-v42 standalone metrics server (:9200)"
    )


def test_a_preserved_disabled_timer_says_how_to_arm_it(tmp_path: Path) -> None:
    """A `--dry-run` then a plain install leaves both timers disabled, by design:
    the only signal is the log line, so it must name the way out."""
    environment, _, unit_dir = _fake_systemd_environment(tmp_path)
    make_directory(unit_dir, parents=True)
    (unit_dir / TIMERS[0]).write_text("existing timer\n")

    result = _run_installer(environment)

    assert result.returncode == 0, result.stderr
    assert f"{TIMERS[0]}: preserved enabled=disabled" in result.stdout
    assert "re-run with --enable-timers to arm it" in result.stdout


def test_a_masked_timer_is_refused_before_anything_is_published(tmp_path: Path) -> None:
    """Publishing would replace the mask with the real unit file and a leftover
    wants link would re-arm the timer: refuse instead of claiming "preserved"."""
    environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
    make_directory(unit_dir, parents=True)
    (unit_dir / TIMERS[0]).write_text("existing timer\n")
    environment = {**environment, "DREAM_TIMER_ENABLED_STATE": "masked"}

    result = _run_installer(environment)

    assert result.returncode != 0
    assert "masked" in result.stderr
    assert (unit_dir / TIMERS[0]).read_text() == "existing timer\n"
    calls = systemctl_log.read_text().splitlines() if systemctl_log.exists() else []
    assert not any(call.split()[1] in {"enable", "start", "daemon-reload"} for call in calls)
