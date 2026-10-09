"""Migrate forward and prove this image's contract before any server starts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from brain_v42.config import Settings
from brain_v42.db import engine as db_engine
from brain_v42.db.engine import pg_server_settings
from brain_v42.db.scram import scram_verifier as _scram_verifier
from brain_v42.release import package_version
from brain_v42.release_recovery import (
    ASSET_KEYS,
    BINDING_FILENAME,
    MAX_BINDING_BYTES,
    RESTORE_MARKER,
    is_safe_asset_filename,
)

logger = structlog.get_logger(__name__)
_RECOVERY_DIR = Path("/app/recovery")
_ROLE_OPTIONS = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"


class MigrationRefused(RuntimeError):
    """A safe reason code, never driver text that could carry credentials."""

    def __init__(self, reason: str, **details: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


@dataclass(frozen=True)
class RecoveryContract:
    contract_id: str
    version: int
    check_ids: frozenset[str]
    live_sql: str
    restored_sql: str


def _alembic_config() -> Config:
    """Prefer the wheel's own revisions; source runs stay anchored to this file."""
    package = Path(__file__).resolve().parents[1]
    ini = package / "alembic.ini"
    if not ini.is_file() and package.parent.name == "src":
        ini = package.parent.parent / "alembic.ini"
    if not ini.is_file():
        raise MigrationRefused("image_migrations_missing")
    return Config(str(ini))


def _owner_settings() -> Settings:
    """Use the explicit migration owner URL, including Alembic's production guard."""
    raw = os.environ.get("POSTGRES_URL")
    if not raw:
        raise MigrationRefused("postgres_url_required")
    try:
        url = make_url(raw)
        valid = (
            url.drivername == "postgresql+asyncpg"
            and not url.query
            and url.database
            and url.host
            and url.username
            and url.password
            and url.port is not None
            and 1 <= url.port <= 65535
        )
    except Exception:  # noqa: BLE001 - never render a URL parsing error
        raise MigrationRefused("postgres_url_invalid") from None
    if not valid:
        raise MigrationRefused("postgres_url_invalid")
    if url.database == "brain" and os.environ.get(
        "BRAIN_ALEMBIC_ALLOW_PROD", ""
    ).casefold() not in {
        "1",
        "true",
        "yes",
    }:
        raise MigrationRefused("production_migration_not_authorized")
    return Settings(postgres_url=raw)


def _app_password() -> str:
    path = os.environ.get("BRAIN_APP_PASSWORD_FILE")
    if not path:
        raise MigrationRefused("app_password_file_required")
    try:
        password = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise MigrationRefused("app_password_file_unreadable") from None
    if not password or "\x00" in password:
        raise MigrationRefused("app_password_file_empty_or_invalid")
    return password


def _json_object(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise MigrationRefused("recovery_contract_invalid")
    return value


def _check_ids(checks: Any) -> frozenset[str]:
    if not isinstance(checks, list) or not checks:
        raise MigrationRefused("recovery_checks_invalid")
    ids = [check.get("id") if isinstance(check, dict) else None for check in checks]
    if any(not isinstance(check_id, str) or not check_id for check_id in ids):
        raise MigrationRefused("recovery_checks_invalid")
    if len(set(ids)) != len(ids):
        raise MigrationRefused("recovery_checks_invalid")
    return frozenset(str(check_id) for check_id in ids)


def _load_contract(directory: Path, code_head: str) -> RecoveryContract:
    """Verify the baked binding and every copied asset before executing attestation SQL."""
    binding_path = directory / BINDING_FILENAME
    if directory.is_symlink() or binding_path.is_symlink() or not binding_path.is_file():
        raise MigrationRefused("recovery_binding_missing")
    if binding_path.stat().st_size > MAX_BINDING_BYTES:
        raise MigrationRefused("recovery_binding_invalid")
    binding = _json_object(binding_path.read_text(encoding="utf-8"))
    if binding.get("schema_head") != code_head:
        raise MigrationRefused("recovery_binding_head_mismatch")
    contract_id = binding.get("contract_id")
    version = binding.get("contract_version")
    if not isinstance(contract_id, str) or not contract_id or type(version) is not int:
        raise MigrationRefused("recovery_binding_invalid")
    assets: dict[str, str] = {}
    for key in ASSET_KEYS:
        entry = binding.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise MigrationRefused("recovery_asset_invalid")
        name = entry["path"]
        if not is_safe_asset_filename(name):
            raise MigrationRefused("recovery_asset_invalid")
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise MigrationRefused("recovery_asset_invalid")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise MigrationRefused("recovery_asset_digest_mismatch")
        assets[key] = data.decode("utf-8")
    manifest = _json_object(assets["manifest"])
    if manifest.get("contract_id") != contract_id or manifest.get("schema_version") != version:
        raise MigrationRefused("recovery_contract_identity_mismatch")
    return RecoveryContract(
        contract_id,
        version,
        _check_ids(manifest.get("checks")),
        assets["attestation_sql"],
        assets["restored_attestation_sql"],
    )


def _new_engine(settings: Settings) -> AsyncEngine:
    """Owner connections must not inherit runtime budgets or expose bind parameters."""
    return create_async_engine(
        settings.postgres_url,
        poolclass=pool.NullPool,
        echo=False,
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "statement_timeout": "0",
                "lock_timeout": "0",
                "idle_in_transaction_session_timeout": "0",
            }
        },
    )


def _current_heads(connection: Connection) -> tuple[str, ...]:
    return MigrationContext.configure(connection).get_current_heads()


async def _read_heads(settings: Settings) -> tuple[str, ...]:
    engine = _new_engine(settings)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_current_heads)
    finally:
        await engine.dispose()


async def _provision_role(connection: AsyncConnection, settings: Settings, verifier: str) -> None:
    """Configure attributes only: migration 064 owns the reproducible object ACLs."""
    exists = await connection.scalar(
        text("SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'brain_app')")
    )
    if not exists:
        raise MigrationRefused("runtime_role_missing")
    capacity = db_engine.PG_POOL_SIZE + db_engine.PG_MAX_OVERFLOW
    # A single interactive pool plus 20% headroom for transient runtime connections.
    connection_limit = capacity + math.ceil(capacity * 0.2)
    await connection.execute(
        text(f"ALTER ROLE brain_app {_ROLE_OPTIONS} CONNECTION LIMIT {connection_limit}")
    )
    # ALTER ROLE cannot bind a password. The generated verifier contains only
    # base64 and SCRAM separators, never quotes or cleartext, and PostgreSQL stores
    # an already-encrypted SCRAM verifier unchanged (independent of password_encryption).
    await connection.exec_driver_sql(f"ALTER ROLE brain_app PASSWORD '{verifier}'")
    for name, value in pg_server_settings(settings, "interactive").items():
        if name != "application_name":
            # Profile values are decimal millisecond strings, never operator SQL.
            await connection.execute(text(f"ALTER ROLE brain_app SET {name} = '{int(value)}'"))
    if os.environ.get("BRAIN_APP_GRANT_PG_CONTROL_SYSTEM", "").casefold() == "true":
        # R1 decides this optional pg_catalog ACL, outside the public schema contract.
        await connection.execute(
            text("GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO brain_app")
        )


def _receipt_checks(
    raw: Any, contract: RecoveryContract, code_head: str
) -> dict[str, dict[str, Any]]:
    """Require a complete receipt before selecting which checks it must prove."""
    try:
        receipt = _json_object(str(raw))
        if (
            receipt.get("contract_id") != contract.contract_id
            or type(receipt.get("schema_version")) is not int
            or receipt.get("schema_version") != contract.version
            or _check_ids(receipt.get("checks")) != contract.check_ids
        ):
            raise MigrationRefused("recovery_contract_not_proven")
    except (ValueError, MigrationRefused):
        raise MigrationRefused("recovery_contract_not_proven", code_head=code_head) from None
    return {check["id"]: check for check in receipt["checks"]}


async def _finish(
    settings: Settings,
    verifier: str,
    code_head: str,
    contract: RecoveryContract,
    *,
    upgraded: bool,
) -> None:
    engine = _new_engine(settings)
    try:
        async with engine.begin() as connection:
            if await connection.run_sync(_current_heads) != (code_head,):
                raise MigrationRefused("database_head_changed", code_head=code_head)
            if upgraded:
                await connection.execute(
                    text(
                        "INSERT INTO public.brain_schema_compat "
                        "(schema_head, oldest_compatible_code_head, recorded_by_version) "
                        "VALUES (:schema_head, :oldest_compatible_code_head, :recorded_by_version) "
                        "ON CONFLICT (schema_head) DO UPDATE SET "
                        "oldest_compatible_code_head = EXCLUDED.oldest_compatible_code_head, "
                        "recorded_by_version = EXCLUDED.recorded_by_version, recorded_at = now()"
                    ),
                    {
                        "schema_head": code_head,
                        "oldest_compatible_code_head": code_head,
                        "recorded_by_version": package_version(),
                    },
                )
        async with engine.begin() as connection:
            await _provision_role(connection, settings, verifier)
        # The attestation is observational: a malicious or broken SQL asset must
        # not be able to repair the very schema it is supposed to prove.
        async with engine.begin() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            marker = await connection.scalar(
                text(
                    "SELECT pg_catalog.shobj_description(d.oid, 'pg_database') "
                    "FROM pg_catalog.pg_database d WHERE d.datname = current_database()"
                )
            )
            # pg_restore changes deparsed fingerprints, so marked databases need
            # the restored variant plus LIVE proof of the sandbox's skipped checks.
            # The marker persists: every later migrate on this host takes this path;
            # databases built by migrations without a marker keep the LIVE path.
            restored = isinstance(marker, str) and RESTORE_MARKER.fullmatch(marker) is not None
            attestation = "restored" if restored else "live"
            checks = _receipt_checks(
                await connection.scalar(
                    text(contract.restored_sql if restored else contract.live_sql)
                ),
                contract,
                code_head,
            )
            if any(check.get("status") != "pass" for check in checks.values()):
                raise MigrationRefused("recovery_contract_not_proven", code_head=code_head)
            if restored:
                live_checks = _receipt_checks(
                    await connection.scalar(text(contract.live_sql)), contract, code_head
                )
                required = {
                    check_id
                    for check_id, check in checks.items()
                    if isinstance(check.get("observed"), str)
                    and check["observed"].startswith("not_applicable")
                }
                if any(live_checks[check_id].get("status") != "pass" for check_id in required):
                    raise MigrationRefused("recovery_contract_not_proven", code_head=code_head)
        logger.info(
            "migration_contract_proven",
            code_head=code_head,
            contract_id=contract.contract_id,
            attestation=attestation,
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain-v42-migrate", description=__doc__)
    parser.add_argument("--recovery-dir", type=Path, default=_RECOVERY_DIR)
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        settings = _owner_settings()
        verifier = _scram_verifier(_app_password())
        config = _alembic_config()
        scripts = ScriptDirectory.from_config(config)
        code_head = scripts.get_current_head()
        if code_head is None:
            raise MigrationRefused("image_head_missing")
        known_heads = {revision.revision for revision in scripts.walk_revisions()}
        contract = _load_contract(arguments.recovery_dir, code_head)
        database_heads = asyncio.run(_read_heads(settings))
        if not database_heads:
            raise MigrationRefused("database_not_initialized", code_head=code_head)
        if len(database_heads) != 1 or database_heads[0] not in known_heads:
            raise MigrationRefused(
                "database_head_incompatible",
                database_head=",".join(database_heads),
                code_head=code_head,
            )
        upgraded = database_heads != (code_head,)
        if upgraded:
            logger.info("migration_upgrade", database_head=database_heads[0], code_head=code_head)
            # env.py connects with POSTGRES_URL and overrides role-level timeouts.
            # It owns asyncio.run(), so never invoke it from our async operations.
            command.upgrade(config, "head")
        asyncio.run(_finish(settings, verifier, code_head, contract, upgraded=upgraded))
    except MigrationRefused as exc:
        logger.error("migration_refused", reason=exc.reason, **exc.details)
        return 1
    except Exception as exc:  # noqa: BLE001 - SQL/URL/validation errors can contain secrets
        logger.error("migration_failed", error_type=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
