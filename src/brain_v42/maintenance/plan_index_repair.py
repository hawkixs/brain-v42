"""Safety boundaries for canonical multi-project plan-index repair."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from collections.abc import Generator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from brain_v42.config import get_settings

HASH_CHUNK_SIZE_BYTES = 64 * 1024
MAX_PLAN_FILE_BYTES = 8 * 1024 * 1024
MAX_INVENTORY_BYTES = 256 * 1024 * 1024
MAX_INVENTORY_FILES = 10_000
MAX_INVENTORY_DIRECTORIES = 10_000
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK

type _FileIdentity = tuple[int, int]


class RepairSafetyError(RuntimeError):
    """Closed, content-safe failure for a repair safety gate."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ProjectTarget:
    """One explicitly owned project root and its canonical scan directories."""

    project_key: str
    project_root: Path
    scan_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _ProjectDirectoryIdentities:
    project_key: str
    project_root: _FileIdentity
    scan_paths: tuple[tuple[Path, _FileIdentity], ...]


# Sentinel used when BRAIN_PLAN_PROJECTS_ROOT is unset (or Settings cannot be
# built, e.g. this module imported without POSTGRES_URL, as unit tests do).
# It deliberately never exists on disk: every operation downstream already
# refuses against directories that fail identity/existence checks
# (RepairSafetyError), so an unconfigured root fails the same closed way an
# unreachable one always has.
_UNCONFIGURED_PROJECTS_ROOT = Path("/brain-v42-plan-projects-root-unconfigured")


def _resolve_target_projects_root() -> Path:
    """Resolve the projects root scanned by this repair boundary.

    Sourced from BRAIN_PLAN_PROJECTS_ROOT via ``brain_v42.config.Settings``.
    Building ``Settings`` requires ``POSTGRES_URL`` (unconditionally, for
    every field) — every real invocation of this repair boundary already
    needs DB access, so that's never a problem in practice. It IS a problem
    for unit tests that import this module in isolation without configuring
    POSTGRES_URL; a missing/invalid Settings build falls back to the
    sentinel instead of raising, so import stays safe.
    """
    try:
        configured = get_settings().brain_plan_projects_root
    except ValidationError:
        configured = None
    return configured if configured is not None else _UNCONFIGURED_PROJECTS_ROOT


_TARGET_PROJECTS_ROOT = _resolve_target_projects_root()
_TARGET_SCAN_PATHS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "red-games": ("docs/specs", "docs/plans"),
        "red-gift": ("docs/specs", "docs/plans"),
        "red-phone": ("docs/specs", "docs/plans"),
        "red-quant": ("docs/specs", "docs/plans", "docs/runbooks"),
        "red-shrik": ("docs/specs", "docs/plans"),
        "red-viewer": ("docs/specs", "docs/plans"),
        "red-writer": ("docs/specs", "docs/plans"),
    }
)
TARGET_PROJECTS: Mapping[str, ProjectTarget] = MappingProxyType(
    {
        project_key: ProjectTarget(
            project_key=project_key,
            project_root=_TARGET_PROJECTS_ROOT / project_key,
            scan_paths=tuple(
                _TARGET_PROJECTS_ROOT / project_key / relative_path
                for relative_path in relative_paths
            ),
        )
        for project_key, relative_paths in _TARGET_SCAN_PATHS.items()
    }
)
TARGET_PROJECT_KEYS = frozenset(TARGET_PROJECTS)


@dataclass(frozen=True, slots=True)
class RepairManifest:
    """Validated version-one repair manifest."""

    version: int
    projects: tuple[ProjectTarget, ...]
    _directory_identities: tuple[_ProjectDirectoryIdentities, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class LocalPlanFile:
    """Content-safe identity for one canonical local plan file."""

    project_key: str
    file_path: str
    content_hash: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _PlanFileCandidate:
    path: Path
    identity: _FileIdentity


def _canonical_directory(value: object, *, relative_reason: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RepairSafetyError("invalid_manifest_schema")

    candidate = Path(value)
    if not candidate.is_absolute():
        raise RepairSafetyError(relative_reason)
    try:
        canonical = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RepairSafetyError("path_missing") from exc
    if not canonical.is_dir():
        raise RepairSafetyError("path_not_directory")
    if not os.access(canonical, os.R_OK | os.X_OK):
        raise RepairSafetyError("path_unreadable")
    return canonical


def _load_manifest_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RepairSafetyError("manifest_unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "projects"}:
        raise RepairSafetyError("invalid_manifest_schema")
    return payload


def _canonical_trusted_target(project_key: str, target: object) -> ProjectTarget:
    if not isinstance(target, ProjectTarget) or target.project_key != project_key:
        raise RepairSafetyError("trusted_project_configuration_invalid")

    try:
        project_root = _canonical_directory(
            str(target.project_root),
            relative_reason="trusted_project_configuration_invalid",
        )
        scan_paths = tuple(
            _canonical_directory(
                str(scan_path),
                relative_reason="trusted_project_configuration_invalid",
            )
            for scan_path in target.scan_paths
        )
    except RepairSafetyError as exc:
        raise RepairSafetyError("trusted_project_configuration_invalid") from exc

    if (
        not scan_paths
        or len(set(scan_paths)) != len(scan_paths)
        or any(not scan_path.is_relative_to(project_root) for scan_path in scan_paths)
    ):
        raise RepairSafetyError("trusted_project_configuration_invalid")
    return ProjectTarget(
        project_key=project_key,
        project_root=project_root,
        scan_paths=tuple(sorted(scan_paths, key=str)),
    )


def load_manifest(
    path: Path,
    *,
    allowed_project_keys: frozenset[str] = TARGET_PROJECT_KEYS,
) -> RepairManifest:
    """Load a closed manifest and return only canonical owned directories."""
    payload = _load_manifest_payload(path)
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise RepairSafetyError("unsupported_manifest_version")

    raw_projects = payload["projects"]
    if not isinstance(raw_projects, list):
        raise RepairSafetyError("invalid_manifest_schema")

    if not allowed_project_keys.issubset(TARGET_PROJECTS):
        raise RepairSafetyError("trusted_project_set_mismatch")
    trusted_projects = {
        project_key: TARGET_PROJECTS[project_key] for project_key in allowed_project_keys
    }

    seen_project_keys: set[str] = set()
    seen_scan_paths: set[Path] = set()
    raw_targets: list[tuple[str, dict[str, object]]] = []
    targets: list[ProjectTarget] = []
    directory_identities: list[_ProjectDirectoryIdentities] = []

    for raw_project in raw_projects:
        if not isinstance(raw_project, dict) or set(raw_project) != {
            "project_key",
            "project_root",
            "scan_paths",
        }:
            raise RepairSafetyError("invalid_manifest_schema")

        project_key = raw_project["project_key"]
        if not isinstance(project_key, str) or not project_key:
            raise RepairSafetyError("invalid_manifest_schema")
        if project_key in seen_project_keys:
            raise RepairSafetyError("duplicate_project_key")
        seen_project_keys.add(project_key)
        raw_targets.append((project_key, raw_project))

    if seen_project_keys != set(allowed_project_keys):
        raise RepairSafetyError("project_set_mismatch")

    for project_key, raw_project in raw_targets:
        project_root, project_root_identity = _validated_directory(
            raw_project["project_root"],
            relative_reason="relative_project_root",
        )
        trusted_target = _canonical_trusted_target(
            project_key,
            trusted_projects[project_key],
        )
        if project_root != trusted_target.project_root:
            raise RepairSafetyError("project_root_mismatch")
        raw_scan_paths = raw_project["scan_paths"]
        if not isinstance(raw_scan_paths, list) or not raw_scan_paths:
            raise RepairSafetyError("invalid_manifest_schema")

        canonical_scan_paths: list[Path] = []
        scan_path_identities: list[tuple[Path, _FileIdentity]] = []
        for raw_scan_path in raw_scan_paths:
            scan_path, scan_path_identity = _validated_directory(
                raw_scan_path,
                relative_reason="relative_scan_path",
            )
            if not scan_path.is_relative_to(project_root):
                raise RepairSafetyError("scan_path_outside_root")
            if scan_path in seen_scan_paths:
                raise RepairSafetyError("duplicate_scan_path")
            seen_scan_paths.add(scan_path)
            canonical_scan_paths.append(scan_path)
            scan_path_identities.append((scan_path, scan_path_identity))

        if set(canonical_scan_paths) != set(trusted_target.scan_paths):
            raise RepairSafetyError("scan_path_set_mismatch")

        targets.append(
            ProjectTarget(
                project_key=project_key,
                project_root=project_root,
                scan_paths=tuple(sorted(canonical_scan_paths, key=str)),
            )
        )
        directory_identities.append(
            _ProjectDirectoryIdentities(
                project_key=project_key,
                project_root=project_root_identity,
                scan_paths=tuple(sorted(scan_path_identities, key=lambda item: str(item[0]))),
            )
        )

    return RepairManifest(
        version=1,
        projects=tuple(sorted(targets, key=lambda item: item.project_key)),
        _directory_identities=tuple(
            sorted(directory_identities, key=lambda item: item.project_key)
        ),
    )


def _is_plan_file_name(name: str) -> bool:
    return name.endswith(("-design.md", "-plan.md"))


def _file_identity(file_stat: os.stat_result) -> _FileIdentity:
    return file_stat.st_dev, file_stat.st_ino


def _open_directory_components(
    descriptor: int,
    components: tuple[str, ...],
    *,
    reason_code: str,
) -> int:
    """Consume a directory fd and open every child component without symlinks."""
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise RepairSafetyError(reason_code)
        for component in components:
            if component in {"", ".", ".."}:
                raise RepairSafetyError(reason_code)
            child_descriptor = os.open(
                component,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=descriptor,
            )
            try:
                if not stat.S_ISDIR(os.fstat(child_descriptor).st_mode):
                    raise RepairSafetyError(reason_code)
            except BaseException:
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
    except RepairSafetyError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise RepairSafetyError(reason_code) from exc
    return descriptor


def _open_absolute_directory(path: Path, *, reason_code: str) -> int:
    if not path.is_absolute():
        raise RepairSafetyError(reason_code)
    try:
        descriptor = os.open("/", _DIRECTORY_OPEN_FLAGS)
    except OSError as exc:
        raise RepairSafetyError(reason_code) from exc
    return _open_directory_components(
        descriptor,
        path.parts[1:],
        reason_code=reason_code,
    )


def _validated_directory(
    value: object,
    *,
    relative_reason: str,
) -> tuple[Path, _FileIdentity]:
    canonical = _canonical_directory(value, relative_reason=relative_reason)
    descriptor = _open_absolute_directory(canonical, reason_code="path_unreadable")
    try:
        return canonical, _file_identity(os.fstat(descriptor))
    except OSError as exc:
        raise RepairSafetyError("path_unreadable") from exc
    finally:
        os.close(descriptor)


def _open_relative_directory(
    parent_descriptor: int,
    path: Path,
    *,
    reason_code: str,
) -> int:
    if path.is_absolute():
        raise RepairSafetyError(reason_code)
    try:
        descriptor = os.dup(parent_descriptor)
    except OSError as exc:
        raise RepairSafetyError(reason_code) from exc
    return _open_directory_components(
        descriptor,
        path.parts,
        reason_code=reason_code,
    )


def _open_absolute_file(path: Path) -> int:
    parent_descriptor = _open_absolute_directory(
        path.parent,
        reason_code="file_unreadable",
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path.name,
            _FILE_OPEN_FLAGS,
            dir_fd=parent_descriptor,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RepairSafetyError("file_unreadable")
        return descriptor
    except RepairSafetyError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise RepairSafetyError("file_unreadable") from exc
    finally:
        os.close(parent_descriptor)


def _scan_plan_files(
    project_root: Path,
    scan_path: Path,
    expected_project_root_identity: _FileIdentity,
    expected_scan_path_identity: _FileIdentity,
) -> Generator[_PlanFileCandidate, None, None]:
    try:
        relative_scan_path = scan_path.relative_to(project_root)
    except ValueError as exc:
        raise RepairSafetyError("directory_unreadable") from exc
    project_descriptor = _open_absolute_directory(
        project_root,
        reason_code="directory_unreadable",
    )

    try:
        if _file_identity(os.fstat(project_descriptor)) != expected_project_root_identity:
            raise RepairSafetyError("directory_unreadable")
        seen_directories = {relative_scan_path}
        if len(seen_directories) > MAX_INVENTORY_DIRECTORIES:
            raise RepairSafetyError("inventory_directory_limit_exceeded")
        pending_directories = [(relative_scan_path, expected_scan_path_identity)]

        while pending_directories:
            relative_directory, expected_identity = pending_directories.pop()
            directory_descriptor = _open_relative_directory(
                project_descriptor,
                relative_directory,
                reason_code="directory_unreadable",
            )
            try:
                if _file_identity(os.fstat(directory_descriptor)) != expected_identity:
                    raise RepairSafetyError("directory_unreadable")
                try:
                    with os.scandir(directory_descriptor) as iterator:
                        for entry in iterator:
                            try:
                                entry_stat = entry.stat(follow_symlinks=False)
                                entry_identity = _file_identity(entry_stat)
                                if stat.S_ISLNK(entry_stat.st_mode):
                                    if _is_plan_file_name(entry.name):
                                        raise RepairSafetyError("file_symlink")
                                    continue
                                if stat.S_ISDIR(entry_stat.st_mode):
                                    child_directory = relative_directory / entry.name
                                    if child_directory not in seen_directories:
                                        seen_directories.add(child_directory)
                                        if len(seen_directories) > MAX_INVENTORY_DIRECTORIES:
                                            raise RepairSafetyError(
                                                "inventory_directory_limit_exceeded"
                                            )
                                        pending_directories.append(
                                            (child_directory, entry_identity)
                                        )
                                elif _is_plan_file_name(entry.name) and stat.S_ISREG(
                                    entry_stat.st_mode
                                ):
                                    yield _PlanFileCandidate(
                                        path=project_root / relative_directory / entry.name,
                                        identity=entry_identity,
                                    )
                            except OSError as exc:
                                raise RepairSafetyError("directory_unreadable") from exc
                except OSError as exc:
                    raise RepairSafetyError("directory_unreadable") from exc
            finally:
                os.close(directory_descriptor)
    finally:
        os.close(project_descriptor)


def _hash_plan_file(
    path: Path,
    *,
    inventory_bytes: int,
    expected_identity: _FileIdentity,
) -> tuple[str, int]:
    descriptor: int | None = None
    digest = hashlib.sha256()
    size_bytes = 0

    try:
        descriptor = _open_absolute_file(path)
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        with stream:
            file_stat = os.fstat(stream.fileno())
            if _file_identity(file_stat) != expected_identity:
                raise RepairSafetyError("file_unreadable")
            if file_stat.st_size > MAX_PLAN_FILE_BYTES:
                raise RepairSafetyError("file_size_limit_exceeded")
            if inventory_bytes + file_stat.st_size > MAX_INVENTORY_BYTES:
                raise RepairSafetyError("inventory_size_limit_exceeded")

            while chunk := stream.read(HASH_CHUNK_SIZE_BYTES):
                size_bytes += len(chunk)
                if size_bytes > MAX_PLAN_FILE_BYTES:
                    raise RepairSafetyError("file_size_limit_exceeded")
                if inventory_bytes + size_bytes > MAX_INVENTORY_BYTES:
                    raise RepairSafetyError("inventory_size_limit_exceeded")
                digest.update(chunk)
    except RepairSafetyError:
        raise
    except OSError as exc:
        raise RepairSafetyError("file_unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    return digest.hexdigest(), size_bytes


def discover_local_files(manifest: RepairManifest) -> tuple[LocalPlanFile, ...]:
    """Return a deterministic, canonical and content-safe local file inventory."""
    path_owners: dict[Path, tuple[str, _FileIdentity]] = {}
    identities_by_project = {
        identities.project_key: identities for identities in manifest._directory_identities
    }
    if len(identities_by_project) != len(manifest._directory_identities) or set(
        identities_by_project
    ) != {project.project_key for project in manifest.projects}:
        raise RepairSafetyError("manifest_identity_mismatch")

    for project in manifest.projects:
        project_identities = identities_by_project[project.project_key]
        scan_path_identities = dict(project_identities.scan_paths)
        if len(scan_path_identities) != len(project_identities.scan_paths) or set(
            scan_path_identities
        ) != set(project.scan_paths):
            raise RepairSafetyError("manifest_identity_mismatch")
        for scan_path in project.scan_paths:
            candidates = _scan_plan_files(
                project.project_root,
                scan_path,
                project_identities.project_root,
                scan_path_identities[scan_path],
            )
            try:
                for candidate in candidates:
                    if not candidate.path.is_relative_to(project.project_root):
                        raise RepairSafetyError("file_outside_project_root")
                    existing = path_owners.get(candidate.path)
                    if existing is not None and existing[0] != project.project_key:
                        raise RepairSafetyError("file_owner_collision")
                    if existing is not None and existing[1] != candidate.identity:
                        raise RepairSafetyError("file_unreadable")
                    if existing is None:
                        path_owners[candidate.path] = (
                            project.project_key,
                            candidate.identity,
                        )
                        if len(path_owners) > MAX_INVENTORY_FILES:
                            raise RepairSafetyError("inventory_file_limit_exceeded")
            finally:
                candidates.close()

    inventory_bytes = 0
    discovered: list[LocalPlanFile] = []
    for file_path in sorted(path_owners, key=str):
        content_hash, size_bytes = _hash_plan_file(
            file_path,
            inventory_bytes=inventory_bytes,
            expected_identity=path_owners[file_path][1],
        )
        inventory_bytes += size_bytes
        discovered.append(
            LocalPlanFile(
                project_key=path_owners[file_path][0],
                file_path=str(file_path),
                content_hash=content_hash,
                size_bytes=size_bytes,
            )
        )

    return tuple(sorted(discovered, key=lambda item: (item.project_key, item.file_path)))


def verify_local_files_unchanged(
    expected_files: tuple[LocalPlanFile, ...],
) -> None:
    """Rehash exact local files through stable descriptors without exposing content."""
    seen_paths: set[str] = set()
    inventory_bytes = 0
    try:
        for expected in sorted(
            expected_files,
            key=lambda item: (item.project_key, item.file_path),
        ):
            if (
                expected.file_path in seen_paths
                or not Path(expected.file_path).is_absolute()
                or expected.size_bytes < 0
                or expected.size_bytes > MAX_PLAN_FILE_BYTES
                or not _is_sha256_digest(expected.content_hash)
            ):
                raise RepairSafetyError("local_file_changed")
            seen_paths.add(expected.file_path)

            descriptor = _open_absolute_file(Path(expected.file_path))
            stream = os.fdopen(descriptor, "rb")
            with stream:
                before = os.fstat(stream.fileno())
                before_fingerprint = (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                digest = hashlib.sha256()
                size_bytes = 0
                while chunk := stream.read(HASH_CHUNK_SIZE_BYTES):
                    size_bytes += len(chunk)
                    if (
                        size_bytes > MAX_PLAN_FILE_BYTES
                        or inventory_bytes + size_bytes > MAX_INVENTORY_BYTES
                    ):
                        raise RepairSafetyError("local_file_changed")
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                after_fingerprint = (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )

            if (
                before_fingerprint != after_fingerprint
                or size_bytes != expected.size_bytes
                or not hmac.compare_digest(digest.hexdigest(), expected.content_hash)
            ):
                raise RepairSafetyError("local_file_changed")
            inventory_bytes += size_bytes
    except Exception:
        raise RepairSafetyError("local_file_changed") from None


def _normalize_json(value: object) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise RepairSafetyError("naive_datetime")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (UUID, Path)):
        return str(value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise RepairSafetyError("non_string_mapping_key")
        return {key: _normalize_json(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize_json(item) for item in value]
        return sorted(normalized, key=canonical_json)
    raise RepairSafetyError("unsupported_snapshot_value")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def canonical_json(value: object) -> bytes:
    """Serialize supported values canonically without Python repr leakage."""
    try:
        return json.dumps(
            _normalize_json(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RepairSafetyError("unsupported_snapshot_value") from exc


def sha256_json(value: object) -> str:
    """Return the SHA-256 digest of canonical JSON bytes."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def database_identity_fingerprint(identity: Mapping[str, object]) -> str:
    """Hash database identity values so snapshots never expose them."""
    return sha256_json(identity)


@dataclass(frozen=True, slots=True)
class ContextRecord:
    """Complete canonical context row and its immutable CAS fingerprint."""

    values: Mapping[str, Any]
    fingerprint: str
    proposed_plan_scan_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        normalized = _normalize_json(self.values)
        if not isinstance(normalized, dict):
            raise RepairSafetyError("invalid_context_record")
        object.__setattr__(self, "values", _freeze_json(normalized))

    @classmethod
    def from_values(
        cls,
        values: Mapping[str, object],
        *,
        proposed_plan_scan_paths: tuple[str, ...],
    ) -> ContextRecord:
        normalized = _normalize_json(values)
        if not isinstance(normalized, dict):
            raise RepairSafetyError("invalid_context_record")
        return cls(
            values=normalized,
            fingerprint=sha256_json(normalized),
            proposed_plan_scan_paths=tuple(proposed_plan_scan_paths),
        )

    @property
    def project_key(self) -> str:
        value = self.values.get("project_key")
        if not isinstance(value, str):
            raise RepairSafetyError("invalid_context_record")
        return value

    def to_dict(self) -> dict[str, object]:
        values = _normalize_json(self.values)
        if not isinstance(values, dict):
            raise RepairSafetyError("invalid_context_record")
        return {
            "values": values,
            "fingerprint": self.fingerprint,
            "proposed_plan_scan_paths": list(self.proposed_plan_scan_paths),
        }

    @classmethod
    def from_dict(cls, payload: object) -> ContextRecord:
        if not isinstance(payload, dict) or set(payload) != {
            "values",
            "fingerprint",
            "proposed_plan_scan_paths",
        }:
            raise RepairSafetyError("invalid_snapshot_schema")
        values = payload["values"]
        fingerprint = payload["fingerprint"]
        proposed = payload["proposed_plan_scan_paths"]
        if (
            not isinstance(values, dict)
            or not isinstance(fingerprint, str)
            or not isinstance(proposed, list)
            or not all(isinstance(item, str) for item in proposed)
        ):
            raise RepairSafetyError("invalid_snapshot_schema")
        record = cls.from_values(values, proposed_plan_scan_paths=tuple(proposed))
        if not hmac.compare_digest(record.fingerprint, fingerprint):
            raise RepairSafetyError("snapshot_context_fingerprint_mismatch")
        return record


@dataclass(frozen=True, slots=True)
class IndexedPlanRecord:
    """Content-safe identity and lifecycle fields for one indexed plan."""

    id: str
    project_key: str
    file_path: str
    content_hash: str
    status: str
    freshness_status: str
    declared_chunk_count: int
    observed_chunk_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "project_key": self.project_key,
            "file_path": self.file_path,
            "content_hash": self.content_hash,
            "status": self.status,
            "freshness_status": self.freshness_status,
            "declared_chunk_count": self.declared_chunk_count,
            "observed_chunk_count": self.observed_chunk_count,
        }


@dataclass(frozen=True, slots=True)
class FeatureLinkRecord:
    """Identity of one feature-to-plan link needed for bounded cleanup."""

    feature_id: str
    plan_id: str
    similarity_score: float
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "feature_id": self.feature_id,
            "plan_id": self.plan_id,
            "similarity_score": self.similarity_score,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class PlanCollisionRecord:
    """Closed classification of one canonical owner or hash collision."""

    plan_id: str
    file_path: str
    expected_project_key: str
    actual_project_key: str
    expected_content_hash: str
    actual_content_hash: str
    reason_code: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "file_path": self.file_path,
            "expected_project_key": self.expected_project_key,
            "actual_project_key": self.actual_project_key,
            "expected_content_hash": self.expected_content_hash,
            "actual_content_hash": self.actual_content_hash,
            "reason_code": self.reason_code,
        }


def _local_file_to_dict(record: LocalPlanFile) -> dict[str, object]:
    return {
        "project_key": record.project_key,
        "file_path": record.file_path,
        "content_hash": record.content_hash,
        "size_bytes": record.size_bytes,
    }


def _canonical_utc_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise RepairSafetyError("invalid_snapshot_schema")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RepairSafetyError("invalid_snapshot_schema") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RepairSafetyError("invalid_snapshot_schema")
    if value != parsed.astimezone(UTC).isoformat():
        raise RepairSafetyError("invalid_snapshot_schema")
    return value


@dataclass(frozen=True, slots=True)
class RepairSnapshot:
    """Version-one private control snapshot for the bounded repair."""

    version: int
    mutation_timestamp: str
    database_identity_hash: str
    alembic_revision: str
    contexts: tuple[ContextRecord, ...]
    local_files: tuple[LocalPlanFile, ...]
    indexed_plans: tuple[IndexedPlanRecord, ...]
    feature_links: tuple[FeatureLinkRecord, ...]
    polluted_plan_ids: tuple[str, ...]
    missing_canonical_files: tuple[LocalPlanFile, ...]
    collisions: tuple[PlanCollisionRecord, ...]

    def __post_init__(self) -> None:
        _canonical_utc_timestamp(self.mutation_timestamp)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "mutation_timestamp": self.mutation_timestamp,
            "database_identity_hash": self.database_identity_hash,
            "alembic_revision": self.alembic_revision,
            "contexts": [item.to_dict() for item in self.contexts],
            "local_files": [_local_file_to_dict(item) for item in self.local_files],
            "indexed_plans": [item.to_dict() for item in self.indexed_plans],
            "feature_links": [item.to_dict() for item in self.feature_links],
            "polluted_plan_ids": list(self.polluted_plan_ids),
            "missing_canonical_files": [
                _local_file_to_dict(item) for item in self.missing_canonical_files
            ],
            "collisions": [item.to_dict() for item in self.collisions],
        }

    @classmethod
    def from_dict(cls, payload: object) -> RepairSnapshot:
        required = {
            "version",
            "mutation_timestamp",
            "database_identity_hash",
            "alembic_revision",
            "contexts",
            "local_files",
            "indexed_plans",
            "feature_links",
            "polluted_plan_ids",
            "missing_canonical_files",
            "collisions",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise RepairSafetyError("invalid_snapshot_schema")
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise RepairSafetyError("invalid_snapshot_schema")

        mutation_timestamp = _canonical_utc_timestamp(payload["mutation_timestamp"])
        database_identity_hash = payload["database_identity_hash"]
        alembic_revision = payload["alembic_revision"]
        if not all(
            isinstance(value, str)
            for value in (
                database_identity_hash,
                alembic_revision,
            )
        ):
            raise RepairSafetyError("invalid_snapshot_schema")

        contexts = _parse_sequence(payload["contexts"], ContextRecord.from_dict)
        local_files = _parse_sequence(payload["local_files"], _local_file_from_dict)
        indexed_plan_records = _parse_sequence(
            payload["indexed_plans"],
            _indexed_plan_from_dict,
        )
        feature_links = _parse_sequence(
            payload["feature_links"],
            _feature_link_from_dict,
        )
        polluted = payload["polluted_plan_ids"]
        if not isinstance(polluted, list) or not all(isinstance(item, str) for item in polluted):
            raise RepairSafetyError("invalid_snapshot_schema")
        missing = _parse_sequence(
            payload["missing_canonical_files"],
            _local_file_from_dict,
        )
        collisions = _parse_sequence(payload["collisions"], _collision_from_dict)

        return cls(
            version=1,
            mutation_timestamp=mutation_timestamp,
            database_identity_hash=database_identity_hash,
            alembic_revision=alembic_revision,
            contexts=contexts,
            local_files=local_files,
            indexed_plans=indexed_plan_records,
            feature_links=feature_links,
            polluted_plan_ids=tuple(polluted),
            missing_canonical_files=missing,
            collisions=collisions,
        )


@dataclass(frozen=True, slots=True)
class MutationProof:
    """Verified private-file digests and explicit mutation attestations."""

    snapshot_sha256: str
    backup_receipt_sha256: str
    postgres_restore_tested: bool
    writers_off_confirmed: bool


def _is_sha256_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True, slots=True)
class ProjectReindexStats:
    """Content-safe counters emitted by one project's bounded reindex."""

    project_key: str
    indexed: int
    skipped: int
    linked: int
    errors: int
    chunks_created: int

    def __post_init__(self) -> None:
        counters = (
            self.indexed,
            self.skipped,
            self.linked,
            self.errors,
            self.chunks_created,
        )
        if (
            not isinstance(self.project_key, str)
            or not self.project_key
            or any(type(counter) is not int or counter < 0 for counter in counters)
        ):
            raise RepairSafetyError("invalid_reindex_project_stats")
        if self.errors != 0:
            raise RepairSafetyError("reindex_errors_reported")

    def to_dict(self) -> dict[str, object]:
        return {
            "project_key": self.project_key,
            "indexed": self.indexed,
            "skipped": self.skipped,
            "linked": self.linked,
            "errors": self.errors,
            "chunks_created": self.chunks_created,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ProjectReindexStats:
        keys = {
            "project_key",
            "indexed",
            "skipped",
            "linked",
            "errors",
            "chunks_created",
        }
        if not isinstance(payload, dict) or set(payload) != keys:
            raise RepairSafetyError("invalid_reindex_evidence")
        project_key = payload["project_key"]
        counters = (
            payload["indexed"],
            payload["skipped"],
            payload["linked"],
            payload["errors"],
            payload["chunks_created"],
        )
        if not isinstance(project_key, str) or any(
            type(counter) is not int for counter in counters
        ):
            raise RepairSafetyError("invalid_reindex_evidence")
        return cls(
            project_key=project_key,
            indexed=counters[0],
            skipped=counters[1],
            linked=counters[2],
            errors=counters[3],
            chunks_created=counters[4],
        )


@dataclass(frozen=True, slots=True)
class ReindexEvidence:
    """Version-one reindex proof bound to one immutable repair snapshot."""

    version: int
    snapshot_sha256: str
    projects: tuple[ProjectReindexStats, ...]

    def __post_init__(self) -> None:
        if self.version != 1 or not _is_sha256_digest(self.snapshot_sha256):
            raise RepairSafetyError("invalid_reindex_evidence")
        if not isinstance(self.projects, tuple) or not all(
            isinstance(project, ProjectReindexStats) for project in self.projects
        ):
            raise RepairSafetyError("invalid_reindex_evidence")
        projects_by_key = {project.project_key: project for project in self.projects}
        if (
            len(projects_by_key) != len(self.projects)
            or set(projects_by_key) != TARGET_PROJECT_KEYS
        ):
            raise RepairSafetyError("reindex_project_set_mismatch")
        object.__setattr__(
            self,
            "projects",
            tuple(projects_by_key[key] for key in sorted(projects_by_key)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "snapshot_sha256": self.snapshot_sha256,
            "projects": [project.to_dict() for project in self.projects],
        }

    @classmethod
    def from_dict(cls, payload: object) -> ReindexEvidence:
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "snapshot_sha256",
            "projects",
        }:
            raise RepairSafetyError("invalid_reindex_evidence")
        projects = payload["projects"]
        if (
            type(payload["version"]) is not int
            or not isinstance(payload["snapshot_sha256"], str)
            or not isinstance(projects, list)
        ):
            raise RepairSafetyError("invalid_reindex_evidence")
        return cls(
            version=payload["version"],
            snapshot_sha256=payload["snapshot_sha256"],
            projects=tuple(ProjectReindexStats.from_dict(item) for item in projects),
        )


@dataclass(frozen=True, slots=True)
class VerifiedPlanRecord:
    """Exact canonical database row proven by verification."""

    id: str
    project_key: str
    file_path: str
    content_hash: str

    def __post_init__(self) -> None:
        try:
            UUID(self.id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise RepairSafetyError("invalid_verification_report") from exc
        if (
            not isinstance(self.project_key, str)
            or self.project_key not in TARGET_PROJECT_KEYS
            or not isinstance(self.file_path, str)
            or not Path(self.file_path).is_absolute()
            or not _is_sha256_digest(self.content_hash)
        ):
            raise RepairSafetyError("invalid_verification_report")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "project_key": self.project_key,
            "file_path": self.file_path,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, payload: object) -> VerifiedPlanRecord:
        keys = {"id", "project_key", "file_path", "content_hash"}
        if (
            not isinstance(payload, dict)
            or set(payload) != keys
            or not all(isinstance(payload[key], str) for key in keys)
        ):
            raise RepairSafetyError("invalid_verification_report")
        return cls(
            id=payload["id"],
            project_key=payload["project_key"],
            file_path=payload["file_path"],
            content_hash=payload["content_hash"],
        )


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Private proof tying verified canonical rows to snapshot and evidence."""

    version: int
    snapshot_sha256: str
    evidence_sha256: str
    evidence: ReindexEvidence
    canonical_plans: tuple[VerifiedPlanRecord, ...]

    def __post_init__(self) -> None:
        if (
            self.version != 1
            or not _is_sha256_digest(self.snapshot_sha256)
            or not _is_sha256_digest(self.evidence_sha256)
            or not isinstance(self.evidence, ReindexEvidence)
            or not isinstance(self.canonical_plans, tuple)
            or not all(isinstance(plan, VerifiedPlanRecord) for plan in self.canonical_plans)
        ):
            raise RepairSafetyError("invalid_verification_report")
        if not hmac.compare_digest(
            self.evidence.snapshot_sha256, self.snapshot_sha256
        ) or not hmac.compare_digest(
            sha256_json(self.evidence.to_dict()),
            self.evidence_sha256,
        ):
            raise RepairSafetyError("invalid_verification_report")
        ids = {plan.id for plan in self.canonical_plans}
        paths = {plan.file_path for plan in self.canonical_plans}
        if len(ids) != len(self.canonical_plans) or len(paths) != len(self.canonical_plans):
            raise RepairSafetyError("verification_report_collision")
        object.__setattr__(
            self,
            "canonical_plans",
            tuple(
                sorted(
                    self.canonical_plans,
                    key=lambda plan: (plan.project_key, plan.file_path, plan.id),
                )
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "snapshot_sha256": self.snapshot_sha256,
            "evidence_sha256": self.evidence_sha256,
            "evidence": self.evidence.to_dict(),
            "canonical_plans": [plan.to_dict() for plan in self.canonical_plans],
        }

    @classmethod
    def from_dict(cls, payload: object) -> VerificationReport:
        keys = {
            "version",
            "snapshot_sha256",
            "evidence_sha256",
            "evidence",
            "canonical_plans",
        }
        if not isinstance(payload, dict) or set(payload) != keys:
            raise RepairSafetyError("invalid_verification_report")
        plans = payload["canonical_plans"]
        if (
            type(payload["version"]) is not int
            or not isinstance(payload["snapshot_sha256"], str)
            or not isinstance(payload["evidence_sha256"], str)
            or not isinstance(plans, list)
        ):
            raise RepairSafetyError("invalid_verification_report")
        try:
            evidence = ReindexEvidence.from_dict(payload["evidence"])
        except RepairSafetyError:
            raise RepairSafetyError("invalid_verification_report") from None
        return cls(
            version=payload["version"],
            snapshot_sha256=payload["snapshot_sha256"],
            evidence_sha256=payload["evidence_sha256"],
            evidence=evidence,
            canonical_plans=tuple(VerifiedPlanRecord.from_dict(item) for item in plans),
        )


@dataclass(frozen=True, slots=True)
class PhaseResult:
    """Content-safe summary of one bounded repair phase."""

    status: str
    affected_rows: int
    backup_receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.affected_rows) is not int or self.affected_rows < 0:
            raise RepairSafetyError("invalid_phase_result")
        if self.backup_receipt_sha256 is not None and not _is_sha256_digest(
            self.backup_receipt_sha256
        ):
            raise RepairSafetyError("invalid_phase_result")

    def to_dict(self) -> dict[str, object]:
        if self.status == "backup_restore_required":
            if self.backup_receipt_sha256 is None:
                raise RepairSafetyError("invalid_phase_result")
            return {
                "status": self.status,
                "backup_receipt_sha256": self.backup_receipt_sha256,
            }
        if self.backup_receipt_sha256 is not None:
            raise RepairSafetyError("invalid_phase_result")
        return {"status": self.status, "affected_rows": self.affected_rows}


def _parse_sequence(value: object, parser: Any) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise RepairSafetyError("invalid_snapshot_schema")
    return tuple(parser(item) for item in value)


def _require_record(payload: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != keys:
        raise RepairSafetyError("invalid_snapshot_schema")
    return payload


def _require_str(record: dict[str, Any], key: str) -> str:
    value = record[key]
    if not isinstance(value, str):
        raise RepairSafetyError("invalid_snapshot_schema")
    return value


def _require_int(record: dict[str, Any], key: str) -> int:
    value = record[key]
    if type(value) is not int:
        raise RepairSafetyError("invalid_snapshot_schema")
    return value


def _local_file_from_dict(payload: object) -> LocalPlanFile:
    record = _require_record(
        payload,
        {"project_key", "file_path", "content_hash", "size_bytes"},
    )
    return LocalPlanFile(
        project_key=_require_str(record, "project_key"),
        file_path=_require_str(record, "file_path"),
        content_hash=_require_str(record, "content_hash"),
        size_bytes=_require_int(record, "size_bytes"),
    )


def _indexed_plan_from_dict(payload: object) -> IndexedPlanRecord:
    keys = {
        "id",
        "project_key",
        "file_path",
        "content_hash",
        "status",
        "freshness_status",
        "declared_chunk_count",
        "observed_chunk_count",
    }
    record = _require_record(payload, keys)
    return IndexedPlanRecord(
        id=_require_str(record, "id"),
        project_key=_require_str(record, "project_key"),
        file_path=_require_str(record, "file_path"),
        content_hash=_require_str(record, "content_hash"),
        status=_require_str(record, "status"),
        freshness_status=_require_str(record, "freshness_status"),
        declared_chunk_count=_require_int(record, "declared_chunk_count"),
        observed_chunk_count=_require_int(record, "observed_chunk_count"),
    )


def _feature_link_from_dict(payload: object) -> FeatureLinkRecord:
    record = _require_record(
        payload,
        {"feature_id", "plan_id", "similarity_score", "created_at"},
    )
    score = record["similarity_score"]
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise RepairSafetyError("invalid_snapshot_schema")
    return FeatureLinkRecord(
        feature_id=_require_str(record, "feature_id"),
        plan_id=_require_str(record, "plan_id"),
        similarity_score=float(score),
        created_at=_require_str(record, "created_at"),
    )


def _collision_from_dict(payload: object) -> PlanCollisionRecord:
    keys = {
        "plan_id",
        "file_path",
        "expected_project_key",
        "actual_project_key",
        "expected_content_hash",
        "actual_content_hash",
        "reason_code",
    }
    record = _require_record(payload, keys)
    return PlanCollisionRecord(
        plan_id=_require_str(record, "plan_id"),
        file_path=_require_str(record, "file_path"),
        expected_project_key=_require_str(record, "expected_project_key"),
        actual_project_key=_require_str(record, "actual_project_key"),
        expected_content_hash=_require_str(record, "expected_content_hash"),
        actual_content_hash=_require_str(record, "actual_content_hash"),
        reason_code=_require_str(record, "reason_code"),
    )


def write_private_json(path: Path, payload: Mapping[str, object]) -> str:
    """Exclusively create, fsync, and return the digest of a private JSON file."""
    data = canonical_json(payload)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        parent_descriptor = os.open(path.parent, _DIRECTORY_OPEN_FLAGS)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except FileExistsError as exc:
        raise RepairSafetyError("private_file_exists") from exc
    except OSError as exc:
        raise RepairSafetyError("private_file_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return hashlib.sha256(data).hexdigest()


def _read_private_bytes(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise RepairSafetyError("private_file_type")
        if details.st_uid != os.getuid():
            raise RepairSafetyError("private_file_owner")
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise RepairSafetyError("private_file_mode")
        before_fingerprint = (
            details.st_dev,
            details.st_ino,
            details.st_mode,
            details.st_uid,
            details.st_gid,
            details.st_size,
            details.st_mtime_ns,
            details.st_ctime_ns,
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read()
            after = os.fstat(stream.fileno())
            after_fingerprint = (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_uid,
                after.st_gid,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        if before_fingerprint != after_fingerprint or len(data) != details.st_size:
            raise RepairSafetyError("private_file_changed") from None
        return data
    except FileNotFoundError as exc:
        raise RepairSafetyError("private_file_missing") from exc
    except RepairSafetyError:
        raise
    except OSError as exc:
        raise RepairSafetyError("private_file_unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_private_json_payload(
    path: Path,
    expected_sha256: str,
    *,
    invalid_reason: str,
) -> object:
    data = _read_private_bytes(path)
    actual_digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_digest, expected_sha256):
        raise RepairSafetyError("private_file_digest_mismatch")
    try:
        return json.loads(data)
    except (UnicodeError, json.JSONDecodeError):
        raise RepairSafetyError(invalid_reason) from None


def load_reindex_evidence(path: Path, expected_sha256: str) -> ReindexEvidence:
    """Load reindex evidence only through the private control-file boundary."""
    return ReindexEvidence.from_dict(
        _load_private_json_payload(
            path,
            expected_sha256,
            invalid_reason="invalid_reindex_evidence",
        )
    )


def load_verification_report(path: Path, expected_sha256: str) -> VerificationReport:
    """Load a verification report only through the private control-file boundary."""
    return VerificationReport.from_dict(
        _load_private_json_payload(
            path,
            expected_sha256,
            invalid_reason="invalid_verification_report",
        )
    )


def load_snapshot(path: Path, expected_sha256: str) -> RepairSnapshot:
    """Load one private snapshot only when its mode, owner and digest match."""
    data = _read_private_bytes(path)
    actual_digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_digest, expected_sha256):
        raise RepairSafetyError("private_file_digest_mismatch")
    try:
        payload = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RepairSafetyError("invalid_snapshot_schema") from exc
    return RepairSnapshot.from_dict(payload)


def validate_mutation_proof(
    snapshot_path: Path,
    snapshot_sha256: str,
    backup_receipt_path: Path,
    backup_receipt_sha256: str,
    postgres_restore_tested: bool,
    writers_off_confirmed: bool,
) -> tuple[RepairSnapshot, MutationProof]:
    """Validate both private files and attestations before any mutating session."""
    if postgres_restore_tested is not True:
        raise RepairSafetyError("postgres_restore_not_tested")
    if writers_off_confirmed is not True:
        raise RepairSafetyError("writers_off_not_confirmed")

    snapshot = load_snapshot(snapshot_path, snapshot_sha256)
    backup_receipt = _read_private_bytes(backup_receipt_path)
    actual_backup_digest = hashlib.sha256(backup_receipt).hexdigest()
    if not hmac.compare_digest(actual_backup_digest, backup_receipt_sha256):
        raise RepairSafetyError("private_file_digest_mismatch")

    return snapshot, MutationProof(
        snapshot_sha256=snapshot_sha256,
        backup_receipt_sha256=backup_receipt_sha256,
        postgres_restore_tested=True,
        writers_off_confirmed=True,
    )


def build_repair_snapshot(
    *,
    manifest: RepairManifest,
    local_files: tuple[LocalPlanFile, ...],
    contexts: tuple[ContextRecord, ...],
    plans: tuple[IndexedPlanRecord, ...],
    feature_links: tuple[FeatureLinkRecord, ...],
    alembic_revision: str,
    database_identity_hash: str,
    mutation_timestamp: str,
) -> RepairSnapshot:
    """Classify exact polluted, missing, and colliding owner/path/hash tuples."""
    target_keys = {project.project_key for project in manifest.projects}
    local_by_owner_path = {(item.project_key, item.file_path): item for item in local_files}
    local_by_path = {item.file_path: item for item in local_files}
    exact_db_tuples = {(item.project_key, item.file_path, item.content_hash) for item in plans}

    polluted = tuple(
        sorted(
            item.id
            for item in plans
            if item.project_key in target_keys
            and (item.project_key, item.file_path) not in local_by_owner_path
        )
    )
    missing = tuple(
        sorted(
            (
                item
                for item in local_files
                if (item.project_key, item.file_path, item.content_hash) not in exact_db_tuples
            ),
            key=lambda item: (item.project_key, item.file_path),
        )
    )

    collisions: list[PlanCollisionRecord] = []
    for plan in plans:
        expected = local_by_path.get(plan.file_path)
        if expected is None:
            continue
        reason_code: str | None = None
        if plan.project_key != expected.project_key:
            reason_code = "project_owner_mismatch"
        elif plan.content_hash != expected.content_hash:
            reason_code = "content_hash_mismatch"
        if reason_code is not None:
            collisions.append(
                PlanCollisionRecord(
                    plan_id=plan.id,
                    file_path=plan.file_path,
                    expected_project_key=expected.project_key,
                    actual_project_key=plan.project_key,
                    expected_content_hash=expected.content_hash,
                    actual_content_hash=plan.content_hash,
                    reason_code=reason_code,
                )
            )

    return RepairSnapshot(
        version=1,
        mutation_timestamp=mutation_timestamp,
        database_identity_hash=database_identity_hash,
        alembic_revision=alembic_revision,
        contexts=tuple(sorted(contexts, key=lambda item: item.project_key)),
        local_files=tuple(sorted(local_files, key=lambda item: (item.project_key, item.file_path))),
        indexed_plans=tuple(sorted(plans, key=lambda item: item.id)),
        feature_links=tuple(
            sorted(feature_links, key=lambda item: (item.plan_id, item.feature_id))
        ),
        polluted_plan_ids=polluted,
        missing_canonical_files=missing,
        collisions=tuple(sorted(collisions, key=lambda item: (item.file_path, item.plan_id))),
    )
