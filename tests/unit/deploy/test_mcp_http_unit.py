"""Config-contract tests for the MCP HTTP systemd unit templates.

These tests assert that the template FILES have the correct content without
running the units themselves (systemd units are not runtime-testable in pytest).
RED phase: all tests fail until the templates are authored (Step 1).
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from tests.unit._fixture_modes import make_directory, write_file

SYSTEMD_DIR = Path(__file__).parent.parent.parent.parent / "deploy" / "systemd"
PROJECTOR_ENV_EXAMPLE = SYSTEMD_DIR / "graph-projector.env.example"
MCP_HTTP_RUNBOOK = SYSTEMD_DIR / "MCP_HTTP_RUNBOOK.md"
PLAN_INDEX_REPAIR_RUNBOOK = Path(__file__).parents[3] / "docs" / "PLAN_INDEX_REPAIR_RUNBOOK.md"


def _read(name: str) -> str:
    return (SYSTEMD_DIR / name).read_text()


class TestMcpHttpServiceTemplate:
    def test_file_exists(self) -> None:
        assert (SYSTEMD_DIR / "brain-mcp-http.service.tmpl").exists()

    def test_type_simple(self) -> None:
        assert "Type=simple" in _read("brain-mcp-http.service.tmpl")

    def test_http_server_flag(self) -> None:
        assert "--http-server" in _read("brain-mcp-http.service.tmpl")

    def test_projector_runtime_does_not_source_a_login_shell_after_preflight(self) -> None:
        content = _read("brain-mcp-http.service.tmpl")
        exec_start = next(line for line in content.splitlines() if line.startswith("ExecStart="))

        assert "/bin/bash" not in exec_start
        assert "__REPO_ROOT__/.venv/bin/python -m brain_v42.mcp.server" in exec_start

    def test_restart_always(self) -> None:
        assert "Restart=always" in _read("brain-mcp-http.service.tmpl")

    def test_start_limit_burst(self) -> None:
        assert "StartLimitBurst=5" in _read("brain-mcp-http.service.tmpl")

    def test_timeout_stop(self) -> None:
        assert "TimeoutStopSec=" in _read("brain-mcp-http.service.tmpl")

    def test_no_io_scheduling_idle(self) -> None:
        assert "IOSchedulingClass=idle" not in _read("brain-mcp-http.service.tmpl")

    def test_no_bind_all_interfaces(self) -> None:
        assert "0.0.0.0" not in _read("brain-mcp-http.service.tmpl")

    def test_http_enablement_is_documented_as_operator_managed(self) -> None:
        for name in (
            "brain-mcp-http.service.tmpl",
            "brain-v42-dream.service.tmpl",
            "install.sh",
        ):
            content = _read(name)
            assert "operator-managed" in content
            assert "spec C2" not in content
            assert "Plan 2" not in content
            assert "production-enable gate" not in content

        service = _read("brain-mcp-http.service.tmpl")
        assert "normal install path" in service
        assert "--uninstall" in service

    def test_placeholder_style(self) -> None:
        content = _read("brain-mcp-http.service.tmpl")
        assert "__REPO_ROOT__" in content

    def test_service_private_projector_environment_is_loaded_last(self) -> None:
        content = _read("brain-mcp-http.service.tmpl")
        shared = "EnvironmentFile=__REPO_ROOT__/.env"
        token = "EnvironmentFile=%h/.config/brain-v42/mcp-token.env"
        projector = "EnvironmentFile=-%h/.config/brain-v42/graph-projector.env"

        assert shared in content
        assert token in content
        assert projector in content
        assert content.index(shared) < content.index(token) < content.index(projector)

    def test_projector_environment_is_not_shared_with_other_units(self) -> None:
        private_environment = "graph-projector.env"

        for unit in SYSTEMD_DIR.glob("*.service.tmpl"):
            if unit.name != "brain-mcp-http.service.tmpl":
                assert private_environment not in unit.read_text(), unit.name

    def test_projector_environment_is_preflighted_before_service_start(self) -> None:
        content = _read("brain-mcp-http.service.tmpl")

        assert "ExecStartPre=" in content
        assert "check_graph_projector_env.py" in content
        assert "--require-effective-private" in content
        assert content.index("ExecStartPre=") < content.index("ExecStart=")

    def test_production_http_port_is_preflighted_before_service_start(self) -> None:
        content = _read("brain-mcp-http.service.tmpl")

        assert "check_mcp_http_port.py" in content
        assert "--expected 8765" in content
        assert "--expected-host 127.0.0.1" in content
        assert "--token-file %h/.config/brain-v42/mcp-token.env" in content
        assert "--require-effective-runtime-settings" in content
        assert "EnvironmentFile=-%h/.config/brain-v42/mcp-token.env" not in content
        assert content.index("check_mcp_http_port.py") < content.index("ExecStart=")

    def test_projector_environment_example_is_complete_and_secret_free(self) -> None:
        content = PROJECTOR_ENV_EXAMPLE.read_text()

        assert "GRAPH_ENABLED=" not in content
        assert "GRAPH_LEDGER_WRITE_ENABLED=" not in content
        assert "GRAPH_PROJECTOR_ENABLED=true" in content
        assert "GRAPH_PROJECTOR_NEO4J_URL=" in content
        assert "GRAPH_PROJECTOR_NEO4J_USER=" in content
        assert "GRAPH_PROJECTOR_NEO4J_PASSWORD=REPLACE_WITH_ROTATED_PASSWORD" in content
        assert "\nNEO4J_URL=" not in content
        assert "\nNEO4J_PASSWORD=" not in content


class TestMcpHttpWatchdogServiceTemplate:
    def test_watchdog_service_exists(self) -> None:
        assert (SYSTEMD_DIR / "brain-mcp-http-watchdog.service.tmpl").exists()

    def test_curl_present(self) -> None:
        assert "curl" in _read("brain-mcp-http-watchdog.service.tmpl")

    def test_health_endpoint(self) -> None:
        assert "/health" in _read("brain-mcp-http-watchdog.service.tmpl")

    def test_systemctl_restart(self) -> None:
        content = _read("brain-mcp-http-watchdog.service.tmpl")
        assert "systemctl --user restart brain-mcp-http" in content

    def test_mcp_port_placeholder(self) -> None:
        assert "__MCP_PORT__" in _read("brain-mcp-http-watchdog.service.tmpl")

    def test_curl_has_timeout(self) -> None:
        """Sans -m, un serveur wedgé (event loop bloquée, accept queue pleine)
        fait pendre le curl indéfiniment : le watchdog reste 'activating' et ne
        restart jamais (incident 2026-07-03 — loop bloquée sur une connexion
        Neo4j defunct après recreate du container, watchdog aveugle)."""
        content = _read("brain-mcp-http-watchdog.service.tmpl")
        assert re.search(r"curl[^\n]* -m \d+", content)


class TestMcpHttpWatchdogTimerTemplate:
    def test_watchdog_timer_exists(self) -> None:
        assert (SYSTEMD_DIR / "brain-mcp-http-watchdog.timer.tmpl").exists()

    def test_on_unit_active_sec(self) -> None:
        assert "OnUnitActiveSec=30" in _read("brain-mcp-http-watchdog.timer.tmpl")


INSTALL_SH = SYSTEMD_DIR / "install.sh"


def _read_install() -> str:
    return INSTALL_SH.read_text()


def _fake_systemd_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    fake_bin = make_directory(tmp_path / "bin")
    systemctl_log = tmp_path / "systemctl.log"
    write_file(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\n'
        'if [[ -n "${SYSTEMCTL_FAIL_MATCH:-}" && "$*" == *"$SYSTEMCTL_FAIL_MATCH"* ]]; then\n'
        "  exit 1\n"
        "fi\n"
        'case "$*" in\n'
        '  "--user is-active brain-mcp-http-watchdog.timer") '
        'printf "%s\\n" "${WATCHDOG_TIMER_ACTIVE_STATE:-inactive}"; exit 0 ;;\n'
        '  "--user is-active brain-mcp-http-watchdog.service") '
        'printf "%s\\n" "${WATCHDOG_SERVICE_ACTIVE_STATE:-inactive}"; exit 0 ;;\n'
        '  "--user is-enabled brain-mcp-http-watchdog.timer") '
        'printf "%s\\n" "${WATCHDOG_TIMER_ENABLED_STATE:-disabled}"; exit 0 ;;\n'
        '  "--user is-enabled brain-mcp-http-watchdog.service") '
        'printf "%s\\n" "${WATCHDOG_SERVICE_ENABLED_STATE:-static}"; exit 0 ;;\n'
        '  "--user is-active "*) printf "inactive\\n"; exit 3 ;;\n'
        '  "--user is-enabled "*) printf "disabled\\n"; exit 1 ;;\n'
        "esac\n",
        mode=0o755,
    )
    write_file(
        fake_bin / "systemd-analyze",
        "#!/usr/bin/env bash\n"
        'if [[ -n "${SYSTEMD_ANALYZE_FAIL_MATCH:-}" '
        '&& "$*" == *"$SYSTEMD_ANALYZE_FAIL_MATCH"* ]]; then\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        mode=0o755,
    )
    write_file(
        fake_bin / "mv",
        "#!/usr/bin/env bash\n"
        'if [[ -n "${MV_FAIL_BACKUP_RESTORE_MATCH:-}" '
        '&& "$*" == *"/backup/$MV_FAIL_BACKUP_RESTORE_MATCH"* ]]; then\n'
        "  exit 43\n"
        "fi\n"
        'if [[ -n "${MV_FAIL_MATCH:-}" && "$*" == *"$MV_FAIL_MATCH"* '
        '&& ! -e "$MV_FAIL_MARKER" ]]; then\n'
        '  : > "$MV_FAIL_MARKER"\n'
        "  exit 42\n"
        "fi\n"
        'exec /usr/bin/mv "$@"\n',
        mode=0o755,
    )
    write_file(
        fake_bin / "loginctl",
        "#!/usr/bin/env bash\nprintf '%s\\n' 'Linger=yes'\n",
        mode=0o755,
    )

    repo = make_directory(tmp_path / "repo")
    # copytree only copies the source mode onto its leaf, so create the intermediate
    # levels explicitly rather than letting them inherit the caller's umask.
    make_directory(repo / "deploy")
    shutil.copytree(SYSTEMD_DIR, repo / "deploy" / "systemd")
    make_directory(repo / "scripts")
    shutil.copy2(
        Path(__file__).resolve().parents[3] / "scripts" / "dream.sh",
        repo / "scripts" / "dream.sh",
    )
    shutil.copy2(
        Path(__file__).resolve().parents[3] / "scripts" / "check_mcp_http_port.py",
        repo / "scripts" / "check_mcp_http_port.py",
    )
    shutil.copy2(
        Path(__file__).resolve().parents[3] / "scripts" / "check_graph_projector_env.py",
        repo / "scripts" / "check_graph_projector_env.py",
    )
    make_directory(repo / ".venv" / "bin", parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(Path(sys.executable).resolve())
    write_file(repo / ".env", "POSTGRES_URL=postgresql+asyncpg://example\n", mode=0o600)

    home = tmp_path / "home"
    token = home / ".config" / "brain-v42" / "mcp-token.env"
    make_directory(token.parent, parents=True)
    write_file(token, "MCP_HTTP_TOKEN=test-only-token\n", mode=0o600)

    unit_dir = tmp_path / "xdg" / "systemd" / "user"
    environment = os.environ.copy()
    for key in tuple(environment):
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
            environment.pop(key)
    environment.update(
        {
            "MCP_HTTP_HOST": "127.0.0.1",
            "MCP_HTTP_PORT": "8765",
            "MCP_HTTP_TOKEN": "test-only-token",
            "BRAIN_TEST_INSTALL_SCRIPT": str(repo / "deploy" / "systemd" / "install.sh"),
            "HOME": str(home),
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "SYSTEMCTL_LOG": str(systemctl_log),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        }
    )
    return environment, systemctl_log, unit_dir


def _run_installer(
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [environment.get("BRAIN_TEST_INSTALL_SCRIPT", str(INSTALL_SH)), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


class TestInstallShWiresMcpUnits:
    """install.sh must wire all three operator-managed MCP templates."""

    def test_install_sh_exists(self) -> None:
        assert INSTALL_SH.exists()

    def test_references_mcp_http_service_tmpl(self) -> None:
        assert "brain-mcp-http.service" in _read_install()

    def test_references_mcp_http_watchdog_service_tmpl(self) -> None:
        assert "brain-mcp-http-watchdog.service" in _read_install()

    def test_references_mcp_http_watchdog_timer_tmpl(self) -> None:
        assert "brain-mcp-http-watchdog.timer" in _read_install()

    def test_mcp_port_substitution_present(self) -> None:
        """install.sh must substitute __MCP_PORT__ in the watchdog service."""
        assert "__MCP_PORT__" in _read_install()

    def test_normal_install_does_not_mutate_mcp_http_lifecycle(self) -> None:
        """install.sh must preserve the operator-managed MCP lifecycle."""
        content = _read_install()
        uninstall_branch = re.search(
            r"^# --- Uninstall branch ---$.*?^fi$",
            content,
            flags=re.MULTILINE | re.DOTALL,
        )
        assert uninstall_branch is not None
        normal_install = content[: uninstall_branch.start()] + content[uninstall_branch.end() :]
        bad_lines = [
            line
            for line in normal_install.splitlines()
            if "brain-mcp-http" in line
            and re.search(r"systemctl\s+--user\s+(?:start|stop|restart|enable|disable)\b", line)
            and not line.strip().startswith("#")
        ]
        units = re.search(r"^UNITS=\((.*?)^\)", content, flags=re.MULTILINE | re.DOTALL)
        assert units is not None
        assert "brain-mcp-http" not in units.group(1)
        assert bad_lines == [], f"Found MCP lifecycle mutation during normal install: {bad_lines}"

    def test_dry_run_makes_no_systemctl_call(self, tmp_path: Path) -> None:
        environment, systemctl_log, _ = _fake_systemd_environment(tmp_path)

        result = _run_installer(environment, "--dry-run")

        assert result.returncode == 0, result.stderr
        assert not systemctl_log.exists() or not systemctl_log.read_text()

    def test_normal_install_only_reads_mcp_lifecycle(self, tmp_path: Path) -> None:
        environment, systemctl_log, _ = _fake_systemd_environment(tmp_path)

        result = _run_installer(environment)

        assert result.returncode == 0, result.stderr
        calls = systemctl_log.read_text().splitlines()
        assert "--user daemon-reload" in calls
        assert "--user enable --now brain-v42-dream.timer" in calls
        assert "--user enable --now brain-v42-graph-recon.timer" in calls
        assert all(
            re.fullmatch(
                r"--user is-(?:active|enabled) brain-mcp-http-watchdog\.(?:service|timer)",
                call,
            )
            for call in calls
            if "brain-mcp-http" in call
        )

    def test_normal_install_schedules_read_only_graph_ledger_inventory(
        self,
        tmp_path: Path,
    ) -> None:
        environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)

        result = _run_installer(environment)

        assert result.returncode == 0, result.stderr
        service = (unit_dir / "brain-v42-graph-recon.service").read_text()
        exec_start = next(line for line in service.splitlines() if line.startswith("ExecStart="))
        fixture_repo = Path(environment["BRAIN_TEST_INSTALL_SCRIPT"]).parents[2]
        assert exec_start == (
            f"ExecStart={fixture_repo}/.venv/bin/python "
            f"{fixture_repo}/scripts/rebuild_graph_projection.py"
        )
        assert "--fix" not in service
        assert "recover_graph_projection.py" not in service
        assert (
            "--user enable --now brain-v42-graph-recon.timer"
            in systemctl_log.read_text().splitlines()
        )

    def test_normal_install_rejects_a_live_watchdog_before_publishing(
        self,
        tmp_path: Path,
    ) -> None:
        environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
        make_directory(unit_dir, parents=True)
        sentinel = unit_dir / "brain-mcp-http.service"
        sentinel.write_text("preserve")
        environment["WATCHDOG_TIMER_ACTIVE_STATE"] = "active"

        result = _run_installer(environment)

        assert result.returncode != 0
        assert "watchdog must be inactive and disabled" in result.stderr
        assert sentinel.read_text() == "preserve"
        assert "--user daemon-reload" not in systemctl_log.read_text().splitlines()

    def test_normal_install_rejects_non_contract_mcp_port(self, tmp_path: Path) -> None:
        environment, systemctl_log, _ = _fake_systemd_environment(tmp_path)
        environment["MCP_HTTP_PORT"] = "9000"

        result = _run_installer(environment, "--dry-run")

        assert result.returncode == 2
        assert "fixed to 8765" in result.stderr
        assert not systemctl_log.exists() or not systemctl_log.read_text()

    def test_normal_install_rejects_non_contract_mcp_host(self, tmp_path: Path) -> None:
        environment, systemctl_log, _ = _fake_systemd_environment(tmp_path)
        environment["MCP_HTTP_HOST"] = "::1"

        result = _run_installer(environment, "--dry-run")

        assert result.returncode == 2
        assert "fixed to 127.0.0.1" in result.stderr
        assert not systemctl_log.exists() or not systemctl_log.read_text()

    def test_installer_attests_private_runtime_files_before_generation(self) -> None:
        content = _read_install()
        token_argument = '--token-file "$HOME/.config/brain-v42/mcp-token.env"'
        projector_argument = '--private "$HOME/.config/brain-v42/graph-projector.env"'

        assert token_argument in content
        assert projector_argument in content
        assert content.index(token_argument) < content.index('mkdir -p "$USER_UNIT_DIR"')
        assert content.index(projector_argument) < content.index('mkdir -p "$USER_UNIT_DIR"')

    def test_active_graph_ledger_fails_before_unit_generation(self, tmp_path: Path) -> None:
        environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
        repo = Path(environment["BRAIN_TEST_INSTALL_SCRIPT"]).parents[2]
        (repo / ".env").write_text("GRAPH_LEDGER_WRITE_ENABLED=true\n")
        make_directory(unit_dir, parents=True)
        sentinel = unit_dir / "brain-mcp-http.service"
        sentinel.write_text("preserve")

        result = _run_installer(environment, "--dry-run")

        assert result.returncode == 2
        assert "private graph projector environment is required" in result.stderr
        assert sentinel.read_text() == "preserve"
        assert not systemctl_log.exists() or not systemctl_log.read_text()

    def test_verification_failure_preserves_every_existing_unit(self, tmp_path: Path) -> None:
        environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
        make_directory(unit_dir, parents=True)
        sentinels = {
            unit_dir / "brain-v42-dream.service": "preserve-dream",
            unit_dir / "brain-mcp-http.service": "preserve-http",
        }
        for path, content in sentinels.items():
            path.write_text(content)
        environment["SYSTEMD_ANALYZE_FAIL_MATCH"] = "brain-mcp-http-watchdog.service"

        result = _run_installer(environment, "--dry-run")

        assert result.returncode != 0
        assert {path: path.read_text() for path in sentinels} == sentinels
        assert not systemctl_log.exists() or not systemctl_log.read_text()

    def test_publish_failure_rolls_back_every_existing_unit(self, tmp_path: Path) -> None:
        environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
        make_directory(unit_dir, parents=True)
        sentinels = {
            unit_dir / "brain-v42-dream.service": "preserve-dream",
            unit_dir / "brain-mcp-http.service": "preserve-http",
        }
        for path, content in sentinels.items():
            path.write_text(content)
        environment["MV_FAIL_MATCH"] = "brain-mcp-http-watchdog.service"
        environment["MV_FAIL_MARKER"] = str(tmp_path / "mv-failed-once")

        result = _run_installer(environment, "--dry-run")

        assert result.returncode != 0
        assert {path: path.read_text() for path in sentinels} == sentinels
        assert not systemctl_log.exists() or not systemctl_log.read_text()

    def test_failed_rollback_retains_recovery_backups(self, tmp_path: Path) -> None:
        environment, _, unit_dir = _fake_systemd_environment(tmp_path)
        make_directory(unit_dir, parents=True)
        sentinel = unit_dir / "brain-v42-dream.service"
        sentinel.write_text("recoverable-dream-unit")
        environment["MV_FAIL_MATCH"] = "brain-mcp-http-watchdog.service"
        environment["MV_FAIL_MARKER"] = str(tmp_path / "mv-failed-once")
        environment["MV_FAIL_BACKUP_RESTORE_MATCH"] = "brain-v42-dream.service"

        result = _run_installer(environment, "--dry-run")

        assert result.returncode != 0
        staging_directories = list(unit_dir.glob(".brain-v42-install.*"))
        assert len(staging_directories) == 1
        backup = staging_directories[0] / "backup" / sentinel.name
        assert backup.read_text() == "recoverable-dream-unit"
        assert "manual recovery backups retained" in result.stderr

    def test_dry_run_and_uninstall_are_rejected_without_side_effects(self, tmp_path: Path) -> None:
        environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
        make_directory(unit_dir, parents=True)
        sentinel = unit_dir / "brain-mcp-http.service"
        sentinel.write_text("preserve")

        result = _run_installer(environment, "--dry-run", "--uninstall")

        assert result.returncode == 2
        assert "cannot be combined" in result.stderr
        assert sentinel.read_text() == "preserve"
        assert not systemctl_log.exists() or not systemctl_log.read_text()

    def test_uninstall_quiesces_watchdog_before_http_service(self, tmp_path: Path) -> None:
        environment, systemctl_log, unit_dir = _fake_systemd_environment(tmp_path)
        make_directory(unit_dir, parents=True)
        managed_units = (
            "brain-v42-dream.service",
            "brain-v42-dream.timer",
            "brain-v42-graph-recon.service",
            "brain-v42-graph-recon.timer",
            "brain-mcp-http.service",
            "brain-mcp-http-watchdog.service",
            "brain-mcp-http-watchdog.timer",
        )
        for unit in managed_units:
            (unit_dir / unit).write_text("managed")

        result = _run_installer(environment, "--uninstall")

        assert result.returncode == 0, result.stderr
        calls = systemctl_log.read_text().splitlines()
        timer = calls.index("--user disable --now brain-mcp-http-watchdog.timer")
        watchdog = calls.index("--user disable --now brain-mcp-http-watchdog.service")
        server = calls.index("--user disable --now brain-mcp-http.service")
        assert timer < watchdog < server
        for unit in ("brain-v42-dream", "brain-v42-graph-recon"):
            timer_stop = calls.index(f"--user disable --now {unit}.timer")
            service_stop = calls.index(f"--user disable --now {unit}.service")
            assert timer_stop < service_stop
        assert all(not (unit_dir / unit).exists() for unit in managed_units)

    def test_uninstall_fails_before_removal_when_quiescing_a_unit_fails(
        self,
        tmp_path: Path,
    ) -> None:
        environment, _, unit_dir = _fake_systemd_environment(tmp_path)
        make_directory(unit_dir, parents=True)
        managed_units = (
            "brain-v42-dream.service",
            "brain-v42-dream.timer",
            "brain-v42-graph-recon.service",
            "brain-v42-graph-recon.timer",
            "brain-mcp-http.service",
            "brain-mcp-http-watchdog.service",
            "brain-mcp-http-watchdog.timer",
        )
        for unit in managed_units:
            (unit_dir / unit).write_text("managed")
        environment["SYSTEMCTL_FAIL_MATCH"] = "disable --now brain-mcp-http-watchdog.timer"

        result = _run_installer(environment, "--uninstall")

        assert result.returncode != 0
        assert "uninstalled" not in result.stdout
        assert all((unit_dir / unit).exists() for unit in managed_units)

    def test_uninstall_reports_daemon_reload_failure(self, tmp_path: Path) -> None:
        environment, _, _ = _fake_systemd_environment(tmp_path)
        environment["SYSTEMCTL_FAIL_MATCH"] = "daemon-reload"

        result = _run_installer(environment, "--uninstall")

        assert result.returncode != 0
        assert "uninstalled" not in result.stdout

    def test_operator_managed_message_present(self) -> None:
        """install.sh must explain that it preserves the live service state."""
        content = _read_install()
        assert "lifecycle remains operator-managed" in content

    def test_uninstall_disables_operator_managed_units_before_removal(self) -> None:
        """Uninstall must not leave enabled systemd symlinks behind."""
        content = _read_install()
        uninstall = content.split("# --- Uninstall branch ---", maxsplit=1)[1]
        help_header = "\n".join(content.splitlines()[2:18])
        assert (
            "--uninstall affects every managed unit, including production MCP HTTP" in help_header
        )
        for unit in (
            "brain-mcp-http.service",
            "brain-mcp-http-watchdog.service",
            "brain-mcp-http-watchdog.timer",
        ):
            disable = f"disable_and_stop_unit {unit}"
            removal = f'rm -f "$USER_UNIT_DIR/{unit}"'
            assert disable in uninstall
            assert removal in uninstall
            assert uninstall.index(disable) < uninstall.index(removal)
        helper = content.split("disable_and_stop_unit()", maxsplit=1)[1].split(
            "stop_unit()",
            maxsplit=1,
        )[0]
        assert 'systemctl --user disable --now "$unit"' in helper
        assert 'assert_unit_inactive "$unit"' in helper
        assert 'assert_unit_disabled "$unit"' in helper


class TestMcpHttpServiceDocumentationPath:
    """Documentation= must point at the current operator runbook."""

    def test_documentation_uses_correct_date(self) -> None:
        content = _read("brain-mcp-http.service.tmpl")
        assert "Documentation=file://__REPO_ROOT__/deploy/systemd/MCP_HTTP_RUNBOOK.md" in content
        assert MCP_HTTP_RUNBOOK.exists()

    def test_documentation_no_wrong_date(self) -> None:
        content = _read("brain-mcp-http.service.tmpl")
        assert "2026-06-29-mcp-http-single-server-design.md" not in content
        assert "2026-06-30-mcp-http-server-design.md" not in content

    def test_operator_runbook_covers_the_full_lifecycle(self) -> None:
        content = MCP_HTTP_RUNBOOK.read_text()

        for heading in (
            "## Preflight",
            "## First activation or host migration",
            "## Validation",
            "## Rollback",
            "## Full uninstall",
        ):
            assert heading in content
        for command in (
            "check_graph_projector_env.py",
            "check_mcp_http_port.py",
            "deploy/systemd/install.sh --check-only",
            'deploy/systemd/install.sh --render-dir "$render_dir"',
            "production systemd contract is fixed to `127.0.0.1:8765`",
            '--token-file "$HOME/.config/brain-v42/mcp-token.env"',
            'test -n "${MCP_HTTP_TOKEN:-}"',
            "--require-effective-token",
            "systemctl --user stop brain-mcp-http-watchdog.timer",
            "systemctl --user stop brain-mcp-http-watchdog.service",
            "systemctl --user disable --no-reload brain-mcp-http-watchdog.timer",
            'old_pid="$(systemctl --user show brain-mcp-http.service -p MainPID --value || true)"',
            "systemctl --user restart brain-mcp-http.service",
            'new_pid="$(systemctl --user show brain-mcp-http.service -p MainPID --value)"',
            'test "$new_pid" -gt 0',
            'test "$new_pid" != "$old_pid"',
            "systemctl --user enable brain-mcp-http.service",
            "systemctl --user enable --now brain-mcp-http-watchdog.timer",
            "for attempt in {1..30}; do",
            "curl -fsS -m 2 http://127.0.0.1:8765/health",
            "healthy=true",
            "journalctl --user -u brain-mcp-http.service",
            "systemctl --user disable --now brain-mcp-http.service",
            "deploy/systemd/install.sh --uninstall",
        ):
            assert command in content
        assert "brain-mcp-http-watchdog.timer 2>/dev/null || true" not in content
        assert "brain-mcp-http-watchdog.service 2>/dev/null || true" not in content
        assert '. "$HOME/.config/brain-v42/mcp-token.env"' not in content
        assert "systemctl --user start brain-mcp-http.service" not in content
        preflight = content.split("## Preflight", maxsplit=1)[1].split(
            "## First activation or host migration",
            maxsplit=1,
        )[0]
        assert "install.sh --dry-run" not in preflight

    def test_operator_runbook_stops_watchdog_before_daemon_reload(self) -> None:
        content = MCP_HTTP_RUNBOOK.read_text()
        preflight = content.split("## Preflight", maxsplit=1)[1].split(
            "## First activation or host migration",
            maxsplit=1,
        )[0]

        stop_timer = "systemctl --user stop brain-mcp-http-watchdog.timer"
        stop_service = "systemctl --user stop brain-mcp-http-watchdog.service"
        disable_timer = "systemctl --user disable --no-reload brain-mcp-http-watchdog.timer"
        daemon_reload = "systemctl --user daemon-reload"
        assert stop_timer in preflight
        assert disable_timer in preflight
        assert stop_service in preflight
        assert preflight.index(stop_timer) < preflight.index(disable_timer)
        assert preflight.index(stop_service) < preflight.index(disable_timer)
        assert preflight.index(disable_timer) < preflight.index(daemon_reload)

    def test_render_parent_and_render_dir_terminology_is_consistent(self) -> None:
        documents = (
            (MCP_HTTP_RUNBOOK, "render_parent", "render_dir"),
            (SYSTEMD_DIR / "README.md", "RENDER_PARENT", "RENDER_DIR"),
            (PLAN_INDEX_REPAIR_RUNBOOK, "render_parent", "render_dir"),
        )

        for path, parent_name, dir_name in documents:
            content = path.read_text()
            assert f"{parent_name} =" in content or f"{parent_name}=" in content
            assert f"{dir_name} =" in content or f"{dir_name}=" in content
            assert "parent directory that contains" in content
            assert "new child directory" in content
            assert "parent ancestry" in content


class TestDreamServiceTemplate:
    """Regression contracts for the systemd-to-Bash readiness handoff."""

    def test_readiness_expansions_are_deferred_to_bash(self) -> None:
        content = _read("brain-v42-dream.service.tmpl")

        assert "$${BRAIN_DREAM_MCP_URL:-http://127.0.0.1:8765/mcp}" in content
        assert "$${url%%/mcp}/health" in content
        assert 'curl -fsS -m 1 "$$health"' in content
        assert "Brain MCP readiness failed: $$health" in content
