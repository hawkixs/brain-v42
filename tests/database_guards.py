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
from ipaddress import IPv6Address, ip_address
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
# domain_backfill, ticket_extract, roadmap_curate and canary_roadmap_model all
# load this file through domain_backfill.load_env_file (os.environ.setdefault).
_SETDEFAULT_ENV_FILES = (Path.home() / ".config" / "brain-v42" / "nvidia.env",)

# Neo4j test data only ever carries these project-key prefixes: ``integ-`` (the
# shared purge prefix, tests/unit/keys.py) and ``test-`` (test_graph_integration).
TEST_NEO4J_PROJECT_PREFIXES = ("integ-", "test-")

_TEST_DATABASE_NAME = re.compile(
    r"brain_(test|migration|fresh)(_[a-z0-9_]+)?|[a-z0-9_]+_test"
    r"|brain_[a-z][a-z0-9]*(?:_[a-z0-9]+)*_[0-9a-f]{8,}"
)
_CONNECTION_OVERRIDES = frozenset({"database", "dbname", "host", "hostaddr", "port"})

PostgresIdentity = tuple[str, int, str]
Neo4jIdentity = tuple[str, int]


class UnsafeTestDatabase(ValueError):
    """A test session was pointed at a database it must never write to."""


_production_postgres: set[PostgresIdentity] = set()
_production_neo4j: set[Neo4jIdentity] = {_COMPOSE_PRODUCTION_NEO4J}


def _normalise_host(host: str | None) -> str:
    lowered = (host or "").lower()
    if lowered in _LOOPBACK_HOSTS:
        return _CANONICAL_LOOPBACK
    try:
        address = ip_address(lowered)
    except ValueError:
        return lowered
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback or str(address) in _LOOPBACK_HOSTS:
        return _CANONICAL_LOOPBACK
    return str(address)


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
    """Accept explicit test names and disposable ``brain_<words>_<hex8+>`` names."""
    return (
        name.lower() == "brain_unit_test_unreachable"
        or _TEST_DATABASE_NAME.fullmatch(name.lower()) is not None
    )


def _configured(
    environ: Mapping[str, str], dotenv_files: Iterable[Path], names: Iterable[str]
) -> list[tuple[str, str]]:
    """Every spelling and value from every source, without applying source precedence."""
    guarded = {name.casefold() for name in names}
    sources = [environ, *(dotenv_values(path) for path in dotenv_files if path.is_file())]
    return [
        (name, value)
        for source in sources
        for name, value in source.items()
        if name.casefold() in guarded and value
    ]


def _neutralise_urls(
    environ: MutableMapping[str, str],
    dotenv_files: Iterable[Path],
    names: Iterable[str],
    safe_url: str,
) -> None:
    """Remove all alias spellings, keeping only the safe canonical legacy variable.

    Pinning BRAIN_ aliases would shadow explicit legacy values set later by tests
    or passed to child processes. Files that could restore them are checked first.
    """
    names = tuple(names)
    configured = _configured(environ, dotenv_files, names)
    guarded = {name.casefold() for name in names}
    if not configured and not any(name.casefold() in guarded for name in environ):
        return
    for name in list(environ):
        if name.casefold() in guarded:
            del environ[name]
    environ[names[0]] = safe_url


def _setdefault_env_values(path: Path) -> dict[str, str]:
    """Match load_env_file's literal systemd parsing without importing its runtime.

    python-dotenv expands variables and removes quotes; load_env_file does neither.
    The preflight must inspect the values the loader would actually insert.
    """
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value
    return values


def _refuse_production_setdefault_values(values: Mapping[str, str]) -> None:
    """Refuse aliases before their removal makes them available to a later loader."""
    for name, value in _configured(values, [], _POSTGRES_URL_NAMES):
        if postgres_identity(value) in _production_postgres:
            raise UnsafeTestDatabase(f"{name} in a setdefault env file names a production database")
    for name, value in _configured(values, [], _NEO4J_URL_NAMES):
        if neo4j_identity(value) in _production_neo4j and value != UNREACHABLE_NEO4J_URL:
            raise UnsafeTestDatabase(f"{name} in a setdefault env file names a production Neo4j")


def production_postgres_identities(
    environ: Mapping[str, str], dotenv_files: Iterable[Path]
) -> frozenset[PostgresIdentity]:
    """The databases the application is configured against, minus test-named ones.

    A test-named ``POSTGRES_URL`` is a deliberate test configuration -- CI exports the
    same ``brain_test`` in both variables -- so it is not a production identity.
    """
    identities = {
        postgres_identity(value)
        for _name, value in _configured(environ, dotenv_files, _POSTGRES_URL_NAMES)
    }
    return frozenset(i for i in identities if not is_test_database_name(i[2]))


def production_neo4j_identities(
    environ: Mapping[str, str], dotenv_files: Iterable[Path], env_files: Iterable[Path]
) -> frozenset[Neo4jIdentity]:
    """Every Neo4j the application is configured against, plus the compose bolt port.

    ``env_files`` (the projector's private file) are read for the URL keys only.
    """
    values = _configured(environ, [*dotenv_files, *env_files], _NEO4J_URL_NAMES)
    return frozenset({_COMPOSE_PRODUCTION_NEO4J, *(neo4j_identity(v) for _name, v in values)})


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
            "brain_fresh_*, brain_<words>_<hex suffix of at least 8 characters> "
            "or a name ending in _test"
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

    The address rule is the primary guard: an empty production graph passes this
    probe, so it cannot prove a graph is a test graph. This read-only check is
    defence in depth for production reached through a tunnel or another hostname.
    Test data carries ``integ-``/``test-`` project keys only.
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
    setdefault_env_files: Iterable[Path] | None = None,
) -> None:
    """Make this process unable to reach a production database, or raise.

    1. Record production identities from the environment and all loadable env files.
    2. Refuse ``BRAIN_V42_TEST_DB_URL`` / ``BRAIN_V42_TEST_NEO4J_URL`` naming one.
    3. Refuse production aliases in files that a setdefault loader could restore.
    4. Point application settings at the test database, or at nothing: ``Settings``
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
    loader_files = _SETDEFAULT_ENV_FILES if setdefault_env_files is None else setdefault_env_files
    loader_values = [_setdefault_env_values(path) for path in loader_files]

    _production_postgres.update(production_postgres_identities(env, dotenvs))
    _production_neo4j.update(production_neo4j_identities(env, dotenvs, projector_files))
    for values in loader_values:
        _production_postgres.update(production_postgres_identities(values, []))
        _production_neo4j.update(production_neo4j_identities(values, [], []))

    test_db_url = (env.get("BRAIN_V42_TEST_DB_URL") or "").strip()
    if test_db_url:
        _refuse("BRAIN_V42_TEST_DB_URL", validate_test_database_url, test_db_url)
    test_neo4j_url = (env.get("BRAIN_V42_TEST_NEO4J_URL") or "").strip()
    if test_neo4j_url:
        _refuse("BRAIN_V42_TEST_NEO4J_URL", validate_test_neo4j_url, test_neo4j_url)

    for values in loader_values:
        _refuse_production_setdefault_values(values)

    _neutralise_urls(env, dotenvs, _POSTGRES_URL_NAMES, test_db_url or UNREACHABLE_POSTGRES_URL)

    for bare in ("NEO4J_URL", "GRAPH_PROJECTOR_NEO4J_URL"):
        _neutralise_urls(
            env, [*dotenvs, *projector_files], (bare, f"BRAIN_{bare}"), UNREACHABLE_NEO4J_URL
        )
