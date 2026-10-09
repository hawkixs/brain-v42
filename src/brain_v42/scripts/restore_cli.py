"""Restore a verified archive once, refusing every ambiguous initialization state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy.engine import make_url

from brain_v42.config import Settings
from brain_v42.release_recovery import RESTORE_MARKER

logger = structlog.get_logger(__name__)
_ROLES = frozenset({"brain", "brain_app", "codex_ro", "pg_database_owner"})
_BOOTSTRAP_ROLES = frozenset({"brain_app", "codex_ro"})
_STATE_SQL = """
SELECT json_build_object(
    'relations', COALESCE((
        SELECT json_agg(json_build_array(c.relname, c.relkind))
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
    ), '[]'::json),
    'marker', pg_catalog.shobj_description(d.oid, 'pg_database')
)
FROM pg_catalog.pg_database AS d WHERE d.datname = current_database();
"""
_BOOTSTRAP_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{role}') THEN
        CREATE ROLE {role} NOLOGIN;
    END IF;
END
$$;
"""
_PG_OPTIONS = {
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "connect_timeout": "PGCONNECT_TIMEOUT",
}


class RestoreError(Exception):
    """Carry only a fixed reason code so database errors cannot leak credentials."""


def _connection() -> tuple[list[str], dict[str, str]]:
    # A one-shot CLI must read its own environment, not another caller's cache.
    settings = Settings()  # type: ignore[call-arg]  # loaded from the environment
    url = make_url(settings.postgres_url)
    if (
        url.get_backend_name() != "postgresql"
        or url.database != "brain"
        or not url.username
        or not url.host
        or set(url.query) - _PG_OPTIONS.keys()
    ):
        raise RestoreError("invalid_restore_connection")
    args = [
        "--host",
        url.host,
        "--port",
        str(url.port or 5432),
        "--username",
        url.username,
        "--dbname",
        "brain",
        "--no-password",
    ]
    # Inherited libpq settings must not redirect a restore to a different cluster.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PG") and key not in {"POSTGRES_URL", "BRAIN_POSTGRES_URL"}
    }
    env["PGPASSWORD"] = url.password or ""
    for key, value in url.query.items():
        if not isinstance(value, str):
            raise RestoreError("invalid_restore_connection")
        env[_PG_OPTIONS[key]] = value
    return args, env


def _run(args: list[str], env: dict[str, str], *, keep_trailing_space: bool = False) -> str:
    result = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        # Neither stderr nor the command/DSN belongs in a diagnostic.
        raise RestoreError("restore_subprocess_failed")
    if keep_trailing_space:
        return result.stdout.strip("\n")
    return result.stdout.strip()


def _sql(statement: str, connection: list[str], env: dict[str, str]) -> str:
    return _run(
        [
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--set",
            "ON_ERROR_STOP=1",
            *connection,
            "--command",
            statement,
        ],
        env,
    )


def _state(connection: list[str], env: dict[str, str]) -> str:
    state = json.loads(_sql(_STATE_SQL, connection, env))
    if not isinstance(state, dict) or not isinstance(state.get("relations"), list):
        raise RestoreError("unrecognized_initialization")
    relations = state["relations"]
    if not relations:
        return "empty"
    marker = state.get("marker")
    match = RESTORE_MARKER.fullmatch(marker) if isinstance(marker, str) else None
    if ["alembic_version", "r"] in relations and match:
        try:
            datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise RestoreError("unrecognized_initialization") from None
        if _sql("SELECT count(*) FROM public.alembic_version;", connection, env) == "1":
            return "marked"
    raise RestoreError("unrecognized_initialization")


def _verify_dump(dump: Path) -> str:
    sidecar = Path(str(dump) + ".sha256").read_text(encoding="utf-8").strip()
    match = re.fullmatch(r"([0-9a-fA-F]{64})(?:[ \t]+\*?[^\r\n]+)?", sidecar)
    if not match:
        raise RestoreError("invalid_dump_checksum")
    with dump.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != match[1].lower():
        raise RestoreError("dump_checksum_mismatch")
    return digest


def _check_role_list(raw: str, *, allow_public: bool = False) -> set[str]:
    roles = set()
    for role in raw.split(","):
        role = role.strip()
        if role == "PUBLIC" and allow_public:
            continue  # PUBLIC is the built-in ACL pseudo-role, not a cluster role.
        if role.startswith('"') and role.endswith('"'):
            role = role[1:-1].replace('""', '"')
        if role not in _ROLES:
            raise RestoreError("dump_role_not_allowed")
        roles.add(role)
    return roles


def _verify_roles(dump: Path, env: dict[str, str]) -> set[str]:
    roles = set()
    # The trailing space is significant: pg_restore --list leaves the owner
    # column empty, ending the line with a space, for ownerless entries such
    # as extensions, whose last word is then their name, not a role.
    toc = _run(["pg_restore", "--list", str(dump)], env, keep_trailing_space=True)
    for line in toc.splitlines():
        if not line or line.startswith(";"):
            continue
        if not re.match(r"^\d+;\s+\d+\s+\d+\s+", line):
            raise RestoreError("invalid_dump_toc")
        if line[-1].isspace():
            continue
        owner = line.rsplit(None, 1)[-1]
        if owner != "-":
            roles.update(_check_role_list(owner))
    # TOC ACL entries name their owner, not their grantees. Render schema only,
    # without connecting, to also validate recipients and default-ACL grantors.
    schema = _run(["pg_restore", "--schema-only", "--file=-", str(dump)], env)
    for match in re.finditer(r"\bOWNER TO\s+([^;]+);", schema):
        roles.update(_check_role_list(match[1]))
    for match in re.finditer(
        r"^\s*(?:ALTER DEFAULT PRIVILEGES\b[^;]+?\b)?"
        r"(?:GRANT\b[^;]+?\bTO|REVOKE\b[^;]+?\bFROM)\s+([^;]+);",
        schema,
        re.MULTILINE,
    ):
        recipients = match[1]
        grantor = re.search(r"\s+GRANTED BY\s+(.+)$", recipients)
        if grantor:
            roles.update(_check_role_list(grantor[1]))
            recipients = recipients[: grantor.start()]
        recipients = re.sub(r"\s+(?:WITH GRANT OPTION|CASCADE|RESTRICT)\s*$", "", recipients)
        roles.update(_check_role_list(recipients, allow_public=True))
    for match in re.finditer(
        r"\bALTER DEFAULT PRIVILEGES FOR (?:ROLE|USER)\s+(.+?)(?=\s+IN SCHEMA|\s+GRANT|\s+REVOKE)",
        schema,
        re.DOTALL,
    ):
        roles.update(_check_role_list(match[1]))
    return roles


def main(argv: list[str] | None = None) -> int:
    """Bless only a completed transactional restore; ambiguous states need a fresh volume."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        connection, env = _connection()
        if _state(connection, env) == "marked":
            logger.info("restore_already_initialized")
            return 0
        dump = args.dump.resolve()
        digest = _verify_dump(dump)
        roles = _verify_roles(dump, env)
        # ACL recipients must exist before pg_restore, including migration 064's
        # runtime role. PostgreSQL supplies pg_database_owner; never create it.
        for role in sorted(roles & _BOOTSTRAP_ROLES):
            _sql(_BOOTSTRAP_SQL.format(role=role), connection, env)
        _run(
            [
                "pg_restore",
                "--single-transaction",
                "--exit-on-error",
                "--no-owner",
                *connection,
                str(dump),
            ],
            env,
        )
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        marker = f"brain-v42-restore sha256={digest} at={timestamp}"
        _sql(f"COMMENT ON DATABASE brain IS '{marker}';", connection, env)
        logger.info("restore_completed")
        return 0
    except RestoreError as exc:
        # A cached logger may be bound to another sink; stderr belongs to the CLI.
        sys.stderr.write(f"restore_refused reason_code={exc}\n")
        logger.error("restore_refused", reason_code=str(exc))
    except Exception:  # noqa: BLE001 -- configuration/subprocess errors may contain secrets
        sys.stderr.write("restore_failed reason_code=restore_runtime_error\n")
        logger.error("restore_failed", reason_code="restore_runtime_error")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
