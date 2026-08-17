"""Unit tests for the dedicated Neo4j integration-test configuration guard."""

from __future__ import annotations

import ast
import types
from pathlib import Path

import pytest


def _load_neo4j_resolver_fn():
    conftest_path = Path(__file__).parents[2] / "tests" / "integration" / "conftest.py"
    tree = ast.parse(conftest_path.read_text())
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_resolve_integration_neo4j_config"
    ]
    assert len(functions) == 1, "dedicated Neo4j guard is missing"
    mini_module = ast.Module(body=imports + functions, type_ignores=[])
    ast.fix_missing_locations(mini_module)
    module = types.ModuleType("_integration_neo4j_guard_mini")
    exec(compile(mini_module, str(conftest_path), "exec"), module.__dict__)  # noqa: S102
    return module._resolve_integration_neo4j_config  # type: ignore[attr-defined]


def _load_neo4j_driver_fn():
    conftest_path = Path(__file__).parents[2] / "tests" / "integration" / "conftest.py"
    tree = ast.parse(conftest_path.read_text())
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "neo4j_driver"
    ]
    assert len(functions) == 1, "Neo4j driver fixture is missing"
    driver_fixture = functions[0]
    driver_fixture.decorator_list = []
    mini_module = ast.Module(body=[*imports, driver_fixture], type_ignores=[])
    ast.fix_missing_locations(mini_module)
    module = types.ModuleType("_integration_neo4j_driver_mini")
    exec(compile(mini_module, str(conftest_path), "exec"), module.__dict__)  # noqa: S102
    return module.neo4j_driver  # type: ignore[attr-defined]


def _load_destructive_recovery_guard_fn():
    conftest_path = Path(__file__).parents[2] / "tests" / "integration" / "conftest.py"
    tree = ast.parse(conftest_path.read_text())
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_require_destructive_neo4j_recovery_target"
    ]
    assert len(functions) == 1, "destructive Neo4j recovery guard is missing"
    mini_module = ast.Module(body=imports + functions, type_ignores=[])
    ast.fix_missing_locations(mini_module)
    module = types.ModuleType("_destructive_neo4j_recovery_guard_mini")
    exec(compile(mini_module, str(conftest_path), "exec"), module.__dict__)  # noqa: S102
    return module._require_destructive_neo4j_recovery_target  # type: ignore[attr-defined]


def _clear_dedicated_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BRAIN_V42_TEST_NEO4J_URL",
        "BRAIN_V42_TEST_NEO4J_USER",
        "BRAIN_V42_TEST_NEO4J_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


def test_requires_all_dedicated_neo4j_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_dedicated_variables(monkeypatch)
    monkeypatch.setenv("BRAIN_V42_TEST_NEO4J_URL", "bolt://test-only:7687")

    with pytest.raises(ValueError, match="BRAIN_V42_TEST_NEO4J_PASSWORD"):
        _load_neo4j_resolver_fn()()


def test_legacy_neo4j_variables_never_enable_driver_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_dedicated_variables(monkeypatch)
    monkeypatch.setenv("NEO4J_URL", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "legacy-default-password")

    with pytest.raises(ValueError, match="are required"):
        _load_neo4j_resolver_fn()()


def test_returns_only_explicit_dedicated_neo4j_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_dedicated_variables(monkeypatch)
    monkeypatch.setenv("NEO4J_URL", "bolt://legacy:7687")
    monkeypatch.setenv("NEO4J_USER", "legacy")
    monkeypatch.setenv("NEO4J_PASSWORD", "legacy-password")
    monkeypatch.setenv("BRAIN_V42_TEST_NEO4J_URL", "bolt://isolated-test:7687")
    monkeypatch.setenv("BRAIN_V42_TEST_NEO4J_USER", "test-user")
    monkeypatch.setenv("BRAIN_V42_TEST_NEO4J_PASSWORD", "test-password")

    assert _load_neo4j_resolver_fn()() == (
        "bolt://isolated-test:7687",
        ("test-user", "test-password"),
    )


def test_destructive_recovery_requires_explicit_opt_in_and_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _load_destructive_recovery_guard_fn()
    monkeypatch.delenv("BRAIN_V42_TEST_NEO4J_DESTRUCTIVE_RECOVERY", raising=False)

    with pytest.raises(ValueError, match="DESTRUCTIVE_RECOVERY"):
        guard("bolt://127.0.0.1:7687")

    monkeypatch.setenv("BRAIN_V42_TEST_NEO4J_DESTRUCTIVE_RECOVERY", "yes")
    with pytest.raises(ValueError, match="loopback"):
        guard("bolt://192.168.1.12:7687")

    guard("bolt://localhost:7687")


@pytest.mark.asyncio
async def test_driver_failure_skip_never_exposes_url_or_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neo4j

    class BrokenGraphDatabase:
        @staticmethod
        def driver(*_args, **_kwargs):
            raise RuntimeError("DRIVER_DETAIL_SECRET")

    monkeypatch.setattr(neo4j, "AsyncGraphDatabase", BrokenGraphDatabase)
    driver_fixture = _load_neo4j_driver_fn()
    generator = driver_fixture(
        "bolt://test-user:URL_PASSWORD_SECRET@db.invalid:7687",
        ("test-user", "AUTH_PASSWORD_SECRET"),
    )

    with pytest.raises(pytest.skip.Exception) as skipped:
        await generator.__anext__()

    rendered = str(skipped.value)
    assert rendered == "Neo4j test database is not reachable"
    for secret in ("URL_PASSWORD_SECRET", "AUTH_PASSWORD_SECRET", "DRIVER_DETAIL_SECRET"):
        assert secret not in rendered
