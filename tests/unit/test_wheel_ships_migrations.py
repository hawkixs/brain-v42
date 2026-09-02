"""The wheel and the production image must carry the Alembic migrations.

Measured on 2026-08-14, before this contract: `uv build --wheel` produced a wheel
of 192 entries with ZERO alembic, and `ls /app/alembic` in the production image
answered "No such file". `/app/.venv/bin/alembic` did exist, pulled in by the
`alembic>=1.13` dependency: the TOOL was shipped, not the MIGRATIONS. Publishing
that wheel would have meant publishing a package unable to migrate its own
database.

The expected set is READ FROM DISK, never pinned to a count: freezing 44 would
redden this file at revision 045 for the wrong reason, that is, at the precise
moment of the nominal case.

The contract bears on the PATH as much as on presence. The migrations must land
under `brain_v42/`, the importable package's root: placed at the root of
site-packages they would be unfindable from `brain_v42.__file__` and would pollute
the global namespace. The SOURCE layout does not move — `alembic/` and
`alembic.ini` stay at the repository root, which
`tests/unit/test_alembic_env.py` requires — it is `force-include` that copies them
into the distribution.
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

# Root of the importable package, as it appears in the wheel.
PACKAGE_ROOT = "brain_v42"
WHEEL_ALEMBIC_ROOT = f"{PACKAGE_ROOT}/alembic"


def _repository_revisions() -> frozenset[str]:
    """The revision files present in the repository, measured, never pinned."""
    return frozenset(path.name for path in VERSIONS_DIR.glob("*.py"))


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel once for the whole module."""
    if shutil.which("uv") is None:  # pragma: no cover - uv is a project prerequisite
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
    """The list of entries in the built wheel."""
    with zipfile.ZipFile(built_wheel) as archive:
        return frozenset(archive.namelist())


@pytest.fixture(scope="module")
def installed_package(built_wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The unpacked wheel, like a `site-packages`: the package, without the repository."""
    target = tmp_path_factory.mktemp("site-packages")
    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(target)
    return target / PACKAGE_ROOT


def _dockerfile_stage(name: str) -> str:
    """The body of a named Dockerfile stage, from `AS <name>` to the next `FROM`."""
    content = DOCKERFILE.read_text()
    start = content.find(f"AS {name}")
    assert start != -1, f"le Dockerfile doit définir un étage `{name}`"
    following = content.find("\nFROM ", start)
    return content[start:] if following == -1 else content[start:following]


def _forced_include_sources() -> frozenset[str]:
    """The SOURCE paths required by `force-include`, read from pyproject.toml."""
    config = tomllib.loads(PYPROJECT.read_text())
    forced = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    return frozenset(forced)


def test_the_repository_has_revisions_to_ship() -> None:
    """Canary: without this guard, a broken glob would make the whole module green on nothing."""
    revisions = _repository_revisions()
    assert revisions, f"aucune révision trouvée dans {VERSIONS_DIR}"


def test_wheel_carries_every_repository_revision(wheel_entries: frozenset[str]) -> None:
    """Every revision in the repository must be found in the wheel."""
    expected = {f"{WHEEL_ALEMBIC_ROOT}/versions/{name}" for name in _repository_revisions()}
    missing = sorted(expected - wheel_entries)
    assert not missing, (
        f"{len(missing)} révision(s) absente(s) de la wheel, "
        f"qui ne pourrait donc pas migrer sa base : {missing[:5]}"
    )


def test_wheel_carries_the_alembic_runtime(wheel_entries: frozenset[str]) -> None:
    """The revisions alone are not enough: env.py, the template and the ini drive them."""
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
    """The path, not only the presence: everything must sit under `brain_v42/`.

    A migration installed at the root of site-packages is unfindable from
    `Path(brain_v42.__file__).parent` and is of no use.
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
    """The installed package must be able to migrate its database from ANY directory.

    Shipping the 44 files is not enough: it is `alembic.ini` that says where to look
    for them. Measured on 2026-08-15 on a wheel installed in a pristine venv, from
    `/tmp`:

        alembic -c .../site-packages/brain_v42/alembic.ini heads
        FAILED: Path doesn't exist: alembic.

    …because `script_location` was resolved from the CURRENT DIRECTORY. The previous
    contract inspected the zip and stopped there; it therefore declared green a
    package the operator can get nothing out of outside their own folder.

    This test reads no hard-coded path: it requires that the revisions found by
    Alembic itself be EXACTLY those of the repository.
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
    """The production image must copy the migrations, not only the alembic binary."""
    stage = _dockerfile_stage("production")
    assert "COPY alembic/ ./alembic/" in stage, (
        "l'étage production doit copier alembic/ (mesuré : `ls /app/alembic` → No such file)"
    )
    assert "COPY alembic.ini ./" in stage, "l'étage production doit copier alembic.ini"


def test_deps_stage_satisfies_every_forced_include() -> None:
    """The `deps` stage must provide every `force-include` source, even empty.

    `force-include` is STRICT on hatchling's side: an absent source raises
    `FileNotFoundError: Forced include not found`. The `deps` stage runs `uv sync`,
    which installs the project and therefore triggers the build backend, having only
    pyproject.toml, uv.lock, README.md and a src stub at its disposal. Without an
    alembic stub, the whole image refused to build — measured.

    The expected list is READ FROM pyproject.toml: adding a third `force-include`
    source without stubbing it will make this test red before CI.
    """
    stage = _dockerfile_stage("deps")
    missing = sorted(source for source in _forced_include_sources() if source not in stage)
    assert not missing, (
        "l'étage deps doit créer un substitut pour chaque source de force-include, "
        f"sinon `uv sync` échoue avec « Forced include not found » : {missing}"
    )
