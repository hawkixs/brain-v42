"""Hermetic subprocess tests for Alembic's fail-closed CLI wiring."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
ALEMBIC_DIR = PROJECT_ROOT / "alembic"
SOURCE_DIR = PROJECT_ROOT / "src"
_PASSTHROUGH_ENV_KEYS = ("PATH", "SYSTEMROOT", "TMPDIR", "LANG", "LC_ALL")


def _write_alembic_config(tmp_path: Path) -> Path:
    """Write the complete minimal config needed by Alembic and fileConfig()."""
    config_path = tmp_path / "alembic-test.ini"
    config_path.write_text(
        f"""\
[alembic]
script_location = {ALEMBIC_DIR}
prepend_sys_path = {SOURCE_DIR}
path_separator = os
sqlalchemy.url =

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
"""
    )
    return config_path


def _run_offline_upgrade(
    tmp_path: Path,
    *,
    postgres_url: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``alembic upgrade head --sql`` with an allowlisted environment."""
    config_path = _write_alembic_config(tmp_path)
    environment = {
        key: value for key in _PASSTHROUGH_ENV_KEYS if (value := os.environ.get(key)) is not None
    }
    environment["PYTHONNOUSERSITE"] = "1"
    if postgres_url is not None:
        environment["POSTGRES_URL"] = postgres_url

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(config_path),
            "upgrade",
            "head",
            "--sql",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    """Return every subprocess stream inspected by non-disclosure assertions."""
    return result.stdout + result.stderr


def test_missing_process_url_ignores_dotenv(tmp_path: Path) -> None:
    """A cwd .env must never become an implicit migration target."""
    dotenv_secret = "dotenv-cli-sentinel-secret"
    dotenv_url = f"postgresql+asyncpg://brain:{dotenv_secret}@localhost:5433/brain_test"
    (tmp_path / ".env").write_text(f"POSTGRES_URL={dotenv_url}\n")

    result = _run_offline_upgrade(tmp_path)
    output = _combined_output(result)

    assert result.returncode != 0
    assert "POSTGRES_URL is required for Alembic migrations" in output
    assert dotenv_secret not in output
    assert dotenv_url not in output


def test_invalid_port_does_not_leak_url_or_sentinel(tmp_path: Path) -> None:
    """Parser failures must expose only the generic invalid-URL error."""
    invalid_port_secret = "invalid-port-cli-sentinel"
    invalid_url = f"postgresql+asyncpg://brain:password@localhost:{invalid_port_secret}/brain_test"

    result = _run_offline_upgrade(tmp_path, postgres_url=invalid_url)
    output = _combined_output(result)

    assert result.returncode != 0
    assert "POSTGRES_URL is invalid" in output
    assert invalid_port_secret not in output
    assert invalid_url not in output


def test_test_database_renders_all_migrations_without_secret(tmp_path: Path) -> None:
    """The explicit test target renders the complete 001-to-041 chain offline."""
    encoded_password = "offline-cli-sentinel%40password%25value"
    decoded_password = "offline-cli-sentinel@password%value"
    test_url = f"postgresql+asyncpg://brain:{encoded_password}@localhost:59999/brain_test"

    result = _run_offline_upgrade(tmp_path, postgres_url=test_url)
    output = _combined_output(result)

    assert result.returncode == 0
    assert result.stderr.count("Running upgrade") == 45
    assert "Running upgrade 031 -> 032" in result.stderr
    assert "Running upgrade 032 -> 033" in result.stderr
    assert "Running upgrade 033 -> 034" in result.stderr
    assert "Running upgrade 034 -> 035" in result.stderr
    assert "Running upgrade 035 -> 036" in result.stderr
    assert "Running upgrade 037 -> 038" in result.stderr
    assert "Running upgrade 038 -> 039" in result.stderr
    assert "Running upgrade 039 -> 040" in result.stderr
    assert "Running upgrade 040 -> 041" in result.stderr
    assert encoded_password not in output
    assert decoded_password not in output
    assert test_url not in output


def test_production_database_without_opt_in_fails_before_rendering_sql(
    tmp_path: Path,
) -> None:
    """The exact production database name requires a per-command opt-in."""
    prod_password = "production-cli-sentinel%40password%25value"
    prod_url = f"postgresql+asyncpg://brain:{prod_password}@localhost:59999/brain"

    result = _run_offline_upgrade(tmp_path, postgres_url=prod_url)
    output = _combined_output(result)

    assert result.returncode != 0
    assert "Refusing the brain database without BRAIN_ALEMBIC_ALLOW_PROD=1|true|yes" in output
    assert "Running upgrade" not in output
    assert "BEGIN;" not in output
    assert "CREATE TABLE" not in output
    assert prod_password not in output
    assert "production-cli-sentinel@password%value" not in output
    assert prod_url not in output
