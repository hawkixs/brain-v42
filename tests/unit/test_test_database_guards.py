"""The test sessions can never reach a production database (tickets e3292865, 2687faf0).

Two measured incidents, one cause. On 2026-09-21 forty rows landed in production
``dream_runs`` (empty project_key, the literals "something went wrong" and "reorg
integrity failure"), and production Neo4j holds 796 ``integ-%`` nodes that exist
nowhere in PostgreSQL (``integ-dedup-<hex8>`` is the key
``tests/unit/test_promote_prepare_dedup_bands.py`` builds). The only refusal of the
production database NAME lived in ``tests/integration/conftest.py`` -- the unit
suite, which holds 17 of the 18 ``require_test_db_url()`` callers, had none, and
nothing at all identified a production Neo4j.

These tests pin the structural guard in ``tests/database_guards.py``: it runs when
the session is configured, before any engine, driver or subprocess exists.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import database_guards
from tests.conftest import require_test_db_url
from tests.database_guards import (
    UnsafeTestDatabase,
    enforce_database_isolation,
    is_test_database_name,
    neo4j_identity,
    postgres_identity,
    production_neo4j_identities,
    production_postgres_identities,
    refuse_neo4j_holding_production_data,
    registered_production_postgres_identities,
    validate_test_database_url,
    validate_test_neo4j_url,
)

PROD_URL = "postgresql+asyncpg://brain:SECRET-PW@localhost:5433/brain"
TEST_URL = "postgresql+asyncpg://brain:SECRET-PW@localhost:5433/brain_test"
PROD_IDENTITY = ("127.0.0.1", 5433, "brain")
REPO_ROOT = Path(__file__).parents[2]


@pytest.fixture(autouse=True)
def _restore_the_session_registry() -> Iterator[None]:
    """These tests record fake production identities: leave the real session's untouched."""
    postgres = set(database_guards._production_postgres)
    neo4j = set(database_guards._production_neo4j)
    yield
    database_guards._production_postgres.clear()
    database_guards._production_postgres.update(postgres)
    database_guards._production_neo4j.clear()
    database_guards._production_neo4j.update(neo4j)


# ---------------------------------------------------------------------------
# PostgreSQL identity and naming
# ---------------------------------------------------------------------------


def test_identity_ignores_credentials_scheme_and_loopback_spelling() -> None:
    assert postgres_identity("postgresql+asyncpg://a:b@127.0.0.1:5433/brain") == PROD_IDENTITY
    assert postgres_identity("postgresql://x@[::1]:5433/brain") == PROD_IDENTITY
    assert postgres_identity("postgresql://x@localhost:5433/br%61in") == PROD_IDENTITY
    assert postgres_identity("postgresql://x@db.example/brain") == ("db.example", 5432, "brain")


@pytest.mark.parametrize(
    "name", ["brain_test", "brain_migration_ab12cd", "brain_fresh_ab12cd", "scratch_test"]
)
def test_disposable_database_names_are_accepted(name: str) -> None:
    assert is_test_database_name(name)


@pytest.mark.parametrize("name", ["brain", "postgres", "brain_v42", "brain_prod", "testbrain", ""])
def test_everything_else_is_not_a_test_database(name: str) -> None:
    assert not is_test_database_name(name)


def test_a_test_url_naming_the_production_database_is_refused() -> None:
    with pytest.raises(UnsafeTestDatabase, match="same database as POSTGRES_URL"):
        validate_test_database_url(PROD_URL, production={PROD_IDENTITY})


def test_the_production_database_is_recognised_through_another_spelling() -> None:
    with pytest.raises(UnsafeTestDatabase, match="same database as POSTGRES_URL"):
        validate_test_database_url(
            "postgresql+asyncpg://u:p@127.0.0.1:5433/brain", production={PROD_IDENTITY}
        )


def test_a_database_that_is_not_named_like_a_test_database_is_refused() -> None:
    with pytest.raises(UnsafeTestDatabase, match="not a test database"):
        validate_test_database_url("postgresql://u:p@localhost:5433/brain", production=set())


def test_a_connection_override_in_the_query_string_is_refused() -> None:
    with pytest.raises(UnsafeTestDatabase, match="override"):
        validate_test_database_url(f"{TEST_URL}?host=prod.example", production=set())


def test_the_refusal_never_echoes_the_credential() -> None:
    with pytest.raises(UnsafeTestDatabase) as refused:
        validate_test_database_url(PROD_URL, production={PROD_IDENTITY})
    assert "SECRET-PW" not in str(refused.value)


def test_the_ci_shape_is_accepted() -> None:
    """CI exports POSTGRES_URL and BRAIN_V42_TEST_DB_URL as the same brain_test."""
    ci_url = "postgresql+asyncpg://brain:brain@localhost:5432/brain_test"
    environ = {"POSTGRES_URL": ci_url, "BRAIN_V42_TEST_DB_URL": ci_url}

    enforce_database_isolation(environ, dotenv_files=[])

    assert environ["POSTGRES_URL"] == ci_url
    assert validate_test_database_url(ci_url, production=set()) == ci_url


def test_production_identities_come_from_the_environment_and_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("BRAIN_POSTGRES_URL=postgresql://u:p@dotenv-host:5432/brain\n")
    environ = {
        "POSTGRES_URL": PROD_URL,
        "BRAIN_DELIVERY_POSTGRES_URL": "postgresql://u:p@delivery-host/brain",
    }

    found = production_postgres_identities(environ, [dotenv])

    assert found == {
        PROD_IDENTITY,
        ("dotenv-host", 5432, "brain"),
        ("delivery-host", 5432, "brain"),
    }


def test_a_test_named_postgres_url_is_not_a_production_identity() -> None:
    assert production_postgres_identities({"POSTGRES_URL": TEST_URL}, []) == frozenset()


# ---------------------------------------------------------------------------
# Session entry point
# ---------------------------------------------------------------------------


def test_the_session_points_application_settings_at_the_test_database() -> None:
    environ = {
        "POSTGRES_URL": PROD_URL,
        "BRAIN_POSTGRES_URL": PROD_URL,
        "BRAIN_V42_TEST_DB_URL": TEST_URL,
    }

    enforce_database_isolation(environ, dotenv_files=[])

    assert environ["POSTGRES_URL"] == TEST_URL
    assert "BRAIN_POSTGRES_URL" not in environ
    assert PROD_IDENTITY in registered_production_postgres_identities()


def test_without_a_test_database_application_settings_point_at_nothing() -> None:
    environ = {"POSTGRES_URL": PROD_URL}

    enforce_database_isolation(environ, dotenv_files=[])

    assert postgres_identity(environ["POSTGRES_URL"]) != PROD_IDENTITY
    assert postgres_identity(environ["POSTGRES_URL"])[1] == 1


def test_a_production_delivery_database_is_dropped_from_the_session() -> None:
    environ = {"BRAIN_DELIVERY_POSTGRES_URL": "postgresql://u:p@localhost:5433/brain"}

    enforce_database_isolation(environ, dotenv_files=[])

    assert "BRAIN_DELIVERY_POSTGRES_URL" not in environ


def test_a_production_test_database_url_fails_the_session() -> None:
    environ = {"POSTGRES_URL": PROD_URL, "BRAIN_V42_TEST_DB_URL": PROD_URL}

    with pytest.raises(UnsafeTestDatabase, match="BRAIN_V42_TEST_DB_URL"):
        enforce_database_isolation(environ, dotenv_files=[])


def test_a_dotenv_production_database_cannot_be_the_test_database(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("POSTGRES_URL=postgresql://u:p@localhost:5433/brain\n")
    environ = {"BRAIN_V42_TEST_DB_URL": "postgresql://u:p@localhost:5433/brain"}

    with pytest.raises(UnsafeTestDatabase, match="same database as POSTGRES_URL"):
        enforce_database_isolation(environ, dotenv_files=[dotenv])


def test_a_production_neo4j_test_url_fails_the_session() -> None:
    environ = {"NEO4J_URL": "bolt://localhost:7687", "BRAIN_V42_TEST_NEO4J_URL": "bolt://127.0.0.1"}

    with pytest.raises(UnsafeTestDatabase, match="BRAIN_V42_TEST_NEO4J_URL"):
        enforce_database_isolation(environ, dotenv_files=[])


def test_ambient_neo4j_settings_are_pointed_at_nothing(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("GRAPH_PROJECTOR_NEO4J_URL=bolt://localhost:7687\n")
    environ = {"NEO4J_URL": "bolt://localhost:7687"}

    enforce_database_isolation(environ, dotenv_files=[dotenv])

    for name in ("NEO4J_URL", "GRAPH_PROJECTOR_NEO4J_URL"):
        assert neo4j_identity(environ[name]) == ("127.0.0.1", 1)


def test_an_unconfigured_neo4j_stays_unconfigured() -> None:
    environ: dict[str, str] = {}

    enforce_database_isolation(environ, dotenv_files=[])

    assert environ == {}


def test_require_test_db_url_refuses_a_production_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_V42_TEST_DB_URL", "postgresql://u:p@localhost:5433/brain")

    with pytest.raises(UnsafeTestDatabase):
        require_test_db_url()


def test_pytest_refuses_to_start_on_a_production_test_database() -> None:
    """The refusal is wired at pytest_configure: it holds for every suite, not one conftest."""
    environ = {
        **os.environ,
        "POSTGRES_URL": "postgresql+asyncpg://u:p@localhost:5433/brain",
        "BRAIN_V42_TEST_DB_URL": "postgresql+asyncpg://u:p@localhost:5433/brain",
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/test_alembic_env.py",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode != 0
    assert "BRAIN_V42_TEST_DB_URL" in result.stderr + result.stdout
    assert "collected" not in result.stdout


# ---------------------------------------------------------------------------
# Neo4j identity
# ---------------------------------------------------------------------------


def test_neo4j_identity_ignores_scheme_credentials_and_loopback_spelling() -> None:
    assert neo4j_identity("bolt://localhost:7687") == ("127.0.0.1", 7687)
    assert neo4j_identity("neo4j://u:p@127.0.0.1") == ("127.0.0.1", 7687)
    assert neo4j_identity("bolt+s://[::1]:7687") == ("127.0.0.1", 7687)
    assert neo4j_identity("bolt://graph.example:17687") == ("graph.example", 17687)


def test_the_compose_published_bolt_port_is_always_production() -> None:
    assert ("127.0.0.1", 7687) in production_neo4j_identities({}, [], [])


def test_production_neo4j_comes_from_settings_the_projector_file_and_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("NEO4J_URL=bolt://env-file-host:7000\n")
    projector = tmp_path / "graph-projector.env"
    projector.write_text(
        "GRAPH_PROJECTOR_NEO4J_URL=bolt://projector-host:7001\n"
        "GRAPH_PROJECTOR_NEO4J_PASSWORD=never-read\n"
    )
    environ = {"BRAIN_NEO4J_URL": "bolt://shell-host:7002"}

    found = production_neo4j_identities(environ, [dotenv], [projector])

    assert {
        ("env-file-host", 7000),
        ("projector-host", 7001),
        ("shell-host", 7002),
        ("127.0.0.1", 7687),
    } <= found


@pytest.mark.parametrize(
    "url",
    ["bolt://127.0.0.1:7687", "bolt://localhost", "neo4j://localhost:7687", "bolt://[::1]:7687"],
)
def test_a_neo4j_test_url_naming_production_is_refused(url: str) -> None:
    with pytest.raises(UnsafeTestDatabase, match="production Neo4j"):
        validate_test_neo4j_url(url, production={("127.0.0.1", 7687)})


def test_a_neo4j_test_url_on_another_port_is_accepted() -> None:
    url = "bolt://127.0.0.1:17687"
    assert validate_test_neo4j_url(url, production={("127.0.0.1", 7687)}) == url


class _Result:
    def __init__(self, record: object | None) -> None:
        self._record = record

    async def single(self) -> object | None:
        return self._record


class _Session:
    def __init__(self, record: object | None) -> None:
        self._record = record
        self.statements: list[str] = []

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def run(self, statement: str, *_args: object, **_kwargs: object) -> _Result:
        self.statements.append(statement)
        return _Result(self._record)


def _driver(record: object | None) -> SimpleNamespace:
    session = _Session(record)
    return SimpleNamespace(session=lambda: session, seen=session)


@pytest.mark.asyncio
async def test_a_neo4j_holding_a_real_project_is_refused() -> None:
    driver = _driver({"project_key": "brain-v42"})

    with pytest.raises(UnsafeTestDatabase, match="production data"):
        await refuse_neo4j_holding_production_data(driver)


@pytest.mark.asyncio
async def test_a_neo4j_holding_only_test_projects_is_accepted_and_read_only() -> None:
    driver = _driver(None)

    await refuse_neo4j_holding_production_data(driver)

    (statement,) = driver.seen.statements
    assert statement.lstrip().upper().startswith("MATCH")
    for verb in ("CREATE", "MERGE", "DELETE", "SET "):
        assert verb not in statement.upper()
