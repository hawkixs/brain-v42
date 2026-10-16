"""Keep the unauthenticated HTTP escape hatch inside the isolated restore drill."""

import os
from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml

from brain_v42.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DRILL_ONLY_FLAG = "MCP_HTTP_ALLOW_UNAUTHENTICATED"
_IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "virtualenv",
    "__pycache__",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
}


def test_restore_database_url_scheme_matches_settings_validator() -> None:
    rail = yaml.safe_load((REPO_ROOT / "rail.yaml").read_bytes())
    restore = rail["drill"]["restore"]
    scheme = restore.get("database_url_scheme", "postgresql")

    assert "POSTGRES_URL" in restore["database_env"]
    assert Settings.validate_postgres_url(f"{scheme}://drill:x@database:5432/drill")


def _rail_flag_locations(value: object, path: tuple[str, ...] = ()) -> list[str]:
    """Inspect keys and values, exempting only the exact restore env mapping."""
    if isinstance(value, dict):
        if path == ("drill", "restore", "env"):
            return []
        locations = []
        for key, child in value.items():
            child_path = (*path, str(key))
            if DRILL_ONLY_FLAG in str(key):
                locations.append("rail.yaml:" + ".".join(child_path))
            locations.extend(_rail_flag_locations(child, child_path))
        return locations
    if isinstance(value, list):
        return [
            location
            for index, child in enumerate(value)
            for location in _rail_flag_locations(child, (*path, str(index)))
        ]
    if isinstance(value, str) and DRILL_ONLY_FLAG in value:
        return ["rail.yaml:" + ".".join(path)]
    return []


def _non_drill_flag_locations(root: Path) -> list[str]:
    """Scan repository configuration without following links into host configuration."""
    locations = []
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in _IGNORED_DIRECTORIES
            and not (current / name).is_symlink()
            and not (current / name / "pyvenv.cfg").is_file()
        ]
        for name in filenames:
            config = current / name
            if config.is_symlink():
                continue
            relative = config.relative_to(root)
            if relative.as_posix() == "rail.yaml":
                locations.extend(_rail_flag_locations(yaml.safe_load(config.read_bytes())))
            elif name.endswith(".md"):
                # Prose may name the flag to forbid it; it configures nothing.
                continue
            elif (
                relative.parts[0] == "deploy"
                or fnmatch(name, "*compose*.y*ml")
                or fnmatch(name, "*.env*")
            ) and DRILL_ONLY_FLAG.encode() in config.read_bytes():
                locations.append(relative.as_posix())
    return sorted(locations)


def test_repository_keeps_unauthenticated_http_drill_only() -> None:
    violations = _non_drill_flag_locations(REPO_ROOT)

    assert not violations, f"Drill-only HTTP flag in non-drill configuration: {violations}"


@pytest.mark.parametrize(
    "relative_path",
    [
        "deploy/nested/compose.yaml",
        "deploy/systemd/brain.service",
        "deploy/settings.conf",
        "deploy/bin/start",
        "compose.yml",
        "nested/compose.restore.yaml",
        "nested/docker-compose.yml",
        "nested/docker-compose.override.yaml",
        ".env",
        ".env.example",
        "nested/production.env",
        "nested/production.env.example",
        "nested/.env.production",
    ],
)
def test_scanner_detects_flag_in_configuration(tmp_path: Path, relative_path: str) -> None:
    config = tmp_path / relative_path
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("LOG_LEVEL=INFO\n", encoding="utf-8")
    assert _non_drill_flag_locations(tmp_path) == []

    # Even a disabled or commented occurrence must never become a production example.
    config.write_text(f"# {DRILL_ONLY_FLAG}=false\n", encoding="utf-8")
    assert _non_drill_flag_locations(tmp_path) == [relative_path]


@pytest.mark.parametrize(
    "other_config",
    [
        {DRILL_ONLY_FLAG: False},
        {"deploy": {"env": {DRILL_ONLY_FLAG: "true"}}},
        {"drill": {"schema_head": {"field": DRILL_ONLY_FLAG}}},
        {"drill": {"restore": {"database_env": [DRILL_ONLY_FLAG]}}},
        {"drill": {"restore": {"env": DRILL_ONLY_FLAG}}},
        {"gates": {"example": {"value": f"{DRILL_ONLY_FLAG}=true"}}},
    ],
)
def test_scanner_rejects_flag_elsewhere_in_rail(
    tmp_path: Path, other_config: dict[str, object]
) -> None:
    rail = tmp_path / "rail.yaml"
    rail.write_text(yaml.safe_dump(other_config), encoding="utf-8")

    assert _non_drill_flag_locations(tmp_path), "Only drill.restore.env may contain the flag"


def test_scanner_allows_only_the_drill_restore_env_mapping(tmp_path: Path) -> None:
    rail = tmp_path / "rail.yaml"
    rail.write_text(
        f"# {DRILL_ONLY_FLAG} is drill-only.\n"
        f"drill:\n  restore:\n    env:\n      {DRILL_ONLY_FLAG}: 'true'\n"
        "deploy:\n  target: private-compose\n",
        encoding="utf-8",
    )

    assert _non_drill_flag_locations(tmp_path) == []


def test_scanner_rejects_drill_env_aliased_into_production(tmp_path: Path) -> None:
    (tmp_path / "rail.yaml").write_text(
        f"drill:\n  restore:\n    env: &drill_env\n      {DRILL_ONLY_FLAG}: 'true'\n"
        "deploy:\n  env: *drill_env\n",
        encoding="utf-8",
    )

    assert _non_drill_flag_locations(tmp_path) == [f"rail.yaml:deploy.env.{DRILL_ONLY_FLAG}"]


@pytest.mark.parametrize(
    "directory",
    [
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    ],
)
def test_scanner_ignores_virtualenvs_and_caches(tmp_path: Path, directory: str) -> None:
    ignored = tmp_path / directory
    ignored.mkdir()
    (ignored / "compose.yaml").write_text(DRILL_ONLY_FLAG, encoding="utf-8")

    assert _non_drill_flag_locations(tmp_path) == []


def test_scanner_ignores_virtualenv_with_custom_name(tmp_path: Path) -> None:
    virtualenv = tmp_path / "custom-python"
    virtualenv.mkdir()
    (virtualenv / "pyvenv.cfg").write_text("", encoding="utf-8")
    (virtualenv / ".env").write_text(DRILL_ONLY_FLAG, encoding="utf-8")

    assert _non_drill_flag_locations(tmp_path) == []


def test_scanner_does_not_follow_configuration_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "fixture.txt"
    target.write_text(DRILL_ONLY_FLAG, encoding="utf-8")
    (tmp_path / "compose.yaml").symlink_to(target)
    (tmp_path / "deploy").symlink_to(tmp_path, target_is_directory=True)

    assert _non_drill_flag_locations(tmp_path) == []


def test_scanner_ignores_documentation(tmp_path: Path) -> None:
    runbook = tmp_path / "deploy" / "systemd" / "RUNBOOK.md"
    runbook.parent.mkdir(parents=True)
    runbook.write_text(f"The unit must never carry `{DRILL_ONLY_FLAG}`.\n")

    assert _non_drill_flag_locations(tmp_path) == []
