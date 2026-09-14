"""The ``headless_agents`` workspace member never depends on ``brain_v42``.

Brain ticket b2a2d1a5 (decision 3c5c56e1): the shared agent runtime lives in
``packages/headless-agents/`` as a uv workspace member that other projects
install on its own. ``brain_v42`` depends on it; the reverse is forbidden, and
this file is the guard the ticket asks for. Three angles, because each catches
a failure the others miss:

- the AST scan catches a literal ``import brain_v42`` even inside a function
  body or a ``TYPE_CHECKING`` block, where a dry import would never execute it;
- the dry import in a fresh interpreter catches a transitive pull -- a module
  the package imports that itself imports ``fastmcp``, say -- which no AST scan
  of this package alone can see;
- the pyproject checks catch the packaging drift that would make a clean tree
  install dirty: a dependency added to the runtime's own ``pyproject.toml``, or
  the root project no longer wiring the member through the workspace.

The dry import runs with ``-I`` so neither ``PYTHONPATH`` (which pytest sets to
``src/`` in-process only, never for children) nor the user site can leak
``brain_v42`` into the child. The package is reachable there because ``uv
sync`` installs every workspace member into the venv.
"""

from __future__ import annotations

import ast
import json
import pkgutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "packages" / "headless-agents"
SOURCE_DIR = PACKAGE_DIR / "src" / "headless_agents"

FORBIDDEN_IMPORT_ROOTS = ("brain_v42", "scripts")
# The six the ticket names, plus ``brain_v42`` itself. ``pydantic`` and
# ``structlog`` are the two the runtime MAY depend on and are deliberately
# absent from this list.
HEAVY_MODULES = ("brain_v42", "sqlalchemy", "asyncpg", "neo4j", "pgvector", "fastmcp", "uvicorn")
ALLOWED_RUNTIME_DEPENDENCIES = frozenset({"pydantic", "structlog"})


def _python_sources() -> list[Path]:
    sources = sorted(SOURCE_DIR.rglob("*.py"))
    assert sources, f"no Python sources under {SOURCE_DIR}"
    return sources


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_runtime_sources_never_import_brain_v42_or_scripts() -> None:
    offenders: list[str] = []
    for source in _python_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        forbidden = _import_roots(tree) & set(FORBIDDEN_IMPORT_ROOTS)
        if forbidden:
            offenders.append(f"{source.relative_to(REPO_ROOT)}: {sorted(forbidden)}")
    assert not offenders, "\n".join(offenders)


def _every_runtime_module() -> list[str]:
    import headless_agents

    names = [headless_agents.__name__]
    for info in pkgutil.walk_packages(headless_agents.__path__, prefix="headless_agents."):
        names.append(info.name)
    return names


def test_dry_import_of_every_runtime_module_loads_neither_brain_v42_nor_a_heavy_dependency() -> (
    None
):
    modules = _every_runtime_module()
    program = (
        "import importlib, json, sys, time\n"
        "started = time.perf_counter()\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "elapsed = time.perf_counter() - started\n"
        "print(json.dumps({'modules': sorted(sys.modules), 'elapsed': elapsed}))\n"
    )
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    wall = time.perf_counter() - started
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    loaded = set(report["modules"])
    leaked = [name for name in HEAVY_MODULES if name in loaded]
    assert not leaked, f"dry import of {modules} pulled {leaked}"
    # A generous bound, not the ticket's 0.5 s: CI hosts are noisy, and this
    # assertion exists to catch a regression back to the ~1 s import that
    # loading fastmcp+neo4j+sqlalchemy cost before the extraction, not to
    # benchmark. The precise figure is measured in a clean venv for the receipt.
    assert report["elapsed"] < 2.0, f"import took {report['elapsed']:.2f}s (wall {wall:.2f}s)"


def _runtime_pyproject() -> dict[str, object]:
    path = PACKAGE_DIR / "pyproject.toml"
    assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _requirement_name(requirement: str) -> str:
    name = requirement.strip()
    for separator in ("[", ">", "<", "=", "!", "~", ";", " "):
        name = name.split(separator, 1)[0]
    return name.lower().replace("_", "-")


def test_runtime_pyproject_declares_only_the_two_allowed_dependencies() -> None:
    project = _runtime_pyproject()["project"]
    assert isinstance(project, dict)
    assert project["name"] == "headless-agents"
    assert project.get("license") == "Apache-2.0"
    declared = {_requirement_name(item) for item in project.get("dependencies", [])}
    assert declared <= ALLOWED_RUNTIME_DEPENDENCIES, declared - ALLOWED_RUNTIME_DEPENDENCIES
    assert "optional-dependencies" not in project, "no extras: nothing may pull a database in"


def test_root_project_depends_on_the_runtime_through_the_uv_workspace() -> None:
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = {_requirement_name(item) for item in root["project"]["dependencies"]}
    assert "headless-agents" in dependencies

    uv = root["tool"]["uv"]
    members = uv["workspace"]["members"]
    assert any(
        Path(member) == Path("packages/headless-agents") or member == "packages/*"
        for member in members
    ), members
    assert uv["sources"]["headless-agents"] == {"workspace": True}


@pytest.mark.parametrize("required", ["README.md", "LICENSE"])
def test_runtime_package_ships_its_public_files(required: str) -> None:
    assert (PACKAGE_DIR / required).is_file(), f"{required} missing: the package is public"
