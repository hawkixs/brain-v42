"""Contract: the build's identity is MEASURED, it is not copied.

This repository has a documented history of stale claims on exactly this point —
the README asserted « la production reste à 037 » for three days after the cutover
to 039. A revision number written by hand in the code would be the same fault,
worse: it would be read from `/health`, hence believed.

Hence the shape of these tests: the expected head is never a literal, it is
recomputed by `ScriptDirectory`, the authoritative implementation, and by a
fabricated chain nothing in the repository knows about.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from brain_v42 import release

REPO_ROOT = Path(__file__).parent.parent.parent
VERSIONS_DIRECTORY = REPO_ROOT / "alembic" / "versions"


@pytest.fixture(autouse=True)
def _clear_identity_caches() -> Iterator[None]:
    """Both reads are memoised: isolate each test from the previous ones."""
    release.package_version.cache_clear()
    release.shipped_alembic_head.cache_clear()
    yield
    release.package_version.cache_clear()
    release.shipped_alembic_head.cache_clear()


def _authoritative_head() -> str:
    """Recompute the head with Alembic itself, not with the code under test."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"chaîne non linéaire, tête ambiguë : {heads}"
    return heads[0]


def _write_revision(directory: Path, revision: str, down_revision: str | None) -> None:
    parent = "None" if down_revision is None else f'"{down_revision}"'
    (directory / f"{revision}_fabricated.py").write_text(
        f'"""fabriquée."""\n\nrevision = "{revision}"\ndown_revision = {parent}\n',
        encoding="utf-8",
    )


def test_the_repository_still_has_revisions_to_identify() -> None:
    """Canary: without it, a broken glob would make this whole module green on nothing."""
    assert VERSIONS_DIRECTORY.is_dir()
    assert list(VERSIONS_DIRECTORY.glob("*.py"))


def test_package_version_reports_the_installed_distribution() -> None:
    """What /health announces is the INSTALLED version, not pyproject's.

    Production runs an editable install: `importlib.metadata` reads the dist-info
    frozen at installation time. That is the honest measurement — it says which
    package is running, not which package the repository claims to describe.
    """
    assert release.package_version() == importlib.metadata.version("brain_v42")


def test_package_version_falls_back_to_dev_when_nothing_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _absent(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("brain_v42")

    monkeypatch.setattr(importlib.metadata, "version", _absent)

    assert release.package_version() == "dev"


def test_shipped_head_matches_the_authoritative_script_directory() -> None:
    assert release.shipped_alembic_head() == _authoritative_head()


def test_shipped_head_follows_a_fabricated_chain(tmp_path: Path) -> None:
    """The proof that the head is DERIVED: a chain the repository knows nothing of.

    The files are written out of the chain's alphabetical order, so that a naive
    `max(filenames)` answers wrong.
    """
    _write_revision(tmp_path, "zzz", None)
    _write_revision(tmp_path, "aaa", "mmm")
    _write_revision(tmp_path, "mmm", "zzz")

    assert release.head_of_versions(tmp_path) == "aaa"


def test_head_of_an_empty_directory_is_unknown(tmp_path: Path) -> None:
    assert release.head_of_versions(tmp_path) is None


def test_head_of_a_forked_chain_is_unknown(tmp_path: Path) -> None:
    """Two heads do not reduce to one: better to announce nothing than to invent."""
    _write_revision(tmp_path, "001", None)
    _write_revision(tmp_path, "002", "001")
    _write_revision(tmp_path, "003", "001")

    assert release.head_of_versions(tmp_path) is None


def test_shipped_head_is_unknown_when_no_revision_ships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package stripped of its migrations must SAY so, not lie by default."""
    monkeypatch.setattr(release, "_versions_directory", lambda: None)

    assert release.shipped_alembic_head() is None


def test_the_reporting_module_carries_no_revision_literal() -> None:
    """Belt: the module that reports the head must not contain it."""
    source = (REPO_ROOT / "src" / "brain_v42" / "release.py").read_text(encoding="utf-8")

    assert _authoritative_head() not in source
