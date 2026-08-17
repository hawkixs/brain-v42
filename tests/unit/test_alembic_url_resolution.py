"""Unit tests for Alembic's fail-closed database URL resolver."""

from __future__ import annotations

import ast
import sys
import traceback
import types
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import asyncpg
import pytest
from sqlalchemy.engine import make_url


class _ResolverModule(types.ModuleType):
    _resolve_sqlalchemy_url: Callable[[], str]


class _FakeConfigModule(types.ModuleType):
    get_settings: MagicMock


def _get_resolver() -> Callable[[], str]:
    """Load only the resolver and imports without executing Alembic's module body."""
    env_path = Path(__file__).parents[2] / "alembic" / "env.py"
    tree = ast.parse(env_path.read_text())

    module_body: list[ast.stmt] = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    resolver_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_resolve_sqlalchemy_url"
        ),
        None,
    )
    assert resolver_node is not None, "_resolve_sqlalchemy_url not found in alembic/env.py"
    module_body.append(resolver_node)

    mini_module = ast.Module(body=module_body, type_ignores=[])
    ast.fix_missing_locations(mini_module)

    module = _ResolverModule("_alembic_env_resolver")
    exec(compile(mini_module, str(env_path), "exec"), module.__dict__)  # noqa: S102
    return module._resolve_sqlalchemy_url


class TestResolveSqlalchemyUrl:
    """The migration target must always be explicit and validated."""

    def test_accepts_explicit_test_database_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        test_url = "postgresql+asyncpg://brain:brain@localhost:5433/brain_test"
        monkeypatch.setenv("POSTGRES_URL", test_url)
        monkeypatch.delenv("BRAIN_ALEMBIC_ALLOW_PROD", raising=False)

        assert _get_resolver()() == test_url

    def test_missing_env_fails_without_loading_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("POSTGRES_URL", raising=False)
        get_settings = MagicMock()
        fake_config = _FakeConfigModule("brain_v42.config")
        fake_config.get_settings = get_settings
        monkeypatch.setitem(sys.modules, "brain_v42.config", fake_config)

        with pytest.raises(RuntimeError, match="POSTGRES_URL"):
            _get_resolver()()

        get_settings.assert_not_called()

    def test_malformed_url_does_not_leak_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        secret = "sentinel-malformed-dsn-secret"
        malformed_url = f"postgresql+asyncpg://brain:password@localhost:{secret}/brain_test"
        monkeypatch.setenv("POSTGRES_URL", malformed_url)

        with pytest.raises(RuntimeError, match="POSTGRES_URL is invalid") as exc_info:
            _get_resolver()()

        rendered_traceback = "".join(
            traceback.format_exception(
                exc_info.type,
                exc_info.value,
                exc_info.tb,
            )
        )
        assert secret not in str(exc_info.value)
        assert secret not in rendered_traceback
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None
        assert secret not in str(exc_info.value.__context__)

    def test_asyncpg_would_apply_database_query_override(self) -> None:
        """Document why validating only URL.database does not protect the real target."""
        override_url = (
            "postgresql+asyncpg://brain:password@localhost:5433/brain_test?database=brain"
        )
        parsed_url = make_url(override_url)

        dialect = parsed_url.get_dialect()()
        _, connect_args = dialect.create_connect_args(parsed_url)

        assert parsed_url.database == "brain_test"
        assert connect_args["database"] == "brain"

    @pytest.mark.parametrize(
        ("query_string", "secret"),
        [
            (
                "database=brain&application_name=sentinel-query-database",
                "sentinel-query-database",
            ),
            ("host=sentinel-query-host", "sentinel-query-host"),
            ("port=sentinel-query-port", "sentinel-query-port"),
            ("ssl=sentinel-query-ssl", "sentinel-query-ssl"),
        ],
        ids=["database", "host", "port", "ssl"],
    )
    def test_rejects_query_parameters_without_leaking_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        query_string: str,
        secret: str,
    ) -> None:
        query_url = "postgresql+asyncpg://brain:password@localhost:5433/brain_test?" + query_string
        monkeypatch.setenv("POSTGRES_URL", query_url)

        with pytest.raises(
            RuntimeError,
            match="POSTGRES_URL must not include query parameters",
        ) as exc_info:
            _get_resolver()()

        rendered_traceback = "".join(
            traceback.format_exception(
                exc_info.type,
                exc_info.value,
                exc_info.tb,
            )
        )
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None
        assert secret not in str(exc_info.value)
        assert secret not in str(exc_info.value.__cause__)
        assert secret not in str(exc_info.value.__context__)
        assert secret not in rendered_traceback
        assert query_string not in str(exc_info.value)
        assert query_string not in rendered_traceback

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://brain:brain@localhost:5433/brain_test",
            "sqlite+aiosqlite:///brain_test",
        ],
    )
    def test_rejects_non_asyncpg_driver(self, monkeypatch: pytest.MonkeyPatch, url: str) -> None:
        monkeypatch.setenv("POSTGRES_URL", url)

        with pytest.raises(RuntimeError, match=r"postgresql\+asyncpg"):
            _get_resolver()()

    def test_rejects_url_without_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "POSTGRES_URL",
            "postgresql+asyncpg://brain:brain@localhost:5433",
        )

        with pytest.raises(RuntimeError, match="database"):
            _get_resolver()()

    @pytest.mark.parametrize(
        ("invalid_url", "expected_message", "secret"),
        [
            (
                "postgresql+asyncpg://brain:sentinel-missing-host@/brain_test",
                "POSTGRES_URL must include a host",
                "sentinel-missing-host",
            ),
            (
                "postgresql+asyncpg://brain:sentinel-missing-port@localhost/brain_test",
                "POSTGRES_URL must include a port between 1 and 65535",
                "sentinel-missing-port",
            ),
            (
                "postgresql+asyncpg://:sentinel-missing-username@localhost:5433/brain_test",
                "POSTGRES_URL must include a username",
                "sentinel-missing-username",
            ),
            (
                "postgresql+asyncpg://sentinel-missing-password@localhost:5433/brain_test",
                "POSTGRES_URL must include a password",
                "sentinel-missing-password",
            ),
            (
                "postgresql+asyncpg://brain:sentinel-port-zero@localhost:0/brain_test",
                "POSTGRES_URL must include a port between 1 and 65535",
                "sentinel-port-zero",
            ),
            (
                "postgresql+asyncpg://brain:sentinel-port-overflow@localhost:65536/brain_test",
                "POSTGRES_URL must include a port between 1 and 65535",
                "sentinel-port-overflow",
            ),
        ],
        ids=["host", "port-missing", "username", "password", "port-zero", "port-overflow"],
    )
    def test_rejects_incomplete_connection_identity_without_connecting_or_leaking(
        self,
        monkeypatch: pytest.MonkeyPatch,
        invalid_url: str,
        expected_message: str,
        secret: str,
    ) -> None:
        connection_attempt = MagicMock()
        monkeypatch.setattr(asyncpg, "connect", connection_attempt)
        monkeypatch.setenv("POSTGRES_URL", invalid_url)

        with pytest.raises(RuntimeError) as exc_info:
            _get_resolver()()

        rendered_traceback = "".join(
            traceback.format_exception(
                exc_info.type,
                exc_info.value,
                exc_info.tb,
            )
        )
        connection_attempt.assert_not_called()
        assert str(exc_info.value) == expected_message
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None
        for sensitive_value in (secret, invalid_url):
            assert sensitive_value not in str(exc_info.value)
            assert sensitive_value not in str(exc_info.value.__cause__)
            assert sensitive_value not in str(exc_info.value.__context__)
            assert sensitive_value not in rendered_traceback

    def test_brain_test_does_not_require_prod_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        test_url = "postgresql+asyncpg://brain:brain@localhost:5433/brain_test"
        monkeypatch.setenv("POSTGRES_URL", test_url)
        monkeypatch.delenv("BRAIN_ALEMBIC_ALLOW_PROD", raising=False)

        assert _get_resolver()() == test_url

    def test_brain_database_requires_prod_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "POSTGRES_URL",
            "postgresql+asyncpg://brain:brain@localhost:5433/brain",
        )
        monkeypatch.delenv("BRAIN_ALEMBIC_ALLOW_PROD", raising=False)

        with pytest.raises(RuntimeError, match="BRAIN_ALEMBIC_ALLOW_PROD"):
            _get_resolver()()

    @pytest.mark.parametrize("opt_in", ["1", "true", "yes", "TRUE", "YeS"])
    def test_brain_database_accepts_known_prod_opt_in_values(
        self, monkeypatch: pytest.MonkeyPatch, opt_in: str
    ) -> None:
        prod_url = "postgresql+asyncpg://brain:brain@localhost:5433/brain"
        monkeypatch.setenv("POSTGRES_URL", prod_url)
        monkeypatch.setenv("BRAIN_ALEMBIC_ALLOW_PROD", opt_in)

        assert _get_resolver()() == prod_url

    @pytest.mark.parametrize("opt_in", ["0", "false", "no", "enabled"])
    def test_brain_database_rejects_unknown_prod_opt_in_values(
        self, monkeypatch: pytest.MonkeyPatch, opt_in: str
    ) -> None:
        monkeypatch.setenv(
            "POSTGRES_URL",
            "postgresql+asyncpg://brain:brain@localhost:5433/brain",
        )
        monkeypatch.setenv("BRAIN_ALEMBIC_ALLOW_PROD", opt_in)

        with pytest.raises(RuntimeError, match="BRAIN_ALEMBIC_ALLOW_PROD"):
            _get_resolver()()

    def test_preserves_percent_encoded_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        encoded_url = "postgresql+asyncpg://brain:p%40ss%25word@localhost:5433/brain_test"
        monkeypatch.setenv("POSTGRES_URL", encoded_url)

        assert _get_resolver()() == encoded_url
