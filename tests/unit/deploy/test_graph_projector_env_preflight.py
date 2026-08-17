"""Filesystem security checks for the service-private projector credential."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.check_graph_projector_env import (
    ProjectorEnvironmentError,
    validate_projector_environment,
)

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_graph_projector_env.py"
RUNBOOK = SCRIPT.parents[1] / "deploy" / "systemd" / "MCP_HTTP_RUNBOOK.md"


def _runbook_shell_block(heading: str) -> str:
    sections = RUNBOOK.read_text(encoding="utf-8").split(f"### {heading}\n", maxsplit=1)
    assert len(sections) == 2, f"missing runbook section: {heading}"
    section = sections[1]
    return section.split("```bash\n", maxsplit=1)[1].split("```", maxsplit=1)[0]


def _shared(path: Path, *, ledger_enabled: bool) -> Path:
    path.write_text(f"GRAPH_LEDGER_WRITE_ENABLED={'true' if ledger_enabled else 'false'}\n")
    return path


def _private(path: Path, *, password: str = "private-canary") -> Path:
    path.write_text(
        "GRAPH_PROJECTOR_ENABLED=true\n"
        "GRAPH_PROJECTOR_NEO4J_URL=bolt://127.0.0.1:7687\n"
        "GRAPH_PROJECTOR_NEO4J_USER=projector\n"
        f"GRAPH_PROJECTOR_NEO4J_PASSWORD={password}\n"
    )
    path.chmod(0o600)
    return path


def test_dormant_ledger_does_not_require_private_environment(tmp_path: Path) -> None:
    validate_projector_environment(
        _shared(tmp_path / "shared.env", ledger_enabled=False),
        tmp_path / "missing-private.env",
        expected_uid=os.getuid(),
    )


def test_shared_environment_rejects_private_projector_keys(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=False)
    with shared.open("a") as stream:
        stream.write("graph_projector_neo4j_password=shared-canary\n")

    with pytest.raises(ProjectorEnvironmentError, match="shared environment contains") as exc_info:
        validate_projector_environment(
            shared,
            tmp_path / "missing-private.env",
            expected_uid=os.getuid(),
        )

    assert "shared-canary" not in str(exc_info.value)


@pytest.mark.parametrize("legacy_key", ("neo4j_url", "NeO4J_UsEr", "NEO4J_PASSWORD"))
def test_shared_environment_rejects_legacy_neo4j_keys(
    tmp_path: Path,
    legacy_key: str,
) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=False)
    with shared.open("a") as stream:
        stream.write(f"{legacy_key}=legacy-shared-canary\n")

    with pytest.raises(
        ProjectorEnvironmentError, match="shared environment contains legacy"
    ) as exc:
        validate_projector_environment(
            shared,
            tmp_path / "missing-private.env",
            expected_uid=os.getuid(),
        )

    assert "legacy-shared-canary" not in str(exc.value)


def test_effective_environment_rejects_legacy_neo4j_keys(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=False)

    with pytest.raises(
        ProjectorEnvironmentError, match="effective environment contains legacy"
    ) as exc:
        validate_projector_environment(
            shared,
            tmp_path / "missing-private.env",
            expected_uid=os.getuid(),
            effective_environment={"nEo4j_PaSsWoRd": "legacy-effective-canary"},
        )

    assert "legacy-effective-canary" not in str(exc.value)


def test_dormant_ledger_still_rejects_an_unsafe_present_private_file(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=False)
    private = _private(tmp_path / "private.env")
    with private.open("a") as stream:
        stream.write("POSTGRES_URL=dormant-override-canary\n")

    with pytest.raises(ProjectorEnvironmentError, match="unexpected keys") as exc_info:
        validate_projector_environment(shared, private, expected_uid=os.getuid())

    assert "dormant-override-canary" not in str(exc_info.value)


def test_effective_ledger_flag_cannot_override_the_shared_cutover_flag(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=False)
    private = _private(tmp_path / "private.env")

    with pytest.raises(ProjectorEnvironmentError, match="effective graph ledger flag"):
        validate_projector_environment(
            shared,
            private,
            expected_uid=os.getuid(),
            effective_environment={"graph_ledger_write_enabled": "true"},
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"graph_ledger_write_enabled": "true"}, "effective graph ledger flag differs"),
        (
            {
                "GRAPH_LEDGER_WRITE_ENABLED": "false",
                "graph_ledger_write_enabled": "true",
            },
            "effective GRAPH_LEDGER_WRITE_ENABLED is assigned more than once",
        ),
    ),
)
def test_cli_rejects_case_insensitive_effective_ledger_overrides(
    tmp_path: Path,
    overrides: dict[str, str],
    message: str,
) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=False)
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.casefold() in {
            "graph_ledger_write_enabled",
            "graph_projector_enabled",
            "graph_projector_neo4j_password",
            "graph_projector_neo4j_url",
            "graph_projector_neo4j_user",
            "neo4j_password",
            "neo4j_url",
            "neo4j_user",
        }:
            environment.pop(key)
    environment.update(overrides)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--shared",
            str(shared),
            "--private",
            str(tmp_path / "missing-private.env"),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert message in result.stderr
    assert not any(value in result.stderr for value in overrides.values())


def test_runbook_separates_executable_file_preflight_from_systemd_attestation(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    python = repo / ".venv" / "bin" / "python"
    checker = repo / "scripts" / "check_graph_projector_env.py"
    checker.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    checker.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    python.symlink_to(sys.executable)
    _shared(repo / ".env", ledger_enabled=True)

    home = tmp_path / "home"
    private = home / ".config" / "brain-v42" / "graph-projector.env"
    private.parent.mkdir(parents=True)
    _private(private)

    environment = os.environ.copy()
    for key in tuple(environment):
        if (
            key.casefold().startswith("graph_projector_")
            or key.casefold() == "graph_ledger_write_enabled"
        ):
            environment.pop(key)
    environment["HOME"] = str(home)

    file_preflight = _runbook_shell_block("Préflight de fichiers hors systemd")
    result = subprocess.run(
        ["bash", "-c", file_preflight],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

    service = (RUNBOOK.parent / "brain-mcp-http.service.tmpl").read_text(encoding="utf-8")
    effective_preflight = next(
        line.removeprefix("ExecStartPre=")
        for line in service.splitlines()
        if "check_graph_projector_env.py" in line
    )
    command = shlex.split(effective_preflight.replace("__REPO_ROOT__", str(repo)))
    command = [part.replace("%h", str(home)) for part in command]

    missing_effective = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing_effective.returncode == 2
    assert "effective private projector" in missing_effective.stderr

    effective_environment = environment | {
        "GRAPH_LEDGER_WRITE_ENABLED": "true",
        "GRAPH_PROJECTOR_ENABLED": "true",
        "GRAPH_PROJECTOR_NEO4J_URL": "bolt://127.0.0.1:7687",
        "GRAPH_PROJECTOR_NEO4J_USER": "projector",
        "GRAPH_PROJECTOR_NEO4J_PASSWORD": "private-canary",
    }
    attested = subprocess.run(
        command,
        env=effective_environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert attested.returncode == 0, attested.stderr
    command_log = tmp_path / "systemctl.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$SYSTEMCTL_LOG"\n'
        'if [[ "$2" == show ]]; then\n'
        "  grep -q '^--user restart' \"$SYSTEMCTL_LOG\" && echo 4321 || echo 0\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    operator_environment = environment | {
        "PATH": f"{fake_bin}:{environment['PATH']}",
        "SYSTEMCTL_LOG": str(command_log),
    }
    operator_attestation = subprocess.run(
        ["bash", "-c", _runbook_shell_block("Attestation effective par systemd")],
        cwd=repo,
        env=operator_environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert operator_attestation.returncode == 0, operator_attestation.stderr
    assert command_log.read_text(encoding="utf-8").splitlines() == [
        "--user show brain-mcp-http.service -p MainPID --value",
        "--user restart brain-mcp-http.service",
        "--user is-active --quiet brain-mcp-http.service",
        "--user show brain-mcp-http.service -p MainPID --value",
    ]


def test_runbook_rollback_compensates_a_failure_after_the_first_replacement(
    tmp_path: Path,
) -> None:
    units = (
        "brain-mcp-http.service",
        "brain-mcp-http-watchdog.service",
        "brain-mcp-http-watchdog.timer",
    )
    user_unit_dir = tmp_path / "user-units"
    backup_dir = tmp_path / "backup"
    user_unit_dir.mkdir()
    backup_dir.mkdir()
    original_contents = {unit: f"before-{unit}\n" for unit in units}
    for unit in units:
        (user_unit_dir / unit).write_text(original_contents[unit], encoding="utf-8")
        (backup_dir / unit).write_text(f"after-{unit}\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    failed_moves = tmp_path / "failed-moves"
    (fake_bin / "mv").write_text(
        "#!/usr/bin/env bash\n"
        'for argument in "$@"; do\n'
        '  [[ "$argument" == *replacement.* ]] || continue\n'
        "  count=0\n"
        '  [[ -f "$ROLLBACK_MOVE_COUNT" ]] && count=$(cat "$ROLLBACK_MOVE_COUNT")\n'
        "  count=$((count + 1))\n"
        '  printf \'%s\' "$count" > "$ROLLBACK_MOVE_COUNT"\n'
        '  if [[ "$count" == 2 ]]; then exit 42; fi\n'
        "done\n"
        'exec /bin/mv "$@"\n',
        encoding="utf-8",
    )
    (fake_bin / "systemctl").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for command in (fake_bin / "mv", fake_bin / "systemctl"):
        command.chmod(0o755)

    environment = os.environ | {
        "BACKUP_DIR": str(backup_dir),
        "USER_UNIT_DIR": str(user_unit_dir),
        "ROLLBACK_MOVE_COUNT": str(failed_moves),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    rollback = _runbook_shell_block("Rollback compensatoire des unités")
    result = subprocess.run(
        [
            "bash",
            "-c",
            'backup_dir="$BACKUP_DIR"\nuser_unit_dir="$USER_UNIT_DIR"\n' + rollback,
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert failed_moves.read_text(encoding="utf-8") == "2"
    assert {
        unit: (user_unit_dir / unit).read_text(encoding="utf-8") for unit in units
    } == original_contents


def test_active_ledger_requires_a_regular_owned_0600_file(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=True)
    private = _private(tmp_path / "private.env")

    validate_projector_environment(shared, private, expected_uid=os.getuid())

    private.chmod(0o640)
    with pytest.raises(ProjectorEnvironmentError, match="mode 0600"):
        validate_projector_environment(shared, private, expected_uid=os.getuid())


def test_active_ledger_rejects_missing_symlinked_or_wrong_owner_file(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=True)
    private = _private(tmp_path / "private.env")

    with pytest.raises(ProjectorEnvironmentError, match="required"):
        validate_projector_environment(shared, tmp_path / "missing.env", expected_uid=os.getuid())

    link = tmp_path / "private-link.env"
    link.symlink_to(private)
    with pytest.raises(ProjectorEnvironmentError, match="regular file"):
        validate_projector_environment(shared, link, expected_uid=os.getuid())

    with pytest.raises(ProjectorEnvironmentError, match="owner"):
        validate_projector_environment(shared, private, expected_uid=os.getuid() + 1)


def test_active_ledger_requires_all_private_keys_and_rejects_legacy_keys(
    tmp_path: Path,
) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=True)
    private = _private(tmp_path / "private.env")
    private.write_text("GRAPH_PROJECTOR_ENABLED=true\n")

    with pytest.raises(ProjectorEnvironmentError, match="required keys"):
        validate_projector_environment(shared, private, expected_uid=os.getuid())

    _private(private)
    with private.open("a") as stream:
        stream.write("NEO4J_PASSWORD=legacy-canary\n")
    with pytest.raises(ProjectorEnvironmentError, match="legacy Neo4j keys") as exc_info:
        validate_projector_environment(shared, private, expected_uid=os.getuid())

    assert "legacy-canary" not in str(exc_info.value)


def test_case_insensitive_shared_ledger_flag_cannot_bypass_private_file(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared.env"
    shared.write_text("graph_ledger_write_enabled=true\n")

    with pytest.raises(ProjectorEnvironmentError, match="private graph projector"):
        validate_projector_environment(
            shared,
            tmp_path / "missing-private.env",
            expected_uid=os.getuid(),
        )


def test_effective_projector_settings_must_match_the_private_file(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=True)
    private = _private(tmp_path / "private.env")

    with pytest.raises(ProjectorEnvironmentError, match="differ from the private") as exc_info:
        validate_projector_environment(
            shared,
            private,
            expected_uid=os.getuid(),
            effective_environment={
                "graph_ledger_write_enabled": "true",
                "graph_projector_neo4j_password": "override-canary",
            },
        )

    assert "override-canary" not in str(exc_info.value)


def test_systemd_mode_requires_all_effective_private_projector_settings(
    tmp_path: Path,
) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=True)
    private = _private(tmp_path / "private.env")

    with pytest.raises(ProjectorEnvironmentError, match="effective private projector"):
        validate_projector_environment(
            shared,
            private,
            expected_uid=os.getuid(),
            effective_environment={"GRAPH_LEDGER_WRITE_ENABLED": "true"},
            require_effective_private=True,
        )


def test_private_projector_file_rejects_export_syntax(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=True)
    private = _private(tmp_path / "private.env")
    private.write_text(
        private.read_text().replace(
            "GRAPH_PROJECTOR_ENABLED=true",
            "export GRAPH_PROJECTOR_ENABLED=true",
        )
    )

    with pytest.raises(ProjectorEnvironmentError, match="systemd assignment"):
        validate_projector_environment(shared, private, expected_uid=os.getuid())


def test_active_ledger_rejects_every_unexpected_private_assignment(tmp_path: Path) -> None:
    shared = _shared(tmp_path / "shared.env", ledger_enabled=True)
    private = _private(tmp_path / "private.env")
    with private.open("a") as stream:
        stream.write("POSTGRES_URL=private-override-canary\n")

    with pytest.raises(ProjectorEnvironmentError, match="unexpected keys") as exc_info:
        validate_projector_environment(shared, private, expected_uid=os.getuid())

    assert "private-override-canary" not in str(exc_info.value)
