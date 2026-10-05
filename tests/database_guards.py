"""Structural refusal of production databases in the test sessions.

Tickets `e3292865` (40 rows written to production ``dream_runs`` on 2026-09-21)
and `2687faf0` (796 ``integ-%`` nodes in production Neo4j, in no PostgreSQL).

Before this module the only refusal of the production DATABASE NAME lived in
``tests/integration/conftest.py``. The unit suite -- 17 of the 18 callers of
``require_test_db_url()`` -- had none: a ``BRAIN_V42_TEST_DB_URL`` naming ``brain``
was accepted, and its tests commit their rows. Writing a learning, ADR, runbook or
decision there also enqueues ``graph_outbox`` through the registry triggers of
migration 033, which the production projector drains into production Neo4j.
Nothing identified a production Neo4j at all.

The rule is applied once, at ``pytest_configure``, through
:func:`enforce_database_isolation`, so it holds for every suite and before any
engine, driver or subprocess exists. The same validators are re-used where a
fixture resolves a URL at run time. Messages never carry a DSN: it holds a password
and the line is pasted into reports.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).parents[1]

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104
_CANONICAL_LOOPBACK = "127.0.0.1"

# An address nothing listens on: a connection attempt is refused at once, never hangs.
UNREACHABLE_POSTGRES_URL = (
    "postgresql+asyncpg://unit-test:unit-test@127.0.0.1:1/brain_unit_test_unreachable"
)
UNREACHABLE_NEO4J_URL = "bolt://127.0.0.1:1"

_POSTGRES_URL_NAMES = ("POSTGRES_URL", "BRAIN_POSTGRES_URL", "BRAIN_DELIVERY_POSTGRES_URL")
_NEO4J_URL_NAMES = (
    "NEO4J_URL",
    "BRAIN_NEO4J_URL",
    "GRAPH_PROJECTOR_NEO4J_URL",
    "BRAIN_GRAPH_PROJECTOR_NEO4J_URL",
)
# Compose publishes production bolt on 127.0.0.1:7687 (docker-compose.yml, neo4j).
_COMPOSE_PRODUCTION_NEO4J = (_CANONICAL_LOOPBACK, 7687)
_PROJECTOR_ENV_FILE = Path.home() / ".config" / "brain-v42" / "graph-projector.env"

# Neo4j test data only ever carries these project-key prefixes: ``integ-`` (the
# shared purge prefix, tests/unit/keys.py) and ``test-`` (test_graph_integration).
TEST_NEO4J_PROJECT_PREFIXES = ("integ-", "test-")

_TEST_DATABASE_NAME = re.compile(r"brain_(test|migration|fresh)(_[a-z0-9_]+)?|[a-z0-9_]+_test")
_CONNECTION_OVERRIDES = frozenset({"database", "dbname", "host", "hostaddr", "port"})

PostgresIdentity = tuple[str, int, str]
Neo4jIdentity = tuple[str, int]


class UnsafeTestDatabase(ValueError):
    """A test session was pointed at a database it must never write to."""


_production_postgres: set[PostgresIdentity] = set()
_production_neo4j: set[Neo4jIdentity] = {_COMPOSE_PRODUCTION_NEO4J}


def _normalise_host(host: str | None) -> str:
    lowered = (host or "").lower()
    return _CANONICAL_LOOPBACK if lowered in _LOOPBACK_HOSTS else lowered


def postgres_identity(url: str) -> PostgresIdentity:
    """``(host, port, database)`` of a DSN: credentials, scheme and loopback spelling erased."""
    parsed = urlparse(url)
    return (
        _normalise_host(parsed.hostname),
        parsed.port or 5432,
        unquote(parsed.path).lstrip("/"),
    )


def neo4j_identity(url: str) -> Neo4jIdentity:
    """``(host, port)`` of a bolt URL: scheme, credentials and loopback spelling erased."""
    parsed = urlparse(url)
    return _normalise_host(parsed.hostname), parsed.port or 7687


def is_test_database_name(name: str) -> bool:
    """True for ``brain_test``, ``brain_migration_*``, ``brain_fresh_*`` and ``*_test``."""
    return _TEST_DATABASE_NAME.fullmatch(name.lower()) is not None


def _configured(
    environ: Mapping[str, str], dotenv_files: Iterable[Path], names: Iterable[str]
) -> dict[str, str]:
    """Value of each name found in the environment, else in the first dotenv carrying it."""
    found: dict[str, str] = {}
    for name in names:
        value = environ.get(name)
        if not value:
            for path in dotenv_files:
                value = dotenv_values(path).get(name) if path.is_file() else None
                if value:
                    break
        if value:
            found[name] = value
    return found


def production_postgres_identities(
    environ: Mapping[str, str], dotenv_files: Iterable[Path]
) -> frozenset[PostgresIdentity]:
    """The databases the application is configured against, minus test-named ones.

    A test-named ``POSTGRES_URL`` is a deliberate test configuration -- CI exports the
    same ``brain_test`` in both variables -- so it is not a production identity.
    """
    identities = {
        postgres_identity(value)
        for value in _configured(environ, dotenv_files, _POSTGRES_URL_NAMES).values()
    }
    return frozenset(i for i in identities if not is_test_database_name(i[2]))


def production_neo4j_identities(
    environ: Mapping[str, str], dotenv_files: Iterable[Path], env_files: Iterable[Path]
) -> frozenset[Neo4jIdentity]:
    """Every Neo4j the application is configured against, plus the compose bolt port.

    ``env_files`` (the projector's private file) are read for the URL keys only.
    """
    values = _configured(environ, [*dotenv_files, *env_files], _NEO4J_URL_NAMES)
    return frozenset({_COMPOSE_PRODUCTION_NEO4J, *(neo4j_identity(v) for v in values.values())})


def registered_production_postgres_identities() -> frozenset[PostgresIdentity]:
    return frozenset(_production_postgres)


def validate_test_database_url(
    url: str, production: Iterable[PostgresIdentity] | None = None
) -> str:
    """Return ``url`` when it can only be a test database, else raise.

    ``production`` defaults to the identities recorded by the session hook.
    """
    parsed = urlparse(url)
    name = unquote(parsed.path).lstrip("/")
    if not name:
        raise UnsafeTestDatabase("an explicit test database path is required")
    keys = {key.casefold() for key, _v in parse_qsl(parsed.query, keep_blank_values=True)}
    if keys & _CONNECTION_OVERRIDES:
        raise UnsafeTestDatabase("connection override query parameters are forbidden")
    forbidden = _production_postgres if production is None else set(production)
    if postgres_identity(url) in forbidden:
        raise UnsafeTestDatabase(
            "it names the same database as POSTGRES_URL (host, port and database name)"
        )
    if not is_test_database_name(name):
        raise UnsafeTestDatabase(
            f"'{name}' is not a test database: use brain_test, brain_migration_*, "
            "brain_fresh_* or a name ending in _test"
        )
    return url


def validate_test_neo4j_url(url: str, production: Iterable[Neo4jIdentity] | None = None) -> str:
    """Return ``url`` unless it addresses a production Neo4j."""
    forbidden = _production_neo4j if production is None else set(production)
    if neo4j_identity(url) in forbidden:
        raise UnsafeTestDatabase(
            "it addresses the production Neo4j (an address the application is configured "
            "against, or the compose bolt port 7687 on loopback)"
        )
    return url


async def refuse_neo4j_holding_production_data(driver: Any) -> None:
    """Refuse a Neo4j that holds a project the tests could not have written.

    The address rule cannot see production reached through a tunnel or another
    hostname; its CONTENT can. Test data carries ``integ-``/``test-`` project keys
    only, and every production entity belongs to a real project. Read-only.
    """
    async with driver.session() as session:
        result = await session.run(
            "MATCH (n) WHERE n.project_key IS NOT NULL "
            "AND NOT any(prefix IN $prefixes WHERE n.project_key STARTS WITH prefix) "
            "RETURN n.project_key AS project_key LIMIT 1",
            {"prefixes": list(TEST_NEO4J_PROJECT_PREFIXES)},
        )
        record = await result.single()
    if record is not None:
        raise UnsafeTestDatabase(
            "the Neo4j target holds production data (a project key outside "
            f"{'/'.join(TEST_NEO4J_PROJECT_PREFIXES)}) -- refusing to write to it"
        )


def _refuse(variable: str, check: Any, value: str) -> None:
    try:
        check(value)
    except UnsafeTestDatabase as exc:
        raise UnsafeTestDatabase(f"{variable} refused: {exc}") from None


def enforce_database_isolation(
    environ: MutableMapping[str, str] | None = None,
    dotenv_files: Iterable[Path] | None = None,
    projector_env_files: Iterable[Path] | None = None,
) -> None:
    """Make this process unable to reach a production database, or raise.

    1. Record the production identities (environment, ``.env``, projector file).
    2. Refuse ``BRAIN_V42_TEST_DB_URL`` / ``BRAIN_V42_TEST_NEO4J_URL`` naming one.
    3. Point application settings at the test database, or at nothing: ``Settings``
       reads ``POSTGRES_URL`` (and ``.env``), so code under test that builds its own
       engine from settings would otherwise open the production database.
    """
    env = os.environ if environ is None else environ
    dotenvs = (
        [Path.cwd() / ".env", PROJECT_ROOT / ".env"] if dotenv_files is None else list(dotenv_files)
    )
    projector_files = (
        [_PROJECTOR_ENV_FILE] if projector_env_files is None else list(projector_env_files)
    )

    _production_postgres.update(production_postgres_identities(env, dotenvs))
    _production_neo4j.update(production_neo4j_identities(env, dotenvs, projector_files))

    test_db_url = (env.get("BRAIN_V42_TEST_DB_URL") or "").strip()
    if test_db_url:
        _refuse("BRAIN_V42_TEST_DB_URL", validate_test_database_url, test_db_url)
    test_neo4j_url = (env.get("BRAIN_V42_TEST_NEO4J_URL") or "").strip()
    if test_neo4j_url:
        _refuse("BRAIN_V42_TEST_NEO4J_URL", validate_test_neo4j_url, test_neo4j_url)

    if _configured(env, dotenvs, ("POSTGRES_URL", "BRAIN_POSTGRES_URL")):
        env["POSTGRES_URL"] = test_db_url or UNREACHABLE_POSTGRES_URL
        env.pop("BRAIN_POSTGRES_URL", None)
    delivery = env.get("BRAIN_DELIVERY_POSTGRES_URL")
    if delivery and postgres_identity(delivery) in _production_postgres:
        del env["BRAIN_DELIVERY_POSTGRES_URL"]

    for bare in ("NEO4J_URL", "GRAPH_PROJECTOR_NEO4J_URL"):
        if _configured(env, dotenvs, (bare, f"BRAIN_{bare}")):
            env[bare] = UNREACHABLE_NEO4J_URL
            env.pop(f"BRAIN_{bare}", None)
