"""Contract tests for the secret-safe Neo4j credential rotation CLI."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import scripts.rotate_neo4j_credential as rotation
import yaml
from neo4j.exceptions import AuthError
from scripts.rotate_neo4j_credential import (
    RotationConfig,
    RotationError,
    _atomic_write,
    _compose_up_command,
    _exclusive_lock,
    _probe_credential,
    _render_shared_environment,
    _rotate_password,
    _validate_container_metadata,
    parse_args,
    run_rotation,
)

from tests.unit._fixture_modes import make_directory, write_file

REPO_ROOT = Path(__file__).parents[2]
OLD_SECRET = "old-secret-canary"
NEW_SECRET = "new-secret-canary"


@pytest.fixture(autouse=True)
def _isolated_operator_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = tmp_path / "private" / "brain-v42"
    make_directory(expected.parent, mode=0o700)
    monkeypatch.setattr(rotation, "_operator_config_dir", lambda: expected, raising=False)


class FakeDriver:
    """Complete one-shot driver surface consumed by the rotation script."""

    def __init__(
        self,
        *,
        verify_error: Exception | None = None,
        rotate: Callable[[str, str], None] | None = None,
    ) -> None:
        self.verify_error = verify_error
        self.rotate = rotate
        self.closed = False
        self.executed: list[tuple[str, dict[str, str], str]] = []

    def verify_connectivity(self) -> None:
        if self.verify_error is not None:
            raise self.verify_error

    def execute_query(
        self,
        query: str,
        *,
        parameters_: dict[str, str],
        database_: str,
    ) -> None:
        self.executed.append((query, parameters_, database_))
        if self.rotate is not None:
            self.rotate(parameters_["old_password"], parameters_["new_password"])

    def close(self) -> None:
        self.closed = True


class CredentialWorld:
    """Stateful fake Neo4j authentication boundary."""

    def __init__(self, valid_password: str = OLD_SECRET) -> None:
        self.valid_password = valid_password
        self.drivers: list[FakeDriver] = []

    def factory(self, _uri: str, *, auth: tuple[str, str]) -> FakeDriver:
        password = auth[1]
        verify_error: Exception | None = None
        if password != self.valid_password:
            verify_error = AuthError("invalid credentials with no rendered password")

        def rotate(old_password: str, new_password: str) -> None:
            if old_password != self.valid_password:
                raise AuthError("invalid credentials")
            self.valid_password = new_password

        driver = FakeDriver(verify_error=verify_error, rotate=rotate)
        self.drivers.append(driver)
        return driver


class FakeCommands:
    """Docker command boundary returning only sanitized evidence."""

    def __init__(self, repo_root: Path, *, fail_up_once: bool = False) -> None:
        self.repo_root = repo_root
        self.fail_up_once = fail_up_once
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.recreated = False

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        self.kwargs.append(kwargs)
        rendered = " ".join(args)
        assert OLD_SECRET not in rendered
        assert NEW_SECRET not in rendered
        if "compose-config" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        if "inspect" in args:
            clean = self.recreated
            output = (
                f"project={self.repo_root}\n"
                f"auth_file={'true' if clean else 'false'}\n"
                f"auth_source={'true' if clean else 'false'}\n"
                f"legacy_auth={'false' if clean else 'true'}\n"
                f"healthcheck_safe={'true' if clean else 'false'}\n"
            )
            return subprocess.CompletedProcess(args, 0, output, "")
        if "compose-up" in args:
            if self.fail_up_once:
                self.fail_up_once = False
                return subprocess.CompletedProcess(args, 1, "", "docker-secret-canary")
            self.recreated = True
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command: {args}")


def _config(tmp_path: Path, *, apply: bool, resume: bool = False) -> RotationConfig:
    repo_root = make_directory(tmp_path / "repo")
    write_file(repo_root / "docker-compose.yml", "services: {}\n")
    helper = repo_root / "scripts/rotate_neo4j_container.sh"
    make_directory(helper.parent)
    write_file(helper, "#!/usr/bin/env bash\nexit 0\n", mode=0o700)
    shared_env = write_file(
        repo_root / ".env",
        "GRAPH_ENABLED=true\n"
        "GRAPH_LEDGER_WRITE_ENABLED=false\n"
        "NEO4J_URL=bolt://127.0.0.1:7687\n"
        "NEO4J_USER=neo4j\n"
        f"NEO4J_PASSWORD={OLD_SECRET}\n"
        "UNRELATED=value\n",
        mode=0o600,
    )
    return RotationConfig(
        repo_root=repo_root,
        shared_env=shared_env,
        config_dir=tmp_path / "private" / "brain-v42",
        neo4j_uri="bolt://127.0.0.1:7687",
        apply=apply,
        writers_off_confirmed=apply,
        neo4j_sessions_zero_confirmed=apply,
        neo4j_dedicated_confirmed=apply,
        postgres_restore_tested=apply,
        resume=resume,
    )


def test_shared_environment_removes_legacy_and_collapses_ledger_flag() -> None:
    original = (
        "# preserved\n"
        "export neo4j_url=bolt://legacy:7687\n"
        "Neo4j_User=neo4j\n"
        "neo4J_PASSWORD=old\n"
        "graph_ledger_write_enabled=false\n"
        "Graph_Ledger_Write_Enabled=false\n"
        "OTHER=still-here\n"
    )

    rendered = _render_shared_environment(original)

    assert "NEO4J_URL=" not in rendered
    assert "\nNEO4J_USER=" not in rendered
    assert "\nNEO4J_PASSWORD=" not in rendered
    assert "neo4j_url=" not in rendered
    assert "Neo4j_User=" not in rendered
    assert "neo4J_PASSWORD=" not in rendered
    assert rendered.count("GRAPH_LEDGER_WRITE_ENABLED=true") == 1
    assert "# preserved" in rendered
    assert "OTHER=still-here" in rendered


def test_atomic_write_sets_mode_and_replaces_complete_content(tmp_path: Path) -> None:
    target = tmp_path / "credential"
    target.write_text("old")

    _atomic_write(target, b"new-complete\n", 0o600)

    assert target.read_bytes() == b"new-complete\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".credential.*"))


def test_atomic_write_failure_preserves_original_and_hides_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "credential"
    target.write_text("original")
    secret_payload = b"payload-secret-canary"

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace-error-secret-canary")

    monkeypatch.setattr(rotation.os, "replace", fail_replace)

    with pytest.raises(RotationError) as exc_info:
        _atomic_write(target, secret_payload, 0o600)

    assert target.read_text() == "original"
    assert "secret-canary" not in str(exc_info.value)
    assert exc_info.value.__context__ is None
    assert not list(tmp_path.glob(".credential.*"))


def test_exclusive_lock_rejects_a_second_owner_and_uses_0600(tmp_path: Path) -> None:
    lock_path = tmp_path / "rotation.lock"

    with _exclusive_lock(lock_path):
        second_fd = os.open(lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(second_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(second_fd)

    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_probe_accepts_only_auth_error_as_a_refusal() -> None:
    refused = FakeDriver(verify_error=AuthError("auth-secret-canary"))
    assert (
        _probe_credential(lambda *_args, **_kwargs: refused, "bolt://local", "neo4j", "x") is False
    )
    assert refused.closed is True

    unavailable = FakeDriver(verify_error=RuntimeError("network-secret-canary"))
    with pytest.raises(RotationError) as exc_info:
        _probe_credential(lambda *_args, **_kwargs: unavailable, "bolt://local", "neo4j", "x")
    assert "secret-canary" not in str(exc_info.value)
    assert exc_info.value.__context__ is None
    assert unavailable.closed is True


def test_rotation_uses_parameters_on_system_database() -> None:
    driver = FakeDriver()

    _rotate_password(
        lambda *_args, **_kwargs: driver,
        "bolt://local",
        "neo4j",
        OLD_SECRET,
        NEW_SECRET,
    )

    assert driver.closed is True
    query, parameters, database = driver.executed[0]
    assert "$old_password" in query
    assert "$new_password" in query
    assert OLD_SECRET not in query
    assert NEW_SECRET not in query
    assert parameters == {"old_password": OLD_SECRET, "new_password": NEW_SECRET}
    assert database == "system"


def test_compose_command_is_bound_to_the_explicit_repository(tmp_path: Path) -> None:
    config = _config(tmp_path, apply=True)

    command = _compose_up_command(config)

    assert command == [
        "scripts/rotate_neo4j_container.sh",
        "compose-up",
    ]
    helper = REPO_ROOT / "scripts/rotate_neo4j_container.sh"
    assert helper.is_file()
    assert helper.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            ["compose-config"],
            ["compose", "-f", "{compose}", "config", "--no-interpolate", "--quiet"],
        ),
        (
            ["compose-up"],
            [
                "compose",
                "-f",
                "{compose}",
                "up",
                "-d",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "90",
                "--no-deps",
                "--force-recreate",
                "neo4j",
            ],
        ),
        (["inspect", "safe-template"], ["inspect", "--format", "safe-template", "brain_v42_neo4j"]),
    ],
    ids=["compose-config", "compose-up", "inspect"],
)
def test_rotation_container_helper_uses_fixed_docker_argv(
    tmp_path: Path, arguments: list[str], expected: list[str]
) -> None:
    capture = tmp_path / "argv"
    fake_bin = make_directory(tmp_path / "bin")
    write_file(
        fake_bin / "docker",
        '#!/usr/bin/env bash\nprintf "%s\\0" "$@" > "$CAPTURE"\n',
        mode=0o700,
    )
    helper = REPO_ROOT / "scripts/rotate_neo4j_container.sh"
    environment = {
        "CAPTURE": str(capture),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [str(helper), *arguments],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    compose = f"{REPO_ROOT}/scripts/../docker-compose.yml"
    assert result.returncode == 0, result.stderr
    assert capture.read_bytes().rstrip(b"\0").split(b"\0") == [
        argument.format(compose=compose).encode() for argument in expected
    ]


@pytest.mark.parametrize(
    "arguments",
    [[], ["unknown"], ["compose-up", "extra"], ["inspect"], ["inspect", "one", "two"]],
)
def test_rotation_container_helper_rejects_open_ended_actions(
    tmp_path: Path, arguments: list[str]
) -> None:
    helper = REPO_ROOT / "scripts/rotate_neo4j_container.sh"

    result = subprocess.run(
        [str(helper), *arguments],
        cwd=REPO_ROOT,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 64


def test_inspection_contract_matches_the_compose_healthcheck_and_auth_file() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    neo4j = compose["services"]["neo4j"]
    expected_healthcheck = tuple(neo4j["healthcheck"]["test"])
    config_dir = Path("/home/service/.config/brain-v42")
    inspect_template = rotation._inspect_command(config_dir)[2]

    assert not inspect_template.endswith("\n")
    assert rotation._EXPECTED_HEALTHCHECK == expected_healthcheck
    assert "NEO4J_AUTH_FILE=/run/secrets/neo4j_auth" in inspect_template
    assert str(config_dir / "neo4j-auth") in inspect_template
    expected_mount_clause = (
        f"(eq .Source {json.dumps(str(config_dir / 'neo4j-auth'))}) "
        '-}}{{- $authSource = "true" -}}'
    )
    assert expected_mount_clause in inspect_template
    assert f"(eq (len .Config.Healthcheck.Test) {len(expected_healthcheck)})" in inspect_template
    for index, argument in enumerate(expected_healthcheck):
        assert (
            f"(eq (index .Config.Healthcheck.Test {index}) {json.dumps(argument)})"
            in inspect_template
        )


def test_shared_environment_must_be_owned_private_0600(tmp_path: Path) -> None:
    shared_env = tmp_path / ".env"
    shared_env.write_text(f"NEO4J_PASSWORD={OLD_SECRET}\n")
    shared_env.chmod(0o644)

    with pytest.raises(RotationError, match="shared environment is unreadable"):
        rotation._read_shared_environment(shared_env)


@pytest.mark.parametrize(
    "unsafe_lines",
    [
        "GRAPH_ENABLED=false\n",
        "GRAPH_ENABLED=true\nGRAPH_ENABLED=true\n",
        "",
        f"GRAPH_ENABLED=true\ngraph_projector_neo4j_password={OLD_SECRET}\n",
    ],
)
def test_preflight_refuses_unsafe_shared_graph_configuration(
    tmp_path: Path, unsafe_lines: str
) -> None:
    config = _config(tmp_path, apply=False)
    original = config.shared_env.read_text()
    without_graph_enabled = "\n".join(
        line for line in original.splitlines() if not line.startswith("GRAPH_ENABLED=")
    )
    config.shared_env.write_text(f"{unsafe_lines}{without_graph_enabled}\n")
    config.shared_env.chmod(0o600)
    commands = FakeCommands(config.repo_root)

    with pytest.raises(RotationError, match="shared environment is unsafe"):
        run_rotation(config, driver_factory=lambda *_a, **_k: None, command_runner=commands)

    assert commands.calls == []


def test_rotation_state_repr_never_contains_secret_material() -> None:
    state = rotation.RotationState(
        old_password=OLD_SECRET,
        new_password=NEW_SECRET,
        original_shared_environment=f"NEO4J_PASSWORD={OLD_SECRET}\n",
        original_shared_mode=0o600,
    )

    rendered = repr(state)
    assert OLD_SECRET not in rendered
    assert NEW_SECRET not in rendered


def test_container_metadata_requires_canonical_label_and_safe_auth(tmp_path: Path) -> None:
    repo_root = tmp_path / "canonical"
    safe = (
        f"project={repo_root}\n"
        "auth_file=true\n"
        "auth_source=true\n"
        "legacy_auth=false\n"
        "healthcheck_safe=true\n"
    )
    assert _validate_container_metadata(safe, repo_root, require_clean=True) is True

    for unsafe in (
        safe.replace(str(repo_root), str(tmp_path / "wrong")),
        safe.replace("auth_file=true", "auth_file=false"),
        safe.replace("auth_source=true", "auth_source=false"),
        safe.replace("legacy_auth=false", "legacy_auth=true"),
        safe.replace("healthcheck_safe=true", "healthcheck_safe=false"),
    ):
        with pytest.raises(RotationError):
            _validate_container_metadata(unsafe, repo_root, require_clean=True)


def test_preflight_is_default_and_read_only(tmp_path: Path) -> None:
    config = _config(tmp_path, apply=False)
    original = config.shared_env.read_bytes()
    commands = FakeCommands(config.repo_root)

    result = run_rotation(config, driver_factory=lambda *_a, **_k: None, command_runner=commands)

    assert result == {
        "rotation_confirmations_required": 4,
        "apply": False,
        "canonical_repo_valid": True,
        "compose_valid": True,
        "neo4j_target_valid": True,
        "status": "preflight_ok",
    }
    assert config.shared_env.read_bytes() == original
    assert not config.config_dir.exists()
    assert not any("up" in call for call in commands.calls)
    assert all(kwargs["cwd"] == config.repo_root for kwargs in commands.kwargs)
    assert all("env" in kwargs for kwargs in commands.kwargs)
    assert all(
        not {"BASH_ENV", "ENV", "SHELLOPTS"}.intersection(kwargs["env"])
        and not any(name.startswith("BASH_FUNC_") for name in kwargs["env"])
        for kwargs in commands.kwargs
    )

    compose_call = next(
        kwargs
        for call, kwargs in zip(commands.calls, commands.kwargs, strict=True)
        if "compose-config" in call
    )
    command_environment = compose_call["env"]
    assert command_environment["BRAIN_NEO4J_AUTH_FILE"] == str(config.config_dir / "neo4j-auth")
    assert OLD_SECRET not in json.dumps(command_environment)
    assert NEW_SECRET not in json.dumps(command_environment)


def test_preflight_requires_the_exact_compose_environment_file(tmp_path: Path) -> None:
    config = _config(tmp_path, apply=False)
    alternate = config.repo_root / ".env.prod"
    alternate.write_bytes(config.shared_env.read_bytes())
    alternate.chmod(0o600)
    config = replace(config, shared_env=alternate)
    commands = FakeCommands(config.repo_root)

    with pytest.raises(RotationError, match="canonical repository validation failed"):
        run_rotation(config, driver_factory=lambda *_a, **_k: None, command_runner=commands)

    assert commands.calls == []


@pytest.mark.parametrize("target", ["repo", "scripts", "compose"])
def test_preflight_rejects_writable_repository_command_assets(tmp_path: Path, target: str) -> None:
    config = _config(tmp_path, apply=False)
    trusted_path = {
        "repo": config.repo_root,
        "scripts": config.repo_root / "scripts",
        "compose": config.repo_root / "docker-compose.yml",
    }[target]
    trusted_path.chmod(stat.S_IMODE(trusted_path.stat().st_mode) | 0o022)
    commands = FakeCommands(config.repo_root)

    with pytest.raises(RotationError, match="canonical repository validation failed"):
        run_rotation(config, driver_factory=lambda *_a, **_k: None, command_runner=commands)

    assert commands.calls == []


def test_preflight_requires_repository_command_assets_owned_by_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, apply=False)
    commands = FakeCommands(config.repo_root)
    operator_uid = os.getuid()
    monkeypatch.setattr(rotation.os, "getuid", lambda: operator_uid + 1)

    with pytest.raises(RotationError, match="canonical repository validation failed"):
        rotation._validated_repo_root(config)

    assert commands.calls == []


def test_command_assets_are_revalidated_immediately_before_each_helper_launch(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, apply=False)

    class DriftingCommands(FakeCommands):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            result = super().__call__(args, **kwargs)
            if "compose-config" in args:
                scripts = self.repo_root / "scripts"
                scripts.chmod(stat.S_IMODE(scripts.stat().st_mode) | 0o022)
            return result

    commands = DriftingCommands(config.repo_root)

    with pytest.raises(RotationError, match="canonical repository validation failed"):
        run_rotation(config, driver_factory=lambda *_a, **_k: None, command_runner=commands)

    assert sum("compose-config" in call for call in commands.calls) == 1
    assert not any("inspect" in call for call in commands.calls)


def test_preflight_requires_the_exact_operator_config_directory(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path, apply=False),
        config_dir=tmp_path / "wrong" / "brain-v42",
    )
    commands = FakeCommands(config.repo_root)

    with pytest.raises(RotationError, match="private credential directory validation failed"):
        run_rotation(config, driver_factory=lambda *_a, **_k: None, command_runner=commands)

    assert commands.calls == []
    assert not config.config_dir.exists()


def test_existing_operator_config_directory_must_already_be_0700(tmp_path: Path) -> None:
    config = _config(tmp_path, apply=False)
    config.config_dir.mkdir(mode=0o755)
    config.config_dir.chmod(0o755)
    commands = FakeCommands(config.repo_root)

    with pytest.raises(RotationError, match="private credential directory validation failed"):
        run_rotation(config, driver_factory=lambda *_a, **_k: None, command_runner=commands)

    assert stat.S_IMODE(config.config_dir.stat().st_mode) == 0o755
    assert commands.calls == []


@pytest.mark.parametrize(
    "missing_confirmation",
    [
        "writers_off_confirmed",
        "neo4j_sessions_zero_confirmed",
        "neo4j_dedicated_confirmed",
        "postgres_restore_tested",
    ],
)
def test_apply_requires_all_four_operator_attestations(
    tmp_path: Path, missing_confirmation: str
) -> None:
    config = _config(tmp_path, apply=True)
    config = replace(config, **{missing_confirmation: False})
    commands = FakeCommands(config.repo_root)

    with pytest.raises(RotationError, match="operator confirmations required"):
        run_rotation(config, driver_factory=lambda *_a, **_k: None, command_runner=commands)

    assert not config.config_dir.exists()
    assert commands.calls == []


def test_apply_rotates_installs_recreates_and_emits_secret_free_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path, apply=True)
    commands = FakeCommands(config.repo_root)
    world = CredentialWorld()
    monkeypatch.setattr(rotation.secrets, "token_urlsafe", lambda _size: NEW_SECRET)

    result = run_rotation(config, driver_factory=world.factory, command_runner=commands)

    assert result == {
        "rotation_preconditions_verified": True,
        "container_metadata_clean": True,
        "container_recreated": True,
        "credential_files_installed": True,
        "ledger_enabled": True,
        "legacy_keys_removed": True,
        "new_credential_valid": True,
        "neo4j_target_valid": True,
        "old_credential_refused": True,
        "status": "rotated",
    }
    assert OLD_SECRET not in json.dumps(result)
    assert NEW_SECRET not in json.dumps(result)
    assert capsys.readouterr() == ("", "")
    assert world.valid_password == NEW_SECRET

    auth_file = config.config_dir / "neo4j-auth"
    projector_file = config.config_dir / "graph-projector.env"
    assert stat.S_IMODE(config.config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o644
    assert stat.S_IMODE(projector_file.stat().st_mode) == 0o600
    assert auth_file.read_text() == f"neo4j/{NEW_SECRET}\n"
    assert f"GRAPH_PROJECTOR_NEO4J_PASSWORD={NEW_SECRET}" in projector_file.read_text()
    shared = config.shared_env.read_text()
    assert "NEO4J_URL=" not in shared
    assert "\nNEO4J_USER=" not in shared
    assert "\nNEO4J_PASSWORD=" not in shared
    assert shared.count("GRAPH_LEDGER_WRITE_ENABLED=true") == 1
    assert not (config.config_dir / ".neo4j-rotation-state").exists()


def test_failed_compose_recreate_keeps_0600_journal_and_resume_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_config = _config(tmp_path, apply=True)
    original_shared_environment = first_config.shared_env.read_bytes()
    commands = FakeCommands(first_config.repo_root, fail_up_once=True)
    world = CredentialWorld()
    monkeypatch.setattr(rotation.secrets, "token_urlsafe", lambda _size: NEW_SECRET)

    with pytest.raises(RotationError) as exc_info:
        run_rotation(first_config, driver_factory=world.factory, command_runner=commands)
    assert "docker-secret-canary" not in str(exc_info.value)
    assert exc_info.value.__context__ is None

    journal = first_config.config_dir / ".neo4j-rotation-state"
    assert journal.exists()
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert world.valid_password == NEW_SECRET
    assert first_config.shared_env.read_bytes() == original_shared_environment

    resume_config = replace(first_config, resume=True)
    result = run_rotation(
        resume_config,
        driver_factory=world.factory,
        command_runner=commands,
    )

    assert result["status"] == "rotated"
    assert not journal.exists()


def test_resume_refuses_shared_environment_drift_without_overwriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_config = _config(tmp_path, apply=True)
    commands = FakeCommands(first_config.repo_root, fail_up_once=True)
    world = CredentialWorld()
    monkeypatch.setattr(rotation.secrets, "token_urlsafe", lambda _size: NEW_SECRET)

    with pytest.raises(RotationError):
        run_rotation(first_config, driver_factory=world.factory, command_runner=commands)

    drifted = first_config.shared_env.read_text() + "OPERATOR_CORRECTION=preserve-me\n"
    first_config.shared_env.write_text(drifted)
    first_config.shared_env.chmod(0o600)
    resume_config = replace(first_config, resume=True)

    with pytest.raises(RotationError, match="shared environment changed since rotation began"):
        run_rotation(
            resume_config,
            driver_factory=world.factory,
            command_runner=commands,
        )

    assert first_config.shared_env.read_text() == drifted
    assert (first_config.config_dir / ".neo4j-rotation-state").exists()
    assert sum("compose-up" in call for call in commands.calls) == 1


def test_parse_args_has_no_secret_arguments_and_defaults_to_preflight(tmp_path: Path) -> None:
    args = parse_args(
        [
            "--repo-root",
            str(tmp_path),
            "--shared-env",
            str(tmp_path / ".env"),
            "--config-dir",
            str(tmp_path / "private"),
            "--neo4j-uri",
            "bolt://127.0.0.1:7687",
        ]
    )

    assert args.apply is False
    assert args.resume is False
    assert args.neo4j_dedicated_confirmed is False
    assert args.postgres_restore_tested is False
    assert not any("password" in action.dest for action in rotation._parser()._actions)


def test_main_masks_unexpected_errors_as_fixed_secret_free_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path, apply=False)

    def fail_unexpectedly(_config: RotationConfig) -> dict[str, str]:
        raise RuntimeError(f"unexpected {NEW_SECRET}")

    monkeypatch.setattr(rotation, "run_rotation", fail_unexpectedly)

    exit_code = rotation.main(
        [
            "--repo-root",
            str(config.repo_root),
            "--shared-env",
            str(config.shared_env),
            "--config-dir",
            str(config.config_dir),
            "--neo4j-uri",
            config.neo4j_uri,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.out) == {
        "error": "credential rotation failed",
        "status": "error",
    }
    assert OLD_SECRET not in captured.out
    assert NEW_SECRET not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://127.0.0.1:7687",
        "bolt://localhost:7687",
        "bolt://[::1]:7687",
        "neo4j://127.0.0.1:7687",
        "neo4j://localhost:7687",
        "neo4j://[::1]:7687",
    ],
)
def test_local_neo4j_uri_aliases_normalize_to_one_endpoint(uri: str) -> None:
    assert rotation._canonical_neo4j_uri(uri) == "bolt://127.0.0.1:7687"


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://neo4j.internal:7687",
        "bolt://127.0.0.1:7688",
        "bolt://neo4j:secret@127.0.0.1:7687",
        "bolt://127.0.0.1:7687/neo4j",
        "bolt://127.0.0.1:7687/?x=1",
        "bolt://127.0.0.1:7687/#fragment",
        "http://127.0.0.1:7687",
        "bolt://127.0.0.1",
    ],
)
def test_neo4j_uri_rejects_any_noncanonical_target(uri: str) -> None:
    with pytest.raises(RotationError, match="invalid Neo4j URI"):
        rotation._canonical_neo4j_uri(uri)


def test_preflight_refuses_cli_target_different_from_legacy_without_commands(
    tmp_path: Path,
) -> None:
    config = replace(
        _config(tmp_path, apply=False),
        neo4j_uri="bolt://127.0.0.1:7688",
    )
    commands = FakeCommands(config.repo_root)

    with pytest.raises(RotationError, match="invalid Neo4j URI"):
        run_rotation(config, driver_factory=lambda *_a, **_k: None, command_runner=commands)

    assert commands.calls == []
