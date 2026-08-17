#!/usr/bin/env python3
"""Rotate the Neo4j projector credential through a resumable, fail-closed cutover."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from neo4j import GraphDatabase
from neo4j.exceptions import AuthError

_LEGACY_KEYS = frozenset({"NEO4J_URL", "NEO4J_USER", "NEO4J_PASSWORD"})
_LEDGER_KEY = "GRAPH_LEDGER_WRITE_ENABLED"
_JOURNAL_NAME = ".neo4j-rotation-state"
_LOCK_NAME = ".neo4j-rotation.lock"
_ROTATION_DOCKER_HELPER = "scripts/rotate_neo4j_container.sh"
_MAX_ENV_BYTES = 64 * 1024
_SAFE_COMPOSE_ENV_KEYS = (
    "DOCKER_CERT_PATH",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "HOME",
    "PATH",
    "XDG_RUNTIME_DIR",
)
_EXPECTED_HEALTHCHECK = (
    "CMD",
    "wget",
    "-q",
    "--spider",
    "http://127.0.0.1:7474/",
)


class RotationError(RuntimeError):
    """A secret-free failure suitable for operator-facing JSON output."""


class Driver(Protocol):
    """Small Neo4j driver surface used by the rotation workflow."""

    def verify_connectivity(self) -> None: ...

    def execute_query(
        self,
        query: str,
        *,
        parameters_: dict[str, str],
        database_: str,
    ) -> Any: ...

    def close(self) -> None: ...


DriverFactory = Callable[..., Driver]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _default_driver_factory(uri: str, *, auth: tuple[str, str]) -> Driver:
    return cast(Driver, GraphDatabase.driver(uri, auth=auth))


@dataclass(frozen=True, slots=True)
class RotationConfig:
    """Non-secret operator inputs for one rotation attempt."""

    repo_root: Path
    shared_env: Path
    config_dir: Path
    neo4j_uri: str
    apply: bool
    writers_off_confirmed: bool
    neo4j_sessions_zero_confirmed: bool
    neo4j_dedicated_confirmed: bool
    postgres_restore_tested: bool
    resume: bool


@dataclass(frozen=True, slots=True)
class RotationState:
    """Sensitive state persisted only in the private 0600 journal."""

    old_password: str = field(repr=False)
    new_password: str = field(repr=False)
    original_shared_environment: str = field(repr=False)
    original_shared_mode: int


def _safe_failure(condition: bool, message: str) -> None:
    """Raise outside exception handlers so internal failures are never chained."""
    if condition:
        raise RotationError(message)


def _fsync_directory(path: Path) -> None:
    failed = False
    directory_fd = -1
    try:
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(directory_fd)
    except OSError:
        failed = True
    finally:
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                failed = True
    _safe_failure(failed, "durable directory update failed")


def _atomic_write(target: Path, payload: bytes, mode: int) -> None:
    """Atomically install complete bytes, fsyncing both file and directory."""
    target_parent = target.parent
    failure = False
    temp_fd = -1
    temp_path: Path | None = None

    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        target_stat = None
    except OSError:
        failure = True

    if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
        failure = True

    if not failure:
        try:
            temp_fd, raw_temp_path = tempfile.mkstemp(
                prefix=f".{target.name}.",
                dir=target_parent,
            )
            temp_path = Path(raw_temp_path)
            os.fchmod(temp_fd, mode)
            with os.fdopen(temp_fd, "wb", closefd=True) as stream:
                temp_fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, target)
            temp_path = None
            _fsync_directory(target_parent)
        except (OSError, RotationError):
            failure = True
        finally:
            if temp_fd >= 0:
                try:
                    os.close(temp_fd)
                except OSError:
                    failure = True
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    failure = True

    _safe_failure(failure, "atomic credential update failed")


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Hold a non-blocking process lock for the entire mutating workflow."""
    lock_fd = -1
    failure = False
    try:
        lock_fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        failure = True

    if failure and lock_fd >= 0:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        lock_fd = -1
    _safe_failure(failure, "another credential rotation is already active")
    try:
        yield
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def _assignment_key(raw_line: str) -> str | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line.removeprefix("export ").lstrip()
    key, separator, _value = line.partition("=")
    if not separator:
        return None
    return key.strip().upper()


def _assignment_value(raw_line: str) -> str:
    line = raw_line.strip()
    if line.startswith("export "):
        line = line.removeprefix("export ").lstrip()
    return line.partition("=")[2].strip().strip("'\"")


def _canonical_neo4j_uri(uri: str) -> str:
    """Map the accepted loopback aliases to the one direct local Bolt endpoint."""
    failed = uri != uri.strip()
    parsed = None
    hostname: str | None = None
    port: int | None = None
    try:
        parsed = urlsplit(uri)
        hostname = parsed.hostname
        port = parsed.port
        failed = failed or parsed.scheme.casefold() not in {"bolt", "neo4j"}
        failed = failed or hostname not in {"127.0.0.1", "localhost", "::1"}
        failed = failed or port != 7687
        failed = failed or parsed.username is not None or parsed.password is not None
        failed = failed or bool(parsed.path or parsed.query or parsed.fragment)
    except (UnicodeError, ValueError):
        failed = True

    _safe_failure(failed or parsed is None, "invalid Neo4j URI")
    return "bolt://127.0.0.1:7687"


def _render_shared_environment(original: str) -> str:
    """Remove only legacy Neo4j keys and make the ledger flag exactly true once."""
    kept: list[str] = []
    ledger_position: int | None = None
    for line in original.splitlines():
        key = _assignment_key(line)
        if key in _LEGACY_KEYS:
            continue
        if key == _LEDGER_KEY:
            if ledger_position is None:
                ledger_position = len(kept)
            continue
        kept.append(line)

    insertion = len(kept) if ledger_position is None else ledger_position
    kept.insert(insertion, f"{_LEDGER_KEY}=true")
    return "\n".join(kept) + "\n"


def _probe_credential(
    driver_factory: DriverFactory,
    uri: str,
    user: str,
    password: str,
) -> bool:
    """Return false only for an explicit Neo4j authentication refusal."""
    driver: Driver | None = None
    auth_refused = False
    failed = False
    try:
        driver = driver_factory(uri, auth=(user, password))
        driver.verify_connectivity()
    except AuthError:
        auth_refused = True
    except Exception:
        failed = True
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                failed = True

    _safe_failure(failed, "Neo4j credential verification failed")
    return not auth_refused


def _rotate_password(
    driver_factory: DriverFactory,
    uri: str,
    user: str,
    old_password: str,
    new_password: str,
) -> None:
    """Change the current user's password with parameters on the system database."""
    driver: Driver | None = None
    failed = False
    try:
        driver = driver_factory(uri, auth=(user, old_password))
        driver.execute_query(
            "ALTER CURRENT USER SET PASSWORD FROM $old_password TO $new_password",
            parameters_={
                "old_password": old_password,
                "new_password": new_password,
            },
            database_="system",
        )
    except Exception:
        failed = True
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                failed = True

    _safe_failure(failed, "Neo4j credential rotation failed")


def _compose_base_command(_config: RotationConfig) -> list[str]:
    return [_ROTATION_DOCKER_HELPER]


def _compose_up_command(config: RotationConfig) -> list[str]:
    """Recreate only Neo4j from the explicit canonical Compose project."""
    return [*_compose_base_command(config), "compose-up"]


def _compose_environment(config: RotationConfig) -> dict[str, str]:
    environment = {
        key: value for key in _SAFE_COMPOSE_ENV_KEYS if (value := os.environ.get(key)) is not None
    }
    environment["BRAIN_NEO4J_AUTH_FILE"] = str(config.config_dir / "neo4j-auth")
    return environment


def _inspect_command(config_dir: Path) -> list[str]:
    healthcheck_conditions = " ".join(
        f"(eq (index .Config.Healthcheck.Test {index}) {json.dumps(argument)})"
        for index, argument in enumerate(_EXPECTED_HEALTHCHECK)
    )
    auth_source = json.dumps(str(config_dir / "neo4j-auth"))
    template = (
        '{{- $authFile := "false" -}}'
        '{{- $authSource := "false" -}}'
        '{{- $legacyAuth := "false" -}}'
        "{{- range .Config.Env -}}"
        '{{- $key := index (split . "=") 0 -}}'
        '{{- if eq . "NEO4J_AUTH_FILE=/run/secrets/neo4j_auth" -}}'
        '{{- $authFile = "true" -}}{{- end -}}'
        '{{- if eq $key "NEO4J_AUTH" -}}{{- $legacyAuth = "true" -}}{{- end -}}'
        "{{- end -}}"
        "{{- range .Mounts -}}"
        '{{- if and (eq .Destination "/run/secrets/neo4j_auth") '
        f"(eq .Source {auth_source}) -}}}}"
        '{{- $authSource = "true" -}}{{- end -}}'
        "{{- end -}}"
        'project={{index .Config.Labels "com.docker.compose.project.working_dir"}}\n'
        "auth_file={{$authFile}}\n"
        "auth_source={{$authSource}}\n"
        "legacy_auth={{$legacyAuth}}\n"
        "healthcheck_safe={{if and "
        f"(eq (len .Config.Healthcheck.Test) {len(_EXPECTED_HEALTHCHECK)}) "
        f"{healthcheck_conditions}"
        "}}true{{else}}false{{end}}"
    )
    return [_ROTATION_DOCKER_HELPER, "inspect", template]


def _validate_container_metadata(
    output: str,
    repo_root: Path,
    *,
    require_clean: bool,
) -> bool:
    """Validate only the fixed, secret-free metadata emitted by the template."""
    metadata: dict[str, str] = {}
    malformed = False
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in metadata:
            malformed = True
            continue
        metadata[key] = value

    expected_keys = {
        "project",
        "auth_file",
        "auth_source",
        "legacy_auth",
        "healthcheck_safe",
    }
    failed = malformed or set(metadata) != expected_keys
    failed = failed or metadata.get("project") != str(repo_root)
    if require_clean:
        failed = failed or metadata.get("auth_file") != "true"
        failed = failed or metadata.get("auth_source") != "true"
        failed = failed or metadata.get("legacy_auth") != "false"
        failed = failed or metadata.get("healthcheck_safe") != "true"
    _safe_failure(failed, "Neo4j container metadata is not canonical")
    return True


def _run_command(
    command_runner: CommandRunner,
    args: list[str],
    failure_message: str,
    *,
    environment: dict[str, str],
    working_directory: Path,
) -> subprocess.CompletedProcess[str]:
    _safe_failure(
        not _trusted_repository_command_assets(working_directory),
        "canonical repository validation failed",
    )
    failed = False
    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = command_runner(
            args,
            capture_output=True,
            check=False,
            cwd=working_directory,
            env=environment,
            text=True,
        )
        failed = result.returncode != 0
    except Exception:
        failed = True
    _safe_failure(failed or result is None, failure_message)
    assert result is not None
    return result


def _trusted_repository_command_assets(repo_root: Path) -> bool:
    trusted_paths = (
        (repo_root, stat.S_ISDIR, False),
        (repo_root / "scripts", stat.S_ISDIR, False),
        (repo_root / "docker-compose.yml", stat.S_ISREG, False),
        (repo_root / _ROTATION_DOCKER_HELPER, stat.S_ISREG, True),
    )
    try:
        for path, expected_type, executable in trusted_paths:
            path_stat = path.lstat()
            mode = stat.S_IMODE(path_stat.st_mode)
            if (
                not expected_type(path_stat.st_mode)
                or path_stat.st_uid != os.getuid()
                or bool(mode & 0o022)
                or (executable and not bool(mode & stat.S_IXUSR))
                or path.resolve(strict=True) != path
            ):
                return False
    except (OSError, RuntimeError):
        return False
    return True


def _validated_repo_root(config: RotationConfig) -> Path:
    failed = False
    resolved: Path | None = None
    try:
        resolved = config.repo_root.resolve(strict=True)
        failed = resolved != config.repo_root
        failed = failed or not _trusted_repository_command_assets(resolved)
        expected_shared_env = resolved / ".env"
        failed = failed or config.shared_env != expected_shared_env
        failed = failed or config.shared_env.resolve(strict=True) != expected_shared_env
    except (OSError, RuntimeError):
        failed = True
    _safe_failure(failed or resolved is None, "canonical repository validation failed")
    assert resolved is not None
    return resolved


def _operator_config_dir() -> Path:
    return Path.home() / ".config" / "brain-v42"


def _validate_private_directory_path(path: Path, *, allow_missing: bool) -> None:
    failed = not path.is_absolute() or path != _operator_config_dir()
    try:
        failed = failed or path.resolve(strict=False) != path
        parent_stat = path.parent.lstat()
        failed = failed or not stat.S_ISDIR(parent_stat.st_mode)
        failed = failed or parent_stat.st_uid != os.getuid()
        failed = failed or bool(stat.S_IMODE(parent_stat.st_mode) & 0o022)
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            failed = failed or not allow_missing
        else:
            failed = failed or not stat.S_ISDIR(path_stat.st_mode)
            failed = failed or path_stat.st_uid != os.getuid()
            failed = failed or stat.S_IMODE(path_stat.st_mode) != 0o700
    except OSError:
        failed = True
    _safe_failure(failed, "private credential directory validation failed")


def _preflight(
    config: RotationConfig, command_runner: CommandRunner
) -> dict[str, bool | str | int]:
    repo_root = _validated_repo_root(config)
    _run_command(
        command_runner,
        [*_compose_base_command(config), "compose-config"],
        "Compose configuration validation failed",
        environment=_compose_environment(config),
        working_directory=repo_root,
    )
    inspected = _run_command(
        command_runner,
        _inspect_command(config.config_dir),
        "Neo4j container inspection failed",
        environment=_compose_environment(config),
        working_directory=repo_root,
    )
    _validate_container_metadata(inspected.stdout, repo_root, require_clean=False)
    return {
        "rotation_confirmations_required": 4,
        "apply": False,
        "canonical_repo_valid": True,
        "compose_valid": True,
        "neo4j_target_valid": True,
        "status": "preflight_ok",
    }


def _ensure_private_directory(path: Path) -> None:
    _validate_private_directory_path(path, allow_missing=True)
    failed = False
    created = False
    try:
        try:
            path.lstat()
        except FileNotFoundError:
            path.mkdir(mode=0o700)
            created = True
        path_stat = path.lstat()
        failed = not stat.S_ISDIR(path_stat.st_mode)
        failed = failed or path_stat.st_uid != os.getuid()
        failed = failed or stat.S_IMODE(path_stat.st_mode) != 0o700
        if not failed and created:
            _fsync_directory(path.parent)
    except (OSError, RotationError):
        failed = True
    _safe_failure(failed, "private credential directory validation failed")


def _read_shared_environment(path: Path) -> tuple[str, int]:
    failed = False
    content = ""
    mode = 0
    try:
        path_stat = path.lstat()
        failed = not stat.S_ISREG(path_stat.st_mode)
        failed = failed or path_stat.st_size > _MAX_ENV_BYTES
        failed = failed or path_stat.st_uid != os.getuid()
        failed = failed or stat.S_IMODE(path_stat.st_mode) != 0o600
        if not failed:
            content = path.read_text(encoding="utf-8")
            mode = stat.S_IMODE(path_stat.st_mode)
    except (OSError, UnicodeError):
        failed = True
    _safe_failure(failed, "shared environment is unreadable")
    return content, mode


def _legacy_credentials(content: str, expected_uri: str) -> tuple[str, str]:
    values: dict[str, str] = {}
    graph_enabled_values: list[str] = []
    duplicate = False
    projector_key_present = False
    for line in content.splitlines():
        key = _assignment_key(line)
        if key == "GRAPH_ENABLED":
            graph_enabled_values.append(_assignment_value(line))
        if key is not None and key.startswith("GRAPH_PROJECTOR_"):
            projector_key_present = True
        if key not in _LEGACY_KEYS:
            continue
        if key in values:
            duplicate = True
            continue
        values[key] = _assignment_value(line)

    unsafe_shared = len(graph_enabled_values) != 1
    unsafe_shared = unsafe_shared or graph_enabled_values != ["true"]
    unsafe_shared = unsafe_shared or projector_key_present
    _safe_failure(unsafe_shared, "shared environment is unsafe")
    failed = duplicate or set(values) != _LEGACY_KEYS
    failed = failed or values.get("NEO4J_USER") != "neo4j"
    failed = failed or not values.get("NEO4J_PASSWORD")
    _safe_failure(failed, "legacy Neo4j credentials are incomplete")
    legacy_uri = _canonical_neo4j_uri(values["NEO4J_URL"])
    _safe_failure(legacy_uri != expected_uri, "Neo4j target differs from legacy configuration")
    return values["NEO4J_USER"], values["NEO4J_PASSWORD"]


def _state_payload(state: RotationState) -> bytes:
    return (
        json.dumps(
            {
                "new_password": state.new_password,
                "old_password": state.old_password,
                "original_shared_environment": state.original_shared_environment,
                "original_shared_mode": state.original_shared_mode,
                "version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _load_state(path: Path) -> RotationState:
    failed = False
    state: RotationState | None = None
    try:
        path_stat = path.lstat()
        failed = not stat.S_ISREG(path_stat.st_mode)
        failed = failed or stat.S_IMODE(path_stat.st_mode) != 0o600
        failed = failed or path_stat.st_uid != os.getuid()
        if not failed:
            raw = json.loads(path.read_text(encoding="utf-8"))
            failed = set(raw) != {
                "new_password",
                "old_password",
                "original_shared_environment",
                "original_shared_mode",
                "version",
            }
            failed = failed or raw.get("version") != 1
            failed = failed or not isinstance(raw.get("old_password"), str)
            failed = failed or not isinstance(raw.get("new_password"), str)
            failed = failed or not isinstance(raw.get("original_shared_environment"), str)
            failed = failed or not isinstance(raw.get("original_shared_mode"), int)
            if not failed:
                state = RotationState(
                    old_password=raw["old_password"],
                    new_password=raw["new_password"],
                    original_shared_environment=raw["original_shared_environment"],
                    original_shared_mode=raw["original_shared_mode"],
                )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        failed = True
    _safe_failure(failed or state is None, "rotation journal is invalid")
    assert state is not None
    return state


def _prepare_state(config: RotationConfig, journal: Path) -> RotationState:
    if config.resume:
        _safe_failure(not journal.exists(), "rotation journal is required for resume")
        state = _load_state(journal)
        current_shared, current_mode = _read_shared_environment(config.shared_env)
        expected_shared_states = {
            state.original_shared_environment,
            _render_shared_environment(state.original_shared_environment),
        }
        shared_changed = current_shared not in expected_shared_states
        shared_changed = shared_changed or current_mode != state.original_shared_mode
        _safe_failure(shared_changed, "shared environment changed since rotation began")
        _legacy_credentials(state.original_shared_environment, config.neo4j_uri)
        return state

    _safe_failure(journal.exists(), "unfinished rotation requires --resume")
    shared_content, shared_mode = _read_shared_environment(config.shared_env)
    _user, old_password = _legacy_credentials(shared_content, config.neo4j_uri)
    state = RotationState(
        old_password=old_password,
        new_password=secrets.token_urlsafe(48),
        original_shared_environment=shared_content,
        original_shared_mode=shared_mode,
    )
    _atomic_write(journal, _state_payload(state), 0o600)
    return state


def _install_credentials(config: RotationConfig, state: RotationState) -> None:
    auth_payload = f"neo4j/{state.new_password}\n".encode()
    projector_payload = (
        "GRAPH_PROJECTOR_ENABLED=true\n"
        f"GRAPH_PROJECTOR_NEO4J_URL={config.neo4j_uri}\n"
        "GRAPH_PROJECTOR_NEO4J_USER=neo4j\n"
        f"GRAPH_PROJECTOR_NEO4J_PASSWORD={state.new_password}\n"
    ).encode()
    _atomic_write(config.config_dir / "neo4j-auth", auth_payload, 0o644)
    _atomic_write(config.config_dir / "graph-projector.env", projector_payload, 0o600)


def _prove_and_rotate(
    config: RotationConfig,
    state: RotationState,
    driver_factory: DriverFactory,
) -> None:
    new_valid = _probe_credential(
        driver_factory,
        config.neo4j_uri,
        "neo4j",
        state.new_password,
    )
    if not new_valid:
        old_valid = _probe_credential(
            driver_factory,
            config.neo4j_uri,
            "neo4j",
            state.old_password,
        )
        _safe_failure(not old_valid, "neither journal credential is valid")
        _rotate_password(
            driver_factory,
            config.neo4j_uri,
            "neo4j",
            state.old_password,
            state.new_password,
        )

    old_still_valid = _probe_credential(
        driver_factory,
        config.neo4j_uri,
        "neo4j",
        state.old_password,
    )
    new_valid = _probe_credential(
        driver_factory,
        config.neo4j_uri,
        "neo4j",
        state.new_password,
    )
    _safe_failure(old_still_valid, "old Neo4j credential is still accepted")
    _safe_failure(not new_valid, "new Neo4j credential is not accepted")


def _remove_journal(journal: Path) -> None:
    failed = False
    try:
        journal.unlink()
        _fsync_directory(journal.parent)
    except (OSError, RotationError):
        failed = True
    _safe_failure(failed, "rotation journal cleanup failed")


def _apply_rotation(
    config: RotationConfig,
    driver_factory: DriverFactory,
    command_runner: CommandRunner,
) -> dict[str, bool | str | int]:
    _ensure_private_directory(config.config_dir)
    journal = config.config_dir / _JOURNAL_NAME
    with _exclusive_lock(config.config_dir / _LOCK_NAME):
        state = _prepare_state(config, journal)
        _prove_and_rotate(config, state, driver_factory)
        _install_credentials(config, state)
        rewritten = _render_shared_environment(state.original_shared_environment)
        _atomic_write(config.shared_env, rewritten.encode(), state.original_shared_mode)

        failed = False
        try:
            _run_command(
                command_runner,
                _compose_up_command(config),
                "Neo4j container recreation failed",
                environment=_compose_environment(config),
                working_directory=config.repo_root,
            )
            inspected = _run_command(
                command_runner,
                _inspect_command(config.config_dir),
                "Neo4j container inspection failed",
                environment=_compose_environment(config),
                working_directory=config.repo_root,
            )
            _validate_container_metadata(
                inspected.stdout,
                config.repo_root,
                require_clean=True,
            )
            _prove_and_rotate(config, state, driver_factory)
        except RotationError:
            failed = True

        if failed:
            _atomic_write(
                config.shared_env,
                state.original_shared_environment.encode(),
                state.original_shared_mode,
            )
            raise RotationError("Neo4j cutover failed; shared environment restored")

        _remove_journal(journal)

    return {
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


def run_rotation(
    config: RotationConfig,
    *,
    driver_factory: DriverFactory = _default_driver_factory,
    command_runner: CommandRunner = subprocess.run,
) -> dict[str, bool | str | int]:
    """Run read-only preflight by default, or a confirmed resumable cutover."""
    canonical_uri = _canonical_neo4j_uri(config.neo4j_uri)
    config = replace(config, neo4j_uri=canonical_uri)
    _validate_private_directory_path(config.config_dir, allow_missing=True)
    _validated_repo_root(config)
    if not config.resume:
        shared_content, _shared_mode = _read_shared_environment(config.shared_env)
        _legacy_credentials(shared_content, config.neo4j_uri)
    if not config.apply:
        _safe_failure(config.resume, "--resume requires --apply")
        return _preflight(config, command_runner)

    confirmed = all(
        (
            config.writers_off_confirmed,
            config.neo4j_sessions_zero_confirmed,
            config.neo4j_dedicated_confirmed,
            config.postgres_restore_tested,
        )
    )
    _safe_failure(not confirmed, "operator confirmations required")
    _preflight(config, command_runner)
    return _apply_rotation(config, driver_factory, command_runner)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--shared-env", required=True, type=Path)
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--neo4j-uri", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-off-confirmed", action="store_true")
    parser.add_argument("--neo4j-sessions-zero-confirmed", action="store_true")
    parser.add_argument("--neo4j-dedicated-confirmed", action="store_true")
    parser.add_argument("--postgres-restore-tested", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return _parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = RotationConfig(
        repo_root=args.repo_root,
        shared_env=args.shared_env,
        config_dir=args.config_dir,
        neo4j_uri=args.neo4j_uri,
        apply=args.apply,
        writers_off_confirmed=args.writers_off_confirmed,
        neo4j_sessions_zero_confirmed=args.neo4j_sessions_zero_confirmed,
        neo4j_dedicated_confirmed=args.neo4j_dedicated_confirmed,
        postgres_restore_tested=args.postgres_restore_tested,
        resume=args.resume,
    )
    try:
        result = run_rotation(config)
    except RotationError as exc:
        print(json.dumps({"error": str(exc), "status": "error"}, sort_keys=True))
        return 2
    except Exception:
        print(
            json.dumps(
                {"error": "credential rotation failed", "status": "error"},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
