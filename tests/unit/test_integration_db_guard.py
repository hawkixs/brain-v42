"""Unit tests for integration conftest prod-DB guard.

Regression tests for Bug 2: tests/integration/conftest.py defaulted INTEGRATION_DB_URL
to the live prod DB when POSTGRES_URL was unset, silently running writes/migrations
against production.

The guard is exposed as the importable helper _resolve_integration_db_url which raises
ValueError on unsafe config, so it can be unit-tested independently of pytest.skip.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest


def _load_resolver_fn():
    """Extract _resolve_integration_db_url from integration conftest via AST exec.

    We compile only the function definitions (and their imports) rather than
    executing the full module body. The module-level call to
    _get_integration_db_url_or_skip() at the bottom of the module would trigger
    pytest.skip during module loading, which interferes with tests that set
    env vars with monkeypatch (applied after load).

    This approach lets us test the pure _resolve_integration_db_url logic without
    any session/fixture side effects.
    """
    import ast

    conftest_path = Path(__file__).parents[2] / "tests" / "integration" / "conftest.py"
    source = conftest_path.read_text()
    tree = ast.parse(source)

    # Collect imports and function definitions only — skip module-level statements
    # (assignments, expressions) that would call pytest.skip on load.
    import_nodes = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    func_nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    constant_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_PROD_DB_NAME" for t in node.targets)
    ]

    func_names = {n.name for n in func_nodes}
    assert "_resolve_integration_db_url" in func_names, (
        "_resolve_integration_db_url not found in tests/integration/conftest.py — "
        "did you add the importable guard helper?"
    )

    mini_module = ast.Module(
        body=import_nodes + constant_nodes + func_nodes,
        type_ignores=[],
    )
    ast.fix_missing_locations(mini_module)

    mod = types.ModuleType("_integration_conftest_mini")
    exec(compile(mini_module, str(conftest_path), "exec"), mod.__dict__)  # noqa: S102
    return mod._resolve_integration_db_url  # type: ignore[attr-defined]


def _load_connection_check_fn():
    """Extract the async connectivity fixture without applying pytest decorators."""
    import ast

    conftest_path = Path(__file__).parents[2] / "tests" / "integration" / "conftest.py"
    tree = ast.parse(conftest_path.read_text())
    import_nodes = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    connection_checks = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "check_db_connection"
    ]
    assert len(connection_checks) == 1
    connection_check = connection_checks[0]
    connection_check.decorator_list = []
    mini_module = ast.Module(body=[*import_nodes, connection_check], type_ignores=[])
    ast.fix_missing_locations(mini_module)

    mod = types.ModuleType("_integration_connection_check_mini")
    exec(compile(mini_module, str(conftest_path), "exec"), mod.__dict__)  # noqa: S102
    return mod.check_db_connection  # type: ignore[attr-defined]


class TestIntegrationDbGuard:
    """Tests for the integration conftest DB URL guard (_resolve_integration_db_url)."""

    def test_accepts_brain_test_url(self, monkeypatch) -> None:
        """A URL pointing at brain_test must be accepted (returned as-is)."""
        test_url = "postgresql+asyncpg://brain:brain@localhost:5433/brain_test"
        monkeypatch.setenv("BRAIN_V42_TEST_DB_URL", test_url)
        monkeypatch.delenv("POSTGRES_URL", raising=False)

        resolver = _load_resolver_fn()
        result = resolver()
        assert result == test_url

    def test_accepts_brain_v42_test_db_url_env(self, monkeypatch) -> None:
        """BRAIN_V42_TEST_DB_URL is the dedicated integration variable."""
        test_url = "postgresql+asyncpg://brain:brain@localhost:5433/brain_test"
        monkeypatch.delenv("POSTGRES_URL", raising=False)
        monkeypatch.setenv("BRAIN_V42_TEST_DB_URL", test_url)

        resolver = _load_resolver_fn()
        result = resolver()
        assert result == test_url

    def test_refuses_prod_brain_url(self, monkeypatch) -> None:
        """A URL with database name exactly 'brain' must raise ValueError."""
        monkeypatch.setenv(
            "BRAIN_V42_TEST_DB_URL",
            "postgresql+asyncpg://brain:brain@localhost:5433/brain",
        )
        monkeypatch.delenv("POSTGRES_URL", raising=False)

        resolver = _load_resolver_fn()
        with pytest.raises(ValueError, match="prod"):
            resolver()

    def test_refuses_percent_encoded_prod_database_path(self, monkeypatch) -> None:
        """Percent encoding cannot disguise the production database name."""
        monkeypatch.setenv(
            "BRAIN_V42_TEST_DB_URL",
            "postgresql+asyncpg://brain:brain@localhost:5433/br%61in",
        )

        resolver = _load_resolver_fn()
        with pytest.raises(ValueError, match="prod"):
            resolver()

    @pytest.mark.parametrize(
        "test_url",
        [
            "postgresql+asyncpg://brain:brain@localhost:5433",
            "postgresql+asyncpg://brain:brain@localhost:5433/",
        ],
    )
    def test_refuses_url_without_explicit_database_path(self, monkeypatch, test_url: str) -> None:
        """An absent path must never fall through to a driver-default database."""
        monkeypatch.setenv("BRAIN_V42_TEST_DB_URL", test_url)

        resolver = _load_resolver_fn()
        with pytest.raises(ValueError, match="explicit test database"):
            resolver()

    @pytest.mark.parametrize(
        "query",
        [
            "database=brain",
            "database=brain_test",
            "dbname=brain",
            "DBNAME=brain_test",
            "data%62ase=brain",
        ],
    )
    def test_refuses_database_name_override_query_keys(self, monkeypatch, query: str) -> None:
        """Driver query options cannot override the validated URL path."""
        monkeypatch.setenv(
            "BRAIN_V42_TEST_DB_URL",
            f"postgresql+asyncpg://brain:brain@localhost:5433/brain_test?{query}",
        )

        resolver = _load_resolver_fn()
        with pytest.raises(ValueError, match="database override"):
            resolver()

    def test_refuses_when_both_envs_unset(self, monkeypatch) -> None:
        """When both POSTGRES_URL and BRAIN_V42_TEST_DB_URL are unset, must raise ValueError."""
        monkeypatch.delenv("POSTGRES_URL", raising=False)
        monkeypatch.delenv("BRAIN_V42_TEST_DB_URL", raising=False)

        resolver = _load_resolver_fn()
        with pytest.raises(ValueError, match="not set"):
            resolver()

    def test_brain_test_url_is_exclusive_when_postgres_url_is_also_set(self, monkeypatch) -> None:
        """The dedicated value wins even when application config targets prod."""
        pg_url = "postgresql+asyncpg://brain:brain@localhost:5433/brain"
        brain_url = "postgresql+asyncpg://brain:brain@localhost:5433/other_test"
        monkeypatch.setenv("POSTGRES_URL", pg_url)
        monkeypatch.setenv("BRAIN_V42_TEST_DB_URL", brain_url)

        resolver = _load_resolver_fn()
        result = resolver()
        assert result == brain_url

    def test_postgres_url_alone_is_ignored(self, monkeypatch) -> None:
        """Application configuration can never opt integration tests in."""
        monkeypatch.setenv(
            "POSTGRES_URL",
            "postgresql+asyncpg://brain:brain@localhost:5433/brain_test",
        )
        monkeypatch.delenv("BRAIN_V42_TEST_DB_URL", raising=False)

        resolver = _load_resolver_fn()
        with pytest.raises(ValueError, match="BRAIN_V42_TEST_DB_URL is not set"):
            resolver()


def test_missing_dedicated_url_skips_before_migration_subprocess() -> None:
    """No dedicated URL means a normal skip, even with hostile POSTGRES_URL."""
    project_root = Path(__file__).parents[2]
    env = os.environ.copy()
    env.pop("BRAIN_V42_TEST_DB_URL", None)
    env["POSTGRES_URL"] = "not-a-postgres-url"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration/mcp/test_dream_project_authorization.py",
            "-q",
        ],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    combined = f"{result.stdout}\n{result.stderr}".lower()
    assert result.returncode == 0, combined
    assert "skipped" in combined
    assert "alembic migration failed" not in combined


def test_engine_fixture_explicitly_depends_on_migration_guard() -> None:
    """Fixture ordering must guard the DSN before engine construction or teardown."""
    import ast

    conftest_path = Path(__file__).parents[2] / "tests" / "integration" / "conftest.py"
    tree = ast.parse(conftest_path.read_text())
    engine_functions = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "engine"
    ]

    assert len(engine_functions) == 1
    dependency_names = {argument.arg for argument in engine_functions[0].args.args}
    assert "run_migrations" in dependency_names


@pytest.mark.asyncio
async def test_connection_failure_skip_never_exposes_dsn_or_driver_exception() -> None:
    """A failed test-DB probe must not print credentials or driver details."""

    class BrokenConnection:
        async def __aenter__(self):
            raise RuntimeError("DRIVER_DETAIL_SECRET")

        async def __aexit__(self, *_args):
            return False

    class BrokenEngine:
        def connect(self):
            return BrokenConnection()

    check_connection = _load_connection_check_fn()
    check_connection.__globals__["INTEGRATION_DB_URL"] = (
        "postgresql+asyncpg://test-user:DSN_PASSWORD_SECRET@db.invalid:5432/brain_test"
    )

    with pytest.raises(pytest.skip.Exception) as skipped:
        await check_connection(BrokenEngine())

    rendered = str(skipped.value)
    assert rendered == "PostgreSQL test database is not reachable"
    assert "DSN_PASSWORD_SECRET" not in rendered
    assert "DRIVER_DETAIL_SECRET" not in rendered
