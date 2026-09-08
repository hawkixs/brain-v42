#!/usr/bin/env python3
"""Fail-closed preflight for one explicitly configured delivery release."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import ssl
import stat
import subprocess
import tarfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.auth import (
    GitHubAuthProvider,
    read_private_file,
)
from brain_v42.delivery_observer.config import load_observer_settings
from brain_v42.delivery_observer.transport import GitHubTransport, ProviderError

_GUARD_SHA = "fcc9328ff6e7f061879af2540c69717fc3061434"
_SHA256 = set("0123456789abcdef")
_SYSTEMD_TIMEOUT_SECONDS = 5
_DB_CONNECT_TIMEOUT_SECONDS = 5
_DB_QUERY_TIMEOUT_MILLISECONDS = 5000
_DB_OVERALL_TIMEOUT_SECONDS = 10
_SYSTEMD_PROPERTIES = (
    "ExecStart",
    "EnvironmentFiles",
    "Environment",
    "UnsetEnvironment",
    "ActiveState",
    "MainPID",
    "WorkingDirectory",
)
_FAILURES = {
    "private_config_invalid",
    "config_schema_invalid",
    "private_environment_invalid",
    "release_path_unsafe",
    "release_artifact_mismatch",
    "installed_package_unexpected",
    "guarded_revision_unverified",
    "schema_capability_unavailable",
    "required_unit_inventory_invalid",
    "writer_launch_unsupported",
    "service_health_unavailable",
}


class PreflightFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("invalid preflight arguments")


@dataclass(frozen=True)
class ReleaseFile:
    path: Path
    sha256: str


@dataclass(frozen=True)
class ProcessIdentity:
    """The small, non-secret portion of a process identity needed for attestation."""

    executable: str
    argv: tuple[str, ...]
    cwd: str
    environment: dict[str, str]


@dataclass(frozen=True)
class PreflightDependencies:
    """Internal seams for controlled boundary tests; never exposed by the CLI."""

    schema_revision: Callable[[DeliverySettings], str] | None = None
    health: Callable[[str], dict[str, Any]] | None = None
    systemd_properties: Callable[[str], dict[str, str]] | None = None
    process_identity: Callable[[int], ProcessIdentity] | None = None
    manager_environment: Callable[[], dict[str, str]] | None = None


def _fail(code: str) -> None:
    raise PreflightFailure(code)


def _object(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(code)
    return value


def _text(value: object, code: str, *, limit: int = 4096) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        _fail(code)
    return value


def _sha(value: object, code: str) -> str:
    result = _text(value, code, limit=64)
    if len(result) != 64 or set(result) - _SHA256:
        _fail(code)
    return result


def _safe_path_chain(path: Path, code: str) -> None:
    """Reject links and replaceable components without rejecting sticky /tmp."""
    current = Path(path.anchor)
    for part in path.absolute().parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError:
            _fail(code)
        if current != path:
            unsafe_write = stat.S_IMODE(metadata.st_mode) & 0o022
            sticky_directory = stat.S_ISDIR(metadata.st_mode) and metadata.st_mode & stat.S_ISVTX
            if (
                current.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or (metadata.st_uid not in {0, os.getuid()} and not sticky_directory)
                or (unsafe_write and not sticky_directory)
            ):
                _fail(code)


def _release_path(root: Path, raw: object) -> Path:
    value = _text(raw, "release_path_unsafe")
    candidate = Path(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        _fail("release_path_unsafe")
    lexical = root / candidate
    try:
        root.absolute().relative_to(root.absolute())
        lexical.absolute().relative_to(root.absolute())
    except ValueError:
        _fail("release_path_unsafe")
    _safe_path_chain(lexical, "release_path_unsafe")
    return lexical.absolute()


def _safe_regular(path: Path, *, private: bool = False) -> None:
    code = "private_environment_invalid" if private else "release_path_unsafe"
    _safe_path_chain(path.parent, code)
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        _fail(code)
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        _fail(code)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_private_json(path: Path) -> dict[str, Any]:
    try:
        return _object(json.loads(read_private_file(path)), "private_config_invalid")
    except (ProviderError, UnicodeError, json.JSONDecodeError):
        _fail("private_config_invalid")


def _release_file(root: Path, raw: object) -> ReleaseFile:
    value = _object(raw, "config_schema_invalid")
    path = _release_path(root, value.get("path"))
    _safe_regular(path)
    digest = _sha(value.get("sha256"), "config_schema_invalid")
    if _digest(path) != digest:
        _fail("release_artifact_mismatch")
    return ReleaseFile(path, digest)


def _validate_writers(config: dict[str, Any]) -> None:
    writers = _object(config.get("writers"), "config_schema_invalid")
    required = {
        "brain-mcp-http.service": "brain_v42.mcp.server",
        "brain-metrics.service": "brain_v42.metrics",
        "brain-v42-delivery-observer.service": "brain_v42.delivery_observer",
    }
    for name, module in required.items():
        item = _object(writers.get(name), "required_unit_inventory_invalid")
        if item.get("kind") != "wheel_module" or item.get("module") != module:
            _fail("required_unit_inventory_invalid")
        active = item.get("must_be_active")
        if type(active) is not bool or (
            name != "brain-v42-delivery-observer.service" and not active
        ):
            _fail("required_unit_inventory_invalid")
    mode = config.get("mode")
    if mode not in {"dormant", "canary"}:
        _fail("config_schema_invalid")
    observer = _object(writers["brain-v42-delivery-observer.service"], "config_schema_invalid")
    if mode == "canary" and not observer.get("must_be_active"):
        _fail("required_unit_inventory_invalid")
    for value in writers.values():
        item = _object(value, "writer_launch_unsupported")
        kind = item.get("kind")
        if kind == "wheel_module":
            _text(item.get("module"), "writer_launch_unsupported", limit=256)
        elif kind == "source_script":
            _text(item.get("source_path"), "writer_launch_unsupported", limit=1024)
        else:
            _fail("writer_launch_unsupported")


def _read_systemd_properties(unit: str) -> dict[str, str]:
    """Read only the effective fields needed to bind one configured unit."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/systemctl",
                "--user",
                "show",
                unit,
                *(f"--property={name}" for name in _SYSTEMD_PROPERTIES),
            ],
            cwd="/",
            env={
                "PATH": os.defpath,
                "LANG": "C",
                "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
            },
            text=True,
            capture_output=True,
            timeout=_SYSTEMD_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _fail("writer_launch_unsupported")
    if result.returncode != 0 or len(result.stdout) > 16384:
        _fail("writer_launch_unsupported")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in _SYSTEMD_PROPERTIES and key not in values and len(value) <= 8192:
            values[key] = value
    values.setdefault("EnvironmentFiles", "")
    values.setdefault("UnsetEnvironment", "")
    if set(values) != set(_SYSTEMD_PROPERTIES):
        _fail("writer_launch_unsupported")
    return values


def _read_manager_environment() -> dict[str, str]:
    try:
        uid = os.getuid()
        result = subprocess.run(
            ["/usr/bin/systemctl", "--user", "show-environment"],
            cwd="/",
            env={
                "PATH": os.defpath,
                "LANG": "C",
                "XDG_RUNTIME_DIR": f"/run/user/{uid}",
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
            },
            text=True,
            capture_output=True,
            timeout=_SYSTEMD_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _fail("writer_launch_unsupported")
    if result.returncode != 0 or len(result.stdout) > 65536:
        _fail("writer_launch_unsupported")
    return _environment_assignments(result.stdout)


def _read_process_identity(pid: int) -> ProcessIdentity:
    if pid <= 1:
        _fail("writer_launch_unsupported")
    proc = Path("/proc") / str(pid)
    try:
        argv_bytes = (proc / "cmdline").read_bytes()
        environment_bytes = (proc / "environ").read_bytes()
        executable = os.readlink(proc / "exe")
        cwd = os.readlink(proc / "cwd")
    except OSError:
        _fail("writer_launch_unsupported")
    if len(argv_bytes) > 8192 or len(environment_bytes) > 131072:
        _fail("writer_launch_unsupported")
    try:
        argv = tuple(item.decode("utf-8") for item in argv_bytes.split(b"\0") if item)
        environment = {
            item.split("=", 1)[0]: item.split("=", 1)[1]
            for item in (item.decode("utf-8") for item in environment_bytes.split(b"\0") if item)
            if "=" in item
        }
    except UnicodeDecodeError:
        _fail("writer_launch_unsupported")
    return ProcessIdentity(executable=executable, argv=argv, cwd=cwd, environment=environment)


def _environment_assignments(value: str) -> dict[str, str]:
    if len(value) > 8192:
        _fail("writer_launch_unsupported")
    try:
        tokens = shlex.split(value)
    except ValueError:
        _fail("writer_launch_unsupported")
    assignments: dict[str, str] = {}
    for token in tokens:
        key, separator, assigned = token.partition("=")
        if separator and key in {
            "PATH",
            "UV_PROJECT_ENVIRONMENT",
            "UV_NO_SYNC",
            "PYTHONSAFEPATH",
            "PYTHONPATH",
            "PYTHONHOME",
        }:
            assignments[key] = assigned
    return assignments


def _unset_controlled_environment(value: str) -> None:
    try:
        names = shlex.split(value)
    except ValueError:
        _fail("writer_launch_unsupported")
    if {name.partition("=")[0] for name in names} & {
        "PATH",
        "UV_PROJECT_ENVIRONMENT",
        "UV_NO_SYNC",
        "PYTHONSAFEPATH",
        "PYTHONPATH",
        "PYTHONHOME",
    }:
        _fail("writer_launch_unsupported")


def _environment_file_assignments(value: str) -> dict[str, str]:
    """Read only import-control keys; contents are never emitted or retained."""
    if not value:
        return {}
    try:
        tokens = shlex.split(value)
    except ValueError:
        _fail("writer_launch_unsupported")
    assignments: dict[str, str] = {}
    for token in tokens:
        if token.startswith("(") and token.endswith(")"):
            continue
        path = Path(token)
        if token.startswith("-") or not path.is_absolute():
            _fail("writer_launch_unsupported")
        try:
            contents = read_private_file(path).decode("utf-8")
        except (ProviderError, UnicodeError):
            _fail("writer_launch_unsupported")
        for line in contents.splitlines():
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue
            key, separator, assigned = candidate.partition("=")
            key = key.strip()
            if separator and key in {
                "PATH",
                "UV_PROJECT_ENVIRONMENT",
                "UV_NO_SYNC",
                "PYTHONSAFEPATH",
                "PYTHONPATH",
                "PYTHONHOME",
            }:
                assigned = assigned.strip()
                if not assigned or any(character in assigned for character in "'\\\""):
                    _fail("writer_launch_unsupported")
                assignments[key] = assigned
    return assignments


def _environment_file_paths(value: str) -> tuple[Path, ...]:
    try:
        tokens = shlex.split(value)
    except ValueError:
        _fail("writer_launch_unsupported")
    paths = tuple(Path(token) for token in tokens if not token.startswith("("))
    if any(not path.is_absolute() or str(path).startswith("-") for path in paths):
        _fail("writer_launch_unsupported")
    return paths


def _expected_environment(root: Path, kind: str) -> dict[str, str]:
    values = {
        "PATH": str(root / "venv" / "bin"),
        "UV_NO_SYNC": "1",
        "PYTHONSAFEPATH": "1",
    }
    if kind == "source_script":
        values.update(
            {
                "UV_PROJECT_ENVIRONMENT": str(root / "venv"),
                "PYTHONPATH": str(root / "brain-v42"),
            }
        )
    return values


def _validate_environment(values: dict[str, str], root: Path, kind: str) -> None:
    expected = _expected_environment(root, kind)
    path = values.get("PATH", "")
    if not path.startswith(expected["PATH"] + os.pathsep):
        _fail("writer_launch_unsupported")
    for key, expected_value in expected.items():
        if key != "PATH" and values.get(key) != expected_value:
            _fail("writer_launch_unsupported")
    if kind == "wheel_module" and values.get("PYTHONPATH"):
        _fail("writer_launch_unsupported")
    if values.get("PYTHONHOME"):
        _fail("writer_launch_unsupported")


def _exec_start_argv(value: str) -> tuple[str, tuple[str, ...]]:
    """Parse systemd's argv field, or the simple form used by controlled tests."""
    if len(value) > 8192:
        _fail("writer_launch_unsupported")
    command = value
    declared_path: str | None = None
    if "argv[]=" in command:
        if command.count("argv[]=") != 1 or command.count("path=") != 1:
            _fail("writer_launch_unsupported")
        declared_path = command.split("path=", 1)[1].split(" ;", 1)[0].strip()
        command = command.split("argv[]=", 1)[1].split(" ;", 1)[0].rstrip(" }")
    try:
        argv = tuple(shlex.split(command))
    except ValueError:
        _fail("writer_launch_unsupported")
    if not argv:
        _fail("writer_launch_unsupported")
    executable = declared_path or argv[0]
    if Path(executable).resolve() != Path(argv[0]).resolve():
        _fail("writer_launch_unsupported")
    return executable, argv


def _matches_module_launch(argv: tuple[str, ...], interpreter: Path, module: str) -> bool:
    return (
        len(argv) >= 3
        and Path(argv[0]).resolve() == interpreter.resolve()
        and argv[1:3]
        == (
            "-m",
            module,
        )
    )


def _matches_observer_launch(argv: tuple[str, ...], interpreter: Path, observer_env: Path) -> bool:
    return (
        len(argv) == 5
        and _matches_module_launch(argv[:3], interpreter, "brain_v42.delivery_observer")
        and argv[3] == "--env-file"
        and Path(argv[4]).absolute() == observer_env.absolute()
    )


def _matches_source_launch(argv: tuple[str, ...], source: Path, interpreter: Path) -> bool:
    if source.suffix == ".sh":
        return (
            len(argv) >= 2
            and Path(argv[0]).resolve() == Path("/bin/bash").resolve()
            and Path(argv[1]) == source
        )
    if (
        len(argv) >= 2
        and Path(argv[0]).resolve() == interpreter.resolve()
        and Path(argv[1]) == source
    ):
        return True
    if source.suffix == ".py":
        module = ".".join(source.relative_to(source.parents[1]).with_suffix("").parts)
        return _matches_module_launch(argv, interpreter, module)
    return False


def _validate_effective_units(
    config: dict[str, Any],
    root: Path,
    interpreter: Path,
    dependencies: PreflightDependencies,
    verified_source_paths: set[str],
    observer_env: Path,
) -> None:
    units = _object(config.get("writers"), "config_schema_invalid")
    systemd = dependencies.systemd_properties or _read_systemd_properties
    process_reader = dependencies.process_identity or _read_process_identity
    manager_environment = dependencies.manager_environment or _read_manager_environment
    for name, raw_writer in units.items():
        writer = _object(raw_writer, "writer_launch_unsupported")
        kind = _text(writer.get("kind"), "writer_launch_unsupported", limit=32)
        properties = systemd(name)
        if not isinstance(properties, dict) or set(properties) != set(_SYSTEMD_PROPERTIES):
            _fail("writer_launch_unsupported")
        if any(not isinstance(value, str) or len(value) > 8192 for value in properties.values()):
            _fail("writer_launch_unsupported")
        _systemd_executable, command = _exec_start_argv(properties["ExecStart"])
        working_directory = properties["WorkingDirectory"]
        if not Path(working_directory).is_absolute():
            _fail("writer_launch_unsupported")
        unit_environment = dict(manager_environment())
        unit_environment.update(_environment_assignments(properties["Environment"]))
        unit_environment.update(_environment_file_assignments(properties["EnvironmentFiles"]))
        _unset_controlled_environment(properties["UnsetEnvironment"])
        _validate_environment(unit_environment, root, kind)
        if kind == "wheel_module":
            module = _text(writer.get("module"), "writer_launch_unsupported", limit=256)
            matches_launch = (
                _matches_observer_launch(command, interpreter, observer_env)
                if name == "brain-v42-delivery-observer.service"
                else _matches_module_launch(command, interpreter, module)
            )
            if name == "brain-v42-delivery-observer.service" and _environment_file_paths(
                properties["EnvironmentFiles"]
            ) != (observer_env.absolute(),):
                _fail("writer_launch_unsupported")
        else:
            source_path = _text(writer.get("source_path"), "writer_launch_unsupported", limit=1024)
            if source_path not in verified_source_paths:
                _fail("writer_launch_unsupported")
            source = _release_path(root, source_path)
            _safe_regular(source)
            matches_launch = _matches_source_launch(command, source, interpreter)
        if not matches_launch:
            _fail("writer_launch_unsupported")
        active = properties["ActiveState"] == "active"
        must_be_active = writer.get("must_be_active") is True
        if must_be_active and not active:
            _fail("writer_launch_unsupported")
        if not active:
            continue
        try:
            pid = int(properties["MainPID"])
        except ValueError:
            _fail("writer_launch_unsupported")
        identity = process_reader(pid)
        _validate_environment(identity.environment, root, kind)
        if Path(identity.cwd).resolve() != Path(working_directory).resolve():
            _fail("writer_launch_unsupported")
        if kind == "wheel_module":
            expected = (
                _matches_observer_launch(identity.argv, interpreter, observer_env)
                if name == "brain-v42-delivery-observer.service"
                else _matches_module_launch(identity.argv, interpreter, module)
            )
            if Path(identity.executable).resolve() != interpreter.resolve() or not expected:
                _fail("writer_launch_unsupported")
        else:
            expected_executable = (
                Path("/bin/bash").resolve() if source.suffix == ".sh" else interpreter.resolve()
            )
            if Path(
                identity.executable
            ).resolve() != expected_executable or not _matches_source_launch(
                identity.argv, source, interpreter
            ):
                _fail("writer_launch_unsupported")


def _validate_payload(
    root: Path,
    manifest: dict[str, Any],
    source: ReleaseFile,
    wheel: ReleaseFile,
    retained_lock: ReleaseFile,
    interpreter: ReleaseFile,
) -> None:
    package = manifest.get("package_payload")
    source_payload = manifest.get("source_payload")
    if not isinstance(package, list) or not package or not isinstance(source_payload, list):
        _fail("config_schema_invalid")
    try:
        with (
            tarfile.open(source.path, "r:gz") as archive,
            zipfile.ZipFile(wheel.path) as distribution,
        ):
            if archive.pax_headers.get("comment") != manifest.get("source_sha"):
                _fail("release_artifact_mismatch")
            archive_members = {
                member.name: member for member in archive.getmembers() if member.isfile()
            }
            wheel_members = set(distribution.namelist())
            metadata_names = {
                name for name in wheel_members if name.endswith(".dist-info/METADATA")
            }
            if len(metadata_names) != 1:
                _fail("release_artifact_mismatch")
            wheel_metadata = distribution.read(metadata_names.pop()).decode("utf-8")
            if f"Version: {manifest.get('version')}\n" not in wheel_metadata:
                _fail("release_artifact_mismatch")
            project = archive_members.get("brain-v42/pyproject.toml")
            if project is None:
                _fail("release_artifact_mismatch")
            project_stream = archive.extractfile(project)
            if (
                project_stream is None
                or f'version = "{manifest.get("version")}"'.encode() not in project_stream.read()
            ):
                _fail("release_artifact_mismatch")
            archived_lock = archive_members.get("brain-v42/uv.lock")
            if archived_lock is None:
                _fail("release_artifact_mismatch")
            lock_stream = archive.extractfile(archived_lock)
            if lock_stream is None or lock_stream.read() != retained_lock.path.read_bytes():
                _fail("release_artifact_mismatch")
            installed: set[Path] = set()
            declared_wheel: set[str] = set()
            for value in package:
                item = _object(value, "config_schema_invalid")
                source_name = _text(item.get("source_path"), "config_schema_invalid")
                wheel_name = _text(item.get("wheel_path"), "config_schema_invalid")
                installed_path = _release_path(root, item.get("installed_path"))
                digest = _sha(item.get("sha256"), "config_schema_invalid")
                if wheel_name == "brain_v42/alembic.ini":
                    expected_source = "brain-v42/alembic.ini"
                elif wheel_name.startswith("brain_v42/alembic/"):
                    expected_source = "brain-v42/alembic/" + wheel_name.removeprefix(
                        "brain_v42/alembic/"
                    )
                elif wheel_name.startswith("brain_v42/"):
                    expected_source = "brain-v42/src/" + wheel_name
                else:
                    _fail("release_artifact_mismatch")
                if source_name != expected_source:
                    _fail("release_artifact_mismatch")
                member = archive_members.get(source_name)
                if member is None or wheel_name not in wheel_members:
                    _fail("release_artifact_mismatch")
                declared_wheel.add(wheel_name)
                source_bytes = archive.extractfile(member)
                if source_bytes is None:
                    _fail("release_artifact_mismatch")
                if (
                    hashlib.sha256(source_bytes.read()).hexdigest() != digest
                    or hashlib.sha256(distribution.read(wheel_name)).hexdigest() != digest
                ):
                    _fail("release_artifact_mismatch")
                _safe_regular(installed_path)
                if _digest(installed_path) != digest:
                    _fail("release_artifact_mismatch")
                installed.add(installed_path)
            actual_wheel = {
                name
                for name in wheel_members
                if name.startswith("brain_v42/") and not name.endswith("/")
            }
            if actual_wheel != declared_wheel:
                _fail("release_artifact_mismatch")
            package_root = root / "venv" / "lib" / "python3.12" / "site-packages" / "brain_v42"
            actual = {
                path.resolve()
                for path in package_root.rglob("*")
                if path.is_file()
                and not (
                    "__pycache__" in path.relative_to(package_root).parts and path.suffix == ".pyc"
                )
            }
            if actual != installed:
                _fail("installed_package_unexpected")
            declared_source: set[str] = set()
            for value in source_payload:
                item = _object(value, "config_schema_invalid")
                archive_name = _text(item.get("archive_path"), "config_schema_invalid")
                extracted = _release_path(root, item.get("extracted_path"))
                if item.get("extracted_path") != archive_name:
                    _fail("release_artifact_mismatch")
                digest = _sha(item.get("sha256"), "config_schema_invalid")
                member = archive_members.get(archive_name)
                if member is None:
                    _fail("release_artifact_mismatch")
                declared_source.add(archive_name)
                stream = archive.extractfile(member)
                if stream is None or hashlib.sha256(stream.read()).hexdigest() != digest:
                    _fail("release_artifact_mismatch")
                _safe_regular(extracted)
                if _digest(extracted) != digest:
                    _fail("release_artifact_mismatch")
            actual_source = {
                name
                for name in archive_members
                if name.startswith("brain-v42/scripts/") or name == "brain-v42/.mcp.json"
            }
            if "brain-v42/.mcp.json" not in actual_source or actual_source != declared_source:
                _fail("release_artifact_mismatch")
            _validate_interpreter(interpreter.path, package_root, manifest.get("version"))
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        _fail("release_artifact_mismatch")


def _validate_interpreter(interpreter: Path, package_root: Path, version: object) -> None:
    probe = (
        "import importlib.metadata,json,sys,brain_v42;"
        "print(json.dumps({'executable':sys.executable,'file':brain_v42.__file__,"
        "'version':importlib.metadata.version('brain_v42')}))"
    )
    try:
        result = subprocess.run(
            [str(interpreter), "-I", "-c", probe],
            cwd="/",
            env={"PATH": os.defpath},
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 4096 or result.stderr:
            _fail("release_artifact_mismatch")
        identity = _object(json.loads(result.stdout), "release_artifact_mismatch")
        if (
            Path(_text(identity.get("executable"), "release_artifact_mismatch")).resolve()
            != interpreter.resolve()
            or Path(_text(identity.get("file"), "release_artifact_mismatch")).resolve()
            != (package_root / "__init__.py").resolve()
            or identity.get("version") != version
        ):
            _fail("release_artifact_mismatch")
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        _fail("release_artifact_mismatch")


async def _compare_guard(
    minimum: str,
    source: str,
    repository: dict[str, Any],
    probe_pull_request: int,
    observer_env: Path,
) -> str:
    try:
        repository_id = repository.get("id")
        slug = _text(repository.get("slug"), "guarded_revision_unverified", limit=201)
        if type(repository_id) is not int or repository_id <= 0 or "/" not in slug:
            _fail("guarded_revision_unverified")
        settings = load_observer_settings(observer_env)
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(),
            trust_env=False,
            follow_redirects=False,
            timeout=settings.request_timeout_seconds,
        ) as http:
            transport = GitHubTransport(http, settings)
            auth = GitHubAuthProvider(settings, transport)
            headers = await auth.authorization_headers()
            identity = await transport.request_json("GET", f"/repos/{slug}", headers=headers)
            if (
                not isinstance(identity, dict)
                or identity.get("id") != repository_id
                or identity.get("full_name") != slug
            ):
                _fail("guarded_revision_unverified")
            pull = await transport.request_json(
                "GET", f"/repos/{slug}/pulls/{probe_pull_request}", headers=headers
            )
            checks = await transport.request_json(
                "GET", f"/repos/{slug}/commits/{source}/check-runs", headers=headers
            )
            statuses = await transport.request_json(
                "GET", f"/repos/{slug}/commits/{source}/status", headers=headers
            )
            contents = await transport.request_json(
                "GET", f"/repos/{slug}/contents/.mcp.json?ref={source}", headers=headers
            )
            if (
                not isinstance(pull, dict)
                or pull.get("number") != probe_pull_request
                or not isinstance(checks, dict)
                or not isinstance(checks.get("check_runs"), list)
                or not isinstance(statuses, dict)
                or not isinstance(statuses.get("statuses"), list)
                or not isinstance(contents, dict)
                or contents.get("type") != "file"
            ):
                _fail("guarded_revision_unverified")
            payload = await transport.request_json(
                "GET",
                f"/repos/{slug}/compare/{minimum}...{source}",
                headers=headers,
            )
        if not isinstance(payload, dict) or payload.get("status") not in {
            "identical",
            "ahead",
            "behind",
            "diverged",
        }:
            _fail("guarded_revision_unverified")
        return payload["status"]
    except (ProviderError, ValueError, TypeError, OSError, httpx.HTTPError):
        _fail("guarded_revision_unverified")


async def _read_schema_revision(settings: DeliverySettings) -> str:
    if settings.postgres_url is None:
        _fail("schema_capability_unavailable")
    engine = create_async_engine(
        settings.postgres_url.get_secret_value(),
        pool_pre_ping=False,
        connect_args={"timeout": _DB_CONNECT_TIMEOUT_SECONDS},
    )
    try:
        async with asyncio.timeout(_DB_OVERALL_TIMEOUT_SECONDS):
            async with engine.connect() as connection:
                async with connection.begin():
                    await connection.execute(sa.text("SET TRANSACTION READ ONLY"))
                    await connection.execute(
                        sa.text(f"SET LOCAL statement_timeout = {_DB_QUERY_TIMEOUT_MILLISECONDS}")
                    )
                    revision = await connection.scalar(
                        sa.text("SELECT version_num FROM alembic_version")
                    )
                    rows = (
                        await connection.execute(
                            sa.text(
                                "SELECT table_name, column_name FROM information_schema.columns "
                                "WHERE table_schema = current_schema() AND table_name IN "
                                "('delivery_workflows','delivery_contract_revisions',"
                                "'delivery_artifact_bindings','delivery_confirmations',"
                                "'delivery_receipts','delivery_events')"
                            )
                        )
                    ).all()
    except (TimeoutError, OSError, sa.SQLAlchemyError):
        _fail("schema_capability_unavailable")
    finally:
        await engine.dispose()
    required_columns = {
        ("delivery_workflows", "claim_epoch"),
        ("delivery_contract_revisions", "context_set_digest"),
        ("delivery_artifact_bindings", "row_version"),
        ("delivery_confirmations", "subject_kind"),
        ("delivery_receipts", "delivery_digest"),
        ("delivery_events", "idempotency_key"),
    }
    if not isinstance(revision, str) or not required_columns <= set(rows):
        _fail("schema_capability_unavailable")
    return revision


def _schema_revision(settings: DeliverySettings) -> str:
    return asyncio.run(_read_schema_revision(settings))


async def _read_health(endpoint: str) -> dict[str, Any]:
    try:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            _fail("service_health_unavailable")
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(),
            trust_env=False,
            follow_redirects=False,
            timeout=5,
        ) as client:
            async with client.stream("GET", endpoint) as response:
                if response.status_code != 200:
                    _fail("service_health_unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        _fail("service_health_unavailable")
            payload = json.loads(body)
    except (ValueError, httpx.HTTPError, OSError):
        _fail("service_health_unavailable")
    return _object(payload, "service_health_unavailable")


def _health(endpoint: str) -> dict[str, Any]:
    return asyncio.run(_read_health(endpoint))


def check(
    config_path: Path,
    *,
    guard_comparator: Callable[[str, str, dict[str, Any]], str] | None = None,
    dependencies: PreflightDependencies | None = None,
) -> dict[str, str]:
    try:
        _safe_regular(config_path, private=True)
    except PreflightFailure:
        _fail("private_config_invalid")
    config = _load_private_json(config_path)
    if config.get("schema_version") != 1:
        _fail("config_schema_invalid")
    _validate_writers(config)
    if config.get("required_schema_revision") != "053":
        _fail("schema_capability_unavailable")
    observer_env = Path(_text(config.get("observer_env_file"), "config_schema_invalid"))
    try:
        settings = load_observer_settings(observer_env)
    except (ProviderError, ValueError):
        _fail("private_environment_invalid")
    dependencies = dependencies or PreflightDependencies()
    actual_revision = (
        dependencies.schema_revision(settings)
        if dependencies.schema_revision is not None
        else _schema_revision(settings)
    )
    if actual_revision != "053":
        _fail("schema_capability_unavailable")
    manifest_path = Path(_text(config.get("release_manifest"), "config_schema_invalid"))
    _safe_regular(manifest_path)
    try:
        manifest = _object(
            json.loads(manifest_path.read_text(encoding="utf-8")), "config_schema_invalid"
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("config_schema_invalid")
    if manifest.get("schema_version") != 1 or manifest.get("minimum_guarded_sha") != _GUARD_SHA:
        _fail("config_schema_invalid")
    source_sha = _text(manifest.get("source_sha"), "config_schema_invalid", limit=40)
    if len(source_sha) != 40 or set(source_sha) - _SHA256:
        _fail("config_schema_invalid")
    root = manifest_path.parent
    source = _release_file(root, manifest.get("source_archive"))
    wheel = _release_file(root, manifest.get("wheel"))
    retained_lock = _release_file(root, manifest.get("uv_lock"))
    interpreter = _release_file(root, manifest.get("interpreter"))
    _validate_payload(root, manifest, source, wheel, retained_lock, interpreter)
    verified_source_paths = {
        _text(_object(item, "config_schema_invalid").get("archive_path"), "config_schema_invalid")
        for item in manifest.get("source_payload", [])
    }
    _validate_effective_units(
        config, root, interpreter.path, dependencies, verified_source_paths, observer_env
    )
    health_endpoint = _text(config.get("health_endpoint"), "config_schema_invalid", limit=2048)
    health = (
        dependencies.health(health_endpoint)
        if dependencies.health is not None
        else _health(health_endpoint)
    )
    if health.get("version") != manifest.get("version") or health.get("alembic_head") != "053":
        _fail("service_health_unavailable")
    repository = _object(config.get("repository"), "config_schema_invalid")
    probe_pull_request = config.get("probe_pull_request")
    if type(probe_pull_request) is not int or probe_pull_request <= 0:
        _fail("config_schema_invalid")
    try:
        relation = (
            guard_comparator(_GUARD_SHA, source_sha, repository)
            if guard_comparator is not None
            else asyncio.run(
                _compare_guard(_GUARD_SHA, source_sha, repository, probe_pull_request, observer_env)
            )
        )
    except Exception:
        _fail("guarded_revision_unverified")
    if relation not in {"identical", "ahead"}:
        _fail("guarded_revision_unverified")
    return {"source_sha": source_sha, "schema_revision": "053"}


def _emit(status: str, **values: str) -> None:
    payload = {"status": status, "timestamp": str(int(time.time())), **values}
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(
    argv: list[str] | None = None,
    *,
    guard_comparator: Callable[[str, str, dict[str, Any]], str] | None = None,
    dependencies: PreflightDependencies | None = None,
) -> int:
    try:
        parser = _SafeParser(description="Verify a guarded delivery release")
        parser.add_argument("--config", required=True, type=Path, metavar="ABSOLUTE_PRIVATE_JSON")
        arguments = parser.parse_args(argv)
        if not arguments.config.is_absolute():
            _fail("private_config_invalid")
        receipt = check(
            arguments.config,
            guard_comparator=guard_comparator,
            dependencies=dependencies,
        )
    except PreflightFailure as failure:
        _emit("failed", failure=failure.code)
        return 2
    except Exception:
        _emit("failed", failure="config_schema_invalid")
        return 2
    _emit("ok", **receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
