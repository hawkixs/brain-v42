"""Importing ``brain_v42`` must leave structlog's default renderer leak-free.

structlog's default configuration ends in ``ConsoleRenderer()``, which renders
``exc_info`` through rich (when installed) with every frame's locals. A Postgres
connect failure then writes the database password in clear into the journal
(ticket 1bffd79a, operator decision Q115 option a). Every entry point that never
configures structlog itself must inherit the safe renderer from the package.

structlog's configuration is process-global, so every behaviour below runs in a
subprocess: the test process's own configuration is never touched.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
_PACKAGE = _SRC / "brain_v42"
_SENTINEL = "s3cret-sentinel"
_TIMEOUT_SECONDS = 60
_MIN_SCRIPTS_PROBED = 38

# module -> why it cannot be imported in a probe subprocess.
_NOT_IMPORTABLE: dict[str, str] = {}

# The sentinel is assembled at runtime so that no source line shown by a
# traceback can carry it: only a rendered frame local can.
_LOG_FAILURE = """
import structlog

def _connect():
    password = "s3cret-" + "sentinel"
    raise ConnectionRefusedError("postgres down (%d)" % len(password))

try:
    _connect()
except ConnectionRefusedError:
    structlog.get_logger("probe").error("probe.failed", exc_info=True)
"""


def _run(code: str) -> str:
    env = {**os.environ, "PYTHONPATH": str(_SRC)}
    result = subprocess.run(  # noqa: S603 - fixed interpreter, test-owned code
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=_ROOT,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout + result.stderr


def _assert_traceback_without_locals(output: str) -> None:
    assert "Traceback" in output, output
    assert "ConnectionRefusedError" in output, output
    assert "postgres down" in output, output
    assert _SENTINEL not in output, "frame local leaked into the rendered traceback"


def test_importing_the_package_installs_the_no_locals_renderer() -> None:
    _assert_traceback_without_locals(_run("import brain_v42\n" + _LOG_FAILURE))


def test_an_explicit_configuration_made_before_the_import_is_untouched() -> None:
    code = """
import structlog
structlog.configure(processors=[structlog.processors.JSONRenderer()])
import brain_v42
(renderer,) = structlog.get_config()["processors"]
assert isinstance(renderer, structlog.processors.JSONRenderer), renderer
"""
    _run(code)


def test_a_later_partial_configure_keeps_the_no_locals_renderer() -> None:
    code = """
import sys
import brain_v42
import structlog
structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
"""
    _assert_traceback_without_locals(_run(code + _LOG_FAILURE))


def test_the_installed_chain_is_the_default_one_with_only_the_renderer_swapped() -> None:
    code = """
import structlog
default = list(structlog.get_config()["processors"])
import brain_v42
installed = structlog.get_config()["processors"]
assert installed[:-1] == default[:-1], (installed, default)
assert isinstance(default[-1], structlog.dev.ConsoleRenderer)
assert isinstance(installed[-1], structlog.dev.ConsoleRenderer)
assert installed[-1] is not default[-1]
"""
    _run(code)


def _imports_brain_v42(tree: ast.AST) -> bool:
    return any(
        (
            isinstance(node, ast.Import)
            and any(a.name.split(".")[0] == "brain_v42" for a in node.names)
        )
        or (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and (node.module or "").split(".")[0] == "brain_v42"
        )
        for node in ast.walk(tree)
    )


def _has_main_guard(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and any(
            isinstance(c, ast.Constant) and c.value == "__main__" for c in node.test.comparators
        )
        for node in ast.walk(tree)
    )


def _runnable_brain_scripts() -> list[str]:
    """Scripts under ``scripts/`` that run as programs and import brain_v42.

    Runnable means: carries an ``if __name__ == "__main__"`` guard. Those that
    never import brain_v42 are out of scope -- they cannot reach its logging.
    """
    found = []
    for path in sorted((_ROOT / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _has_main_guard(tree) and _imports_brain_v42(tree):
            found.append(path.relative_to(_ROOT).as_posix())
    return found


def _entry_point_targets() -> dict[str, str]:
    """Map every discovered entry point to the way a probe imports it."""
    targets: set[str] = set()
    for path in sorted(_PACKAGE.rglob("*.py")):
        relative = path.relative_to(_SRC).with_suffix("")
        parts = relative.parts
        module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        if path.name == "__main__.py" or 'if __name__ == "__main__"' in path.read_text(
            encoding="utf-8"
        ):
            targets.add(module)
    scripts = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for target in scripts["project"]["scripts"].values():
        targets.add(target.partition(":")[0])
    discovered = {f"module:{module}": module for module in sorted(targets)}
    discovered.update({f"file:{path}": path for path in _runnable_brain_scripts()})
    return discovered


_ENTRY_POINTS = _entry_point_targets()


def _probe_entry_point(key: str) -> str:
    kind, _, target = key.partition(":")
    if kind == "module":
        load = f"importlib.import_module({target!r})"
    else:
        load = (
            "spec = importlib.util.spec_from_file_location('probe_script', "
            f"{str(_ROOT / target)!r}); module = importlib.util.module_from_spec(spec); "
            f"sys.path.insert(0, {str((_ROOT / target).parent)!r}); "
            "sys.modules['probe_script'] = module; spec.loader.exec_module(module)"
        )
    code = f"import importlib, importlib.util, sys\n{load}\n{_LOG_FAILURE}"
    try:
        return _run(code)
    except AssertionError as error:
        return f"PROBE FAILED: {error}"


@pytest.fixture(scope="module")
def _probe_outputs() -> dict[str, str]:
    """Probe every entry point once, concurrently, to keep the module fast."""
    keys = [key for key in _ENTRY_POINTS if _ENTRY_POINTS[key] not in _NOT_IMPORTABLE]
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(keys, pool.map(_probe_entry_point, keys), strict=True))


def test_the_discovery_finds_the_known_entry_points() -> None:
    assert "module:brain_v42.automation.__main__" in _ENTRY_POINTS
    assert "module:brain_v42.scripts.domain_backfill" in _ENTRY_POINTS
    assert "file:scripts/refresh_plan_embeddings.py" in _ENTRY_POINTS
    assert "file:scripts/dream/cross_project_resonance.py" in _ENTRY_POINTS


def test_the_discovery_cannot_silently_shrink() -> None:
    # Measured when the guard was written: 38 runnable scripts import brain_v42.
    # A lower count almost always means the discovery broke; if scripts were
    # really removed, lower the floor in the same change.
    scripts = [key for key in _ENTRY_POINTS if key.startswith("file:")]
    assert len(scripts) >= _MIN_SCRIPTS_PROBED, scripts


@pytest.mark.parametrize("key", sorted(_ENTRY_POINTS))
def test_every_entry_point_renders_tracebacks_without_locals(
    key: str, _probe_outputs: dict[str, str]
) -> None:
    if _ENTRY_POINTS[key] in _NOT_IMPORTABLE:
        pytest.skip(_NOT_IMPORTABLE[_ENTRY_POINTS[key]])
    _assert_traceback_without_locals(_probe_outputs[key])
