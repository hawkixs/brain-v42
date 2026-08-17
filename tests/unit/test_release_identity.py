"""Contrat : l'identité du build se MESURE, elle ne se recopie pas.

Ce dépôt a un historique documenté de claims périmés sur exactement ce point —
le README a affirmé « la production reste à 037 » pendant trois jours après la
bascule en 039. Un numéro de révision écrit à la main dans le code serait la
même faute en pire : il se lirait dans `/health`, donc il se croirait.

D'où la forme des tests : la tête attendue n'est jamais un littéral, elle est
recalculée par `ScriptDirectory`, l'implémentation faisant autorité, et par une
chaîne fabriquée que rien dans le dépôt ne connaît.
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
    """Les deux lectures sont mémoïsées : isoler chaque test des précédents."""
    release.package_version.cache_clear()
    release.shipped_alembic_head.cache_clear()
    yield
    release.package_version.cache_clear()
    release.shipped_alembic_head.cache_clear()


def _authoritative_head() -> str:
    """Recalcule la tête avec Alembic lui-même, pas avec le code sous test."""
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
    """Canari : sans lui, un glob cassé rendrait tout ce module vert à vide."""
    assert VERSIONS_DIRECTORY.is_dir()
    assert list(VERSIONS_DIRECTORY.glob("*.py"))


def test_package_version_reports_the_installed_distribution() -> None:
    """Ce que /health annonce est la version INSTALLÉE, pas celle de pyproject.

    La production tourne un install éditable : `importlib.metadata` lit le
    dist-info figé à l'installation. C'est la mesure honnête — elle dit quel
    paquet tourne, et non quel paquet le dépôt prétend décrire.
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
    """La preuve que la tête est DÉRIVÉE : une chaîne que le dépôt ignore.

    Les fichiers sont écrits dans le désordre alphabétique de la chaîne, pour
    qu'un `max(filenames)` naïf réponde faux.
    """
    _write_revision(tmp_path, "zzz", None)
    _write_revision(tmp_path, "aaa", "mmm")
    _write_revision(tmp_path, "mmm", "zzz")

    assert release.head_of_versions(tmp_path) == "aaa"


def test_head_of_an_empty_directory_is_unknown(tmp_path: Path) -> None:
    assert release.head_of_versions(tmp_path) is None


def test_head_of_a_forked_chain_is_unknown(tmp_path: Path) -> None:
    """Deux têtes ne se résument pas : mieux vaut ne rien annoncer qu'inventer."""
    _write_revision(tmp_path, "001", None)
    _write_revision(tmp_path, "002", "001")
    _write_revision(tmp_path, "003", "001")

    assert release.head_of_versions(tmp_path) is None


def test_shipped_head_is_unknown_when_no_revision_ships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un paquet amputé de ses migrations doit le DIRE, pas mentir par défaut."""
    monkeypatch.setattr(release, "_versions_directory", lambda: None)

    assert release.shipped_alembic_head() is None


def test_the_reporting_module_carries_no_revision_literal() -> None:
    """Ceinture : le module qui rapporte la tête ne doit pas la contenir."""
    source = (REPO_ROOT / "src" / "brain_v42" / "release.py").read_text(encoding="utf-8")

    assert _authoritative_head() not in source
