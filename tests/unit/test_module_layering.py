"""Contract tests for the module layering preflight.

The contract keeps service extraction a reversible option: a module can only
become a standalone service when it sits outside every cycle.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = REPO_ROOT / "scripts" / "check_module_layering.py"
PACKAGE_ROOT = REPO_ROOT / "src" / "brain_v42"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_module_layering", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _write_package(root: Path, modules: dict[str, str]) -> Path:
    package_root = root / "brain_v42"
    for relative_path, source in modules.items():
        target = package_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return package_root


def test_checker_resolves_package_imports_to_their_top_level_module(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path,
        {
            "services/__init__.py": "",
            # `from brain_v42.repositories import x` must resolve to
            # `repositories`, not to the root module.
            "services/reader.py": "from brain_v42.repositories import Repo\n",
            "repositories/__init__.py": "",
            "repositories/store.py": "class Repo:\n    pass\n",
        },
    )
    graph = checker.build_module_graph(package_root)
    assert graph["services"] == frozenset({"repositories"})


def test_checker_resolves_relative_imports_across_sibling_packages(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path,
        {
            "services/__init__.py": "",
            "services/reader.py": "from ..repositories.store import Repo\n",
            "repositories/__init__.py": "",
            "repositories/store.py": "class Repo:\n    pass\n",
        },
    )
    graph = checker.build_module_graph(package_root)
    assert graph["services"] == frozenset({"repositories"})


def test_checker_maps_root_level_modules_to_the_root_node(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path,
        {
            "config.py": "SETTING = 1\n",
            "services/__init__.py": "",
            "services/reader.py": "from brain_v42.config import SETTING\n",
        },
    )
    graph = checker.build_module_graph(package_root)
    assert graph["services"] == frozenset({"_root"})


def test_checker_ignores_third_party_and_self_imports(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path,
        {
            "services/__init__.py": "",
            "services/reader.py": ("import httpx\nfrom brain_v42.services.writer import write\n"),
            "services/writer.py": "def write() -> None:\n    return None\n",
        },
    )
    graph = checker.build_module_graph(package_root)
    assert graph["services"] == frozenset()


def test_checker_fails_closed_on_unparseable_source(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path,
        {"services/__init__.py": "", "services/broken.py": "def (\n"},
    )
    with pytest.raises(checker.ModuleLayeringError, match="unparseable"):
        checker.build_module_graph(package_root)


def test_checker_detects_a_two_module_cycle(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path,
        {
            "left/__init__.py": "",
            "left/mod.py": "from brain_v42.right import thing\n",
            "right/__init__.py": "",
            "right/__main__.py": "from brain_v42.left import other\n",
        },
    )
    cycles = checker.find_cycles(checker.build_module_graph(package_root))
    assert cycles == [frozenset({"left", "right"})]


def test_checker_reports_no_cycle_for_a_clean_layering(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path,
        {
            "high/__init__.py": "",
            "high/mod.py": "from brain_v42.low import thing\n",
            "low/__init__.py": "",
            "low/thing.py": "thing = 1\n",
        },
    )
    assert checker.find_cycles(checker.build_module_graph(package_root)) == []


def test_validate_rejects_any_cycle(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path,
        {
            "left/__init__.py": "",
            "left/mod.py": "from brain_v42.right import thing\n",
            "right/__init__.py": "",
            "right/mod.py": "from brain_v42.left import other\n",
        },
    )
    with pytest.raises(checker.ModuleLayeringError, match="module cycle"):
        checker.validate_module_layering(package_root)


def test_main_returns_two_when_the_contract_is_violated(tmp_path: Path) -> None:
    package_root = _write_package(
        tmp_path,
        {
            "left/__init__.py": "",
            "left/mod.py": "from brain_v42.right import thing\n",
            "right/__init__.py": "",
            "right/mod.py": "from brain_v42.left import other\n",
        },
    )
    assert checker.main(["--package", str(package_root)]) == 2


# --- Contract against the real package -------------------------------------


def test_real_package_is_acyclic() -> None:
    checker.validate_module_layering(PACKAGE_ROOT)
