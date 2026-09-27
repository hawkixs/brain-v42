"""One version everywhere: pyproject.toml, the CHANGELOG, every install pin, uv.lock
(0.5.2 lot 5, Task 4). A regression guard written BEFORE the 0.5.2 release: green
against the merged tree at 0.5.1, so it proves nothing new until the version actually
moves -- then it is the thing that must turn green again.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "packages" / "headless-agents"
PYPROJECT_PATH = PACKAGE_ROOT / "pyproject.toml"
CHANGELOG_PATH = PACKAGE_ROOT / "CHANGELOG.md"
README_PATH = PACKAGE_ROOT / "README.md"
UV_LOCK_PATH = REPO_ROOT / "uv.lock"

_RELEASED_HEADING = re.compile(r"^## (\d+\.\d+\.\d+) — ", re.MULTILINE)
_INSTALL_PIN = re.compile(
    r"@headless-agents-v(\d+\.\d+\.\d+)#subdirectory=packages/headless-agents"
)


def _package_version() -> str:
    with PYPROJECT_PATH.open("rb") as stream:
        return tomllib.load(stream)["project"]["version"]


def test_the_latest_released_changelog_section_is_the_package_version() -> None:
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    match = _RELEASED_HEADING.search(text)
    assert match, f"no '## X.Y.Z — ' heading found in {CHANGELOG_PATH}"
    assert match.group(1) == _package_version()


def test_no_unreleased_section_remains_for_the_package_version() -> None:
    version = _package_version()
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^## Unreleased — {re.escape(version)}\b", re.MULTILINE)
    stray = pattern.findall(text)
    assert not stray, f"{CHANGELOG_PATH}: still has an 'Unreleased — {version}' section"


def test_every_install_pin_names_the_package_version_tag() -> None:
    version = _package_version()
    versions_named: set[str] = set()
    for path in (README_PATH, CHANGELOG_PATH):
        versions_named |= set(_INSTALL_PIN.findall(path.read_text(encoding="utf-8")))
    assert versions_named, "no @headless-agents-vX.Y.Z install pin found in README or CHANGELOG"
    assert versions_named == {version}, (
        f"install pins name {sorted(versions_named)}, expected only {{{version!r}}}"
    )


def test_the_lockfile_carries_the_package_version() -> None:
    with UV_LOCK_PATH.open("rb") as stream:
        lock = tomllib.load(stream)
    packages = [p for p in lock.get("package", []) if p.get("name") == "headless-agents"]
    assert len(packages) == 1, f"expected exactly one 'headless-agents' package in {UV_LOCK_PATH}"
    assert packages[0]["version"] == _package_version()
