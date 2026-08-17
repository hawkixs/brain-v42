"""Contracts for the dormant automation unit and its operator runbook."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.unit._fixture_modes import make_directory, write_file

ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = ROOT / "deploy" / "systemd"
TEMPLATE = SYSTEMD_DIR / "brain-v42-automation.service.tmpl"
INSTALL = SYSTEMD_DIR / "install.sh"
RUNBOOK = SYSTEMD_DIR / "README.md"
PLAN = ROOT / "docs" / "plans" / "2026-07-14-arc1-automation-runtime-plan.md"
ARCHITECTURE = ROOT / "docs" / "ARCHITECTURE.md"
INTEGRATION_SCRIPT = ROOT / "tests" / "integration" / "test_dream_systemd_install.sh"
FLAG = "METRICS_LEGACY_AUTOMATION_ENABLED"


def _active_lines(path: Path) -> set[str]:
    assert path.is_file(), f"missing contract file: {path}"
    return {
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    }


def _write_executable(path: Path, content: str) -> None:
    make_directory(path.parent, parents=True, exist_ok=True)
    write_file(path, textwrap.dedent(content).lstrip(), mode=0o755)


def _fixture_repo(tmp_path: Path, *, omit_automation: bool = False) -> Path:
    repo = make_directory(tmp_path / "repo", parents=True)
    ignore = None
    if omit_automation:
        ignore = shutil.ignore_patterns("brain-v42-automation.service.tmpl")
    # copytree only copies the source mode onto its leaf, so create the intermediate
    # levels explicitly rather than letting them inherit the caller's umask.
    make_directory(repo / "deploy")
    shutil.copytree(SYSTEMD_DIR, repo / "deploy" / "systemd", ignore=ignore)
    make_directory(repo / "scripts")
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


def _install_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    systemctl_log = tmp_path / "systemctl.log"
    analyze_log = tmp_path / "systemd-analyze.log"
    _write_executable(
        fake_bin / "systemctl",
        """
        #!/bin/sh
        printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
        case "$*" in
          "--user is-active "*) printf 'inactive\n'; exit 3 ;;
          "--user is-enabled "*) printf 'disabled\n'; exit 1 ;;
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
        fake_bin / "loginctl",
        """
        #!/bin/sh
        printf 'Linger=yes\n'
    """,
    )
    env = os.environ.copy()
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
            "SYSTEMCTL_LOG": str(systemctl_log),
            "SYSTEMD_ANALYZE_LOG": str(analyze_log),
        }
    )
    return env, systemctl_log, analyze_log


def _run_install(
    repo: Path,
    env: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "deploy" / "systemd" / "install.sh"), *arguments],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _extract_runbook_block(name: str) -> str:
    assert RUNBOOK.is_file(), f"operator runbook missing: {RUNBOOK}"
    pattern = re.compile(
        rf"<!-- runbook:{re.escape(name)}:start -->\s*"
        rf"```bash\s*(.*?)\s*```\s*"
        rf"<!-- runbook:{re.escape(name)}:end -->",
        re.DOTALL,
    )
    match = pattern.search(RUNBOOK.read_text())
    assert match is not None, f"missing executable runbook block: {name}"
    return match.group(1)


def _operator_env(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    fake_bin = tmp_path / "operator-bin"
    systemctl_log = tmp_path / "operator-systemctl.log"
    curl_log = tmp_path / "operator-curl.log"
    lease_log = tmp_path / "operator-lease.log"
    event_log = tmp_path / "operator-events.log"
    home = tmp_path / "home"
    owner_env = home / ".config" / "brain-v42" / "automation-owner.env"
    make_directory(owner_env.parent, parents=True)
    write_file(owner_env, f"{FLAG}=false\n", mode=0o600)
    # The runbook creates its evidence directory with `mkdir -p`, which hardens only
    # the leaf: pre-create the XDG roots it descends from so their modes are pinned
    # instead of inherited from the caller's umask.
    make_directory(tmp_path / "xdg")
    make_directory(tmp_path / "state")

    _write_executable(
        fake_bin / "systemctl",
        """
        #!/bin/sh
        printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
        printf 'systemctl %s\n' "$*" >> "$EVENT_LOG"
        case "$*" in
          *MainPID*--value*) printf '%s\n' "${FAKE_MAIN_PID:-4242}" ;;
          *EnvironmentFiles*--value*) printf '%s\n' "$FAKE_ENVIRONMENT_FILES" ;;
        esac
    """,
    )
    _write_executable(
        fake_bin / "systemd-analyze",
        """
        #!/bin/sh
        exit 0
    """,
    )
    _write_executable(
        fake_bin / "loginctl",
        """
        #!/bin/sh
        printf 'Linger=yes\n'
    """,
    )
    _write_executable(
        fake_bin / "ss",
        """
        #!/bin/sh
        if [ "${FAKE_SS_ERROR_ZERO:-0}" = "1" ]; then
          printf 'Cannot open netlink socket: Operation not permitted\n' >&2
          exit 0
        fi
        if [ "${FAKE_TCP_9201_BOUND:-0}" = "1" ]; then
          printf 'LISTEN 0 128 127.0.0.1:9201 0.0.0.0:*\n'
        fi
        """,
    )
    _write_executable(
        fake_bin / "curl",
        """
        #!/bin/sh
        method=GET
        url=
        printf '%s\n' "$*" >> "$CURL_LOG"
        while [ "$#" -gt 0 ]; do
          case "$1" in
            -X|--request) shift; method="$1" ;;
            http://*) url="$1" ;;
          esac
          shift
        done
        if [ "${CURL_FAIL_URL:-}" = "$url" ]; then
          printf '%s' "${CURL_FAIL_STATUS:-500}"
          exit 0
        fi
        case "$method $url" in
          'GET http://127.0.0.1:9201/health') printf 200 ;;
          'GET http://127.0.0.1:9201/metrics') printf 404 ;;
          'GET http://127.0.0.1:9201/api/cockpit') printf 404 ;;
          'POST http://127.0.0.1:9201/gitlab/webhook') printf 401 ;;
          'GET http://127.0.0.1:9200/metrics') printf 200 ;;
          'GET http://127.0.0.1:9200/api/cockpit') printf 200 ;;
          'POST http://127.0.0.1:9200/gitlab/webhook')
            printf '%s' "${LEGACY_WEBHOOK_STATUS:-404}"
            ;;
          *) printf 599 ;;
        esac
    """,
    )
    lease_probe = tmp_path / "lease-probe"
    _write_executable(
        lease_probe,
        """
        #!/bin/sh
        expected="${EXPECTED_AUTOMATION_LEASES:?missing expected lease count}"
        printf '%s\n' "$expected" >> "$LEASE_LOG"
        printf 'lease %s\n' "$expected" >> "$EVENT_LOG"
        if [ "${FAIL_LEASE_EXPECTED:-}" = "$expected" ]; then
          exit 42
        fi
    """,
    )

    proc_root = tmp_path / "proc"
    make_directory(proc_root / "4242", parents=True)
    (proc_root / "4242" / "environ").write_bytes(
        b"IGNORED_SECRET=not-printed\0" + f"{FLAG}=false\0".encode()
    )
    repo = _fixture_repo(tmp_path / "operator-repo")
    environment_files = f"{repo}/.env (ignore_errors=no) {owner_env} (ignore_errors=no)"
    env = os.environ.copy()
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
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "REPO_ROOT": str(repo),
            "LEASE_PROBE": str(lease_probe),
            "LEASE_LOG": str(lease_log),
            "EVENT_LOG": str(event_log),
            "SYSTEMCTL_LOG": str(systemctl_log),
            "CURL_LOG": str(curl_log),
            "FAKE_MAIN_PID": "4242",
            "FAKE_ENVIRONMENT_FILES": environment_files,
            "PROC_ROOT": str(proc_root),
        }
    )
    paths = {
        "systemctl_log": systemctl_log,
        "curl_log": curl_log,
        "lease_log": lease_log,
        "event_log": event_log,
        "owner_env": owner_env,
        "proc_root": proc_root,
        "repo": repo,
    }
    return env, paths


def _run_block(
    name: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash"],
        input=_extract_runbook_block(name),
        cwd=env["REPO_ROOT"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _set_process_environment(
    paths: dict[str, Path],
    *,
    flag_value: str,
) -> None:
    environ = paths["proc_root"] / "4242" / "environ"
    environ.write_bytes(
        b"IGNORED_SECRET=must-not-leak\0OTHER_PRIVATE=value\0" + f"{FLAG}={flag_value}\0".encode()
    )


class TestAutomationTemplate:
    def test_template_exists(self) -> None:
        assert TEMPLATE.is_file()

    def test_active_contract_is_exact(self) -> None:
        assert _active_lines(TEMPLATE) == {
            "[Unit]",
            "Description=Brain v42 automation runtime",
            "Documentation=file:__REPO_ROOT__/deploy/systemd/README.md",
            "StartLimitIntervalSec=300",
            "StartLimitBurst=5",
            "[Service]",
            "Type=simple",
            "WorkingDirectory=__REPO_ROOT__",
            "EnvironmentFile=__REPO_ROOT__/.env",
            "ExecStart=__REPO_ROOT__/.venv/bin/python -m brain_v42.automation",
            "UMask=0077",
            "NoNewPrivileges=true",
            "PrivateUsers=true",
            "PrivateTmp=true",
            "PrivateDevices=true",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "ProtectClock=true",
            "ProtectControlGroups=true",
            "ProtectKernelLogs=true",
            "ProtectKernelModules=true",
            "ProtectKernelTunables=true",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "KeyringMode=private",
            "LockPersonality=true",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "RestrictRealtime=true",
            "RestrictSUIDSGID=true",
            "SystemCallArchitectures=native",
            "StandardOutput=journal",
            "StandardError=journal",
            "SyslogIdentifier=brain-v42-automation",
            "KillSignal=SIGTERM",
            "Restart=on-failure",
            "RestartSec=5",
            "TimeoutStopSec=30",
            "[Install]",
            "WantedBy=default.target",
        }

    def test_template_has_no_metrics_lifecycle_relation(self) -> None:
        active = _active_lines(TEMPLATE)
        assert "ExecStart=__REPO_ROOT__/.venv/bin/python -m brain_v42.automation" in active
        forbidden = ("brain-metrics", "Requires=", "Wants=", "PartOf=", "BindsTo=", "Conflicts=")
        assert not [line for line in active if any(item in line for item in forbidden)]


class TestAutomationInstaller:
    def test_missing_template_fails_with_exact_message(self, tmp_path: Path) -> None:
        repo = _fixture_repo(tmp_path, omit_automation=True)
        env, _, _ = _install_env(tmp_path)

        result = _run_install(repo, env, "--dry-run")

        missing = repo / "deploy" / "systemd" / TEMPLATE.name
        assert result.returncode != 0
        assert result.stderr.strip() == f"ERROR: missing automation template: {missing}"

    def test_existing_automation_environment_is_warned_without_leaking_values(
        self, tmp_path: Path
    ) -> None:
        repo = _fixture_repo(tmp_path)
        env, _, _ = _install_env(tmp_path)
        unit = (
            Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / TEMPLATE.name.removesuffix(".tmpl")
        )
        make_directory(unit.parent, parents=True)
        primary_secret = "arc1 secret with spaces"
        quoted_secret = 'arc1-"quoted"-secret'
        unit.write_text(
            "[Service]\n"
            f'Environment="ARC1_PRIMARY={primary_secret}" PUBLIC_FLAG=true\n'
            f"Environment='ARC1_SECOND={quoted_secret}' THIRD=third-value\n"
        )

        result = _run_install(repo, env, "--dry-run")

        assert result.returncode == 0, result.stderr
        output = result.stdout + result.stderr
        assert primary_secret not in output
        assert quoted_secret not in output
        assert f"WARN: {unit} carries Environment= lines that this reinstall WIPES:" in (
            result.stderr
        )
        assert "2 Environment= directives; values redacted" in result.stderr
        assert f"Move them to {unit}.d/*.conf" in result.stderr

    def test_spaced_environment_assignment_is_counted_without_leaking_values(
        self, tmp_path: Path
    ) -> None:
        repo = _fixture_repo(tmp_path)
        env, _, _ = _install_env(tmp_path)
        unit = (
            Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / TEMPLATE.name.removesuffix(".tmpl")
        )
        make_directory(unit.parent, parents=True)
        primary_secret = "arc1 spaced secret value"
        secondary_secret = 'arc1-secondary-"quoted"-value'
        unit.write_text(
            "[Service]\n"
            f'Environment = "ARC1_PRIMARY={primary_secret}" '
            f'"ARC1_SECONDARY={secondary_secret}"\n'
        )

        result = _run_install(repo, env, "--dry-run")

        assert result.returncode == 0, result.stderr
        output = result.stdout + result.stderr
        assert primary_secret not in output
        assert secondary_secret not in output
        assert f"WARN: {unit} carries Environment= lines that this reinstall WIPES:" in (
            result.stderr
        )
        assert "1 Environment= directives; values redacted" in result.stderr

    def test_indented_environment_assignment_is_counted_without_leaking_values(
        self, tmp_path: Path
    ) -> None:
        repo = _fixture_repo(tmp_path)
        env, _, _ = _install_env(tmp_path)
        unit = (
            Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / TEMPLATE.name.removesuffix(".tmpl")
        )
        make_directory(unit.parent, parents=True)
        primary_secret = "arc1 indented secret value"
        secondary_secret = 'arc1-indented-"quoted"-value'
        unit.write_text(
            "[Service]\n"
            f'  Environment = "ARC1_PRIMARY={primary_secret}" '
            f'"ARC1_SECONDARY={secondary_secret}"\n'
        )

        result = _run_install(repo, env, "--dry-run")

        assert result.returncode == 0, result.stderr
        output = result.stdout + result.stderr
        assert primary_secret not in output
        assert secondary_secret not in output
        assert f"WARN: {unit} carries Environment= lines that this reinstall WIPES:" in (
            result.stderr
        )
        assert "1 Environment= directives; values redacted" in result.stderr

    def test_continued_environment_assignment_is_counted_without_leaking_values(
        self, tmp_path: Path
    ) -> None:
        repo = _fixture_repo(tmp_path)
        env, _, _ = _install_env(tmp_path)
        unit = (
            Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / TEMPLATE.name.removesuffix(".tmpl")
        )
        make_directory(unit.parent, parents=True)
        primary_secret = "arc1 continued secret value"
        secondary_secret = 'arc1-continued-"quoted"-value'
        unit.write_text(
            "[Service]\n"
            "Environment \\\n"
            f'= "ARC1_PRIMARY={primary_secret}" \\\n'
            f'  "ARC1_SECONDARY={secondary_secret}"\n'
        )

        result = _run_install(repo, env, "--dry-run")

        assert result.returncode == 0, result.stderr
        output = result.stdout + result.stderr
        assert primary_secret not in output
        assert secondary_secret not in output
        assert f"WARN: {unit} carries Environment= lines that this reinstall WIPES:" in (
            result.stderr
        )
        assert "1 Environment= directives; values redacted" in result.stderr

    def test_environment_scan_error_fails_closed_before_overwriting_unit(
        self, tmp_path: Path
    ) -> None:
        repo = _fixture_repo(tmp_path)
        env, _, _ = _install_env(tmp_path)
        unit = (
            Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / TEMPLATE.name.removesuffix(".tmpl")
        )
        make_directory(unit.parent, parents=True)
        secret = "arc1 preserved secret"
        original = f"[Service]\nEnvironment=ARC1_SECRET={secret}\nExecStart=/bin/false\n"
        unit.write_text(original)
        fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
        _write_executable(
            fake_bin / "awk",
            """
            #!/bin/sh
            exit 2
            """,
        )

        result = _run_install(repo, env, "--dry-run")

        preserved = unit.read_text() == original
        assert result.returncode != 0 and preserved, (
            "Environment scan errors must fail closed before regeneration: "
            f"returncode={result.returncode}, unit_preserved={preserved}"
        )
        output = result.stdout + result.stderr
        assert secret not in output
        assert "ERROR:" in result.stderr
        assert str(unit) in result.stderr

    def test_non_numeric_environment_count_fails_closed_without_echoing_scanner_output(
        self, tmp_path: Path
    ) -> None:
        repo = _fixture_repo(tmp_path)
        env, _, _ = _install_env(tmp_path)
        unit = (
            Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / TEMPLATE.name.removesuffix(".tmpl")
        )
        make_directory(unit.parent, parents=True)
        secret = "arc1 non-numeric preserved secret"
        scanner_output = "not-a-count arc1-scanner-output-secret"
        original = f"[Service]\nEnvironment=ARC1_SECRET={secret}\nExecStart=/bin/false\n"
        unit.write_text(original)
        fake_bin = Path(env["PATH"].split(os.pathsep, 1)[0])
        _write_executable(
            fake_bin / "awk",
            f"""
            #!/bin/sh
            printf '%s\\n' '{scanner_output}'
            """,
        )

        result = _run_install(repo, env, "--dry-run")

        preserved = unit.read_text() == original
        assert result.returncode != 0 and preserved, (
            "Non-numeric scanner output must fail closed before regeneration: "
            f"returncode={result.returncode}, unit_preserved={preserved}"
        )
        output = result.stdout + result.stderr
        assert secret not in output
        assert scanner_output not in output
        assert "ERROR:" in result.stderr
        assert str(unit) in result.stderr

    def test_installer_generates_and_verifies_automation(self, tmp_path: Path) -> None:
        repo = _fixture_repo(tmp_path)
        env, _, analyze_log = _install_env(tmp_path)

        result = _run_install(repo, env, "--dry-run")

        generated = (
            Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / "brain-v42-automation.service"
        )
        assert result.returncode == 0, result.stderr
        assert generated.is_file()
        assert "__REPO_ROOT__" not in generated.read_text()
        verify_lines = analyze_log.read_text().splitlines()
        assert any(
            line.startswith("--user verify ") and line.endswith("/brain-v42-automation.service")
            for line in verify_lines
        )

    def test_normal_install_keeps_automation_dormant(self, tmp_path: Path) -> None:
        repo = _fixture_repo(tmp_path)
        env, systemctl_log, _ = _install_env(tmp_path)

        result = _run_install(repo, env)

        generated = (
            Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / "brain-v42-automation.service"
        )
        assert result.returncode == 0, result.stderr
        assert generated.is_file()
        assert "brain-v42-automation.service remains dormant" in result.stdout
        assert "brain-v42-automation" not in systemctl_log.read_text()

    def test_uninstall_stops_disables_and_removes_automation(self, tmp_path: Path) -> None:
        repo = _fixture_repo(tmp_path)
        env, systemctl_log, _ = _install_env(tmp_path)
        unit = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / "brain-v42-automation.service"
        make_directory(unit.parent, parents=True)
        unit.write_text("[Service]\nExecStart=/bin/true\n")

        result = _run_install(repo, env, "--uninstall")

        log = systemctl_log.read_text().splitlines()
        assert result.returncode == 0, result.stderr
        assert "--user disable --now brain-v42-automation.service" in log
        assert not unit.exists()


class TestSystemdIntegrationModes:
    def test_required_user_manager_fails_instead_of_skipping(self, tmp_path: Path) -> None:
        fake_bin = tmp_path / "manager-unavailable-bin"
        _write_executable(
            fake_bin / "systemd-analyze",
            """
            #!/bin/sh
            exit 0
            """,
        )
        _write_executable(
            fake_bin / "systemctl",
            """
            #!/bin/sh
            exit 1
            """,
        )
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}:{env['PATH']}",
                "REQUIRE_SYSTEMD_ANALYZE": "1",
                "REQUIRE_USER_SYSTEMD": "1",
            }
        )

        result = subprocess.run(
            ["bash", str(INTEGRATION_SCRIPT)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0
        assert "ERROR: user systemd manager is required" in result.stderr
        assert "SKIP: user systemd manager unavailable" not in result.stderr


class TestAutomationRunbookContract:
    def test_runbook_has_all_operator_sections(self) -> None:
        assert RUNBOOK.is_file()
        content = RUNBOOK.read_text()
        for heading in (
            "## Preflight",
            "## Cutover",
            "## Abort immédiat",
            "## Smoke tests",
            "## Rollback",
        ):
            assert heading in content

    def test_late_environment_file_is_authoritative(self) -> None:
        assert RUNBOOK.is_file()
        content = RUNBOOK.read_text()
        assert "90-automation-owner.conf" in content
        assert "EnvironmentFile=%h/.config/brain-v42/automation-owner.env" in content
        assert "Environment=METRICS_LEGACY_AUTOMATION_ENABLED" not in content
        assert "chmod 0600" in content
        assert "EnvironmentFiles" in content
        assert "/$MAIN_PID/environ" in content
        assert not re.search(
            r"systemctl --user show brain-metrics\.service -p Environment(?:\s|$)",
            content,
        )

    def test_preflight_orders_generation_reload_and_inspection(self) -> None:
        block = _extract_runbook_block("preflight")
        assert 'USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"' in block
        check_only = block.index('"$REPO_ROOT/deploy/systemd/install.sh" --check-only')
        rendering = block.index('"$REPO_ROOT/deploy/systemd/install.sh" --render-dir "$RENDER_DIR"')
        publication = block.index(
            'mv -f -- "$NEW_UNIT" "$USER_UNIT_DIR/brain-v42-automation.service"'
        )
        reload = block.index("systemctl --user daemon-reload")
        inspection = block.index(
            '"$USER_UNIT_DIR/brain-v42-automation.service"',
            reload,
        )
        show = block.index("EnvironmentFiles")
        assert check_only < rendering < publication < reload < inspection < show

    def test_preflight_captures_unit_relations_and_enabled_state(self) -> None:
        block = _extract_runbook_block("preflight")
        assert "systemctl --user cat" not in block
        for location in ("FragmentPath", "DropInPaths"):
            assert f"-p {location}" in block
        for relation in ("Requires", "Wants", "PartOf", "BindsTo", "Conflicts"):
            assert f"-p {relation}" in block
        assert "-p UnitFileState" in block

    def test_lease_probe_is_scoped_to_current_database_and_exclusive_lock(self) -> None:
        block = _extract_runbook_block("preflight")
        assert "LOCK_KEY=4151019227643017711" in block
        assert "classid::bigint = 966484478::bigint" in block
        assert "objid::bigint = 2541386223::bigint" in block
        assert "objsubid = 1" in block
        assert (
            "database = (SELECT oid FROM pg_database WHERE datname = current_database())" in block
        )
        assert "mode = 'ExclusiveLock'" in block
        assert "count(*) FILTER (WHERE granted)" in block
        assert "count(*) FILTER (WHERE NOT granted)" in block
        assert "owners=1 waiters=0" in block

    def test_runbook_explains_health_visibility_and_diagnostics(self) -> None:
        assert RUNBOOK.is_file()
        content = RUNBOOK.read_text()
        assert "liveness" in content
        assert "readiness" in content
        assert "cockpit.recent" in content
        assert "in-process" in content
        assert "journalctl --user -u brain-v42-automation.service" in content
        assert "## Diagnostics" in content
        diagnostics = content.split("## Diagnostics", maxsplit=1)[1]
        assert "9201" in diagnostics
        assert "lease conflict" in diagnostics
        assert "ownership_lost" in diagnostics
        assert "503" in diagnostics

    def test_docs_explain_topology_and_link_the_runbook(self) -> None:
        assert RUNBOOK.is_file()
        # The automation-topology paragraph moved from README.md to
        # docs/OPERATIONS.md when README became the open-source draft
        # (ticket bdc4db73); README keeps only a short pointer.
        operations = (ROOT / "docs" / "OPERATIONS.md").read_text()
        architecture = ARCHITECTURE.read_text()
        plan = PLAN.read_text()
        assert "deploy/systemd/README.md" in operations
        assert "AUTOMATION_PORT" in operations
        assert FLAG in operations
        assert "cockpit.recent" in architecture
        assert "non-fencing" in architecture
        assert "127.0.0.1:9201" in architecture
        assert "Les validations précommit utilisent uniquement" in plan
        assert "Le runbook décrit des commandes opérateur hôte" in plan
        assert "EnvironmentFile=%h/.config/brain-v42/automation-owner.env" in plan

    def test_architecture_draws_three_sibling_runtime_boxes(self) -> None:
        overview = ARCHITECTURE.read_text().split("## Overview", maxsplit=1)[1]
        diagram = overview.split("## Core principles", maxsplit=1)[0]
        sibling_headers = [
            line
            for line in diagram.splitlines()
            if all(
                label in line
                for label in ("FastMCP server", "Metrics runtime", "Automation runtime")
            )
        ]

        assert len(sibling_headers) == 1
        assert re.search(
            r"\|\s*FastMCP server.*\|\s+\|\s*Metrics runtime.*"
            r"\|\s+\|\s*Automation runtime.*\|",
            sibling_headers[0],
        )


class TestExecutableRunbookBlocks:
    def test_operator_fixture_ignores_host_mcp_overrides(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MCP_HTTP_HOST", "localhost")
        monkeypatch.setenv("mcp_http_host", "::1")
        monkeypatch.setenv("MCP_HTTP_PORT", "9000")
        monkeypatch.setenv("mcp_http_port", "9001")
        monkeypatch.setenv("MCP_HTTP_TOKEN", "ambient")
        monkeypatch.setenv("mcp_http_token", "shadow")

        environment, _ = _operator_env(tmp_path)
        mcp_environment = {
            key: value
            for key, value in environment.items()
            if key.casefold() in {"mcp_http_host", "mcp_http_port", "mcp_http_token"}
        }

        assert mcp_environment == {
            "MCP_HTTP_HOST": "127.0.0.1",
            "MCP_HTTP_PORT": "8765",
        }

    def test_preflight_executes_with_fakes(self, tmp_path: Path) -> None:
        env, paths = _operator_env(tmp_path)
        _set_process_environment(paths, flag_value="true")
        env["CURL_FAIL_URL"] = "http://127.0.0.1:9201/health"
        env["CURL_FAIL_STATUS"] = "000"

        result = _run_block("preflight", env)

        assert result.returncode == 0, result.stderr
        generated = (
            Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / "brain-v42-automation.service"
        )
        assert generated.is_file()
        assert "--user daemon-reload" in paths["systemctl_log"].read_text()

    def test_preflight_never_emits_raw_unit_contents(self, tmp_path: Path) -> None:
        env, paths = _operator_env(tmp_path)
        _set_process_environment(paths, flag_value="true")
        env["CURL_FAIL_URL"] = "http://127.0.0.1:9201/health"
        env["CURL_FAIL_STATUS"] = "000"
        raw_secret = "arc1-raw-unit-secret-must-not-leak"
        env["FAKE_RAW_UNIT_SECRET"] = raw_secret
        fake_systemctl = Path(env["PATH"].split(os.pathsep)[0]) / "systemctl"
        _write_executable(
            fake_systemctl,
            """
            #!/bin/sh
            printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
            printf 'systemctl %s\n' "$*" >> "$EVENT_LOG"
            case "$*" in
              *MainPID*--value*) printf '%s\n' "${FAKE_MAIN_PID:-4242}" ;;
              *EnvironmentFiles*--value*) printf '%s\n' "$FAKE_ENVIRONMENT_FILES" ;;
              *cat*) printf 'Environment=ARC1_SECRET=%s\n' "$FAKE_RAW_UNIT_SECRET" ;;
            esac
            """,
        )

        result = _run_block("preflight", env)

        output = result.stdout + result.stderr
        assert result.returncode == 0, result.stderr
        assert raw_secret not in output
        assert "Environment=ARC1_SECRET=" not in output

    def test_preflight_replaces_an_executable_stale_probe(self, tmp_path: Path) -> None:
        env, paths = _operator_env(tmp_path)
        _set_process_environment(paths, flag_value="true")
        env["CURL_FAIL_URL"] = "http://127.0.0.1:9201/health"
        env["CURL_FAIL_STATUS"] = "000"
        probe = Path(env["LEASE_PROBE"])
        probe.write_text("#!/bin/sh\nprintf 'stale-probe-survived\\n'\n")
        probe.chmod(0o700)

        result = _run_block("preflight", env)

        assert result.returncode == 0, result.stderr
        content = probe.read_text()
        assert "stale-probe-survived" not in content
        assert "classid::bigint = 966484478::bigint" in content

    def test_preflight_rejects_an_already_bound_automation_port(
        self,
        tmp_path: Path,
    ) -> None:
        env, paths = _operator_env(tmp_path)
        _set_process_environment(paths, flag_value="true")

        result = _run_block("preflight", env)

        assert result.returncode != 0
        assert "already bound" in result.stderr

    def test_preflight_rejects_a_non_http_tcp_listener_on_9201(
        self,
        tmp_path: Path,
    ) -> None:
        env, paths = _operator_env(tmp_path)
        _set_process_environment(paths, flag_value="true")
        env["CURL_FAIL_URL"] = "http://127.0.0.1:9201/health"
        env["CURL_FAIL_STATUS"] = "000"
        env["FAKE_TCP_9201_BOUND"] = "1"

        result = _run_block("preflight", env)

        assert result.returncode != 0
        assert "TCP port 9201 is already bound" in result.stderr
        assert not paths["curl_log"].exists()

    def test_preflight_rejects_an_ss_diagnostic_with_zero_exit(
        self,
        tmp_path: Path,
    ) -> None:
        env, paths = _operator_env(tmp_path)
        _set_process_environment(paths, flag_value="true")
        env["CURL_FAIL_URL"] = "http://127.0.0.1:9201/health"
        env["CURL_FAIL_STATUS"] = "000"
        env["FAKE_SS_ERROR_ZERO"] = "1"

        result = _run_block("preflight", env)

        assert result.returncode != 0
        assert "TCP port 9201 is already bound or could not be inspected" in result.stderr
        assert not paths["curl_log"].exists()

    def test_preflight_displays_only_the_effective_legacy_flag(
        self,
        tmp_path: Path,
    ) -> None:
        env, paths = _operator_env(tmp_path)
        _set_process_environment(paths, flag_value="true")
        env["CURL_FAIL_URL"] = "http://127.0.0.1:9201/health"
        env["CURL_FAIL_STATUS"] = "000"

        result = _run_block("preflight", env)

        output = result.stdout + result.stderr
        assert result.returncode == 0, result.stderr
        assert output.count(f"{FLAG}=true") == 1
        assert "IGNORED_SECRET" not in output
        assert "must-not-leak" not in output
        assert "OTHER_PRIVATE" not in output

    def test_preflight_rejects_a_non_true_effective_legacy_flag(
        self,
        tmp_path: Path,
    ) -> None:
        env, paths = _operator_env(tmp_path)
        _set_process_environment(paths, flag_value="false")
        env["CURL_FAIL_URL"] = "http://127.0.0.1:9201/health"
        env["CURL_FAIL_STATUS"] = "000"

        result = _run_block("preflight", env)

        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert f"expected {FLAG}=true" in result.stderr
        assert "IGNORED_SECRET" not in output
        assert "must-not-leak" not in output
        assert "OTHER_PRIVATE" not in output

    def test_cutover_executes_and_writes_false_env(self, tmp_path: Path) -> None:
        env, paths = _operator_env(tmp_path)

        result = _run_block("cutover", env)

        assert result.returncode == 0, result.stderr
        assert paths["owner_env"].read_text() == f"{FLAG}=false\n"
        assert paths["owner_env"].stat().st_mode & 0o777 == 0o600
        assert paths["lease_log"].read_text().splitlines() == ["0", "1", "1"]

    def test_abort_stops_after_zero_lease_failure(self, tmp_path: Path) -> None:
        env, paths = _operator_env(tmp_path)
        env["FAIL_LEASE_EXPECTED"] = "0"

        result = _run_block("abort", env)

        assert result.returncode == 42
        assert paths["owner_env"].read_text() == f"{FLAG}=false\n"
        assert paths["lease_log"].read_text().splitlines() == ["0"]
        assert paths["event_log"].read_text().splitlines()[:3] == [
            "systemctl --user stop brain-v42-automation.service",
            "systemctl --user reset-failed brain-v42-automation.service",
            "lease 0",
        ]
        log = paths["systemctl_log"].read_text()
        assert "stop brain-v42-automation.service" in log
        assert "restart brain-metrics.service" not in log
        assert "start brain-metrics.service" not in log

    def test_smoke_checks_complete_matrix(self, tmp_path: Path) -> None:
        env, paths = _operator_env(tmp_path)

        result = _run_block("smoke", env)

        assert result.returncode == 0, result.stderr
        curl_log = paths["curl_log"].read_text()
        for endpoint in (
            "http://127.0.0.1:9201/health",
            "http://127.0.0.1:9201/metrics",
            "http://127.0.0.1:9201/api/cockpit",
            "http://127.0.0.1:9201/gitlab/webhook",
            "http://127.0.0.1:9200/metrics",
            "http://127.0.0.1:9200/api/cockpit",
            "http://127.0.0.1:9200/gitlab/webhook",
        ):
            assert endpoint in curl_log
        assert "--connect-timeout" in curl_log
        assert "--max-time" in curl_log
        assert paths["lease_log"].read_text().splitlines() == ["1"]

    def test_smoke_fails_fast_on_intermediate_http_error(self, tmp_path: Path) -> None:
        env, paths = _operator_env(tmp_path)
        env["CURL_FAIL_URL"] = "http://127.0.0.1:9201/api/cockpit"
        env["CURL_FAIL_STATUS"] = "500"

        result = _run_block("smoke", env)

        assert result.returncode != 0
        curl_log = paths["curl_log"].read_text()
        assert "http://127.0.0.1:9201/api/cockpit" in curl_log
        assert "http://127.0.0.1:9201/gitlab/webhook" not in curl_log
        assert not paths["lease_log"].exists()

    def test_rollback_executes_in_safe_order(self, tmp_path: Path) -> None:
        env, paths = _operator_env(tmp_path)
        env["HOOK_DISABLED_CONFIRMED"] = "yes"
        env["LEGACY_WEBHOOK_STATUS"] = "401"
        environ = paths["proc_root"] / "4242" / "environ"
        environ.write_bytes(f"{FLAG}=true\0".encode())

        result = _run_block("rollback", env)

        assert result.returncode == 0, result.stderr
        assert paths["owner_env"].read_text() == f"{FLAG}=true\n"
        assert paths["owner_env"].stat().st_mode & 0o777 == 0o600
        log = paths["systemctl_log"].read_text().splitlines()
        stop_index = log.index("--user stop brain-v42-automation.service")
        disable_index = log.index("--user disable brain-v42-automation.service")
        reload_index = log.index("--user daemon-reload")
        restart_index = log.index("--user restart brain-metrics.service")
        assert stop_index < disable_index < reload_index < restart_index
        assert paths["lease_log"].read_text().splitlines() == ["0", "1"]

    def test_rollback_stops_after_zero_lease_failure(self, tmp_path: Path) -> None:
        env, paths = _operator_env(tmp_path)
        env["HOOK_DISABLED_CONFIRMED"] = "yes"
        env["FAIL_LEASE_EXPECTED"] = "0"

        result = _run_block("rollback", env)

        assert result.returncode == 42
        assert paths["owner_env"].read_text() == f"{FLAG}=false\n"
        assert paths["lease_log"].read_text().splitlines() == ["0"]
        assert paths["event_log"].read_text().splitlines()[:3] == [
            "systemctl --user stop brain-v42-automation.service",
            "systemctl --user disable brain-v42-automation.service",
            "lease 0",
        ]
        log = paths["systemctl_log"].read_text()
        assert "stop brain-v42-automation.service" in log
        assert "restart brain-metrics.service" not in log

    def test_rollback_rejects_non_yes_hook_confirmation(self, tmp_path: Path) -> None:
        env, paths = _operator_env(tmp_path)
        env["HOOK_DISABLED_CONFIRMED"] = "no"

        result = _run_block("rollback", env)

        assert result.returncode != 0
        assert "must equal yes" in result.stderr
        assert paths["owner_env"].read_text() == f"{FLAG}=false\n"
        assert not paths["event_log"].exists()
        assert not paths["lease_log"].exists()


@pytest.mark.parametrize("name", ["preflight", "cutover", "abort", "smoke", "rollback"])
def test_each_runbook_block_is_fail_fast(name: str) -> None:
    block = _extract_runbook_block(name)
    assert block.lstrip().startswith("set -euo pipefail")
