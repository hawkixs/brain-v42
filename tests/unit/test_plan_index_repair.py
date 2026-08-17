"""Unit tests for the canonical multi-project plan-index repair boundary."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import BinaryIO, TypedDict, cast
from unittest.mock import patch
from uuid import UUID

import pytest
from pydantic import ValidationError

from brain_v42.config import Settings
from brain_v42.maintenance import plan_index_repair
from brain_v42.maintenance.plan_index_repair import (
    ContextRecord,
    FeatureLinkRecord,
    IndexedPlanRecord,
    LocalPlanFile,
    ProjectTarget,
    RepairSafetyError,
    RepairSnapshot,
    build_repair_snapshot,
    canonical_json,
    database_identity_fingerprint,
    discover_local_files,
    load_snapshot,
    sha256_json,
    write_private_json,
)
from brain_v42.maintenance.plan_index_repair import (
    load_manifest as public_load_manifest,
)


class ProjectPayload(TypedDict):
    project_key: str
    project_root: str
    scan_paths: list[str]


def _write_manifest(tmp_path: Path, projects: object, **extra: object) -> Path:
    manifest_path = tmp_path / "manifest.json"
    payload: dict[str, object] = {"version": 1, "projects": projects, **extra}
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


def _project(
    tmp_path: Path,
    project_key: str = "red-phone",
    scan_names: tuple[str, ...] = ("docs/specs", "docs/plans"),
) -> ProjectPayload:
    project_root = tmp_path / project_key
    project_root.mkdir()
    scan_paths: list[str] = []
    for name in scan_names:
        scan_path = project_root / name
        scan_path.mkdir(parents=True)
        scan_paths.append(str(scan_path))
    return {
        "project_key": project_key,
        "project_root": str(project_root),
        "scan_paths": scan_paths,
    }


def _trusted_projects(*projects: ProjectPayload) -> dict[str, ProjectTarget]:
    return {
        str(project["project_key"]): ProjectTarget(
            project_key=str(project["project_key"]),
            project_root=Path(project["project_root"]).resolve(),
            scan_paths=tuple(Path(path).resolve() for path in project["scan_paths"]),
        )
        for project in projects
    }


_DEFAULT_ALLOWED_PROJECT_KEYS = object()


def load_manifest(
    path: Path,
    *,
    allowed_project_keys: frozenset[str] | object = _DEFAULT_ALLOWED_PROJECT_KEYS,
    trusted_projects: Mapping[str, ProjectTarget] | None = None,
) -> plan_index_repair.RepairManifest:
    """Call the public loader with a test-local canonical mapping override."""
    if trusted_projects is None:
        if allowed_project_keys is _DEFAULT_ALLOWED_PROJECT_KEYS:
            return public_load_manifest(path)
        return public_load_manifest(
            path,
            allowed_project_keys=cast(frozenset[str], allowed_project_keys),
        )

    effective_allowed_project_keys = (
        frozenset(trusted_projects)
        if allowed_project_keys is _DEFAULT_ALLOWED_PROJECT_KEYS
        else cast(frozenset[str], allowed_project_keys)
    )
    with patch.object(plan_index_repair, "TARGET_PROJECTS", trusted_projects):
        return public_load_manifest(
            path,
            allowed_project_keys=effective_allowed_project_keys,
        )


def test_load_manifest_requires_exact_ticket_projects(tmp_path: Path) -> None:
    """Accepting a partial production manifest must make this test fail."""
    manifest_path = _write_manifest(tmp_path, projects=[])

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(manifest_path)

    assert exc.value.reason_code == "project_set_mismatch"


def test_load_manifest_accepts_injected_isolated_project_set(tmp_path: Path) -> None:
    """Ignoring the explicit test allow-list must make isolated tests unusable."""
    project = _project(tmp_path)
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )

    assert manifest.version == 1
    assert tuple(target.project_key for target in manifest.projects) == ("red-phone",)
    assert all(path.is_absolute() for path in manifest.projects[0].scan_paths)


def test_load_manifest_accepts_allowed_keys_with_matching_trusted_projects(
    tmp_path: Path,
) -> None:
    """Removing the planned allow-list keyword must make this test fail."""
    project = _project(tmp_path)

    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        allowed_project_keys=frozenset({"red-phone"}),
        trusted_projects=_trusted_projects(project),
    )

    assert tuple(target.project_key for target in manifest.projects) == ("red-phone",)


def test_load_manifest_accepts_allowed_keys_only_for_canonical_targets(
    tmp_path: Path,
) -> None:
    """Breaking the historical allow-list-only call must make this test fail."""
    project = _project(tmp_path)

    with patch(
        "brain_v42.maintenance.plan_index_repair.TARGET_PROJECTS",
        _trusted_projects(project),
    ):
        manifest = load_manifest(
            _write_manifest(tmp_path, [project]),
            allowed_project_keys=frozenset({"red-phone"}),
        )

    assert tuple(target.project_key for target in manifest.projects) == ("red-phone",)


def test_load_manifest_allowed_keys_only_rejects_arbitrary_root(tmp_path: Path) -> None:
    """Treating an allowed key as trust in its manifest root must make this test fail."""
    trusted = _project(tmp_path, scan_names=("docs/plans",))
    arbitrary = _project(tmp_path, project_key="arbitrary", scan_names=("docs/plans",))
    arbitrary["project_key"] = "red-phone"

    with patch(
        "brain_v42.maintenance.plan_index_repair.TARGET_PROJECTS",
        _trusted_projects(trusted),
    ):
        with pytest.raises(RepairSafetyError) as exc:
            load_manifest(
                _write_manifest(tmp_path, [arbitrary]),
                allowed_project_keys=frozenset({"red-phone"}),
            )

    assert exc.value.reason_code == "project_root_mismatch"


def test_load_manifest_does_not_expose_trusted_projects_override(tmp_path: Path) -> None:
    """Allowing callers to replace the canonical authority must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    call_with_unreviewed_keyword = cast(Callable[..., object], public_load_manifest)

    with pytest.raises(TypeError):
        call_with_unreviewed_keyword(
            _write_manifest(tmp_path, [project]),
            allowed_project_keys=frozenset({"red-phone"}),
            trusted_projects=_trusted_projects(project),
        )


def test_load_manifest_rejects_unknown_schema_field(tmp_path: Path) -> None:
    """Silently accepting an unreviewed top-level field must make this test fail."""
    project = _project(tmp_path)

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(
            _write_manifest(tmp_path, [project], unreviewed=True),
            trusted_projects=_trusted_projects(project),
        )

    assert exc.value.reason_code == "invalid_manifest_schema"


def test_load_manifest_rejects_unknown_version(tmp_path: Path) -> None:
    """Accepting a manifest version other than one must make this test fail."""
    project = _project(tmp_path)
    manifest_path = _write_manifest(tmp_path, [project])
    manifest_path.write_text(
        json.dumps({"version": 2, "projects": [project]}),
        encoding="utf-8",
    )

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(manifest_path, trusted_projects=_trusted_projects(project))

    assert exc.value.reason_code == "unsupported_manifest_version"


def test_load_manifest_rejects_duplicate_project_keys(tmp_path: Path) -> None:
    """Letting a later duplicate project override the first must make this test fail."""
    project = _project(tmp_path)

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(
            _write_manifest(tmp_path, [project, project]),
            trusted_projects=_trusted_projects(project),
        )

    assert exc.value.reason_code == "duplicate_project_key"


def test_load_manifest_rejects_duplicate_canonical_scan_paths(tmp_path: Path) -> None:
    """Assigning one canonical scan directory twice must make this test fail."""
    shared = tmp_path / "shared" / "docs"
    shared.mkdir(parents=True)
    trusted: ProjectPayload = {
        "project_key": "red-phone",
        "project_root": str(tmp_path),
        "scan_paths": [str(shared)],
    }
    projects = [
        {
            **trusted,
            "scan_paths": [str(shared), str(shared / ".." / "docs")],
        }
    ]

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(
            _write_manifest(tmp_path, projects),
            trusted_projects=_trusted_projects(trusted),
        )

    assert exc.value.reason_code == "duplicate_scan_path"


def test_load_manifest_rejects_relative_project_root(tmp_path: Path) -> None:
    """Resolving a project root from the process cwd must make this test fail."""
    trusted = _project(tmp_path, scan_names=("docs/plans",))
    project = {
        "project_key": "red-phone",
        "project_root": "relative/root",
        "scan_paths": [str(tmp_path)],
    }

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(
            _write_manifest(tmp_path, [project]),
            trusted_projects=_trusted_projects(trusted),
        )

    assert exc.value.reason_code == "relative_project_root"


def test_load_manifest_rejects_relative_scan_path(tmp_path: Path) -> None:
    """Resolving a scan path from the process cwd must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    trusted_projects = _trusted_projects(project)
    project["scan_paths"] = ["docs/plans"]

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(
            _write_manifest(tmp_path, [project]),
            trusted_projects=trusted_projects,
        )

    assert exc.value.reason_code == "relative_scan_path"


def test_load_manifest_rejects_missing_path(tmp_path: Path) -> None:
    """Accepting a missing scan directory must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    trusted_projects = _trusted_projects(project)
    project["scan_paths"] = [str(tmp_path / "red-phone" / "missing")]

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(
            _write_manifest(tmp_path, [project]),
            trusted_projects=trusted_projects,
        )

    assert exc.value.reason_code == "path_missing"


def test_load_manifest_rejects_regular_file(tmp_path: Path) -> None:
    """Accepting a regular file as a scan directory must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    trusted_projects = _trusted_projects(project)
    scan_file = tmp_path / "red-phone" / "docs-plan"
    scan_file.write_text("not a directory", encoding="utf-8")
    project["scan_paths"] = [str(scan_file)]

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(
            _write_manifest(tmp_path, [project]),
            trusted_projects=trusted_projects,
        )

    assert exc.value.reason_code == "path_not_directory"


def test_load_manifest_rejects_unreadable_path(tmp_path: Path) -> None:
    """Dropping read/search permission checks must make this test fail."""
    project = _project(tmp_path)

    with patch("brain_v42.maintenance.plan_index_repair.os.access", return_value=False):
        with pytest.raises(RepairSafetyError) as exc:
            load_manifest(
                _write_manifest(tmp_path, [project]),
                trusted_projects=_trusted_projects(project),
            )

    assert exc.value.reason_code == "path_unreadable"


def test_load_manifest_rejects_scan_symlink_escaping_project_root(tmp_path: Path) -> None:
    """Following a scan-directory symlink outside its owner root must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    trusted_projects = _trusted_projects(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = tmp_path / "red-phone" / "escaped"
    escaped.symlink_to(outside, target_is_directory=True)
    project["scan_paths"] = [str(escaped)]

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(
            _write_manifest(tmp_path, [project]),
            trusted_projects=trusted_projects,
        )

    assert exc.value.reason_code == "scan_path_outside_root"


def test_load_manifest_rejects_project_roots_swapped_between_keys(tmp_path: Path) -> None:
    """Binding each trusted root to the wrong project key must make this test fail."""
    phone = _project(tmp_path, project_key="red-phone", scan_names=("docs/plans",))
    gift = _project(tmp_path, project_key="red-gift", scan_names=("docs/plans",))
    trusted_projects = _trusted_projects(phone, gift)
    phone["project_root"], gift["project_root"] = gift["project_root"], phone["project_root"]
    phone["scan_paths"], gift["scan_paths"] = gift["scan_paths"], phone["scan_paths"]

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(
            _write_manifest(tmp_path, [phone, gift]),
            trusted_projects=trusted_projects,
        )

    assert exc.value.reason_code == "project_root_mismatch"


def test_load_manifest_rejects_arbitrary_root_for_trusted_key(tmp_path: Path) -> None:
    """Replacing an owned project root with an arbitrary directory must make this test fail."""
    trusted = _project(tmp_path, scan_names=("docs/plans",))
    arbitrary = _project(tmp_path, project_key="arbitrary", scan_names=("docs/plans",))
    arbitrary["project_key"] = "red-phone"

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(
            _write_manifest(tmp_path, [arbitrary]),
            trusted_projects=_trusted_projects(trusted),
        )

    assert exc.value.reason_code == "project_root_mismatch"


def test_load_manifest_rejects_unowned_scan_path_under_trusted_root(tmp_path: Path) -> None:
    """Adding an undeclared scan directory below an owned root must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    trusted_projects = _trusted_projects(project)
    unexpected = Path(project["project_root"]) / "docs" / "unexpected"
    unexpected.mkdir()
    project["scan_paths"] = [*project["scan_paths"], str(unexpected)]

    with pytest.raises(RepairSafetyError) as exc:
        load_manifest(
            _write_manifest(tmp_path, [project]),
            trusted_projects=trusted_projects,
        )

    assert exc.value.reason_code == "scan_path_set_mismatch"


def test_discover_local_files_returns_deterministic_canonical_inventory(tmp_path: Path) -> None:
    """Missing suffix filtering, ownership, hashing, or ordering must make this test fail."""
    project = _project(tmp_path)
    specs = Path(project["scan_paths"][0])
    plans = Path(project["scan_paths"][1])
    design = specs / "zeta-design.md"
    plan = plans / "alpha-plan.md"
    design.write_bytes(b"# Zeta\n")
    plan.write_bytes(b"# Alpha\n")
    (plans / "notes.md").write_bytes(b"ignored")
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )

    files = discover_local_files(manifest)

    assert [item.file_path for item in files] == sorted(
        [str(design.resolve()), str(plan.resolve())]
    )
    by_path = {item.file_path: item for item in files}
    assert by_path[str(design.resolve())].project_key == "red-phone"
    assert by_path[str(design.resolve())].content_hash == hashlib.sha256(b"# Zeta\n").hexdigest()
    assert by_path[str(design.resolve())].size_bytes == len(b"# Zeta\n")
    assert by_path[str(plan.resolve())].content_hash == hashlib.sha256(b"# Alpha\n").hexdigest()


def test_discover_local_files_deduplicates_overlapping_scan_paths(tmp_path: Path) -> None:
    """Returning one file twice through nested scan roots must make this test fail."""
    project = _project(tmp_path, scan_names=("docs", "docs/plans"))
    plan = tmp_path / "red-phone" / "docs" / "plans" / "once-plan.md"
    plan.write_text("# Once", encoding="utf-8")
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )

    files = discover_local_files(manifest)

    assert [(item.project_key, item.file_path) for item in files] == [
        ("red-phone", str(plan.resolve()))
    ]


@pytest.mark.parametrize("swap_target", ["project_root", "scan_path"])
def test_discover_local_files_rejects_regular_initial_directory_swap(
    tmp_path: Path,
    swap_target: str,
) -> None:
    """Scanning a regular replacement after manifest validation must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    project_root = Path(project["project_root"])
    scan_path = Path(project["scan_paths"][0])
    (scan_path / "owned-plan.md").write_bytes(b"owned")

    with patch(
        "brain_v42.maintenance.plan_index_repair.TARGET_PROJECTS",
        _trusted_projects(project),
    ):
        manifest = public_load_manifest(
            _write_manifest(tmp_path, [project]),
            allowed_project_keys=frozenset({"red-phone"}),
        )

    if swap_target == "project_root":
        parked = tmp_path / "parked-project-root-before-scan"
        replacement = tmp_path / "replacement-project-root"
        replacement_scan_path = replacement / "docs" / "plans"
        replacement_scan_path.mkdir(parents=True)
        (replacement_scan_path / "replacement-plan.md").write_bytes(b"replacement")
        project_root.rename(parked)
        replacement.rename(project_root)
    else:
        parked = tmp_path / "parked-scan-path-before-scan"
        replacement = tmp_path / "replacement-scan-path"
        replacement.mkdir()
        (replacement / "replacement-plan.md").write_bytes(b"replacement")
        scan_path.rename(parked)
        replacement.rename(scan_path)

    with pytest.raises(RepairSafetyError) as exc:
        discover_local_files(manifest)

    assert exc.value.reason_code == "directory_unreadable"


def test_discover_local_files_rejects_symlinked_file_outside_root(tmp_path: Path) -> None:
    """Following a symlinked plan outside its owner root must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    outside = tmp_path / "outside-design.md"
    outside.write_text("# Outside", encoding="utf-8")
    linked = tmp_path / "red-phone" / "docs" / "plans" / "linked-design.md"
    linked.symlink_to(outside)
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )

    with pytest.raises(RepairSafetyError) as exc:
        discover_local_files(manifest)

    assert exc.value.reason_code == "file_symlink"


def test_discover_local_files_rejects_root_swap_between_scan_and_hash(tmp_path: Path) -> None:
    """Following a replaced project root while hashing must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    project_root = Path(project["project_root"])
    plan = Path(project["scan_paths"][0]) / "owned-plan.md"
    plan.write_bytes(b"owned")
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )
    parked_root = tmp_path / "parked-project-root"
    attacker_root = tmp_path / "attacker-root"
    attacker_scan_path = attacker_root / "docs" / "plans"
    attacker_scan_path.mkdir(parents=True)
    (attacker_scan_path / plan.name).write_bytes(b"attacker")
    real_scan = cast(
        Callable[..., tuple[Path, ...]],
        plan_index_repair._scan_plan_files,
    )

    def scan_then_replace_root(*paths: Path) -> Iterator[Path]:
        candidates = tuple(real_scan(*paths))
        project_root.rename(parked_root)
        project_root.symlink_to(attacker_root, target_is_directory=True)
        yield from candidates

    with patch(
        "brain_v42.maintenance.plan_index_repair._scan_plan_files",
        side_effect=scan_then_replace_root,
    ):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "file_unreadable"


def test_discover_local_files_rejects_regular_root_swap_before_hash(tmp_path: Path) -> None:
    """Hashing through a regular replacement root must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    project_root = Path(project["project_root"])
    plan = Path(project["scan_paths"][0]) / "owned-plan.md"
    plan.write_bytes(b"owned")
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )
    parked_root = tmp_path / "parked-regular-project-root"
    attacker_root = tmp_path / "regular-attacker-root"
    attacker_scan_path = attacker_root / "docs" / "plans"
    attacker_scan_path.mkdir(parents=True)
    (attacker_scan_path / plan.name).write_bytes(b"attacker")
    real_scan = cast(
        Callable[..., tuple[Path, ...]],
        plan_index_repair._scan_plan_files,
    )

    def scan_then_replace_root(*paths: Path) -> Iterator[Path]:
        candidates = tuple(real_scan(*paths))
        project_root.rename(parked_root)
        attacker_root.rename(project_root)
        yield from candidates

    with patch(
        "brain_v42.maintenance.plan_index_repair._scan_plan_files",
        side_effect=scan_then_replace_root,
    ):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "file_unreadable"


def test_discover_local_files_rejects_subdirectory_swap_before_descent(tmp_path: Path) -> None:
    """Following a directory replaced after inspection must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    scan_path = Path(project["scan_paths"][0])
    replaced = scan_path / "replace-me"
    replaced.mkdir()
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )
    parked = tmp_path / "parked-subdirectory"
    attacker = tmp_path / "attacker-subdirectory"
    attacker.mkdir()
    (attacker / "escaped-plan.md").write_bytes(b"attacker")
    real_open = os.open
    real_scandir = os.scandir
    swapped = False

    def replace_subdirectory() -> None:
        nonlocal swapped
        replaced.rename(parked)
        replaced.symlink_to(attacker, target_is_directory=True)
        swapped = True

    def racing_scandir(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
    ) -> object:
        if not swapped and not isinstance(path, int):
            path_value = os.fspath(path)
            if isinstance(path_value, str) and Path(path_value) == replaced:
                replace_subdirectory()
        return real_scandir(path)

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        path_value = os.fspath(path)
        if (
            not swapped
            and dir_fd is not None
            and flags & os.O_DIRECTORY
            and path_value == replaced.name
        ):
            replace_subdirectory()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    with (
        patch(
            "brain_v42.maintenance.plan_index_repair.os.scandir",
            side_effect=racing_scandir,
        ),
        patch(
            "brain_v42.maintenance.plan_index_repair.os.open",
            side_effect=racing_open,
        ),
    ):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "directory_unreadable"


def test_discover_local_files_rejects_regular_subdirectory_swap_before_descent(
    tmp_path: Path,
) -> None:
    """Descending into a regular replacement directory must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    scan_path = Path(project["scan_paths"][0])
    replaced = scan_path / "replace-regular"
    replaced.mkdir()
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )
    parked = tmp_path / "parked-regular-subdirectory"
    attacker = tmp_path / "regular-attacker-subdirectory"
    attacker.mkdir()
    (attacker / "escaped-plan.md").write_bytes(b"attacker")
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        path_value = os.fspath(path)
        if (
            not swapped
            and dir_fd is not None
            and flags & os.O_DIRECTORY
            and path_value == replaced.name
        ):
            replaced.rename(parked)
            attacker.rename(replaced)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    with patch(
        "brain_v42.maintenance.plan_index_repair.os.open",
        side_effect=racing_open,
    ):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "directory_unreadable"


def test_discover_local_files_rejects_regular_file_swap_before_hash(
    tmp_path: Path,
) -> None:
    """Hashing a regular replacement file must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    plan = Path(project["scan_paths"][0]) / "owned-plan.md"
    plan.write_bytes(b"owned")
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )
    parked = tmp_path / "parked-owned-plan.md"
    attacker = tmp_path / "attacker-plan.md"
    attacker.write_bytes(b"attacker")
    real_scan = cast(
        Callable[..., tuple[Path, ...]],
        plan_index_repair._scan_plan_files,
    )

    def scan_then_replace_file(*paths: Path) -> Iterator[Path]:
        candidates = tuple(real_scan(*paths))
        plan.rename(parked)
        attacker.rename(plan)
        yield from candidates

    with patch(
        "brain_v42.maintenance.plan_index_repair._scan_plan_files",
        side_effect=scan_then_replace_file,
    ):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "file_unreadable"


def test_discover_local_files_translates_root_scandir_error(tmp_path: Path) -> None:
    """Leaking a raw scan error instead of a stable domain error must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )

    with patch(
        "brain_v42.maintenance.plan_index_repair.os.scandir",
        side_effect=PermissionError("denied"),
    ):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "directory_unreadable"


def test_discover_local_files_fails_closed_on_subdirectory_scan_error(tmp_path: Path) -> None:
    """Returning files found before an unreadable subtree must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    scan_path = Path(project["scan_paths"][0])
    (scan_path / "visible-plan.md").write_text("# Visible", encoding="utf-8")
    blocked = scan_path / "blocked"
    blocked.mkdir()
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )
    real_scandir = os.scandir
    blocked_stat = blocked.stat()
    blocked_identity = (blocked_stat.st_dev, blocked_stat.st_ino)

    def deny_blocked(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
    ) -> object:
        if isinstance(path, int):
            path_stat = os.fstat(path)
            is_blocked = (path_stat.st_dev, path_stat.st_ino) == blocked_identity
        else:
            path_value = os.fspath(path)
            is_blocked = (
                Path(path_value) == blocked
                if isinstance(path_value, str)
                else path_value == os.fsencode(blocked)
            )
        if is_blocked:
            raise PermissionError("denied")
        return real_scandir(path)

    with patch(
        "brain_v42.maintenance.plan_index_repair.os.scandir",
        side_effect=deny_blocked,
    ):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "directory_unreadable"


def test_discover_local_files_rejects_file_over_size_limit(tmp_path: Path) -> None:
    """Hashing a file beyond the explicit per-file budget must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    (Path(project["scan_paths"][0]) / "large-plan.md").write_bytes(b"1234")
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )

    with patch(
        "brain_v42.maintenance.plan_index_repair.MAX_PLAN_FILE_BYTES",
        3,
        create=True,
    ):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "file_size_limit_exceeded"


def test_discover_local_files_rejects_cumulative_inventory_over_limit(tmp_path: Path) -> None:
    """Hashing files beyond the total inventory budget must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    scan_path = Path(project["scan_paths"][0])
    (scan_path / "first-plan.md").write_bytes(b"123")
    (scan_path / "second-plan.md").write_bytes(b"456")
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )

    with (
        patch(
            "brain_v42.maintenance.plan_index_repair.MAX_PLAN_FILE_BYTES",
            4,
            create=True,
        ),
        patch(
            "brain_v42.maintenance.plan_index_repair.MAX_INVENTORY_BYTES",
            5,
            create=True,
        ),
    ):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "inventory_size_limit_exceeded"


def test_discover_local_files_rejects_inventory_over_file_count_limit(tmp_path: Path) -> None:
    """Building an unbounded zero-byte inventory must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    scan_path = Path(project["scan_paths"][0])
    (scan_path / "first-plan.md").touch()
    (scan_path / "second-plan.md").touch()
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )

    with patch("brain_v42.maintenance.plan_index_repair.MAX_INVENTORY_FILES", 1):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "inventory_file_limit_exceeded"


def test_discover_local_files_stops_candidate_enumeration_at_file_limit(
    tmp_path: Path,
) -> None:
    """Materializing candidates past the file limit must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    scan_path = Path(project["scan_paths"][0])
    first = scan_path / "first-plan.md"
    second = scan_path / "second-plan.md"
    first.touch()
    second.touch()
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )

    def guarded_candidates() -> Iterator[object]:
        first_stat = first.stat()
        second_stat = second.stat()
        yield plan_index_repair._PlanFileCandidate(
            path=first,
            identity=(first_stat.st_dev, first_stat.st_ino),
        )
        yield plan_index_repair._PlanFileCandidate(
            path=second,
            identity=(second_stat.st_dev, second_stat.st_ino),
        )
        raise AssertionError("candidate enumeration continued past the limit")

    with (
        patch(
            "brain_v42.maintenance.plan_index_repair._scan_plan_files",
            return_value=guarded_candidates(),
        ),
        patch("brain_v42.maintenance.plan_index_repair.MAX_INVENTORY_FILES", 1),
    ):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "inventory_file_limit_exceeded"


def test_discover_local_files_stops_before_descending_past_directory_limit(
    tmp_path: Path,
) -> None:
    """Queuing an unbounded empty directory tree must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    scan_path = Path(project["scan_paths"][0])
    (scan_path / "first-empty").mkdir()
    (scan_path / "second-empty").mkdir()
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )
    scan_stat = scan_path.stat()
    scan_identity = (scan_stat.st_dev, scan_stat.st_ino)
    real_scandir = os.scandir

    def reject_child_descent(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
    ) -> object:
        if isinstance(path, int):
            path_stat = os.fstat(path)
            if (path_stat.st_dev, path_stat.st_ino) != scan_identity:
                raise AssertionError("scanner descended after exhausting directory budget")
        return real_scandir(path)

    with (
        patch(
            "brain_v42.maintenance.plan_index_repair.os.scandir",
            side_effect=reject_child_descent,
        ),
        patch(
            "brain_v42.maintenance.plan_index_repair.MAX_INVENTORY_DIRECTORIES",
            1,
            create=True,
        ),
    ):
        with pytest.raises(RepairSafetyError) as exc:
            discover_local_files(manifest)

    assert exc.value.reason_code == "inventory_directory_limit_exceeded"


def test_discover_local_files_reads_hash_input_in_bounded_chunks(tmp_path: Path) -> None:
    """Using read_bytes or an unbounded stream read must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    content = b"bounded-hash-input"
    plan = Path(project["scan_paths"][0]) / "bounded-plan.md"
    plan.write_bytes(content)
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )
    real_fdopen = os.fdopen
    requested_sizes: list[int] = []

    class BoundedReader:
        def __init__(self, stream: BinaryIO) -> None:
            self._stream = stream

        def __enter__(self) -> BoundedReader:
            return self

        def __exit__(self, *_args: object) -> None:
            self._stream.close()

        def fileno(self) -> int:
            return self._stream.fileno()

        def read(self, size: int = -1) -> bytes:
            if size <= 0:
                raise AssertionError("hash input read was unbounded")
            requested_sizes.append(size)
            return self._stream.read(size)

    def guarded_fdopen(descriptor: int, mode: str) -> BoundedReader:
        return BoundedReader(cast(BinaryIO, real_fdopen(descriptor, mode)))

    with (
        patch(
            "brain_v42.maintenance.plan_index_repair.os.fdopen",
            side_effect=guarded_fdopen,
        ),
        patch("brain_v42.maintenance.plan_index_repair.HASH_CHUNK_SIZE_BYTES", 3),
        patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("read_bytes must not be used for hashing"),
        ),
    ):
        files = discover_local_files(manifest)

    assert requested_sizes
    assert set(requested_sizes) == {3}
    assert files[0].content_hash == hashlib.sha256(content).hexdigest()


def test_discover_local_files_hashes_and_counts_overlapping_file_once(
    tmp_path: Path,
) -> None:
    """Rehashing or recounting an overlapping file must make this test fail."""
    project = _project(tmp_path, scan_names=("docs", "docs/plans"))
    content = b"count-once"
    plan = tmp_path / "red-phone" / "docs" / "plans" / "once-plan.md"
    plan.write_bytes(content)
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )
    real_hash = plan_index_repair._hash_plan_file
    hash_calls: list[tuple[Path, int]] = []

    def recording_hash(
        path: Path,
        *,
        inventory_bytes: int,
        expected_identity: tuple[int, int],
    ) -> tuple[str, int]:
        hash_calls.append((path, inventory_bytes))
        return real_hash(
            path,
            inventory_bytes=inventory_bytes,
            expected_identity=expected_identity,
        )

    with (
        patch(
            "brain_v42.maintenance.plan_index_repair._hash_plan_file",
            side_effect=recording_hash,
        ),
        patch(
            "brain_v42.maintenance.plan_index_repair.MAX_INVENTORY_BYTES",
            len(content),
        ),
    ):
        files = discover_local_files(manifest)

    assert [(item.project_key, item.file_path) for item in files] == [("red-phone", str(plan))]
    assert hash_calls == [(plan, 0)]


def test_discover_local_files_streaming_hash_matches_sha256(tmp_path: Path) -> None:
    """Changing chunked hashing semantics from SHA-256 must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    content = b"chunked-content"
    plan = Path(project["scan_paths"][0]) / "chunked-plan.md"
    plan.write_bytes(content)
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )

    with patch(
        "brain_v42.maintenance.plan_index_repair.HASH_CHUNK_SIZE_BYTES",
        3,
        create=True,
    ):
        files = discover_local_files(manifest)

    assert files[0].content_hash == hashlib.sha256(content).hexdigest()
    assert files[0].size_bytes == len(content)


def _snapshot_fixture() -> RepairSnapshot:
    context = ContextRecord.from_values(
        {
            "id": UUID("11111111-1111-1111-1111-111111111111"),
            "project_key": "red-phone",
            "name": "Phone",
            "description": "Context",
            "languages": ["Python"],
            "frameworks": [],
            "databases": ["PostgreSQL"],
            "code_style": None,
            "git_workflow": "trunk",
            "test_strategy": "pytest",
            "current_phase": "delivery",
            "current_focus": "repair",
            "focus_revision": 7,
            "blockers": [],
            "related_projects": [],
            "local_path": "/srv/red-phone",
            "repo_url": None,
            "decisions_count": 1,
            "learnings_count": 2,
            "snippets_count": 3,
            "runbooks_count": 4,
            "adrs_count": 5,
            "metadata": {"owner": "red-phone"},
            "plan_scan_paths": ["docs/plans"],
            "gitlab_project_path": None,
            "project_group": "red",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 7, 1, tzinfo=UTC),
        },
        proposed_plan_scan_paths=("/srv/red-phone/docs/plans",),
    )
    local = LocalPlanFile(
        project_key="red-phone",
        file_path="/srv/red-phone/docs/plans/one-plan.md",
        content_hash="a" * 64,
        size_bytes=42,
    )
    plan = IndexedPlanRecord(
        id="22222222-2222-2222-2222-222222222222",
        project_key="red-phone",
        file_path="docs/plans/one-plan.md",
        content_hash="a" * 64,
        status="active",
        freshness_status="fresh",
        declared_chunk_count=2,
        observed_chunk_count=2,
    )
    link = FeatureLinkRecord(
        feature_id="33333333-3333-3333-3333-333333333333",
        plan_id=plan.id,
        similarity_score=0.85,
        created_at="2026-07-01T00:00:00+00:00",
    )
    return RepairSnapshot(
        version=1,
        mutation_timestamp="2026-07-28T01:02:03+00:00",
        database_identity_hash="d" * 64,
        alembic_revision="037",
        contexts=(context,),
        local_files=(local,),
        indexed_plans=(plan,),
        feature_links=(link,),
        polluted_plan_ids=(plan.id,),
        missing_canonical_files=(local,),
        collisions=(),
    )


def test_canonical_json_is_stable_for_supported_database_values() -> None:
    """Depending on mapping order or Python object repr must make this test fail."""
    left = {
        "uuid": UUID("11111111-1111-1111-1111-111111111111"),
        "when": datetime(2026, 7, 28, 1, 2, 3, tzinfo=UTC),
        "mapping": {"z": 2, "a": 1},
    }
    right = {"mapping": {"a": 1, "z": 2}, "when": left["when"], "uuid": left["uuid"]}

    assert canonical_json(left) == canonical_json(right)
    assert sha256_json(left) == sha256_json(right)
    assert canonical_json(left) == (
        b'{"mapping":{"a":1,"z":2},"uuid":"11111111-1111-1111-1111-111111111111",'
        b'"when":"2026-07-28T01:02:03+00:00"}'
    )


def test_context_record_fingerprints_every_column() -> None:
    """Omitting an untouched context column from CAS must make this test fail."""
    first = ContextRecord.from_values(
        {"project_key": "red-phone", "name": "Phone", "metadata": {"revision": 1}},
        proposed_plan_scan_paths=("/srv/phone/docs/plans",),
    )
    drifted = ContextRecord.from_values(
        {"project_key": "red-phone", "name": "Phone", "metadata": {"revision": 2}},
        proposed_plan_scan_paths=("/srv/phone/docs/plans",),
    )

    assert first.values == {
        "metadata": {"revision": 1},
        "name": "Phone",
        "project_key": "red-phone",
    }
    assert first.fingerprint != drifted.fingerprint


def test_context_record_values_resist_nested_mutation_and_serialization_aliases() -> None:
    """Mutating nested values or serialized copies must not drift the signed CAS state."""
    record = ContextRecord.from_values(
        {"project_key": "red-phone", "metadata": {"revision": 1}},
        proposed_plan_scan_paths=("/srv/phone/docs/plans",),
    )
    original_fingerprint = record.fingerprint

    nested_values = cast(dict[str, object], record.values["metadata"])
    try:
        nested_values["revision"] = 2
    except TypeError:
        pass

    assert cast(Mapping[str, object], record.values["metadata"])["revision"] == 1
    assert record.fingerprint == original_fingerprint

    serialized_values = cast(dict[str, object], record.to_dict()["values"])
    cast(dict[str, object], serialized_values["metadata"])["revision"] = 3

    assert cast(Mapping[str, object], record.values["metadata"])["revision"] == 1
    assert record.fingerprint == original_fingerprint


def test_repair_snapshot_requires_canonical_utc_mutation_timestamp() -> None:
    """A non-UTC offset must be rejected by construction and private snapshot loading."""
    snapshot = _snapshot_fixture()
    non_utc_timestamp = "2026-07-28T03:02:03+02:00"

    with pytest.raises(RepairSafetyError) as construction_exc:
        replace(snapshot, mutation_timestamp=non_utc_timestamp)
    assert construction_exc.value.reason_code == "invalid_snapshot_schema"

    payload = snapshot.to_dict()
    payload["mutation_timestamp"] = non_utc_timestamp
    with pytest.raises(RepairSafetyError) as loading_exc:
        RepairSnapshot.from_dict(payload)
    assert loading_exc.value.reason_code == "invalid_snapshot_schema"


def test_write_private_json_creates_0600_file_and_roundtrips_snapshot(tmp_path: Path) -> None:
    """A permissive or non-deterministic control snapshot must make this test fail."""
    target = tmp_path / "snapshot.json"
    snapshot = _snapshot_fixture()

    digest = write_private_json(target, snapshot.to_dict())

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_uid == os.getuid()
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
    assert load_snapshot(target, digest) == snapshot


def test_write_private_json_refuses_to_overwrite(tmp_path: Path) -> None:
    """Overwriting the immutable control file must make this test fail."""
    target = tmp_path / "snapshot.json"
    target.write_text("existing", encoding="utf-8")

    with pytest.raises(RepairSafetyError) as exc:
        write_private_json(target, {"version": 1})

    assert exc.value.reason_code == "private_file_exists"
    assert target.read_text(encoding="utf-8") == "existing"


def test_write_private_json_accepts_read_only_mapping(tmp_path: Path) -> None:
    """Restricting the public payload contract to mutable dicts must make this test fail."""
    target = tmp_path / "snapshot.json"

    digest = write_private_json(target, MappingProxyType({"version": 1}))

    assert digest == hashlib.sha256(b'{"version":1}').hexdigest()


def test_load_snapshot_rejects_digest_mode_owner_and_type_drift(tmp_path: Path) -> None:
    """Trusting changed bytes, metadata, ownership, or file type must make this test fail."""
    target = tmp_path / "snapshot.json"
    snapshot = _snapshot_fixture()
    digest = write_private_json(target, snapshot.to_dict())

    with pytest.raises(RepairSafetyError) as digest_exc:
        load_snapshot(target, "0" * 64)
    assert digest_exc.value.reason_code == "private_file_digest_mismatch"

    target.chmod(0o640)
    with pytest.raises(RepairSafetyError) as mode_exc:
        load_snapshot(target, digest)
    assert mode_exc.value.reason_code == "private_file_mode"
    target.chmod(0o600)

    current_uid = os.getuid()
    with patch("brain_v42.maintenance.plan_index_repair.os.getuid", return_value=current_uid + 1):
        with pytest.raises(RepairSafetyError) as owner_exc:
            load_snapshot(target, digest)
    assert owner_exc.value.reason_code == "private_file_owner"

    directory = tmp_path / "not-a-snapshot"
    directory.mkdir()
    with pytest.raises(RepairSafetyError) as type_exc:
        load_snapshot(directory, digest)
    assert type_exc.value.reason_code == "private_file_type"


def test_build_repair_snapshot_classifies_pollution_missing_and_collisions(
    tmp_path: Path,
) -> None:
    """Misclassifying any owner/path/hash tuple must make this test fail."""
    project = _project(tmp_path, scan_names=("docs/plans",))
    manifest = load_manifest(
        _write_manifest(tmp_path, [project]),
        trusted_projects=_trusted_projects(project),
    )
    canonical_path = str((tmp_path / "red-phone/docs/plans/one-plan.md").resolve())
    local_files = (LocalPlanFile("red-phone", canonical_path, "a" * 64, 10),)
    contexts = (
        ContextRecord.from_values(
            {"project_key": "red-phone", "plan_scan_paths": ["docs/plans"]},
            proposed_plan_scan_paths=(str(tmp_path / "red-phone/docs/plans"),),
        ),
    )
    plans = (
        IndexedPlanRecord(
            id="11111111-1111-1111-1111-111111111111",
            project_key="red-phone",
            file_path="docs/plans/legacy-plan.md",
            content_hash="b" * 64,
            status="active",
            freshness_status="fresh",
            declared_chunk_count=1,
            observed_chunk_count=1,
        ),
        IndexedPlanRecord(
            id="22222222-2222-2222-2222-222222222222",
            project_key="red-phone",
            file_path=canonical_path,
            content_hash="c" * 64,
            status="active",
            freshness_status="fresh",
            declared_chunk_count=0,
            observed_chunk_count=0,
        ),
        IndexedPlanRecord(
            id="33333333-3333-3333-3333-333333333333",
            project_key="red-gift",
            file_path=canonical_path,
            content_hash="a" * 64,
            status="active",
            freshness_status="fresh",
            declared_chunk_count=0,
            observed_chunk_count=0,
        ),
    )

    snapshot = build_repair_snapshot(
        manifest=manifest,
        local_files=local_files,
        contexts=contexts,
        plans=plans,
        feature_links=(),
        alembic_revision="037",
        database_identity_hash="d" * 64,
        mutation_timestamp="2026-07-28T01:02:03+00:00",
    )

    assert snapshot.polluted_plan_ids == ("11111111-1111-1111-1111-111111111111",)
    assert snapshot.missing_canonical_files == local_files
    assert {item.reason_code for item in snapshot.collisions} == {
        "content_hash_mismatch",
        "project_owner_mismatch",
    }
    serialized = repr(snapshot.to_dict())
    assert "embedding" not in serialized
    assert "plan_content" not in serialized


def test_database_identity_fingerprint_does_not_return_identity_values() -> None:
    """Returning raw database identity instead of only its digest must make this test fail."""
    identity = {
        "database_name": "SENSITIVE_DATABASE_NAME",
        "server_address": "SENSITIVE_SERVER_ADDRESS",
        "server_port": 5432,
    }

    fingerprint = database_identity_fingerprint(identity)

    assert fingerprint == sha256_json(identity)
    assert "SENSITIVE" not in fingerprint


def _mutation_proof_files(tmp_path: Path) -> tuple[Path, str, Path, str]:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_sha256 = write_private_json(snapshot_path, _snapshot_fixture().to_dict())
    backup_receipt_path = tmp_path / "backup-receipt.json"
    backup_receipt_sha256 = write_private_json(
        backup_receipt_path,
        {"backup_id": "backup-2026-07-28"},
    )
    return snapshot_path, snapshot_sha256, backup_receipt_path, backup_receipt_sha256


def _validate_mutation_proof(
    snapshot_path: Path,
    snapshot_sha256: str,
    backup_receipt_path: Path,
    backup_receipt_sha256: str,
    *,
    postgres_restore_tested: bool = True,
    writers_off_confirmed: bool = True,
) -> tuple[RepairSnapshot, object]:
    return plan_index_repair.validate_mutation_proof(
        snapshot_path,
        snapshot_sha256,
        backup_receipt_path,
        backup_receipt_sha256,
        postgres_restore_tested,
        writers_off_confirmed,
    )


def test_validate_mutation_proof_returns_verified_snapshot_and_closed_proof(
    tmp_path: Path,
) -> None:
    """Skipping either private file or attestation must make this test fail."""
    paths = _mutation_proof_files(tmp_path)

    snapshot, proof = _validate_mutation_proof(*paths)

    assert snapshot == _snapshot_fixture()
    assert proof.snapshot_sha256 == paths[1]
    assert proof.backup_receipt_sha256 == paths[3]
    assert proof.postgres_restore_tested is True
    assert proof.writers_off_confirmed is True


@pytest.mark.parametrize("missing_file", ["snapshot", "backup_receipt"])
def test_validate_mutation_proof_rejects_missing_proof_file(
    tmp_path: Path,
    missing_file: str,
) -> None:
    """Opening a mutating gate with either proof file missing must make this test fail."""
    paths = list(_mutation_proof_files(tmp_path))
    target_index = 0 if missing_file == "snapshot" else 2
    Path(paths[target_index]).unlink()

    with pytest.raises(RepairSafetyError) as exc:
        _validate_mutation_proof(*cast(tuple[Path, str, Path, str], tuple(paths)))

    assert exc.value.reason_code == "private_file_missing"


@pytest.mark.parametrize("changed_file", ["snapshot", "backup_receipt"])
def test_validate_mutation_proof_rejects_changed_proof_file(
    tmp_path: Path,
    changed_file: str,
) -> None:
    """Trusting bytes changed after their signed digest must make this test fail."""
    paths = list(_mutation_proof_files(tmp_path))
    target_index = 0 if changed_file == "snapshot" else 2
    target = Path(paths[target_index])
    target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(RepairSafetyError) as exc:
        _validate_mutation_proof(*cast(tuple[Path, str, Path, str], tuple(paths)))

    assert exc.value.reason_code == "private_file_digest_mismatch"


@pytest.mark.parametrize("non_regular_file", ["snapshot", "backup_receipt"])
def test_validate_mutation_proof_rejects_non_regular_proof_file(
    tmp_path: Path,
    non_regular_file: str,
) -> None:
    """Accepting a directory in place of either private file must make this test fail."""
    paths = list(_mutation_proof_files(tmp_path))
    target_index = 0 if non_regular_file == "snapshot" else 2
    target = Path(paths[target_index])
    target.unlink()
    target.mkdir()

    with pytest.raises(RepairSafetyError) as exc:
        _validate_mutation_proof(*cast(tuple[Path, str, Path, str], tuple(paths)))

    assert exc.value.reason_code == "private_file_type"


@pytest.mark.parametrize("wrong_owner_file", ["snapshot", "backup_receipt"])
def test_validate_mutation_proof_rejects_wrong_owner(
    tmp_path: Path,
    wrong_owner_file: str,
) -> None:
    """Trusting a proof file not owned by the operator must make this test fail."""
    paths = _mutation_proof_files(tmp_path)
    owner = os.getuid()
    owner_checks = [owner + 1] if wrong_owner_file == "snapshot" else [owner, owner + 1]

    with patch.object(plan_index_repair.os, "getuid", side_effect=owner_checks):
        with pytest.raises(RepairSafetyError) as exc:
            _validate_mutation_proof(*paths)

    assert exc.value.reason_code == "private_file_owner"


@pytest.mark.parametrize("permissive_file", ["snapshot", "backup_receipt"])
def test_validate_mutation_proof_rejects_mode_other_than_0600(
    tmp_path: Path,
    permissive_file: str,
) -> None:
    """Accepting group-readable proof material must make this test fail."""
    paths = _mutation_proof_files(tmp_path)
    target = paths[0] if permissive_file == "snapshot" else paths[2]
    target.chmod(0o640)

    with pytest.raises(RepairSafetyError) as exc:
        _validate_mutation_proof(*paths)

    assert exc.value.reason_code == "private_file_mode"


@pytest.mark.parametrize(
    ("attestation", "reason_code"),
    [
        ("postgres_restore_tested", "postgres_restore_not_tested"),
        ("writers_off_confirmed", "writers_off_not_confirmed"),
    ],
)
def test_validate_mutation_proof_requires_explicit_attestations(
    tmp_path: Path,
    attestation: str,
    reason_code: str,
) -> None:
    """Treating either absent operator attestation as truthy must make this test fail."""
    paths = _mutation_proof_files(tmp_path)
    attestations = {
        "postgres_restore_tested": True,
        "writers_off_confirmed": True,
    }
    attestations[attestation] = False

    with pytest.raises(RepairSafetyError) as exc:
        _validate_mutation_proof(*paths, **attestations)

    assert exc.value.reason_code == reason_code


def _valid_reindex_stats() -> tuple[plan_index_repair.ProjectReindexStats, ...]:
    return tuple(
        plan_index_repair.ProjectReindexStats(
            project_key=project_key,
            indexed=1 if project_key == "red-phone" else 0,
            skipped=0,
            linked=1 if project_key == "red-phone" else 0,
            errors=0,
            chunks_created=2 if project_key == "red-phone" else 0,
        )
        for project_key in sorted(plan_index_repair.TARGET_PROJECT_KEYS)
    )


def test_reindex_evidence_is_version_one_snapshot_bound_and_exactly_seven_projects() -> None:
    """Weakening the evidence envelope or project set must make this test fail."""
    evidence = plan_index_repair.ReindexEvidence(
        version=1,
        snapshot_sha256="a" * 64,
        projects=_valid_reindex_stats(),
    )

    assert evidence.to_dict() == {
        "version": 1,
        "snapshot_sha256": "a" * 64,
        "projects": [
            {
                "project_key": project_key,
                "indexed": 1 if project_key == "red-phone" else 0,
                "skipped": 0,
                "linked": 1 if project_key == "red-phone" else 0,
                "errors": 0,
                "chunks_created": 2 if project_key == "red-phone" else 0,
            }
            for project_key in sorted(plan_index_repair.TARGET_PROJECT_KEYS)
        ],
    }


@pytest.mark.parametrize(
    ("case", "reason_code"),
    [
        ("version", "invalid_reindex_evidence"),
        ("digest", "invalid_reindex_evidence"),
        ("missing", "reindex_project_set_mismatch"),
        ("extra", "reindex_project_set_mismatch"),
    ],
)
def test_reindex_evidence_rejects_invalid_envelope(
    case: str,
    reason_code: str,
) -> None:
    """Accepting a bad version, digest, or project membership must make this test fail."""
    version = 2 if case == "version" else 1
    snapshot_sha256 = "not-a-digest" if case == "digest" else "a" * 64
    projects = _valid_reindex_stats()
    if case == "missing":
        projects = projects[:-1]
    elif case == "extra":
        projects = (
            *projects,
            plan_index_repair.ProjectReindexStats(
                project_key="Dream",
                indexed=0,
                skipped=0,
                linked=0,
                errors=0,
                chunks_created=0,
            ),
        )

    with pytest.raises(RepairSafetyError) as exc:
        plan_index_repair.ReindexEvidence(
            version=version,
            snapshot_sha256=snapshot_sha256,
            projects=projects,
        )

    assert exc.value.reason_code == reason_code


@pytest.mark.parametrize(
    ("field_name", "field_value", "reason_code"),
    [
        ("indexed", -1, "invalid_reindex_project_stats"),
        ("skipped", True, "invalid_reindex_project_stats"),
        ("linked", -1, "invalid_reindex_project_stats"),
        ("errors", 1, "reindex_errors_reported"),
        ("chunks_created", -1, "invalid_reindex_project_stats"),
    ],
)
def test_reindex_evidence_rejects_invalid_or_nonzero_project_stats(
    field_name: str,
    field_value: int,
    reason_code: str,
) -> None:
    """Trusting negative, boolean, or non-zero error stats must make this test fail."""
    stats = list(_valid_reindex_stats())

    with pytest.raises(RepairSafetyError) as exc:
        stats[0] = replace(stats[0], **{field_name: field_value})

    assert exc.value.reason_code == reason_code


def test_verification_report_is_bound_to_snapshot_and_evidence_digests() -> None:
    """Dropping either digest or accepting duplicate canonical paths must make this fail."""
    evidence = plan_index_repair.ReindexEvidence(
        version=1,
        snapshot_sha256="a" * 64,
        projects=_valid_reindex_stats(),
    )
    plan = plan_index_repair.VerifiedPlanRecord(
        id="11111111-1111-1111-1111-111111111111",
        project_key="red-phone",
        file_path="/srv/red-phone/docs/plans/one-plan.md",
        content_hash="b" * 64,
    )
    report = plan_index_repair.VerificationReport(
        version=1,
        snapshot_sha256="a" * 64,
        evidence_sha256=sha256_json(evidence.to_dict()),
        evidence=evidence,
        canonical_plans=(plan,),
    )

    assert report.to_dict() == {
        "version": 1,
        "snapshot_sha256": "a" * 64,
        "evidence_sha256": sha256_json(evidence.to_dict()),
        "evidence": evidence.to_dict(),
        "canonical_plans": [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "project_key": "red-phone",
                "file_path": "/srv/red-phone/docs/plans/one-plan.md",
                "content_hash": "b" * 64,
            }
        ],
    }

    with pytest.raises(RepairSafetyError) as exc:
        replace(report, evidence_sha256="c" * 64)
    assert exc.value.reason_code == "invalid_verification_report"

    with pytest.raises(RepairSafetyError) as exc:
        replace(report, canonical_plans=(plan, replace(plan, id=str(UUID(int=2)))))
    assert exc.value.reason_code == "verification_report_collision"


def _private_verification_objects() -> tuple[
    plan_index_repair.ReindexEvidence,
    plan_index_repair.VerificationReport,
]:
    evidence = plan_index_repair.ReindexEvidence(
        version=1,
        snapshot_sha256="a" * 64,
        projects=_valid_reindex_stats(),
    )
    report = plan_index_repair.VerificationReport(
        version=1,
        snapshot_sha256="a" * 64,
        evidence_sha256=sha256_json(evidence.to_dict()),
        evidence=evidence,
        canonical_plans=(
            plan_index_repair.VerifiedPlanRecord(
                id="11111111-1111-1111-1111-111111111111",
                project_key="red-phone",
                file_path="/srv/red-phone/docs/plans/one-plan.md",
                content_hash="b" * 64,
            ),
        ),
    )
    return evidence, report


def test_private_evidence_and_report_loaders_round_trip_closed_payloads(
    tmp_path: Path,
) -> None:
    """Making Task6 parse private control JSON itself must make this test fail."""
    evidence, report = _private_verification_objects()
    evidence_path = tmp_path / "evidence.json"
    report_path = tmp_path / "report.json"
    evidence_digest = write_private_json(evidence_path, evidence.to_dict())
    report_digest = write_private_json(report_path, report.to_dict())

    assert plan_index_repair.load_reindex_evidence(evidence_path, evidence_digest) == evidence
    assert plan_index_repair.load_verification_report(report_path, report_digest) == report


@pytest.mark.parametrize("artifact", ["evidence", "report"])
def test_private_verification_loaders_reject_wrong_digest_and_mode(
    tmp_path: Path,
    artifact: str,
) -> None:
    """Loading a substituted or non-private control artifact must make this test fail."""
    evidence, report = _private_verification_objects()
    payload = evidence.to_dict() if artifact == "evidence" else report.to_dict()
    loader = (
        plan_index_repair.load_reindex_evidence
        if artifact == "evidence"
        else plan_index_repair.load_verification_report
    )
    path = tmp_path / f"{artifact}.json"
    digest = write_private_json(path, payload)

    with pytest.raises(RepairSafetyError) as digest_exc:
        loader(path, "0" * 64)
    assert digest_exc.value.reason_code == "private_file_digest_mismatch"

    path.chmod(0o640)
    with pytest.raises(RepairSafetyError) as mode_exc:
        loader(path, digest)
    assert mode_exc.value.reason_code == "private_file_mode"


@pytest.mark.parametrize("artifact", ["evidence", "report"])
def test_private_verification_loaders_reject_open_or_unknown_schema(
    tmp_path: Path,
    artifact: str,
) -> None:
    """Ignoring unknown private-artifact fields must make this test fail."""
    loader = (
        plan_index_repair.load_reindex_evidence
        if artifact == "evidence"
        else plan_index_repair.load_verification_report
    )
    path = tmp_path / f"bad-{artifact}.json"
    digest = write_private_json(path, {"version": 1, "unknown": True})

    with pytest.raises(RepairSafetyError) as exc:
        loader(path, digest)

    assert exc.value.reason_code == (
        "invalid_reindex_evidence" if artifact == "evidence" else "invalid_verification_report"
    )


def test_private_loader_rejects_fstat_drift_during_read(tmp_path: Path) -> None:
    """Trusting only the pre-read file identity must make this test fail."""
    evidence, _report = _private_verification_objects()
    path = tmp_path / "drifting-evidence.json"
    digest = write_private_json(path, evidence.to_dict())
    real_fstat = os.fstat
    calls = 0

    def drifting_fstat(descriptor: int) -> os.stat_result | SimpleNamespace:
        nonlocal calls
        calls += 1
        details = real_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_dev=details.st_dev,
                st_ino=details.st_ino,
                st_mode=details.st_mode,
                st_uid=details.st_uid,
                st_gid=details.st_gid,
                st_size=details.st_size + 1,
                st_mtime_ns=details.st_mtime_ns,
                st_ctime_ns=details.st_ctime_ns,
            )
        return details

    with patch(
        "brain_v42.maintenance.plan_index_repair.os.fstat",
        side_effect=drifting_fstat,
    ):
        with pytest.raises(RepairSafetyError) as exc:
            plan_index_repair.load_reindex_evidence(path, digest)

    assert calls == 2
    assert exc.value.reason_code == "private_file_changed"
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


class TestResolveTargetProjectsRoot:
    """BRAIN_PLAN_PROJECTS_ROOT: the only formerly-hardcoded personal path.

    Hermetic by construction: these tests stub
    ``plan_index_repair.get_settings`` directly instead of round-tripping
    through real env vars + the process-wide ``get_settings`` lru_cache
    singleton. That singleton is shared with the rest of the test suite, so
    asserting on it via env vars is order-dependent on whatever else in a
    7000+ test run happened to populate or invalidate the cache first.
    """

    def test_uses_configured_root_when_settings_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_settings = SimpleNamespace(brain_plan_projects_root=Path("/tmp/configured-root"))
        monkeypatch.setattr(plan_index_repair, "get_settings", lambda: fake_settings)

        root = plan_index_repair._resolve_target_projects_root()

        assert root == Path("/tmp/configured-root")

    def test_falls_back_to_sentinel_without_crashing_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_settings = SimpleNamespace(brain_plan_projects_root=None)
        monkeypatch.setattr(plan_index_repair, "get_settings", lambda: fake_settings)

        root = plan_index_repair._resolve_target_projects_root()

        assert "hawixs" not in str(root)
        assert "hawkixs_infra" not in str(root)

    def test_falls_back_to_sentinel_when_settings_cannot_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise() -> Settings:
            raise ValidationError.from_exception_data("Settings", [])

        monkeypatch.setattr(plan_index_repair, "get_settings", _raise)

        root = plan_index_repair._resolve_target_projects_root()

        assert "hawixs" not in str(root)
        assert "hawkixs_infra" not in str(root)
