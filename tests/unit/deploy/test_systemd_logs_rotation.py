"""Contracts for memory limits and the log retention units."""

from pathlib import Path

from tests.unit.deploy.test_systemd_sandbox_profiles import _section_directives

ROOT = Path(__file__).parents[3]
SYSTEMD = ROOT / "deploy/systemd"


def test_every_service_template_has_memory_limits() -> None:
    templates = sorted(SYSTEMD.glob("*.service.tmpl"))

    assert templates
    for template in templates:
        directives = dict(_section_directives(template.name, "Service"))
        assert "MemoryHigh" in directives, template.name
        assert "MemoryMax" in directives, template.name


def test_log_rotation_unit_is_rendered_but_not_enabled(tmp_path: Path) -> None:
    from tests.unit.deploy.test_mcp_http_unit import (
        _fake_systemd_environment,
        _run_installer,
    )

    environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
    result = _run_installer(environment)

    assert result.returncode == 0, result.stderr
    service = (unit_dir / "brain-v42-logs-rotate.service").read_text()
    timer = (unit_dir / "brain-v42-logs-rotate.timer").read_text()
    exec_start = next(
        line.removeprefix("ExecStart=")
        for line in service.splitlines()
        if line.startswith("ExecStart=")
    )
    rendered_repo_root = Path(exec_start).parent.parent
    assert "Type=oneshot" in service
    assert "ReadWritePaths=-" in service
    assert "ReadWritePaths=-__REPO_ROOT__" not in service
    assert (
        f"ReadWritePaths=-{rendered_repo_root}/logs -%h/.local/share/brain-v42/releases"
    ) in service
    assert "-%h/.local/share/brain-v42/releases" in service
    assert "__LOG_TARGETS__" not in service
    assert "Persistent=false" in timer
    assert not any("logs-rotate" in line for line in systemctl_log.read_text().splitlines())
    assert "brain-v42-logs-rotate.timer" not in result.stdout
