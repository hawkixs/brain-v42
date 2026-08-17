"""La wheel et l'image de production doivent porter les migrations Alembic.

Mesuré le 2026-08-14, avant ce contrat : `uv build --wheel` produisait une wheel
de 192 entrées dont ZÉRO alembic, et `ls /app/alembic` dans l'image de production
répondait « No such file ». `/app/.venv/bin/alembic` existait pourtant, tiré par
la dépendance `alembic>=1.13` : l'OUTIL était livré, pas les MIGRATIONS. Publier
cette wheel, c'eût été publier un paquet incapable de migrer sa propre base.

L'ensemble attendu est LU SUR LE DISQUE, jamais épinglé à un compte : figer 44
ferait rougir ce fichier à la révision 045 pour la mauvaise raison, c'est-à-dire
au moment précis du cas nominal.

Le contrat porte sur le CHEMIN autant que sur la présence. Les migrations doivent
atterrir sous `brain_v42/`, la racine du paquet importable : posées à la racine de
site-packages elles seraient introuvables depuis `brain_v42.__file__` et
pollueraient l'espace de noms global. La disposition des SOURCES ne bouge pas —
`alembic/` et `alembic.ini` restent à la racine du dépôt, ce que
`tests/unit/test_alembic_env.py` exige — c'est `force-include` qui les recopie
dans la distribution.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).parent.parent.parent
VERSIONS_DIR = PROJECT_ROOT / "alembic" / "versions"
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"

# Racine du paquet importable, telle qu'elle apparaît dans la wheel.
PACKAGE_ROOT = "brain_v42"
WHEEL_ALEMBIC_ROOT = f"{PACKAGE_ROOT}/alembic"


def _repository_revisions() -> frozenset[str]:
    """Les fichiers de révision présents dans le dépôt, mesurés, jamais épinglés."""
    return frozenset(path.name for path in VERSIONS_DIR.glob("*.py"))


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Construit la wheel une seule fois pour tout le module."""
    if shutil.which("uv") is None:  # pragma: no cover - uv est un prérequis du projet
        pytest.fail("uv est requis pour construire la wheel (le projet s'exécute via `uv run`)")

    out_dir = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    built = sorted(out_dir.glob("*.whl"))
    assert len(built) == 1, f"attendu exactement une wheel, obtenu {built}"
    return built[0]


@pytest.fixture(scope="module")
def wheel_entries(built_wheel: Path) -> frozenset[str]:
    """La liste des entrées de la wheel construite."""
    with zipfile.ZipFile(built_wheel) as archive:
        return frozenset(archive.namelist())


@pytest.fixture(scope="module")
def installed_package(built_wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """La wheel dépliée, comme un `site-packages` : le paquet, sans le dépôt."""
    target = tmp_path_factory.mktemp("site-packages")
    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(target)
    return target / PACKAGE_ROOT


def _dockerfile_stage(name: str) -> str:
    """Le corps d'un étage nommé du Dockerfile, du `AS <nom>` au `FROM` suivant."""
    content = DOCKERFILE.read_text()
    start = content.find(f"AS {name}")
    assert start != -1, f"le Dockerfile doit définir un étage `{name}`"
    following = content.find("\nFROM ", start)
    return content[start:] if following == -1 else content[start:following]


def _forced_include_sources() -> frozenset[str]:
    """Les chemins SOURCES exigés par `force-include`, lus dans pyproject.toml."""
    config = tomllib.loads(PYPROJECT.read_text())
    forced = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    return frozenset(forced)


def test_the_repository_has_revisions_to_ship() -> None:
    """Canari : sans cette garde, un glob cassé rendrait tout le module vert à vide."""
    revisions = _repository_revisions()
    assert revisions, f"aucune révision trouvée dans {VERSIONS_DIR}"


def test_wheel_carries_every_repository_revision(wheel_entries: frozenset[str]) -> None:
    """Chaque révision du dépôt doit se retrouver dans la wheel."""
    expected = {f"{WHEEL_ALEMBIC_ROOT}/versions/{name}" for name in _repository_revisions()}
    missing = sorted(expected - wheel_entries)
    assert not missing, (
        f"{len(missing)} révision(s) absente(s) de la wheel, "
        f"qui ne pourrait donc pas migrer sa base : {missing[:5]}"
    )


def test_wheel_carries_the_alembic_runtime(wheel_entries: frozenset[str]) -> None:
    """Les révisions seules ne suffisent pas : env.py, le gabarit et l'ini les pilotent."""
    expected = {
        f"{WHEEL_ALEMBIC_ROOT}/env.py",
        f"{WHEEL_ALEMBIC_ROOT}/script.py.mako",
        f"{PACKAGE_ROOT}/alembic.ini",
    }
    missing = sorted(expected - wheel_entries)
    assert not missing, f"runtime Alembic incomplet dans la wheel : {missing}"


def test_wheel_migrations_sit_inside_the_importable_package(
    wheel_entries: frozenset[str],
) -> None:
    """Le chemin, pas seulement la présence : tout doit tenir sous `brain_v42/`.

    Une migration installée à la racine de site-packages est introuvable depuis
    `Path(brain_v42.__file__).parent` et ne sert à rien.
    """
    assert f"{PACKAGE_ROOT}/__init__.py" in wheel_entries, (
        "la wheel doit porter le paquet importable, sinon la co-localisation ne veut rien dire"
    )

    stray = sorted(
        name
        for name in wheel_entries
        if "alembic" in name
        and not name.startswith(f"{PACKAGE_ROOT}/")
        and ".dist-info/" not in name
    )
    assert not stray, f"entrées Alembic hors du paquet importable : {stray}"


def test_shipped_alembic_ini_finds_its_scripts_from_any_working_directory(
    installed_package: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le paquet installé doit savoir migrer sa base depuis N'IMPORTE QUEL répertoire.

    Livrer les 44 fichiers ne suffit pas : c'est `alembic.ini` qui dit où les
    chercher. Mesuré le 2026-08-15 sur une wheel installée dans un venv vierge,
    depuis `/tmp` :

        alembic -c .../site-packages/brain_v42/alembic.ini heads
        FAILED: Path doesn't exist: alembic.

    …parce que `script_location` était résolu depuis le RÉPERTOIRE COURANT. Le
    contrat précédent inspectait le zip et s'arrêtait là ; il déclarait donc vert
    un paquet dont l'opérateur ne peut rien tirer hors de son propre dossier.

    Ce test ne lit aucun chemin en dur : il exige que les révisions retrouvées
    par Alembic lui-même soient EXACTEMENT celles du dépôt.
    """
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "alembic").exists(), (
        "le répertoire courant du test ne doit porter aucun alembic/, "
        "sinon la résolution relative passerait par accident"
    )

    config = Config(str(installed_package / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    found = frozenset(Path(revision.path).name for revision in script.walk_revisions())

    assert found == _repository_revisions(), (
        "Alembic, piloté par l'ini LIVRÉE, ne retrouve pas les révisions du dépôt : "
        f"{sorted(_repository_revisions() - found)} manquante(s)"
    )


def test_production_image_carries_the_migrations() -> None:
    """L'image de production doit copier les migrations, pas seulement le binaire alembic."""
    stage = _dockerfile_stage("production")
    assert "COPY alembic/ ./alembic/" in stage, (
        "l'étage production doit copier alembic/ (mesuré : `ls /app/alembic` → No such file)"
    )
    assert "COPY alembic.ini ./" in stage, "l'étage production doit copier alembic.ini"


def test_deps_stage_satisfies_every_forced_include() -> None:
    """L'étage `deps` doit fournir chaque source de `force-include`, même vide.

    `force-include` est STRICT côté hatchling : une source absente fait lever
    `FileNotFoundError: Forced include not found`. L'étage `deps` lance
    `uv sync`, qui installe le projet et déclenche donc le backend de build, en
    ne disposant que de pyproject.toml, uv.lock, README.md et d'un stub de src.
    Sans stub d'alembic, l'image entière refusait de se construire — mesuré.

    La liste attendue est LUE DANS pyproject.toml : ajouter une troisième source
    de `force-include` sans la stubber rendra ce test rouge avant le CI.
    """
    stage = _dockerfile_stage("deps")
    missing = sorted(source for source in _forced_include_sources() if source not in stage)
    assert not missing, (
        "l'étage deps doit créer un substitut pour chaque source de force-include, "
        f"sinon `uv sync` échoue avec « Forced include not found » : {missing}"
    )
