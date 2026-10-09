"""Forward-only migration and secret-safe runtime role provisioning."""

from __future__ import annotations

import base64
import hashlib
import json
import tomllib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from brain_v42.config import Settings
from brain_v42.db import engine as db_engine
from brain_v42.db import scram
from brain_v42.db.engine import pg_server_settings
from brain_v42.scripts import migrate_cli

ROOT = Path(__file__).parents[2]
PASSWORD = "unit-only-password-'\\-never-log"
OWNER_URL = "postgresql+asyncpg://owner:unit-only-owner-password@localhost:5432/test"


@dataclass
class Database:
    heads: tuple[str, ...] = ("064",)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    receipt: dict[str, Any] = field(
        default_factory=lambda: {
            "contract_id": "unit/recovery/v1",
            "schema_version": 1,
            "checks": [{"id": "shape", "status": "pass"}],
        }
    )
    fail_sql: str | None = None
    role_exists: bool = True

    async def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> None:
        sql = str(statement)
        self.calls.append((sql, parameters or {}))
        # Exercise SQLAlchemy's parameter discovery too: SCRAM's colon-separated
        # keys must not accidentally become bind parameters inside a quoted literal.
        if "PASSWORD" in sql:
            assert not getattr(statement, "_bindparams", {})
        if self.fail_sql and self.fail_sql in sql:
            raise RuntimeError(f"database error containing {PASSWORD} and {OWNER_URL}")

    async def exec_driver_sql(self, statement: str) -> None:
        await self.execute(statement)

    async def scalar(self, statement: Any) -> str | bool:
        await self.execute(statement)
        if "pg_catalog.pg_roles" in str(statement):
            return self.role_exists
        return json.dumps(self.receipt)

    async def run_sync(self, callback: Any) -> tuple[str, ...]:
        return self.heads

    @asynccontextmanager
    async def connect(self):
        yield self

    @asynccontextmanager
    async def begin(self):
        yield self

    async def dispose(self) -> None:
        self.calls.append(("dispose", {}))


def write_binding(directory: Path, *, head: str = "064") -> Path:
    directory.mkdir()
    manifest = {
        "contract_id": "unit/recovery/v1",
        "schema_version": 1,
        "checks": [{"id": "shape"}],
    }
    binding: dict[str, Any] = {
        "schema_head": head,
        "contract_id": manifest["contract_id"],
        "contract_version": 1,
        "release_sha": "a" * 40,
    }
    assets = {
        "manifest": ("manifest.json", json.dumps(manifest)),
        "attestation_sql": ("live.sql", "SELECT 'fixture-receipt'"),
        "restored_attestation_sql": ("restored.sql", "SELECT 'restored-receipt'"),
    }
    for key, (name, content) in assets.items():
        asset = directory / name
        asset.write_text(content)
        binding[key] = {"path": name, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}
    (directory / "recovery-binding.json").write_text(json.dumps(binding))
    return directory


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = Database()
    password_file = tmp_path / "password"
    password_file.write_text(PASSWORD + "\n")
    binding = write_binding(tmp_path / "recovery")
    monkeypatch.setenv("POSTGRES_URL", OWNER_URL)
    monkeypatch.setenv("BRAIN_APP_PASSWORD_FILE", str(password_file))
    monkeypatch.delenv("BRAIN_APP_GRANT_PG_CONTROL_SYSTEM", raising=False)
    monkeypatch.setattr(migrate_cli, "_new_engine", lambda settings: database)

    def upgrade(config: Any, target: str) -> None:
        assert target == "head"
        assert config.config_file_name == str(ROOT / "alembic.ini")
        assert migrate_cli.os.environ["POSTGRES_URL"] == OWNER_URL
        database.calls.append(("upgrade", {}))
        database.heads = ("064",)

    monkeypatch.setattr(migrate_cli.command, "upgrade", upgrade)
    return database, ["--recovery-dir", str(binding)], password_file, binding


def test_equal_head_proves_live_contract_without_upgrade_or_compat_write(runtime) -> None:
    database, arguments, _, _ = runtime
    assert migrate_cli.main(arguments) == 0
    sql = [call[0] for call in database.calls]
    assert "upgrade" not in sql
    assert not any("INSERT INTO public.brain_schema_compat" in statement for statement in sql)
    assert "SELECT 'fixture-receipt'" in sql
    assert "SELECT 'restored-receipt'" not in sql
    assert sql.index("SET TRANSACTION READ ONLY") < sql.index("SELECT 'fixture-receipt'")


def test_ancestor_upgrades_then_records_head_pinned_compat_before_proving(runtime) -> None:
    database, arguments, _, _ = runtime
    database.heads = ("063",)
    assert migrate_cli.main(arguments) == 0
    compat = [(sql, params) for sql, params in database.calls if "INSERT INTO" in sql]
    assert len(compat) == 1
    assert "public.brain_schema_compat" in compat[0][0]
    assert compat[0][1]["schema_head"] == "064"
    assert compat[0][1]["oldest_compatible_code_head"] == "064"
    assert compat[0][1]["recorded_by_version"]
    sql = [call[0] for call in database.calls]
    assert sql.index("upgrade") < sql.index(compat[0][0]) < sql.index("SELECT 'fixture-receipt'")


@pytest.mark.parametrize("head", ["065", "unrecognized-revision"])
def test_newer_or_unknown_database_refuses_without_changes_and_names_both_heads(
    runtime, head: str, capsys: pytest.CaptureFixture[str]
) -> None:
    database, arguments, _, _ = runtime
    database.heads = (head,)
    assert migrate_cli.main(arguments) == 1
    output = capsys.readouterr()
    assert head in output.out + output.err
    assert "064" in output.out + output.err
    assert all(sql == "dispose" for sql, _ in database.calls)


def test_empty_database_refuses_before_any_mutation(runtime, capsys) -> None:
    database, arguments, _, _ = runtime
    database.heads = ()
    assert migrate_cli.main(arguments) == 1
    output = capsys.readouterr()
    assert "database_not_initialized" in output.out + output.err
    assert all(sql == "dispose" for sql, _ in database.calls)


def test_multiple_database_heads_refuse(runtime) -> None:
    database, arguments, _, _ = runtime
    database.heads = ("062", "063")
    assert migrate_cli.main(arguments) == 1
    assert all(sql == "dispose" for sql, _ in database.calls)


@pytest.mark.parametrize("problem", ["missing", "unreadable", "empty", "unset"])
def test_password_file_is_required_before_upgrade(runtime, monkeypatch, problem: str) -> None:
    database, arguments, password_file, _ = runtime
    database.heads = ("062",)
    if problem == "missing":
        password_file.unlink()
    elif problem == "unreadable":
        password_file.unlink()
        password_file.mkdir()
    elif problem == "empty":
        password_file.write_text(" \n")
    else:
        monkeypatch.delenv("BRAIN_APP_PASSWORD_FILE")
    assert migrate_cli.main(arguments) == 1
    assert database.calls == []


def test_role_is_idempotent_rotates_verifier_without_creating_roles_or_granting_objects(
    runtime, capsys
) -> None:
    database, arguments, password_file, _ = runtime
    assert migrate_cli.main(arguments) == 0
    password_file.write_text("rotated-unit-password")
    assert migrate_cli.main(arguments) == 0
    sql = "\n".join(call[0] for call in database.calls)
    assert "pg_catalog.pg_roles" in sql
    assert "CREATE ROLE" not in sql
    for flag in (
        "LOGIN",
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
    ):
        assert flag in sql
    verifiers = [statement for statement, _ in database.calls if "PASSWORD" in statement]
    assert len(verifiers) == 2 and verifiers[0] != verifiers[1]
    assert all("PASSWORD 'SCRAM-SHA-256$4096:" in statement for statement in verifiers)
    assert "GRANT" not in sql and "REVOKE" not in sql
    assert "DEFAULT PRIVILEGES" not in sql
    assert "set_config" not in sql
    assert PASSWORD not in repr(database.calls)
    assert "rotated-unit-password" not in repr(database.calls)
    assert PASSWORD not in sql
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err
    assert "rotated-unit-password" not in output.out + output.err
    assert "unit-only-owner-password" not in output.out + output.err


def test_scram_verifier_matches_rfc_7677_fixed_salt_vector(monkeypatch) -> None:
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")

    def fixed_salt(size: int) -> bytes:
        assert size == 16
        return salt

    monkeypatch.setattr(scram.secrets, "token_bytes", fixed_salt)
    assert migrate_cli._scram_verifier("pencil") == (
        "SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$"
        "WG5d8oPm3OtcPnkdi4Uo7BkeZkBFzpcXkuLmtbsT4qY=:"
        "wfPLwcE6nTWhTAmQ7tl2KeoiWGPlZqQxSrmfPwDl2dU="
    )


def test_scram_verifier_uses_fresh_random_salts() -> None:
    first = migrate_cli._scram_verifier(PASSWORD)
    second = migrate_cli._scram_verifier(PASSWORD)
    assert first != second
    for verifier in (first, second):
        salt = verifier.split("$")[1].split(":")[1]
        assert len(base64.b64decode(salt)) == 16


def test_missing_runtime_role_refuses_without_role_mutation_or_proof(runtime, capsys) -> None:
    database, arguments, _, _ = runtime
    database.role_exists = False
    assert migrate_cli.main(arguments) == 1
    output = capsys.readouterr()
    assert "runtime_role_missing" in output.out + output.err
    sql = "\n".join(call[0] for call in database.calls)
    assert "ALTER ROLE" not in sql and "CREATE ROLE" not in sql
    assert "SELECT 'fixture-receipt'" not in sql


def test_role_connection_limit_tracks_engine_capacity_with_headroom(runtime, monkeypatch) -> None:
    database, arguments, _, _ = runtime
    monkeypatch.setattr(db_engine, "PG_POOL_SIZE", 37)
    monkeypatch.setattr(db_engine, "PG_MAX_OVERFLOW", 13)
    # Twenty percent headroom over the same hard cap used by get_engine().
    assert migrate_cli.main(arguments) == 0
    sql = "\n".join(call[0] for call in database.calls)
    assert "CONNECTION LIMIT 60" in sql


def test_engine_pool_uses_shared_capacity_constants(monkeypatch) -> None:
    factory = Mock()
    monkeypatch.setattr(db_engine, "_engine", None)
    monkeypatch.setattr(db_engine, "PG_POOL_SIZE", 37)
    monkeypatch.setattr(db_engine, "PG_MAX_OVERFLOW", 13)
    monkeypatch.setattr(db_engine, "get_settings", lambda: Settings(postgres_url=OWNER_URL))
    monkeypatch.setattr(db_engine, "create_async_engine", factory)
    db_engine.get_engine()
    assert factory.call_args.kwargs["pool_size"] == 37
    assert factory.call_args.kwargs["max_overflow"] == 13


def test_role_timeouts_reuse_interactive_engine_profile(runtime, monkeypatch) -> None:
    database, arguments, _, _ = runtime
    monkeypatch.setenv("PG_STATEMENT_TIMEOUT_MS", "1234")
    monkeypatch.setenv("PG_LOCK_TIMEOUT_MS", "567")
    monkeypatch.setenv("PG_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS", "9000")
    monkeypatch.setenv("EMBEDDING_TIMEOUT", "333")
    expected = pg_server_settings(Settings(postgres_url=OWNER_URL), "interactive")
    assert migrate_cli.main(arguments) == 0
    sql = "\n".join(call[0] for call in database.calls)
    for name, value in expected.items():
        if name != "application_name":
            assert f"ALTER ROLE brain_app SET {name} = '{value}'" in sql


@pytest.mark.parametrize("enabled", [None, "false", "true"])
def test_pg_control_system_grant_requires_explicit_opt_in(runtime, monkeypatch, enabled) -> None:
    database, arguments, _, _ = runtime
    if enabled is not None:
        monkeypatch.setenv("BRAIN_APP_GRANT_PG_CONTROL_SYSTEM", enabled)
    assert migrate_cli.main(arguments) == 0
    sql = "\n".join(call[0] for call in database.calls)
    assert ("GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO brain_app" in sql) == (
        enabled == "true"
    )


@pytest.mark.parametrize("stage", ["PASSWORD", "SELECT 'fixture-receipt'"])
def test_failures_never_render_exception_passwords_or_owner_dsn(runtime, capsys, stage) -> None:
    database, arguments, _, _ = runtime
    database.fail_sql = stage
    assert migrate_cli.main(arguments) == 1
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err
    assert "unit-only-owner-password" not in output.out + output.err


@pytest.mark.parametrize(
    "mutation", ["failed", "empty", "wrong_identity", "wrong_version", "duplicate"]
)
def test_contract_receipt_must_be_complete_and_pass_every_check(runtime, mutation) -> None:
    database, arguments, _, _ = runtime
    if mutation == "failed":
        database.receipt["checks"][0]["status"] = "fail"
    elif mutation == "empty":
        database.receipt["checks"] = []
    elif mutation == "wrong_identity":
        database.receipt["contract_id"] = "another/contract"
    elif mutation == "wrong_version":
        database.receipt["schema_version"] = 2
    else:
        database.receipt["checks"] *= 2
    assert migrate_cli.main(arguments) == 1


@pytest.mark.parametrize("mutation", ["digest", "head", "identity", "escape", "symlink"])
def test_binding_and_every_asset_are_verified_before_mutations(runtime, mutation, tmp_path) -> None:
    database, arguments, _, directory = runtime
    path = directory / "recovery-binding.json"
    binding = json.loads(path.read_text())
    if mutation == "digest":
        (directory / "restored.sql").write_text("changed")
    elif mutation == "head":
        binding["schema_head"] = "062"
    elif mutation == "identity":
        binding["contract_id"] = "another/contract"
    elif mutation == "escape":
        binding["attestation_sql"]["path"] = "../outside.sql"
    else:
        outside = tmp_path / "outside.sql"
        outside.write_text((directory / "live.sql").read_text())
        (directory / "live.sql").unlink()
        (directory / "live.sql").symlink_to(outside)
    path.write_text(json.dumps(binding))
    assert migrate_cli.main(arguments) == 1
    assert database.calls == []


def test_engine_uses_owner_url_unbounded_migration_settings_and_hidden_parameters(
    monkeypatch,
) -> None:
    factory = Mock()
    monkeypatch.setattr(migrate_cli, "create_async_engine", factory)
    migrate_cli._new_engine(Settings(postgres_url=OWNER_URL))
    assert factory.call_args.args == (OWNER_URL,)
    options = factory.call_args.kwargs
    assert options["echo"] is False and options["hide_parameters"] is True
    server_settings = options["connect_args"]["server_settings"]
    assert server_settings["statement_timeout"] == "0"
    assert server_settings["idle_in_transaction_session_timeout"] == "0"


def test_cli_returns_usage_code_without_connecting(runtime) -> None:
    database, _, _, _ = runtime
    assert migrate_cli.main(["--unknown"]) == 2
    assert database.calls == []


def test_console_script_is_registered() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["project"]["scripts"]["brain-v42-migrate"] == "brain_v42.scripts.migrate_cli:main"


def test_installed_alembic_ini_wins_over_checkout_and_current_directory(
    tmp_path, monkeypatch
) -> None:
    package = tmp_path / "site-packages" / "brain_v42"
    scripts = package / "scripts"
    scripts.mkdir(parents=True)
    ini = package / "alembic.ini"
    ini.write_text("[alembic]\nscript_location = %(here)s/alembic\n")
    monkeypatch.setattr(migrate_cli, "__file__", str(scripts / "migrate_cli.py"))
    monkeypatch.chdir(tmp_path)
    assert migrate_cli._alembic_config().config_file_name == str(ini)


def test_failed_upgrade_does_not_record_compatibility_or_provision_role(
    runtime, monkeypatch
) -> None:
    database, arguments, _, _ = runtime
    database.heads = ("062",)

    def fail_upgrade(config, target) -> None:
        raise RuntimeError(OWNER_URL)

    monkeypatch.setattr(migrate_cli.command, "upgrade", fail_upgrade)
    assert migrate_cli.main(arguments) == 1
    assert all(sql == "dispose" for sql, _ in database.calls)


def test_upgrade_that_does_not_reach_code_head_is_refused(runtime, monkeypatch) -> None:
    database, arguments, _, _ = runtime
    database.heads = ("062",)
    monkeypatch.setattr(migrate_cli.command, "upgrade", lambda config, target: None)
    assert migrate_cli.main(arguments) == 1
    assert all(sql == "dispose" for sql, _ in database.calls)


@pytest.mark.parametrize("url", [None, "invalid", "postgresql+asyncpg://owner@localhost:5432/test"])
def test_explicit_owner_url_is_required_and_never_falls_back(runtime, monkeypatch, url) -> None:
    database, arguments, _, _ = runtime
    if url is None:
        monkeypatch.delenv("POSTGRES_URL")
    else:
        monkeypatch.setenv("POSTGRES_URL", url)
    assert migrate_cli.main(arguments) == 1
    assert database.calls == []


def test_boolean_receipt_version_is_not_a_valid_contract_version(runtime) -> None:
    database, arguments, _, _ = runtime
    database.receipt["schema_version"] = True
    assert migrate_cli.main(arguments) == 1
